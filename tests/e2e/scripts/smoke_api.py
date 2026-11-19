#!/usr/bin/env python3
"""端到端接口冒烟测试：对着真实运行的服务打，覆盖 ~9 的关键路径。

为什么不用 pytest：这里要验的是**部署形态**（容器 + Milvus + nginx 代理），
不是应用内逻辑。应用逻辑由 backend/tests 覆盖，这里管的是"装起来还能不能用"。

用法：
    python3 tests/e2e/scripts/smoke_api.py --base http://localhost:8000
    python3 tests/e2e/scripts/smoke_api.py --base http://localhost:8081   # 经 nginx 验证代理
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# 共享 HTTP 客户端（此前本文件与 api_sweep.py 各写一份，参数还不一致）
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from console import FAILED, PASSED, check, section  # noqa: E402
from http_client import call, data_of, set_token, upload_multipart  # noqa: E402



# 认证已下线：管理面端点不再校验角色，不再注入任何身份头。
# 认证已下线：不再注入身份头，所有端点直接可达。
AUTH_HEADERS: dict[str, str] = {}
# 令牌统一由 http_client 持有（call() 从那里读）
def rows_of(body, key=None):
    """取列表型 data。"""
    if not isinstance(body, dict):
        return []
    value = body.get("data")
    if isinstance(value, list):
        return value
    if isinstance(value, dict) and key:
        inner = value.get(key)
        return inner if isinstance(inner, list) else []
    return []


SAMPLE_MD = """# 曼昆经济学核心概念

## 机会成本
机会成本是为了得到某物而放弃的最高价值替代品。
经济学家把一切都看作取舍，因此机会成本无处不在。
想象一个学生 deciding 考研还是直接工作：读研要放弃两年的工资收入，
这笔放弃的收入就是读研的机会成本。

## 边际思维
边际分析关注的是"多一单位"带来的变化，而非总量。
例如多喝一杯咖啡的收益，要减去它占用的睡眠时间。
边际思维的核心问题是：再增加一单位投入，净收益是正还是负。

## 需求曲线
需求曲线向右下方倾斜，反映边际效用递减规律。
价格上升导致需求量下降，这被称为需求定律。
当价格上涨时，消费者会用自己的预算去替代相对更便宜的商品，
这种替代效应构成了需求曲线向下倾斜的微观基础。

## 供给曲线
供给曲线向右上方倾斜，反映生产者追求利润最大化的行为。
价格上涨让生产者愿意投入更多产能，因为单位产品的利润空间变大了。
但在长期中，供给曲线会受到技术进步与要素价格变动的双重影响。

## 通货膨胀
通货膨胀意味着货币购买力下降。
通胀率过高会让储蓄贬值，也会扭曲价格信号。
恶性通货膨胀会让经济交易退回以物易物，因为货币不再被信任。

## 消费者剩余与生产者剩余
消费者剩余是消费者愿意支付的最高价格与实际支付价格之间的差额。
生产者剩余是实际收到的价格与生产成本之间的差额。
两者之和构成社会总剩余，这是效率判断的根本标准。

## 外部性与公共物品
外部性是指一个人的行为给第三方带来的未被计入价格的影响。
负外部性包括污染和拥堵，正外部性包括教育带来的社会收益。
公共物品具有非排他性与非竞争性，容易出现搭便车问题，
这解释了为什么路灯、国防等物品通常由政府提供。

## 机会成本在投资决策中的应用
投资决策的本质是比较不同项目的未来现金流与机会成本。
一个项目值得做，当且仅当它的净现值大于资金被用于其他途径的收益。
忽略机会成本会导致系统性高估——把所有支出都算作"成本"，
而没有算上这笔资金本可以获得的回报。

## 比较优势与国际贸易
即使一个国家在所有产品上都更有效率，仍然存在分工与贸易的理由。
比较优势来自机会成本的相对差异，而非绝对效率差异。
国际分工可以让各方都消费到超出本国生产潜能的商品。

