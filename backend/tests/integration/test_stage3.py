""" 功能测试：四种切块策略 + 多模态解析 + 入库任务 + 预览 + 高级操作。

- 策略矩阵：recursive / semantic / parent_child / raptor 的块结构与字段；
- 解析器：Markdown 大纲 / PDF 文字分页 / OCR（mock 引擎）/ 音频（mock 转写时间戳）；
- 任务队列：创建 → 轮询 → 完成 / 失败 → 重试（复用解析产物）→ 取消语义；
- 预览与高级操作：preview / chunk 详情 / reparse / offline / retrieval-probe。
"""
import io
import time

from tests.conftest import SAMPLE_MD, upload_sample

LONG_MD = """# 电池产品白皮书

## 第一章 电池规格

XB-2024 电池的标称电压为 24V，容量 5000mAh，采用磷酸铁锂电芯，循环寿命超过 2000 次。
该电池的工作温度范围为零下 20 度到 60 度，支持低温自加热功能，适合北方地区冬季使用。
电池内置智能管理芯片，可以实时监测电压、电流与温度，并在异常时自动断电保护。

## 第二章 充电规范

XB-2024 电池推荐使用 CD-100 充电器进行充电，充电电流不得超过 10A。
充电环境温度应保持在 0 度到 45 度之间，低于 0 度时充电器会自动降低电流以保护电芯。
完整充电时间约为 2 小时，电量从 20% 充至 80% 仅需 40 分钟。
禁止在潮湿环境或易燃物附近充电，充电完成后应及时断开充电器。

## 第三章 保养与存放

长期存放电池时，应将电量保持在 40% 到 60% 之间，并放置于阴凉干燥处。
每三个月应进行一次充放电循环，以防止电芯活性下降。
电池外壳出现鼓包、漏液或异味时应立即停止使用，并联系售后处理，切勿自行拆解。

## 第四章 保修政策

XB-2024 电池享受两年免费保修，保修范围涵盖电芯容量衰减超过 20% 的情况。
人为拆解、进水、跌落造成的损坏不在保修范围内。
保修期内更换的电池延续原保修期限，不重新计算。
"""


