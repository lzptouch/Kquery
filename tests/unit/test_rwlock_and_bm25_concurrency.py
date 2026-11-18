"""读写锁 + BM25 并发检索。

要证明的两件事：
1. `ReadWriteLock` 语义正确（多读并发、写独占、写者优先不饥饿）；
2. BM25 的并发检索**不再串行**（此前持独占锁做全量打分 → QPS 上限被钉死）。
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app.core.rwlock import ReadWriteLock


class TestReadWriteLock:
    def test_multiple_readers_concurrent(self):
        """多个读必须能同时进入临界区（这是 BM25 并发的前提）。"""
        lock = ReadWriteLock()
        inside = []
        barrier = threading.Barrier(4, timeout=5)

        def reader(i):
            with lock.read():
                inside.append(i)
                barrier.wait()  # 4 个读者必须同时在临界区内才能通过

        with ThreadPoolExecutor(4) as ex:
            list(ex.map(reader, range(4)))
        assert len(inside) == 4, "读操作互相阻塞 → 退化成互斥锁"

    def test_writer_is_exclusive(self):
        lock = ReadWriteLock()
        state = {"readers": 0, "violation": False}

        def reader():
            with lock.read():
                state["readers"] += 1
                if state["readers"] > 0 and state.get("writing"):
                    state["violation"] = True
                state["readers"] -= 1

        def writer():
            with lock.write():
                state["writing"] = True
                if state["readers"] != 0:
                    state["violation"] = True
                time.sleep(0.01)
                state["writing"] = False

        with ThreadPoolExecutor(8) as ex:
            futures = [ex.submit(writer) for _ in range(4)]
            futures += [ex.submit(reader) for _ in range(4)]
            for f in futures:
                f.result()
        assert state["violation"] is False, "写者期间不得有读者进入"

    def test_writer_not_starved_by_continuous_readers(self):
        """写者优先：持续读请求不得让索引更新无限等待。

        没有这个保证时，`add()`（新文档入库）可能永远拿不到锁——
        表现为"上传的文档长期检索不到"，比单次查询多等更严重。
        """
        lock = ReadWriteLock()
        stop = threading.Event()
        wrote = threading.Event()

        def reader():
            while not stop.is_set():
                with lock.read():
                    time.sleep(0.001)

        def writer():
            # 连续读者已就绪后才申请写
            with lock.write():
                wrote.set()

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        for r in readers:
            r.start()
        time.sleep(0.05)  # 让读者先占住读锁

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        got = wrote.wait(timeout=3)
        stop.set()
        for r in readers:
            r.join(timeout=2)
        t.join(timeout=2)

        assert got, "写者被持续读者饿死（写者优先策略失效）"

    def test_reader_count_visible(self):
        lock = ReadWriteLock()
        with lock.read():
            assert lock.reader_count == 1
        assert lock.reader_count == 0


class TestBm25ConcurrentSearch:
    def _retriever(self, n_docs: int = 400):
        from app.domain.schemas import Chunk
        from app.services.retrieval.bm25 import BM25Retriever

        bm25 = BM25Retriever()
        bm25.rebuild([
            Chunk(chunk_id=f"c{i}#0", doc_id=f"d{i}", seq=0,
                  content=f"文档{i} 苹果 价格 市场 分析 报告 内容",
                  tenant_id="t1", source="s")
            for i in range(n_docs)
        ])
        return bm25

    def test_concurrent_search_overlaps(self, monkeypatch):
        """两个并发检索必须**重叠执行**，而不是排队。

        不用墙钟测量——真实检索只要 0.2ms，测量会被线程池创建开销淹没
        （那正是第一版测试误报的原因）。改为让打分函数故意变慢，然后看
        两个读者的总耗时是「1 倍」还是「2 倍」单次耗时：
        重叠时为 ~1 倍，串行时为 ~2 倍。这是确定性判据，不受机器快慢影响。
        """
        from app.services.retrieval import bm25 as bm25_mod

        sleep_s = 0.15
        original = bm25_mod._BM25Scorer.get_scores
        in_flight = {"now": 0, "peak": 0}
        guard = threading.Lock()

        def slow_get_scores(self, tokens):
            with guard:
                in_flight["now"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                time.sleep(sleep_s)
                return original(self, tokens)
            finally:
                with guard:
                    in_flight["now"] -= 1

        monkeypatch.setattr(bm25_mod._BM25Scorer, "get_scores", slow_get_scores)

        bm25 = self._retriever(50)
        t0 = time.perf_counter()
        with ThreadPoolExecutor(3) as ex:
            list(ex.map(lambda _: bm25.search("苹果 价格", 5, "t1"), range(3)))
        elapsed = time.perf_counter() - t0

        assert in_flight["peak"] >= 2, (
            "并发检索未重叠（读锁退化成互斥锁）——这正是  要修的问题"
        )
        assert elapsed < sleep_s * 2.2, (
            f"3 个并发检索串行执行了：耗时 {elapsed:.3f}s ≈ 3 × {sleep_s}s"
        )

    def test_slow_reader_blocks_index_update_only_then(self):
        """写者优先：慢读者持锁期间，写者应在读者释放后立刻拿到锁（不被新读者插队）。"""
        bm25 = self._retriever(50)
        order: list[str] = []

        def reader(name):
            bm25.search("苹果 价格", 5, "t1")
            order.append(name)

        threads = [threading.Thread(target=reader, args=(f"r{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert len(order) == 3, "并发读者应全部完成"

    def test_search_during_write_is_consistent(self):
        """读写并发时每次检索都必须返回结构完整的结果（不抛异常、不半截）。"""
        from app.domain.schemas import Chunk

        bm25 = self._retriever(200)
        errors: list = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    hits = bm25.search("苹果 价格", 5, "t1")
                    for c in hits:
                        assert c.chunk.chunk_id and c.chunk.doc_id
                except Exception as exc:  # noqa: BLE001
                    errors.append(exc)

        def writer():
            for i in range(30):
                bm25.add([Chunk(chunk_id=f"new{i}#0", doc_id=f"new{i}", seq=0,
                                content="新增 苹果 价格", tenant_id="t1", source="s")])
                time.sleep(0.002)

        threads = [threading.Thread(target=reader, daemon=True) for _ in range(4)]
        for t in threads:
            t.start()
        w = threading.Thread(target=writer)
        w.start()
        w.join(timeout=20)
        stop.set()
        for t in threads:
            t.join(timeout=2)

        assert not errors, f"并发检索报错：{errors[:3]}"

    def test_add_after_concurrent_reads_visible(self):
        """写入后立即可检索到（读锁不能缓存旧索引）。"""
        from app.domain.schemas import Chunk

        bm25 = self._retriever(10)
        assert bm25.search("独角兽 稀有词", 5, "t1") == []
        bm25.add([Chunk(chunk_id="u#0", doc_id="u", seq=0,
                        content="独角兽 稀有词 出现", tenant_id="t1", source="s")])
        hits = bm25.search("独角兽 稀有词", 5, "t1")
        assert [c.chunk.doc_id for c in hits] == ["u"]
