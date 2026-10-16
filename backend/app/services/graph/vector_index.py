"""实体向量索引（TASK-153）。

## 问题

图谱有两处"每个实体算一次点积"的 Python 循环：

- `resolver.resolve()` 消歧：新实体要和**所有同类实体**逐个比相似度（`resolver.py:47`）
- `query.find_seeds()` 种子召回：问题向量和所有实体逐个比（`query.py:54`）

实体数 N 增长时，这两处都是 O(N) 次 **Python 层** 的 `np.dot` 单向量调用——
每次调用都要跨越 Python/C 边界，常数极大。实测点积本身耗时可忽略，
循环与属性访问才是瓶颈。

## 方案选择：向量化精确检索，而非 ANN

直觉上应该上 FAISS（HNSW/IVF），但**在本场景是错的**：

| 方案 | 适用条件 | 本项目情况 |
| --- | --- | --- |
| HNSW/IVF（ANN 近似） | 百万级向量，可容忍召回损失，有训练/构建预算 | ❌ 实体量级千级；且图谱消歧**不允许**近似（漏合并 = 乱合并） |
| 向量化精确检索 | 万级以内，要求精确 | ✅ |

`np.dot(matrix, vec)` 一次调用完成 N 次点积（BLAS 内循环），比 Python 循环快 1~2 个数量级，
且**结果与逐个点积完全一致**——消歧的"不误合并"约束要求精确性，近似索引在这里反而危险。

维护成本上，追加式矩阵（`vstack` 摊销）比"每次新增实体重建 FAISS 索引"更低。

## 增量维护

`add()` 用**容量翻倍**的预分配矩阵（amortized O(1)），避免每个实体一次 `vstack` 拷贝。
`suspend()/resume()` 供批量写入使用（批量时直接一次性扩矩阵）。
"""
import logging

import numpy as np

logger = logging.getLogger(__name__)

# 小于该规模直接线性扫（省掉矩阵维护开销）
LINEAR_THRESHOLD = 32


class EntityVectorIndex:
    """实体名 → 向量的可增量维护索引。

    只保证**同类型内**比较由调用方自行处理（消歧的 `type` 硬约束在图谱层），
    因此本类只做"给定向量，找出最相似的前 k 个实体"。
    """

    def __init__(self, dim: int = 0):
        self._names: list[str] = []
        self._rows: dict[str, int] = {}      # name → 行号
        self._matrix: np.ndarray | None = None
        self._size = 0                        # 已用行数
        self._dim = dim
        self._suspended = False
        self._pending: dict[str, np.ndarray] = {}

    # ---- 维护 ----

    def add(self, name: str, vector: np.ndarray) -> None:
        vec = np.asarray(vector, dtype=np.float32).ravel()
        if self._suspended:
            # 批量写入期间只暂存（保留最后一次，同名覆盖语义与直接 add 一致），
            # resume 时按容量预算一次性扩容，避免逐条 vstack 拷贝。
            # 注意此时**不**修改 _rows/_names —— 中间态查询到"尚未落矩阵的名字"
            # 会拿到错误的行号（-1），那比查不到更糟。
            self._pending[name] = vec
            return
        if name in self._rows:
            self._write(self._rows[name], vec)
            return
        self._append(name, vec)

    def suspend(self) -> None:
        """进入批量写入模式。"""
        self._suspended = True

    def resume(self) -> None:
        """结束批量写入：一次性把暂存向量落进矩阵。"""
        if not self._suspended:
            return
        self._suspended = False
        if not self._pending:
            return
        pending, self._pending = self._pending, {}
        self._reserve(len(pending))
        for name, vec in pending.items():
            if name in self._rows:
                self._write(self._rows[name], vec)
            else:
                self._append(name, vec)

    def _reserve(self, extra: int) -> None:
        """一次性预留 `extra` 行容量（把扩容从 N 次降为 1 次）。"""
        if self._matrix is None:
            self._matrix = np.zeros((max(64, extra), 0), dtype=np.float32)
            self._size = 0
            return
        need = self._size + extra
        if need <= self._matrix.shape[0]:
            return
        new = np.zeros((need, self._matrix.shape[1]), dtype=np.float32)
        new[: self._size] = self._matrix[: self._size]
        self._matrix = new

    def _ensure(self, vec: np.ndarray) -> None:
        dim = vec.shape[0]
        if self._matrix is None:
            self._dim = dim
            self._matrix = np.zeros((max(64, 1), dim), dtype=np.float32)
            self._size = 0
        elif self._matrix.shape[1] == 0:  # _reserve 预建的零列矩阵
            self._dim = dim
            self._matrix = np.zeros((self._matrix.shape[0], dim), dtype=np.float32)
        elif self._size >= self._matrix.shape[0]:
            # 容量翻倍：均摊 O(1)，避免每次 add 都整表拷贝
            grow = max(64, self._matrix.shape[0])
            new = np.zeros((self._matrix.shape[0] + grow, dim), dtype=np.float32)
            new[: self._size] = self._matrix[: self._size]
            self._matrix = new

    def _append(self, name: str, vec: np.ndarray) -> None:
        self._ensure(vec)
        self._matrix[self._size] = vec
        self._rows[name] = self._size
        self._names.append(name)
        self._size += 1

    def _write(self, row: int, vec: np.ndarray) -> None:
        if row < 0:
            return
        self._ensure(vec)
        self._matrix[row] = vec

    # ---- 查询 ----

    def __len__(self) -> int:
        return self._size

    @property
    def names(self) -> list[str]:
        return list(self._names[: self._size]) if self._matrix is not None else []

    def search(
        self, query: np.ndarray, top_k: int = 1, threshold: float = 0.0,
        type_of: dict[str, str] | None = None, want_type: str | None = None,
    ) -> list[tuple[str, float]]:
        """返回 [(name, similarity)]，按相似度降序，只保留 ≥ threshold 的。

        `want_type` 非空时按实体类型过滤（消歧的类型硬约束）。
        """
        n = self._size
        if n == 0 or self._matrix is None:
            return []
        q = np.asarray(query, dtype=np.float32).ravel()
        if q.shape[0] != self._dim:
            return []
        # 一次矩阵乘完成 N 次点积（BLAS 内循环），而非 N 次 Python 调用
        sims = self._matrix[:n] @ q
        names = self._names[:n]
        if want_type is not None and type_of is not None:
            order = [i for i, nm in enumerate(names) if type_of.get(nm) == want_type]
            if not order:
                return []
            idx = np.asarray(order)
            sims = sims[idx]
            names = [names[i] for i in order]
        k = min(max(1, top_k), len(sims))
        if k >= len(sims):
            top = np.argsort(-sims)
        else:
            # argpartition 是 O(N)，比全排序 O(N log N) 快；再对 k 个候选排序
            part = np.argpartition(-sims, k - 1)[:k]
            top = part[np.argsort(-sims[part])]
        out: list[tuple[str, float]] = []
        for i in top:
            sim = float(sims[i])
            if sim < threshold:
                break
            out.append((names[i], sim))
        return out