def _make_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (400, 200), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 80), "测试图片 OCR", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _ingest(client, files: list[tuple[str, bytes, str]], chunk_policy: dict, parse_policy: dict | None = None) -> str:
    import json as _json

    data = {"chunk_policy": _json.dumps(chunk_policy)}
    if parse_policy is not None:
        data["parse_policy"] = _json.dumps(parse_policy)
    resp = client.post(
        "/knowledge/ingest",
        files=[("files", (name, content, mime)) for name, content, mime in files],
        data=data,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok", body
    return body["data"]["task_id"]


def _wait_task(client, task_id: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = client.get(f"/knowledge/tasks/{task_id}").json()["data"]
        if view["task"]["status"] in ("done", "failed", "partial_failed", "cancelled"):
            return view
        time.sleep(0.1)
    raise TimeoutError(f"任务 {task_id} 超时未完成")


# ---------- 切块策略 ----------

class TestChunkStrategies:
    def test_semantic_chunking(self, client):
        task_id = _ingest(
            client,
            [("白皮书.md", LONG_MD.encode(), "text/markdown")],
            {"strategy": "semantic", "params": {"threshold": 0.3, "min_sentences": 2, "max_sentences": 4}},
        )
        view = _wait_task(client, task_id)
        assert view["task"]["status"] == "done"
        chunks = client.get("/knowledge/preview/白皮书").json()["data"]["chunks"]
        assert len(chunks) >= 3
        assert all(c["chunker"] == "semantic" for c in chunks)

    def test_semantic_policy_validation(self, client):
        import json as _json

        resp = client.post(
            "/knowledge/ingest",
            files=[("files", ("a.md", b"# t\n\ncontent", "text/markdown"))],
            data={"chunk_policy": _json.dumps({"strategy": "semantic", "params": {"threshold": 5}})},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == 3601

    def test_parent_child_retrieval_expands_to_parent(self, client):
        task_id = _ingest(
            client,
            [("白皮书.md", LONG_MD.encode(), "text/markdown")],
            {"strategy": "parent_child", "params": {"parent_size": 400, "child_size": 150, "child_overlap": 30}},
        )
        view = _wait_task(client, task_id)
        assert view["task"]["status"] == "done"

        preview = client.get("/knowledge/preview/白皮书").json()["data"]
        children = [c for c in preview["chunks"] if c["parent_id"]]
        assert children, "父子分块应产生带 parent_id 的子块"

        # 命中子块 → 证据内容应扩展为完整父块（§7.3 父子检索流程）
        from urllib.parse import quote
        child_detail = client.get(f"/knowledge/preview/白皮书/chunk/{quote(children[0]['chunk_id'], safe='')}").json()["data"]
        assert child_detail["parent_content"], "子块详情应能回溯父块内容"

        data = client.post("/ask", json={"question": "XB-2024 电池的充电电流限制是多少？"}).json()["data"]
        parent_ids = {c["chunk_id"] for c in preview["chunks"] if c["parent_id"]}
        hit = next((e for e in data["evidence"] if e["chunk_id"] in parent_ids), None)
        assert hit is not None
        child_content = next(
            c for c in preview["chunks"] if c["chunk_id"] == hit["chunk_id"]
        )
        assert len(hit["content"]) > len(child_content["preview"])  # 父块比子块预览长
        assert "10A" in hit["content"]

    def test_raptor_builds_summary_tree(self, client):
        task_id = _ingest(
            client,
            [("白皮书.md", LONG_MD.encode(), "text/markdown")],
            {"strategy": "raptor", "params": {"chunk_size": 150, "max_clusters": 3, "max_top_nodes": 2}},
        )
        view = _wait_task(client, task_id)
        assert view["task"]["status"] == "done"

        preview = client.get("/knowledge/preview/白皮书").json()["data"]
        tree = preview["raptor_tree"]
        assert tree, "RAPTOR 应产生摘要树"
        assert all(node["level"] >= 1 and node["child_count"] >= 1 for node in tree)
        # 叶子块回指摘要节点（层级 + 父块标识同时存在）
        leaves_with_parent = [c for c in preview["chunks"] if c["level"] == 0 and c["parent_id"]]
        assert leaves_with_parent
        # 概览型问题可以命中摘要层
        data = client.post("/ask", json={"question": "这份文档讲了电池的哪些内容？"}).json()["data"]
        assert data["evidence"]


# ---------- 解析器 ----------

class TestParsers:
    def test_pdf_text_parsing_by_page(self, client):
        import pymupdf

        pdf = pymupdf.open()
        for i in range(2):
            page = pdf.new_page()
            page.insert_text((72, 72), f"这是第 {i + 1} 页的电池技术说明文字内容，用于验证分页解析。")
        pdf_bytes = pdf.tobytes()
        pdf.close()

        resp = client.post("/knowledge/upload", files={"file": ("说明.pdf", pdf_bytes, "application/pdf")})
        data = resp.json()["data"]
        assert data["status"] == "indexed", data
        preview = client.get("/knowledge/preview/说明").json()["data"]
        sources = {c["source"] for c in preview["chunks"]}
        assert any("第1页" in s for s in sources) and any("第2页" in s for s in sources)

    def test_image_upload_with_mock_ocr(self, client):
        resp = client.post("/knowledge/upload", files={"file": ("规格图.png", _make_png(), "image/png")})
        data = resp.json()["data"]
        assert data["status"] == "indexed", data
        preview = client.get("/knowledge/preview/规格图").json()["data"]
        assert any("OCR" in c["source"] for c in preview["chunks"])
        # 图像本体进资源存储（MediaAsset），与文本分开
        assert preview["assets"] and preview["assets"][0]["kind"] == "image"
        # OCR 文本可被检索
        ask = client.post("/ask", json={"question": "图像里有什么文字？"}).json()["data"]
        assert ask["evidence"]

    def test_audio_upload_with_mock_transcriber(self, client):
        resp = client.post("/knowledge/upload", files={"file": ("演讲.mp3", b"fake-audio-bytes", "audio/mpeg")})
        data = resp.json()["data"]
        assert data["status"] == "indexed", data
        preview = client.get("/knowledge/preview/演讲").json()["data"]
        # 转写文本按时间戳分段（跳转到视频位置的基础）
        assert any("[0.0s" in c["source"] for c in preview["chunks"])
        assert preview["assets"][0]["kind"] == "audio"

    def test_upload_unsupported_still_rejected(self, client):
        resp = client.post("/knowledge/upload", files={"file": ("表.xlsx", b"x", "application/vnd.ms-excel")})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == 2003


# ---------- 入库任务 ----------

class TestIngestTasks:
    def test_task_lifecycle_with_stats(self, client):
        task_id = _ingest(
            client,
            [("手册.md", SAMPLE_MD.encode(), "text/markdown"), ("白皮书.md", LONG_MD.encode(), "text/markdown")],
            {"strategy": "recursive", "params": {}},
        )
        view = _wait_task(client, task_id)
        task = view["task"]
        assert task["status"] == "done"
        assert task["done_files"] == 2
        for f in task["files"]:
            assert f["status"] == "done" and f["stage"] == "done"
            assert f["stats"]["chunks"] >= 1
            assert f["doc_id"]
        assert "estimated_remaining_ms" in view["stats"]

    def test_task_failure_records_stage_and_retry(self, client):
        # 损坏的 PDF → 解析阶段失败
        task_id = _ingest(
            client,
            [("坏的.pdf", b"not-a-pdf-at-all", "application/pdf"), ("好的.md", SAMPLE_MD.encode(), "text/markdown")],
            {"strategy": "recursive", "params": {}},
        )
        view = _wait_task(client, task_id)
        task = view["task"]
        assert task["status"] == "partial_failed"
        failed = next(f for f in task["files"] if f["status"] == "failed")
        ok = next(f for f in task["files"] if f["status"] == "done")
        assert failed["error_stage"] == "parsing"
        assert failed["error_reason"]

        # 单文件重试（不清空已完成产物；解析阶段失败不保留解析缓存）
        resp = client.post(f"/knowledge/tasks/{task_id}/retry", json={"file_id": failed["file_id"]})
        result = resp.json()["data"]
        assert result["cleared_products"] is False
        assert failed["file_id"] in result["retried_files"]
        view2 = _wait_task(client, task_id)
        # 坏文件重试仍失败，但好文件的产物不受影响
        ok2 = next(f for f in view2["task"]["files"] if f["file_id"] == ok["file_id"])
        assert ok2["status"] == "done"

    def test_cancel_queued_task(self, client):
        task_id = _ingest(
            client,
            [("手册.md", SAMPLE_MD.encode(), "text/markdown")],
            {"strategy": "recursive", "params": {}},
        )
        resp = client.post(f"/knowledge/tasks/{task_id}/cancel")
        assert resp.json()["status"] == "ok"
        assert "cancelled_scope" in resp.json()["data"]
        _wait_task(client, task_id, timeout=10)

    def test_task_not_found(self, client):
        resp = client.get("/knowledge/tasks/t-20990101-nonexist")
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == 3301


# ---------- 预览与高级操作 ----------

class TestPreviewAndOps:
    def test_preview_outline_and_chunk_detail(self, client):
        upload_sample(client)
        preview = client.get("/knowledge/preview/产品手册").json()["data"]
        titles = [o["title"] for o in preview["outline"]]
        assert "产品简介" in titles
        chunk_id = preview["chunks"][0]["chunk_id"]
        from urllib.parse import quote
        detail = client.get(f"/knowledge/preview/产品手册/chunk/{quote(chunk_id, safe='')}").json()["data"]
        assert detail["content"] and detail["chunker"] == "recursive"
        assert detail["position"]

    def test_reparse_changes_chunker(self, client):
        upload_sample(client)
        resp = client.post(
            "/knowledge/产品手册/reparse",
            json={"chunk_policy": {"strategy": "semantic", "params": {"threshold": 0.3}}, "parse_policy": {}},
        )
        assert resp.json()["data"]["status"] == "indexed"
        chunks = client.get("/knowledge/preview/产品手册").json()["data"]["chunks"]
        assert all(c["chunker"] == "semantic" for c in chunks)

    def test_offline_removes_from_retrieval(self, client):
        upload_sample(client)
        before = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
        assert before["evidence"]

        resp = client.post("/knowledge/产品手册/offline")
        assert resp.json()["data"]["status"] == "offline"
        after = client.post("/ask", json={"question": "支持哪些文档格式？"}).json()["data"]
        # 下线后检索为空 → 通用知识兜底（行为变更：原为显式拒答）
        assert after["verdict"] == "answered"
        assert after["answer_source"] == "parametric"
        assert after["evidence"] == []

        # 记录与源文件保留（可重建恢复）
        files = client.get("/knowledge/files").json()["data"]
        assert files[0]["status"] == "offline"
        rebuild = client.post("/knowledge/rebuild/产品手册").json()["data"]
        assert rebuild["status"] == "indexed"

    def test_retrieval_probe_body_endpoint(self, client):
        upload_sample(client)
        resp = client.post("/knowledge/retrieval-probe", json={"doc_id": "产品手册", "query": "文档格式", "top_k": 2})
        data = resp.json()["data"]
        assert data["doc_id"] == "产品手册"
        assert all(h["chunk_id"].startswith("产品手册#") for h in data["hits"])
