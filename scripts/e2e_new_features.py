#!/usr/bin/env python3
"""浏览器端到端：验证本轮新增能力（/117/124/125/126/131/134/165）。

与 e2e_browser.py 的分工：
- e2e_browser.py 验**既有**核心流程（提问、引用、上传、图谱、报告、管理页）
- 本脚本验**本轮新增**的能力，其中一部分只有后端端点（还没做 UI），
  因此对它们用「浏览器内发起同源请求」的方式验证——这仍然覆盖了
  用户关心的问题：**经前端代理能否访问到后端新接口**。

三类验证方式：
1. 真实点击（UI 存在）：Agent 轨迹、身份切换后的权限提示
2. 浏览器内同源请求（后端优先后新增的端点）：成本、工具健康、探针
3. 控制台错误检查：任何 4xx/5xx 都必须被解释清楚，不能静默

用法：
    python3 scripts/e2e_new_features.py --base http://127.0.0.1:5173
"""
import argparse
import json
import sys
import time

from playwright.sync_api import sync_playwright

PASSED: list[str] = []
FAILED: list[tuple[str, str]] = []
CONSOLE_ERRORS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  \033[32m✓\033[0m {name}")
        return True
    FAILED.append((name, detail))
    print(f"  \033[31m✗\033[0m {name} — {detail}")
    return False


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m")


def page_text(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:
        return ""


def api(page, path: str, method: str = "GET", body=None, timeout_ms: int = 30000) -> dict:
    """在浏览器上下文内发起同源请求。

    刻意用浏览器而不是 requests：这样请求会走**前端的 vite 代理**，
    验证的是"用户在页面里能否访问到该接口"这条真实链路。
    """
    expr = """
    async ([path, method, body, timeoutMs]) => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), timeoutMs);
      try {
        const headers = { "Content-Type": "application/json" };
        const tok = localStorage.getItem("kquery.token");
        if (tok) headers["Authorization"] = "Bearer " + tok;
        else {
          headers["X-User-Id"] = localStorage.getItem("kquery.user_id") || "admin";
          headers["X-Tenant-Id"] = localStorage.getItem("kquery.tenant_id") || "default";
          headers["X-Roles"] = localStorage.getItem("kquery.roles") ?? "admin";
        }
        const resp = await fetch(path, {
          method, headers, signal: ctrl.signal,
          ...(body ? { body: JSON.stringify(body) } : {}),
        });
        const text = await resp.text();
        let json = null;
        try { json = JSON.parse(text); } catch { /* 非 JSON */ }
        return { status: resp.status, json, text: text.slice(0, 200000) };
      } catch (e) {
        return { status: 0, error: String(e) };
      } finally {
        clearTimeout(timer);
      }
    }
    """
    return page.evaluate(expr, [path, method, body, timeout_ms])


def run(base: str, headed: bool) -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed)
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        page.on(
            "console",
            lambda m: CONSOLE_ERRORS.append(f"{m.type}: {m.text}") if m.type == "error" else None,
        )
        page.on("pageerror", lambda e: CONSOLE_ERRORS.append(f"pageerror: {e}"))

        page.goto(base, wait_until="networkidle", timeout=45000)

        # ================================================== 1. Agent 轨迹
        section("1. Agent 模式问答与轨迹（真实点击）")
        box = page.locator('textarea, input[type="text"]').first
        box.wait_for(state="visible", timeout=15000)
        question = "年假政策规定满三年有几天的带薪年假？"
        box.fill(question)
        send = page.locator(
            'button[type="submit"], button:has-text("发送"), button:has-text("提问")'
        ).first
        if send.count() > 0 and send.is_visible():
            send.click()
        else:
            box.press("Enter")
        check("提交问题（真实点击）", True)

        # 等轨迹节点出现——Agent 模式的标志性产物
        trace_seen = False
        for _ in range(90):
            page.wait_for_timeout(500)
            if page.locator(".trace-node, .agent-trace, .citation-chip").count() > 0:
                trace_seen = True
                break
        check("Agent 轨迹或引用已渲染", trace_seen, "等待 45s 未见轨迹/引用")

        text = page_text(page)
        check("回答正文可见", len(text) > 200, f"正文长度 {len(text)}")

        # 轨迹可展开并含工具步骤（真实点击展开）
        expanders = page.locator(
            'button:has-text("轨迹"), summary:has-text("轨迹"), button:has-text("检索过程")'
        )
        if expanders.count() > 0:
            try:
                expanders.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                check("点击展开轨迹后页面正常", True)
            except Exception as exc:  # noqa: BLE001
                check("点击展开轨迹后页面正常", False, str(exc)[:120])
        else:
            check("存在轨迹展开入口", False, "未找到展开按钮（轨迹可能默认展开）")

        # ================================================== 2. 新端点（同源）
        section("2. 本轮新增端点的浏览器同源访问")

        r = api(page, "/admin/tools")
        check("GET /admin/tools 可用（工具健康）", r["status"] == 200,
              f"实际 {r['status']} {r.get('text','')[:120]}")
        tools = ((r.get("json") or {}).get("data") or {}).get("tools") or []
        check("工具清单含危险级与熔断状态",
              bool(tools) and all("danger" in t and "tripped" in t for t in tools),
              f"字段缺失：{tools[:1]}")
        check("工具清单含本轮新增工具",
              any(t.get("name") == "list_documents" for t in tools),
              f"实际：{[t.get('name') for t in tools]}")

        r = api(page, "/admin/costs")
        check("GET /admin/costs 可用（成本看板）", r["status"] == 200,
              f"实际 {r['status']} {r.get('text','')[:120]}")
        cost_data = (r.get("json") or {}).get("data") or {}
        check("成本看板含归集维度与可信标记",
              {"group_by", "total_yuan", "groups", "priced"} <= set(cost_data),
              f"keys={sorted(cost_data)[:8]}")

        r = api(page, "/readyz")
        check("GET /readyz 可用（就绪探针，需在 proxy 列表内）",
              r["status"] in (200, 503) and isinstance(r.get("json"), dict),
              f"实际 {r['status']}；拿到的不是 JSON 说明回落到了 SPA HTML")

        # 探针明细在 /healthz（逐个依赖的状态与耗时）
        r = api(page, "/healthz")
        check("GET /healthz 可用（依赖健康报告）", r["status"] in (200, 503),
              f"实际 {r['status']}")
        health = r.get("json") or {}
        # 必须先断言"拿到了依赖列表"，再验字段——写成 `if deps:` 会让拿不到时静默跳过
        deps = health.get("dependencies")
        check("健康报告返回结构化 dependencies", isinstance(deps, list) and bool(deps),
              f"实际：{str(health)[:140]}")
        by_name = {d.get("name"): d for d in (deps or [])}

        check("探针含共享状态后端", "shared_state" in by_name,
              f"实际探针：{sorted(by_name)}")
        ss = by_name.get("shared_state") or {}
        check("共享状态标明实际后端（防「以为在用 Redis 其实已降级」）",
              bool(ss.get("backend")), f"shared_state={ss}")

        check("探针含长期记忆", "long_term_memory" in by_name,
              f"实际探针：{sorted(by_name)}")
        ltm = by_name.get("long_term_memory") or {}
        check("长期记忆探针含容量与条目数（证明真的挂了存储）",
              {"entries", "max_per_tenant"} <= set(ltm), f"long_term_memory={ltm}")

        check("探针含图谱存储（ 持久化后端）", "graph_store" in by_name,
              f"实际探针：{sorted(by_name)}")
        gs = by_name.get("graph_store") or {}
        check("图谱探针含实体/关系计数", {"entities", "relations"} <= set(gs), f"{gs}")

        check("所有依赖均为 up（无降级依赖）",
              all(d.get("status") == "up" for d in deps or []),
              f"down={health.get('down')}")

        r = api(page, "/metrics")
        metrics_text = r.get("text") or ""
        check("GET /metrics 可用", r["status"] == 200, f"实际 {r['status']}")
        for name in ("rag_agent_runs_total", "rag_agent_fuse_total",
                     "rag_agent_next_action_total", "rag_agent_tool_calls_total"):
            check(f"指标存在：{name}", name in metrics_text, "未在 /metrics 中找到")
        check("latency 指标不再带 tenant 标签（高基数反模式已修）",
              'latency_seconds{' not in metrics_text
              or 'tenant="' not in metrics_text.split("latency_seconds{")[1][:200]
              if "latency_seconds{" in metrics_text else True,
              "latency 指标仍带 tenant label")

        # ================================================== 3. 成本与 HITL
        section("3. 单次成本与 HITL 端点")

        r = api(page, "/ask/trace-not-exist/cost")
        check("GET /ask/{trace}/cost 可用", r["status"] == 200, f"实际 {r['status']}")
        cd = (r.get("json") or {}).get("data") or {}
        check("未知 trace 返回空账单而非报错",
              cd.get("found") is False and cd.get("total_yuan") == 0.0,
              f"data={cd}")

        r = api(page, "/ask/runs/other-tenant:other-user:s1:1/interrupt")
        check("GET interrupt 对非本人 run 返回 404（防探测）", r["status"] == 404,
              f"实际 {r['status']}")

        r = api(page, "/ask/runs/default:admin:s1:1/cancel", method="POST")
        check("POST cancel 端点存在且归属校验通过", r["status"] == 200,
              f"实际 {r['status']} {r.get('text','')[:100]}")

        # ================================================== 4. 身份与权限
        section("4. 身份切换与权限提示（真实点击）")

        page.evaluate("localStorage.setItem('kquery.roles', '')")
        page.goto(f"{base}/admin/users", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2500)
        body = page_text(page)
        check("非 admin 打开管理页不白屏", len(body.strip()) > 50, f"正文长度 {len(body)}")
        check("非 admin 看到明确的无权限提示",
              any(k in body for k in ("无权限", "403", "未认证", "管理员")),
              f"页面文案：{body[:200]}")

        page.evaluate("localStorage.setItem('kquery.roles', 'admin')")
        page.goto(f"{base}/admin/audit", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(2500)
        body = page_text(page)
        check("恢复 admin 后审计页可用", "审计" in body, f"页面文案：{body[:200]}")
        check("审计页不含 undefined 泄漏", "undefined" not in body, "出现 undefined")

        # 成本端点对非 admin 必须被拒（越权验证）
        page.evaluate("localStorage.setItem('kquery.roles', '')")
        r = api(page, "/admin/costs")
        check("非 admin 访问成本看板被拒（403）", r["status"] == 403, f"实际 {r['status']}")
        page.evaluate("localStorage.setItem('kquery.roles', 'admin')")

        # ================================================== 5. 控制台
        section("5. 运行时健康")
        # 过滤掉"故意制造"的 403/404：它们是上面越权用例的预期结果
        expected = ("403 (Forbidden)", "404 (Not Found)")
        real_errors = [e for e in CONSOLE_ERRORS if not any(k in e for k in expected)]
        check("无非预期 JS 运行时错误", not real_errors,
              f"{len(real_errors)} 条: {real_errors[:2]}")

        browser.close()

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
    parser.add_argument("--base", default="http://127.0.0.1:5173")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    start = time.perf_counter()
    code = run(args.base, args.headed)
    print(f"耗时 {time.perf_counter() - start:.1f}s")
    sys.exit(code)
