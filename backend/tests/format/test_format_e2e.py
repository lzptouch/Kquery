"""格式专项的**端到端**部分：C8 可检索回读 + PDF-20~22 / PDF-27~30 / XFM-06。

这一层回答的是矩阵 §0.2 的原则 2：**"入库成功"不等于"内容可用"**。

解析器层的用例再全，也只能证明"解析器吐出了结构合理的产物"。
真正决定用户能不能搜到东西的，是入库、切块、索引、检索这一整条链。
所以每种主要格式都必须走一遍完整链路，并断言：
1. 上传后 `status == "indexed"`（不是"看起来成功了"）；
2. 预览能拿到块；
3. **提问能召回该文件的特征串**（真的能搜到，不只是"库里有个文件"）。

端到端用 Mock 模型（`MODEL_PROVIDER=mock`，见 `tests/conftest.py`），
不消耗云端额度，也不受网络波动影响。
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient

from .fixtures import (
    make_corrupt_pdf,
    make_docx,
    make_markdown,
    make_pptx,
    make_table_pdf,
    make_text_pdf,
    make_xlsx,
)


def _upload(client: TestClient, filename: str, data: bytes, **form: str) -> dict:
    resp = client.post(
        "/knowledge/upload",
        files={"file": (filename, data, "application/octet-stream")},
        data=form or None,
    )
    assert resp.status_code == 200, f"上传失败 {resp.status_code}: {resp.text[:300]}"
    return resp.json()["data"]


def _preview(client: TestClient, doc_id: str) -> dict:
    resp = client.get(f"/knowledge/preview/{doc_id}")
    assert resp.status_code == 200, f"预览失败 {resp.status_code}: {resp.text[:200]}"
    return resp.json()["data"]


def _ask(client: TestClient, question: str) -> dict:
    resp = client.post("/ask", json={"question": question})
    assert resp.status_code == 200, f"提问失败 {resp.status_code}: {resp.text[:200]}"
    return resp.json()["data"]


def _evidence_text(data: dict) -> str:
    """把 evidence 拼成一段文本，便于做子串断言。"""
    parts: list[str] = []
    for item in data.get("evidence") or []:
        if isinstance(item, dict):
            parts.append(str(item.get("content") or item.get("snippet") or ""))
            parts.append(str(item.get("source") or ""))
        else:
            parts.append(str(item))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# C8：可检索回读
# --------------------------------------------------------------------------


def test_c8_markdown_searchable_roundtrip(client: TestClient) -> None:
    """C8：Markdown → 入库 → 预览有块 → **提问召回特征串**。"""
    data = _upload(
        client,
        "知识库问答.md",
        make_markdown(),
    )
    assert data["status"] == "indexed", f"状态应为 indexed，实际 {data['status']}"
    assert data["chunks"] > 0, "入库后块数为 0"

    preview = _preview(client, data["doc_id"])
    assert preview["chunks"], "预览没有块"

    answer = _ask(client, "MARKDOWNSAMPLE 是什么")
    text = _evidence_text(answer)
    assert "MARKDOWNSAMPLE" in text, (
        f"提问没召回该文档的特征串——'入库成功'但内容不可用。evidence={text[:300]!r}"
    )


def test_c8_docx_searchable_roundtrip(client: TestClient) -> None:
    """C8：DOCX → 入库 → 提问召回特征串。"""
    data = _upload(client, "产品说明.docx", make_docx())
    assert data["status"] == "indexed", f"状态应为 indexed，实际 {data['status']}"

    answer = _ask(client, "DOCXPARAGRAPH1 讲了什么")
    assert "DOCXPARAGRAPH1" in _evidence_text(answer), "DOCX 内容没被召回"


def test_c8_xlsx_searchable_roundtrip(client: TestClient) -> None:
    """C8：XLSX → 入库 → 提问命中表格里的单元格值。"""
    data = _upload(client, "套餐表.xlsx", make_xlsx())
    assert data["status"] == "indexed", f"状态应为 indexed，实际 {data['status']}"

    answer = _ask(client, "知识库套餐 价格 是多少")
    text = _evidence_text(answer)
    assert "知识库套餐" in text, f"表格内容没被召回：{text[:300]!r}"


def test_c8_pptx_searchable_roundtrip(client: TestClient) -> None:
    """C8：PPTX → 入库 → 提问召回幻灯片文字。

    问句只带**特征串**，不带第二个实词——这不是为了让用例好看，而是 Mock 重排器
    的计分方式决定的：它按 |Q′∩D| / |Q′| 打分，问句里多一个实词而文档里没有，
    分数就摊薄一半。生产用 gte-rerank-v2 是整体相关性，不会有这个效应。
    摊薄后平均分跌破测试档的 CRAG 阈值 → 判 ambiguous → 走通用知识兜底 →
    evidence 被清空——那是**测试环境的阈值效应**，不是"内容检索不到"。
    """
    data = _upload(client, "汇报.pptx", make_pptx())
    assert data["status"] == "indexed", f"状态应为 indexed，实际 {data['status']}"

    answer = _ask(client, "PPTXSLIDE1 是什么")
    assert "PPTXSLIDE1" in _evidence_text(answer), (
        f"PPTX 内容没被召回。verdict={answer.get('verdict')} "
        f"crag={answer.get('crag')} retrieval={answer.get('retrieval')} "
        f"answer={str(answer.get('answer'))[:120]!r}"
    )


def test_c8_pdf_searchable_roundtrip(client: TestClient) -> None:
    """PDF-20：PDF → 入库 → 预览可溯源 → 提问召回。"""
    data = _upload(client, "白皮书.pdf", make_text_pdf(pages=1))
    assert data["status"] == "indexed", f"状态应为 indexed，实际 {data['status']}"

    preview = _preview(client, data["doc_id"])
    assert preview["chunks"], "预览没有块"
    sources = [c["source"] for c in preview["chunks"]]
    assert any("第1页" in s for s in sources), f"溯源未精确到页：{sources}"

    answer = _ask(client, "测试内容页面内容 知识库")
    assert answer.get("evidence"), "提问没有返回任何证据"


# --------------------------------------------------------------------------
# PDF-21 / PDF-22：表格与多页溯源
# --------------------------------------------------------------------------


def test_pdf21_table_retrievable_as_table_chunk(client: TestClient) -> None:
    """PDF-21：表格内容出现在 evidence 里，且该块 `modality == "table"`。"""
    data = _upload(client, "报表.pdf", make_table_pdf(rows=3, cols=3))
    assert data["status"] == "indexed", f"状态应为 indexed，实际 {data['status']}"

    preview = _preview(client, data["doc_id"])
    table_chunks = [c for c in preview["chunks"] if c["modality"] == "table"]
    assert table_chunks, "预览里没有 modality=='table' 的块——表格没被单独成块"
    assert preview["tables"], "预览里没有表格记录"

    answer = _ask(client, "R0C0 R1C1 表格内容")
    text = _evidence_text(answer)
    assert "R0C0" in text, f"表格内容没被召回：{text[:300]!r}"


def test_pdf22_multi_page_source_precision(client: TestClient) -> None:
    """PDF-22：多页 PDF 的 evidence source 精确到「第N页」。"""
    data = _upload(client, "多页.pdf", make_text_pdf(pages=3))
    assert data["status"] == "indexed"

    preview = _preview(client, data["doc_id"])
    sources = [c["source"] for c in preview["chunks"]]
    assert len([s for s in sources if "第" in s and "页" in s]) >= 2, (
        f"多页溯源应覆盖多页，实际 {sources}"
    )
    for s in sources:
        if "页" in s:
            assert "第" in s, f"source 缺少页码格式：{s!r}"


# --------------------------------------------------------------------------
# PDF-27~30：策略透传与失败定位
# --------------------------------------------------------------------------


def test_pdf27_pdf_tables_policy_passthrough(client: TestClient) -> None:
    """PDF-27：`parse_policy={"pdf_tables": false}` → 预览无表格。"""
    resp = client.post(
        "/knowledge/ingest",
        files={"files": ("无表格.pdf", make_table_pdf(), "application/pdf")},
        data={"parse_policy": '{"pdf_tables": false}'},
    )
    assert resp.status_code == 200, resp.text[:300]
    task_id = resp.json()["data"]["task_id"]
    _wait_task(client, task_id)

    files = client.get("/knowledge/files").json()["data"]
    record = next(f for f in files if f["filename"] == "无表格.pdf")
    preview = _preview(client, record["doc_id"])
    assert not preview["tables"], f"策略关闭后仍有表格：{preview['tables']}"


def test_pdf28_pdf_ocr_policy_passthrough(client: TestClient) -> None:
    """PDF-28：`parse_policy={"pdf_ocr": false}` → 扫描页不入库且有 warning。

    用**扫描页** PDF：文字页不受 pdf_ocr 影响，只有扫描页能验出这条策略。
    """
    from .fixtures import make_scanned_pdf

    resp = client.post(
        "/knowledge/ingest",
        files={"files": ("扫描件.pdf", make_scanned_pdf(), "application/pdf")},
        data={"parse_policy": '{"pdf_ocr": false}'},
    )
    assert resp.status_code == 200, resp.text[:300]
    task_id = resp.json()["data"]["task_id"]
    view = _wait_task(client, task_id)

    files = client.get("/knowledge/files").json()["data"]
    record = next((f for f in files if f["filename"] == "扫描件.pdf"), None)
    if record is None:
        # 扫描页不入库 → 文件失败，这是"诚实失败"，同样算策略生效
        file_view = (view.get("task") or {}).get("files", [{}])[0]
        assert file_view.get("status") == "failed", f"任务状态异常：{file_view}"
        return
    preview = _preview(client, record["doc_id"])
    assert not any("OCR" in c["source"] for c in preview["chunks"]), (
        f"策略关闭后仍有 OCR 段：{[c['source'] for c in preview['chunks']]}"
    )


def test_pdf29_illegal_policy_rejected(client: TestClient) -> None:
    """PDF-29：非法 policy → 明确错误，**不静默忽略**。

    静默忽略最坏：用户以为自己关掉了 OCR，实际上没关——
    扫描件照样走云端 OCR，费用照收。
    """
    resp = client.post(
        "/knowledge/ingest",
        files={"files": ("x.pdf", make_text_pdf(), "application/pdf")},
        data={"parse_policy": '{"pdf_tables": "yes"}'},
    )
    assert resp.status_code == 400, f"期望 400，实际 {resp.status_code}"
    body = resp.json()
    assert body["status"] == "error"
    assert body["error"]["code"] == 2001, f"期望 2001，实际 {body['error']['code']}"


def test_pdf30_task_failure_stage_is_parsing(client: TestClient) -> None:
    """PDF-30：损坏 PDF 在异步任务里 → `status=="failed"` + `error_stage=="parsing"`。

    失败阶段必须准确：解析阶段失败可以重试（换个解析策略可能就好了），
    索引阶段失败是另一回事（一致性临界区）。阶段标错，重试策略就会错。
    """
    resp = client.post(
        "/knowledge/ingest",
        files={"files": ("坏文件.pdf", make_corrupt_pdf(), "application/pdf")},
    )
    assert resp.status_code == 200, resp.text[:300]
    task_id = resp.json()["data"]["task_id"]
    view = _wait_task(client, task_id)

    file_view = (view.get("task") or {}).get("files", [{}])[0]
    assert file_view.get("status") == "failed", f"期望 failed，实际 {file_view}"
    assert file_view.get("error_stage") == "parsing", (
        f"期望 error_stage=='parsing'，实际 {file_view.get('error_stage')!r}"
    )
    assert file_view.get("error_reason"), "失败必须带原因，否则用户只看到一个红叉"


def _wait_task(client: TestClient, task_id: str, timeout_s: float = 60.0) -> dict:
    """轮询入库任务直到结束。"""
    deadline = time.time() + timeout_s
    view: dict = {}
    while time.time() < deadline:
        resp = client.get(f"/knowledge/tasks/{task_id}")
        assert resp.status_code == 200, resp.text[:200]
        view = resp.json()["data"]
        task = view.get("task") or {}
        status = task.get("status")
        if status in ("done", "partial_failed", "failed", "cancelled"):
            return view
        time.sleep(0.2)
    raise AssertionError(f"入库任务超时未结束：{view}")


# --------------------------------------------------------------------------
# XFM-06：同步与异步一致
# --------------------------------------------------------------------------


def test_xfm06_sync_and_async_same_chunks(client: TestClient) -> None:
    """XFM-06：同一文件走 `/knowledge/upload`（同步）与 `/knowledge/ingest`（异步）
    → 产出的**块数与表格数一致**。

    两条路径是不同代码（同步走 `ingest_path`，异步走任务队列），
    很容易一个改了策略另一个没改。
    """
    payload = make_table_pdf(rows=3, cols=3)

    sync_data = _upload(client, "同步对比.pdf", payload)
    assert sync_data["status"] == "indexed"

    resp = client.post(
        "/knowledge/ingest",
        files={"files": ("异步对比.pdf", payload, "application/pdf")},
    )
    assert resp.status_code == 200, resp.text[:300]
    task_id = resp.json()["data"]["task_id"]
    _wait_task(client, task_id)

    files = client.get("/knowledge/files").json()["data"]
    async_record = next(f for f in files if f["filename"] == "异步对比.pdf")

    sync_preview = _preview(client, sync_data["doc_id"])
    async_preview = _preview(client, async_record["doc_id"])

    assert len(sync_preview["chunks"]) == len(async_preview["chunks"]), (
        f"同步 {len(sync_preview['chunks'])} 块 vs 异步 {len(async_preview['chunks'])} 块"
    )
    assert len(sync_preview["tables"]) == len(async_preview["tables"]), (
        f"同步 {len(sync_preview['tables'])} 表 vs 异步 {len(async_preview['tables'])} 表"
    )


# --------------------------------------------------------------------------
# PDF-25：PII 盲区（缺口登记）
# --------------------------------------------------------------------------


def test_pdf25_pii_blind_spot_in_binary(client: TestClient) -> None:
    """PDF-25：**已知安全缺口登记** —— 二进制格式不进 PII 入口扫描。

    矩阵 §1.4 风险 #2：纯文本（txt/md/csv…）会在入口被 PII 硬拦截，
    但 PDF/DOCX 这类二进制**不拦**——文档里的手机号/身份证会被入库、
    被检索引用、最终可能出现在回答里。

    这条用例断言的是**当前行为**（不拦）。等哪天补上二进制内文本扫描，
    它会红——那时正好更新矩阵与这条用例。
    """
    pdf_with_phone = make_text_pdf(pages=1)
    # 在 PDF 里塞一个手机号（纯文本 PDF 的内容就是可解码的）
    import pymupdf

    doc = pymupdf.open(stream=pdf_with_phone, filetype="pdf")
    page = doc[0]
    page.insert_text((72, 200), "联系电话 13800138000", fontsize=11, fontname="china-s")
    payload = doc.tobytes()
    doc.close()

    data = _upload(client, "含手机号.pdf", payload)
    assert data["status"] == "indexed", (
        "当前行为：二进制不进 PII 入口扫描，含手机号的 PDF **会**被入库。"
        "若此断言失败，说明二进制 PII 扫描已补上——请更新矩阵 §十二 风险 #2。"
    )