## 通货膨胀的度量
消费者价格指数是最常用的通胀度量，它衡量一篮子商品与服务的价格变化。
核心通胀剔除食品与能源，因为这两项价格波动大但不代表通胀压力。
菲利普斯曲线最初描述通胀与失业的此消彼长，但长期看二者并不存在稳定替代。

## 经济增长与生产函数
生产函数描述产出与资本、劳动、技术三要素之间的关系。
技术进步是人均产出长期增长的主要来源。
经济增长的可持续性取决于投资是否转化为有效的资本积累。

## 市场失灵与政府干预
市场失灵包括外部性、公共物品、信息不对称与市场势力四种。
政府干预本身也可能失败，因为政府同样存在信息不足与激励问题。
政策评价的标准应当是干预的净收益，而不是"有没有道理"。
"""


def run(base: str) -> int:
    section(f"目标服务: {base}")

    # ---------------------------------------------------- 认证端点下线边界
    section("认证 · 端点已下线")
    # 认证与授权已整体下线：/auth/* 与权限类端点必须 404。
    # 保留这条检查是为了防止"路由还挂着但实现没了"导致 500。
    for path in ("/auth/login", "/auth/register", "/auth/me"):
        status, body = call(base, "GET", path, auth=False)
        check(f"{path} 已下线（404）", status == 404, f"实际 {status}: {str(body)[:100]}")

    # ----------------------------------------------------------  契约
    section("全局契约与探针")
    status, body = call(base, "GET", "/healthz")
    check("/healthz 返回 200", status == 200, f"实际 {status}")
    # 经代理时最常见的坑：健康端点被当成 SPA 页面返回 HTML。
    # 这里显式断言是 JSON，失败时把内容前 80 字打出来便于定位。
    check("健康报告是 JSON（未被误当 SPA 页面）",
          isinstance(body, dict), f"实际类型={type(body).__name__} 内容={str(body)[:80]}")
    check("健康报告含逐依赖明细", isinstance(body, dict) and "dependencies" in body,
          f"keys={sorted(body) if isinstance(body, dict) else '非字典'}")
    deps = {}
    if isinstance(body, dict):
        deps = {d.get("name"): d.get("status")
                for d in body.get("dependencies", []) if isinstance(d, dict)}
    check("vector_store 依赖为 up", deps.get("vector_store") == "up", f"实际 {deps}")
    check("graph_store 依赖为 up", deps.get("graph_store") == "up", f"实际 {deps}")

    status, _ = call(base, "GET", "/livez")
    check("/livez 返回 200", status == 200, f"实际 {status}")

    status, body = call(base, "GET", "/readyz")
    check("/readyz 返回 200（空索引不应判为未就绪）", status == 200, f"实际 {status}: {body}")

    status, body = call(base, "GET", "/metrics")
    check("/metrics 返回 Prometheus 文本", status == 200 and "# HELP" in str(body))

    # ----------------------------------------------------------  入库
    section("文档上传与索引")
    boundary = "----kquery-smoke-boundary"
    payload = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="smoke.md"\r\n'
        "Content-Type: text/markdown\r\n\r\n"
    ).encode() + SAMPLE_MD.encode() + f"\r\n--{boundary}--\r\n".encode()
    status, body = call(base, "POST", "/knowledge/upload", body=payload,
                         content_type=f"multipart/form-data; boundary={boundary}")
    check("上传文档返回 200", status == 200, f"实际 {status}: {str(body)[:180]}")
    doc_id = data_of(body).get("doc_id")
    check("响应含 doc_id", bool(doc_id), f"body={str(body)[:180]}")
    check("文件状态为 indexed", data_of(body).get("status") == "indexed",
          f"实际 {data_of(body).get('status')}")

    status, body = call(base, "GET", "/knowledge/files")
    files = rows_of(body)
    check("文件列表包含刚上传的文档", any(f.get("doc_id") == doc_id for f in files),
          f"列表 {len(files)} 个文件")
    indexed = [f for f in files if f.get("status") == "indexed"]
    check("存在已索引文件", len(indexed) > 0, f"indexed={len(indexed)}")

    status, body = call(base, "GET", "/knowledge/stats")
    check("统计端点返回块数", data_of(body).get("total_chunks", 0) > 0,
          f"实际 {str(body)[:160]}")

    if doc_id:
        status, body = call(base, "GET", f"/knowledge/preview/{doc_id}")
        chunks = data_of(body).get("chunks", [])
        check("解析预览返回切块", status == 200 and len(chunks) > 0,
              f"实际 {status}: {str(body)[:160]}")
        # 文档约 1600 字 / chunk_size 300，应当切成多块。
        # 只切出 1 块说明分块参数没生效或整篇被当成短文档，检索会退化成整篇匹配。
        check("长文档被切成多块（分块生效）", len(chunks) > 1, f"只切出 {len(chunks)} 块")
        check("每块不超过 chunk_size 上限",
              all(len(c.get("preview") or "") <= 600 for c in chunks),
              f"最大预览 {max((len(c.get('preview') or '') for c in chunks), default=0)} 字")
        if chunks:
            cid = chunks[0]["chunk_id"]
            # chunk_id 形如 "smoke#1"，含 # 会被当成 URL 的 fragment 分隔符，
            # 后面的 "1" 直接丢失 → 404。必须整体编码后再拼进路径。
            # （前端 api-client 已经这么做了，这里跟它保持一致）
            cid_seg = urllib.parse.quote(cid, safe="")
            doc_seg = urllib.parse.quote(doc_id, safe="")
            status, detail = call(base, "GET", f"/knowledge/preview/{doc_seg}/chunk/{cid_seg}")
            check("切块详情可查", status == 200 and data_of(detail).get("chunk_id") == cid,
                  f"实际 {status}")

    # ----------------------------------------------------------  检索
    section("混合检索与检索配置")
    status, body = call(base, "POST", "/knowledge/retrieval-probe",
                        body={"doc_id": doc_id, "query": "机会成本是什么", "top_k": 3})
    check("检索探针端点返回 200", status == 200, f"实际 {status}: {str(body)[:160]}")
    probe = data_of(body)
    hits = probe.get("hits") or probe.get("results") or []
    check("检索命中结果非空", len(hits) > 0, f"probe keys={sorted(probe)}")
    # 混合检索的可观测性落在 stats 里（每路召回数 + 耗时分解），
    # hits 是精简后的呈现结果，不带各路 rank
    stats = probe.get("stats") or {}
    check("检索统计含双路召回", "keyword_candidates" in stats and "vector_candidates" in stats,
          f"stats keys={sorted(stats)}")
    check("检索统计含融合与重排计数",
          all(k in stats for k in ("fused_count", "reranked_count", "into_context_count")),
          f"stats keys={sorted(stats)}")
    check("检索统计含耗时分解",
          all(k in stats for k in ("keyword_ms", "vector_ms", "fusion_ms", "rerank_ms")),
          f"stats keys={sorted(stats)}")
    if hits:
        first = hits[0]
        check("命中可溯源到块", any(f in first for f in ("chunk_id", "doc_id", "content")),
              f"字段: {sorted(first)[:14]}")

    status, body = call(base, "GET", "/admin/retrieval-config")
    check("检索配置端点返回 200", status == 200, f"实际 {status}")
    cfg = data_of(body)
    active = cfg.get("active") or cfg.get("current") or {}
    check("检索配置含生效参数", bool(active), f"keys={sorted(cfg)}")
    check("生效配置含 rrf_k", "rrf_k" in active, f"active keys={sorted(active)[:12]}")

    # ----------------------------------------------------------  问答
    section("/4/5 · 问答与自检")
    status, body = call(base, "POST", "/ask", body={"question": "什么是机会成本？"})
    check("问答返回 200", status == 200, f"实际 {status}: {str(body)[:200]}")
    data = data_of(body)
    check("答案为非空字符串", bool(data.get("answer")), "answer 为空")
    check("返回引用溯源证据", len(data.get("evidence") or []) > 0,
          f"answer={data.get('answer')!r} evidence={len(data.get('evidence') or [])}")
    check("含 trace_id", bool(data.get("trace_id")), "缺 trace_id")
    # 意图识别与成本核算
    analysis = data.get("analysis") or {}
    check("问答响应含意图判定", bool(analysis.get("intent")), f"analysis={analysis}")
    check("问答响应含预处理分析", bool(analysis), "缺 analysis")
    if analysis:
        check("分析含 cost 字段（ 成本）", "cost" in analysis,
              f"analysis keys={sorted(analysis)}")
        check("分析含意图置信度", analysis.get("intent_confidence") is not None)
        check("分析含改写列表", isinstance(analysis.get("rewrites"), list))
        check("分析含耗时", isinstance(analysis.get("elapsed_ms"), int))
    check("分析含数据源路由", isinstance(analysis.get("sources"), list))
    #  的核心：BM25 与向量两路都真实召回，不是只跑一路
    retrieval = data.get("retrieval") or {}
    check("问答链路走混合检索",
          retrieval.get("keyword_candidates", 0) > 0 and retrieval.get("vector_candidates", 0) > 0,
          f"keyword={retrieval.get('keyword_candidates')} vector={retrieval.get('vector_candidates')}")
    check("检索经过重排", retrieval.get("rerank_ms", 0) >= 0 and bool(data.get("reranked")),
          f"reranked={data.get('reranked')} rerank_ms={retrieval.get('rerank_ms')}")

    # ---------------------------------------------------------- SSE 流式
    section("SSE 流式问答")
    status, text = call(base, "POST", "/ask/stream",
                         body={"question": "边际思维是什么意思"},
                         raw=True, timeout=180)
    check("SSE 端点返回 200", status == 200, f"实际 {status}")
    check("SSE 产出 token 事件", "event: token" in str(text), f"前 200 字: {str(text)[:200]}")
    check("SSE 有 done 事件", "event: done" in str(text))

    # ----------------------------------------------------------  分析
    section("Query 分析")
    # 说明：项目没有独立的"分析这个 query"端点，预处理是在 /ask 内部完成的。
    # 这里只能验证历史分析记录可查，成本字段则在下面的 ask 响应里查。
    status, body = call(base, "GET", "/knowledge/query-analysis?limit=5")
    check("Query 分析历史端点返回 200", status == 200, f"实际 {status}: {str(body)[:160]}")
    rows = rows_of(body)
    check("分析历史可查", status == 200, f"返回 {len(rows)} 条")
    if rows:
        row = rows[0]
        check("分析记录含意图字段", bool(row.get("intent")), f"keys={sorted(row)}")
        check("分析记录含降级标注", "degraded" in row, f"keys={sorted(row)}")

    # ----------------------------------------------------------  图谱
    section("图谱与跨模态")
    status, body = call(base, "GET", "/graph/entities")
    check("图谱实体端点返回 200", status == 200, f"实际 {status}")
    status, body = call(base, "GET", "/graph/stats")
    check("图谱统计端点返回 200", status == 200, f"实际 {status}")
    status, body = call(base, "GET", "/graph/communities")
    check("社区端点返回 200", status == 200, f"实际 {status}")

    status, body = call(base, "GET", "/graph/subgraph?name=%E6%9C%BA%E4%BC%9A%E6%88%90%E6%9C%AC&depth=1")
    check("子图查询返回 200", status == 200, f"实际 {status}")

    # ----------------------------------------------------------  安全
    section("认证与审计")
    # 先验鉴权真的在拦人（ 的验收口）：匿名与越权都必须被拒。
    # 认证与授权已下线：管理端点不再校验角色，越权矩阵整体消失。
    # 保留的断言是**反向**的——权限类端点必须 404（防止路由残留）。
    for gone_path in ("/admin/users/permission-matrix", "/admin/users",
                      "/admin/documents/acl"):
        gone_status, _ = call(base, "GET", gone_path, auth=False)
        check(f"{gone_path} 已下线（404）", gone_status == 404, f"实际 {gone_status}")

    for path in ("/admin/audit", "/admin/audit/export", "/admin/quota",
                 "/admin/quota/platform", "/admin/sensitive"):
        status, _ = call(base, "GET", path)
        check(f"已下线端点 {path} 返回 404", status == 404, f"实际 {status}")

    # ----------------------------------------------------------  会话
    section("Agent 会话")
    status, body = call(base, "GET", "/sessions")
    check("会话列表返回 200", status == 200, f"实际 {status}")

    # ---------------------------------------------------------- 错误契约
    section("错误语义")
    status, body = call(base, "GET", "/knowledge/preview/does-not-exist")
    check("不存在的文档返回 404", status == 404, f"实际 {status}")
    check("404 响应仍是统一信封", isinstance(body, dict) and "error" in body, f"实际 {str(body)[:150]}")

    status, body = call(base, "POST", "/knowledge/upload",
                        body=b"not-a-real-multipart-body",
                        content_type="multipart/form-data; boundary=----nope")
    check("非法上传返回 4xx 而非 500", 400 <= status < 500, f"实际 {status}")

    status, body = call(base, "POST", "/ask", body={})
    check("空问题返回 400", status == 400, f"实际 {status}")
    check("400 响应含错误码", isinstance(body, dict) and (body.get("error") or {}).get("code") is not None,
          f"实际 {str(body)[:150]}")
    check("400 响应不含 FastAPI 的 detail 结构", isinstance(body, dict) and "detail" not in body,
          f"实际 {str(body)[:150]}")

    # ---------------------------------------------------------- F4 快问
    section("F4 快问（doc-qa 能力整合）")

    # 快问：上传 → 提问 → 删除
    status, body = upload_multipart(base, "/quickask/upload", "冒烟临时.md",
                                     SAMPLE_MD.encode("utf-8"))
    check("快问上传返回 200", status == 200, f"实际 {status} {str(body)[:150]}")
    quick = data_of(body)
    doc_id = quick.get("doc_id") or ""
    check("快问返回 doc_id", bool(doc_id), f"keys={sorted(quick)}")
    check("快问 doc_id 带 quick_ 前缀", doc_id.startswith("quick_"), doc_id)

    # 快问文档不得进知识库列表
    status, body = call(base, "GET", "/knowledge/files")
    names = [f.get("filename") for f in rows_of(body)]
    check("快问文档未进知识库列表", "冒烟临时.md" not in names, f"files={names}")

    if doc_id:
        status, body = call(base, "POST", "/quickask/ask",
                            body={"question": "机会成本是什么？", "doc_id": doc_id},
                            raw=True)
        text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
        check("快问提问返回 200", status == 200, f"实际 {status}")
        check("快问 SSE 含 done 事件", "event: done" in text, text[:200])
        check("快问 SSE 含证据事件", "event: evidence" in text, text[:200])

        status, _ = call(base, "DELETE", f"/quickask/{doc_id}")
        check("快问删除返回 200", status == 200, f"实际 {status}")

    # ---------------------------------------------------------- 汇总
    total = len(PASSED) + len(FAILED)
    print("\n" + "=" * 62)
    if FAILED:
        print(f"\033[31m失败 {len(FAILED)}/{total}\033[0m")
        for name, detail in FAILED:
            print(f"  · {name}\n    {detail}")
        return 1
    print(f"\033[32m全部通过：{len(PASSED)}/{total}\033[0m")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000",
                        help="服务基址。传 http://localhost:8081 可顺带验证 nginx 代理。")
    args = parser.parse_args()
    set_token(args.token)
    start = time.perf_counter()
    code = run(args.base)
    print(f"耗时 {time.perf_counter() - start:.1f}s")
    sys.exit(code)
