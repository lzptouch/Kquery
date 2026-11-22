"""`/metrics` 的访问策略（`X-18` / `D-2`）与共享状态单实例（装配缺陷）。

## 为什么要有这组用例

`/metrics` 的 fail-closed 分支原先读 `settings.auth_enabled`——该配置项已随认证
下线删除，`getattr(..., False)` 只能拿到恒假的默认值。结果是**这条兜底从未生效**：
生产漏配 `METRICS_TOKEN` 时指标面依然匿名可抓。

这组用例把「什么档位允许匿名抓取」钉成契约：

| 档位 | `METRICS_TOKEN` | 期望 |
|---|---|---|
| `metrics_public=true` | 任意 | 200（显式开放，责任在配置方） |
| `app_env=prod` | 空 | **401**（fail-closed） |
| `app_env=prod` | 有 | 带对 200 / 带错 401 / 不带 401 |
| `app_env=dev` | 空 | 200（本地调试友好） |

## 为什么没有「认证已关闭 → 一律放行」这条

那是缺陷本身。指标里含路径、状态码、工具名与时延分布，属于运营数据；
「应用层没有身份体系」不等于「运营数据可以公开」——两者的责任主体不同：
前者交给部署层（反代/内网），后者必须由应用自己拦。
"""
import pytest

from app.core.envelope import ErrCode


@pytest.fixture()
def metrics_settings(client):
    """拿到当前 app 的 settings，并在用例结束后恢复三个相关字段。"""
    s = client.app.state.settings
    saved = (s.metrics_public, s.metrics_token, s.app_env)
    yield s
    s.metrics_public, s.metrics_token, s.app_env = saved


class TestMetricsAccess:
    def test_prod_without_token_is_rejected(self, client, metrics_settings):
        """生产档漏配令牌 → 401（此前恒 200：修的就是这个）。"""
        metrics_settings.metrics_public = False
        metrics_settings.metrics_token = ""
        metrics_settings.app_env = "prod"

        resp = client.get("/metrics")

        assert resp.status_code == 401
        body = resp.json()
        assert body["status"] == "error"
        assert body["error"]["code"] == int(ErrCode.UNAUTHORIZED)
        assert "METRICS_TOKEN" in body["error"]["message"]

    @pytest.mark.parametrize("env", ["production", "staging", "PROD"])
    def test_non_dev_envs_are_rejected_case_insensitively(self, client, metrics_settings, env):
        metrics_settings.metrics_public = False
        metrics_settings.metrics_token = ""
        metrics_settings.app_env = env

        assert client.get("/metrics").status_code == 401

    @pytest.mark.parametrize("env", ["dev", "development", "local", "test"])
    def test_dev_envs_stay_open_for_local_debugging(self, client, metrics_settings, env):
        """本地调试友好：开发档不配令牌也能抓（默认行为不变）。"""
        metrics_settings.metrics_public = False
        metrics_settings.metrics_token = ""
        metrics_settings.app_env = env

        resp = client.get("/metrics")

        assert resp.status_code == 200
        assert "kquery_" in resp.text

    def test_token_required_when_configured(self, client, metrics_settings):
        metrics_settings.metrics_public = False
        metrics_settings.metrics_token = "s3cret"
        metrics_settings.app_env = "dev"

        assert client.get("/metrics").status_code == 401
        assert client.get("/metrics", headers={"X-Metrics-Token": "wrong"}).status_code == 401
        ok = client.get("/metrics", headers={"X-Metrics-Token": "s3cret"})
        assert ok.status_code == 200

    def test_metrics_public_overrides_everything(self, client, metrics_settings):
        """显式开放优先：这是配置方的决策，应用不再二次拦截。"""
        metrics_settings.metrics_public = True
        metrics_settings.metrics_token = "s3cret"
        metrics_settings.app_env = "prod"

        assert client.get("/metrics").status_code == 200
