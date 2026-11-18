"""模型路由在请求链路上的可观察行为（`LM-01`）。

## 两条验收

① **开关关闭时行为与开启前完全一致**（`model_used` 为空、`routing_degraded` 为 false）；
② 开关打开且主模型失败（可降级）时 → 落到备选，响应里 `routing_degraded=true`
   且 `model_used` 是实际使用的模型。
"""
from __future__ import annotations

from app.core.envelope import ErrCode
from app.services.llm import LLMError


def test_switch_off_keeps_behavior_identical(client):
    """① 开关关闭 → 响应里没有路由痕迹（与开启前逐字一致）。"""
    resp = client.post("/ask", json={"question": "默认开关下的普通问答"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data.get("model_used") in ("", None), data.get("model_used")
    assert data.get("routing_degraded") in (False, None)


def test_degraded_route_is_visible_in_response(client, monkeypatch):
    """② 主模型失败（可降级）→ 落到备选，响应里看得见。"""
    from app.services.llm_router import LLMRouter, ModelPolicy

    class _Primary:
        """主模型：超时（可降级）。"""

        def generate(self, prompt, deadline=None):
            raise LLMError(ErrCode.GENERATION_TIMEOUT, "超时（测试注入）", retryable=True)

        def stream(self, prompt, deadline=None):
            raise LLMError(ErrCode.GENERATION_TIMEOUT, "超时（测试注入）", retryable=True)
            yield  # pragma: no cover

    class _Backup:
        """备选模型：正常返回。"""

        def generate(self, prompt, deadline=None):
            return "备选模型的答案"

        def stream(self, prompt, deadline=None):
            yield "备选模型的答案"

    def factory(model_name: str):
        return _Primary() if model_name == "main-model" else _Backup()

    router = LLMRouter(factory, ModelPolicy("main-model", ("backup-model",)))
    # 直接替换 **实例** 的模型（pipeline 读的是 `self._llm`）：
    # 替换后生成走路由器，`_route_degraded(self._llm)` 也能读到路由元信息。
    pipeline = client.app.state.pipeline
    monkeypatch.setattr(pipeline, "_llm", router)

    resp = client.post("/ask", json={"question": "主模型超时会怎样"})
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data.get("routing_degraded") is True, data
    assert data.get("model_used") == "backup-model", data
