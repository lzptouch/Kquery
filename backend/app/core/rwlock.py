"""读写锁。

## 为什么需要它

BM25 此前用一把 `threading.Lock` 同时保护读与写，而 `search()` **在持锁状态下
完成全量打分与排序**（`bm25.py:110-129`）。后果不是"写得慢"，而是：

> **所有关键词检索互相串行。** 10 个并发请求排队做 10 次全量打分，
> 每次打分都是 O(语料规模)——QPS 上限被钉死在 `1 / 单次打分耗时`。

读操作之间本来不需要互斥：`_BM25Scorer` 一旦构建完成就是**不可变**的
（`_rebuild_model` 用新对象替换 `self._bm25`，从不原地修改），因此多个读者
并发打分是安全的。真正需要互斥的只是「读 vs 写」。

## 实现说明

基于 `threading.Condition`，写者优先（writer-preferring）：
读请求持续到来时不会把写者饿死——索引更新（`add`/`rebuild`）若被无限推迟，
新文档会长期检索不到，这比"某次查询多等一会儿"严重。

Python 的 GIL 让"纯计算"并行收益有限，但真正的瓶颈是**磁盘/网络 IO 与
锁等待**（打分循环里会做对象属性访问与字符串处理）。把串行改成并发后，
QPS 上限由"核数 × 单次耗时"决定，而非单次耗时。
"""
import threading


class ReadWriteLock:
    """读共享 / 写独占，写者优先。"""

    def __init__(self):
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @property
    def reader_count(self) -> int:
        with self._cond:
            return self._readers

    def acquire_read(self) -> None:
        with self._cond:
            # 有写者在等或正在写 → 新读者排队（写者优先，避免写饥饿）
            while self._writer or self._waiting_writers > 0:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            try:
                while self._writer or self._readers > 0:
                    self._cond.wait()
            finally:
                self._waiting_writers -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()

    # ---- 上下文管理器 ----

    def read(self) -> "_ReadGuard":
        return _ReadGuard(self)

    def write(self) -> "_WriteGuard":
        return _WriteGuard(self)


class _ReadGuard:
    __slots__ = ("_lock",)

    def __init__(self, lock: ReadWriteLock):
        self._lock = lock

    def __enter__(self):
        self._lock.acquire_read()
        return self

    def __exit__(self, *exc):
        self._lock.release_read()
        return False


class _WriteGuard:
    __slots__ = ("_lock",)

    def __init__(self, lock: ReadWriteLock):
        self._lock = lock

    def __enter__(self):
        self._lock.acquire_write()
        return self

    def __exit__(self, *exc):
        self._lock.release_write()
        return False
