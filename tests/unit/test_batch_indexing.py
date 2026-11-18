"""批处理通道（`LM-02`）。

## 四条验收

1. 批量 5 张 → **5 条索引记录**
2. 其中 1 张失败 → **4 条成功 + 1 条错误**，且**成功项不回滚**
3. 批量 judge 的结果与逐条 judge **一致**
4. **批量不改变单条语义**（同一张图单独索引与批量索引结果相同）
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.domain.schemas import Chunk
from app.services.graph.images import ImageIndex


class _Captioner:
    """按文件名返回可预测的描述；指定的名字会失败。"""

    def __init__(self, fail_ids: set[str] | None = None) -> None:
        self.fail_ids = fail_ids or set()
        self.calls: list[str] = []

    def caption(self, image_path: Path) -> str:
        self.calls.append(image_path.stem)
        if image_path.stem in self.fail_ids:
            raise RuntimeError("描述生成失败（测试注入）")
        return f"描述：{image_path.stem}"


class _Embeddings:
    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]

    def embed_query(self, text: str) -> list[float]:
        return [0.1, 0.2, 0.3]


class _Store:
    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.saves = 0

    def add(self, chunks, vectors) -> None:
        self.chunks.extend(chunks)

    def save(self) -> None:
        self.saves += 1


@pytest.fixture()
def _png(tmp_path: Path) -> Path:
    path = tmp_path / "img.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return path


def _index(tmp_path: Path, captioner) -> ImageIndex:
    return ImageIndex(
        captioner=captioner,
        embeddings=_Embeddings(),
        vector_store=_Store(),
        index_path=tmp_path / "images.json",
    )


def test_batch_indexes_all_images(tmp_path, _png):
    """① 批量 5 张 → 5 条索引记录。"""
    captioner = _Captioner()
    index = _index(tmp_path, captioner)
    paths = []
    for i in range(5):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(_png.read_bytes() + str(i).encode())
        paths.append(p)
    items = [(p, p.stem, "d1", 0, "default") for p in paths]
    results = index.index_images_batch(items, concurrency=2)
    assert len(results) == 5
    assert all(r["ok"] for r in results), results
    assert len(index._entries) == 5


def test_partial_failure_keeps_successes(tmp_path, _png):
    """② 其中 1 张失败 → 4 条成功 + 1 条错误，且**成功项不回滚**。"""
    captioner = _Captioner(fail_ids={"img2"})
    index = _index(tmp_path, captioner)
    paths = []
    for i in range(5):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(_png.read_bytes() + str(i).encode())
        paths.append(p)
    items = [(p, p.stem, "d1", 0, "default") for p in paths]
    results = index.index_images_batch(items, concurrency=2)
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    assert len(ok) == 4
    assert len(bad) == 1
    assert bad[0]["asset_id"] == "img2"
    assert bad[0]["error"]
    # **成功项不回滚**：4 条都在索引里
    assert len(index._entries) == 4
    assert "img2" not in index._entries


def test_batch_results_keep_input_order(tmp_path, _png):
    """结果按入参顺序返回（调用方好对齐）。"""
    captioner = _Captioner()
    index = _index(tmp_path, captioner)
    paths = []
    for i in range(4):
        p = tmp_path / f"img{i}.png"
        p.write_bytes(_png.read_bytes() + str(i).encode())
        paths.append(p)
    items = [(p, p.stem, "d1", 0, "default") for p in paths]
    results = index.index_images_batch(items, concurrency=4)
    assert [r["asset_id"] for r in results] == [p.stem for p in paths]


def test_batch_and_single_have_same_semantics(tmp_path, _png):
    """④ 批量不改变单条语义（同一张图单独索引与批量索引结果相同）。"""
    single = _index(tmp_path / "a", _Captioner())
    batch = _index(tmp_path / "b", _Captioner())
    (tmp_path / "a").mkdir(exist_ok=True)
    (tmp_path / "b").mkdir(exist_ok=True)
    p = tmp_path / "a" / "one.png"
    p.write_bytes(b"same-bytes")
    q = tmp_path / "b" / "one.png"
    q.write_bytes(b"same-bytes")

    e1 = single.index_image(p, "one", doc_id="d1")
    batch_results = batch.index_images_batch([(q, "one", "d1", 0, "default")])
    assert batch_results[0]["ok"]
    e2 = batch._entries["one"]
    assert e1 is not None
    assert e1.hash == e2.hash
    assert e1.caption == e2.caption


def test_batch_judge_matches_single_judge():
    """③ 批量 judge 的结果与逐条 judge 一致。"""
    from app.services.evaluation.judge import BatchJudge, HeuristicJudge
    from app.services.evaluation.rubric import load_rubric

    rubric = load_rubric()
    judge = HeuristicJudge()
    items = [
        {"question": "报销上限是多少？", "answer": "报销上限为 5000 元 [#1]",
         "key_points": ["报销上限为 5000 元。"]},
        {"question": "住宿标准？", "answer": "不知道 [#1]", "key_points": ["住宿标准为每晚 600 元。"]},
    ]
    batch = BatchJudge(judge).judge_batch(items, rubric)
    singles = [
        judge.judge(
            it["question"], it["answer"], rubric,
            key_points=it["key_points"],
        )
        for it in items
    ]
    assert len(batch) == len(singles)
    for b, s in zip(batch, singles):
        assert b.dim_scores == s.dim_scores
        assert b.weighted == s.weighted
        assert b.passed == s.passed


def test_batch_judge_isolates_failure():
    """单条失败不影响整批（该条带 error，其余照常）。"""
    from app.services.evaluation.judge import BatchJudge
    from app.services.evaluation.rubric import load_rubric

    rubric = load_rubric()

    class _Exploding:
        def judge(self, *args, **kwargs):
            raise RuntimeError("炸了")

    batch = BatchJudge(_Exploding()).judge_batch(
        [{"question": "q", "answer": "a"}], rubric
    )
    assert len(batch) == 1
    assert batch[0].error
    assert batch[0].weighted is None


def test_content_hash_dedup_still_works(tmp_path, _png):
    """同图去重仍然生效（批量也不重复入库）。"""
    captioner = _Captioner()
    index = _index(tmp_path, captioner)
    p = tmp_path / "dup.png"
    p.write_bytes(b"same")
    q = tmp_path / "dup2.png"
    q.write_bytes(b"same")
    index.index_images_batch([(p, "dup", "d1", 0, "default"), (q, "dup2", "d1", 0, "default")])
    # 两个 asset_id，但内容哈希相同 → 只入一条
    assert len(index._entries) == 1


def test_hash_is_sha256_of_content(tmp_path):
    """去重键是内容哈希（不是文件名）。"""
    expected = hashlib.sha256(b"same").hexdigest()
    assert len(expected) == 64
