"""K8s 清单与告警规则的**结构守卫**（批次 5 / 批次 6）。

这些 YAML 不参与运行时，**坏了也不会有人立刻发现**：
Ingress 少一行 SSE 注解 → 流式变成一次性返回（用户看到"卡住"）；
告警规则引用了不存在的指标 → 规则永远不触发（监控一片绿）；
探针用错端点 → 依赖抖动时 Pod 被批量重启。

所以这里把"必须成立"写成断言。
"""
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[2]
K8S_DIR = REPO_ROOT / "deploy" / "k8s" / "base"
RULES_FILE = REPO_ROOT / "deploy" / "prometheus-rules.yaml"


def _load(path: Path) -> list[dict]:
    return [d for d in yaml.safe_load_all(path.read_text(encoding="utf-8")) if d]


def _find(docs: list[dict], kind: str, name: str) -> dict:
    for d in docs:
        if d.get("kind") == kind and d.get("metadata", {}).get("name") == name:
            return d
    raise AssertionError(f"清单里找不到 {kind}/{name}")


class TestIngress:
    def test_sse_annotations_present(self):
        """SSE 三件套：关缓冲 + 读超时 + 写超时。少一个流式就会坏。"""
        docs = _load(K8S_DIR / "ingress.yaml")
        ann = _find(docs, "Ingress", "kquery")["metadata"]["annotations"]
        assert ann["nginx.ingress.kubernetes.io/proxy-buffering"] == "off"
        assert int(ann["nginx.ingress.kubernetes.io/proxy-read-timeout"]) >= 300
        assert int(ann["nginx.ingress.kubernetes.io/proxy-send-timeout"]) >= 300

    def test_routes_to_frontend(self):
        docs = _load(K8S_DIR / "ingress.yaml")
        spec = _find(docs, "Ingress", "kquery")["spec"]
        assert spec["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"] == "kquery-frontend"


class TestBackendDeployment:
    def _container(self) -> dict:
        docs = _load(K8S_DIR / "backend-deployment.yaml")
        dep = _find(docs, "Deployment", "kquery-backend")
        return dep["spec"]["template"]["spec"]["containers"][0]

    def test_probes_use_the_right_endpoints(self):
        """liveness **绝不用 /healthz**（它查 9 个依赖，依赖抖动会误杀 Pod）。"""
        c = self._container()
        assert c["startupProbe"]["httpGet"]["path"] == "/livez"
        assert c["livenessProbe"]["httpGet"]["path"] == "/livez"
        assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"

    def test_graceful_exit_for_sse(self):
        """SSE 长连接需要排空时间：preStop + 宽限期。"""
        docs = _load(K8S_DIR / "backend-deployment.yaml")
        dep = _find(docs, "Deployment", "kquery-backend")
        spec = dep["spec"]["template"]["spec"]
        assert spec["terminationGracePeriodSeconds"] >= 60
        c = spec["containers"][0]
        assert "preStop" in c["lifecycle"]

    def test_replicas_and_resources(self):
        c = self._container()
        assert c["resources"]["requests"]["cpu"]
        assert c["resources"]["limits"]["memory"]

    def test_rolling_update_keeps_capacity(self):
        docs = _load(K8S_DIR / "backend-deployment.yaml")
        dep = _find(docs, "Deployment", "kquery-backend")
        ru = dep["spec"]["strategy"]["rollingUpdate"]
        assert ru["maxUnavailable"] == 0, "滚动更新不该同时停掉所有实例"


class TestFrontendDeployment:
    def test_exists(self):
        docs = _load(K8S_DIR / "frontend-deployment.yaml")
        dep = _find(docs, "Deployment", "kquery-frontend")
        # 必须与镜像里 nginx 的 listen 端口一致（deploy/docker/frontend.nginx.conf），
        # 否则探针全挂——这个断言就是防端口再被抄错。
        assert dep["spec"]["template"]["spec"]["containers"][0]["ports"][0]["containerPort"] == 8080


class TestScalingAndSafety:
    def test_hpa_uses_existing_custom_metric(self):
        docs = _load(K8S_DIR / "hpa.yaml")
        hpa = _find(docs, "HorizontalPodAutoscaler", "kquery-backend")
        metrics = hpa["spec"]["metrics"]
        names = [m.get("external", {}).get("metric", {}).get("name") for m in metrics]
        assert "kquery_inflight_requests" in names
        assert hpa["spec"]["maxReplicas"] >= 2

    def test_pdb_keeps_one_instance(self):
        docs = _load(K8S_DIR / "hpa.yaml")
        pdb = _find(docs, "PodDisruptionBudget", "kquery-backend")
        assert pdb["spec"]["minAvailable"] >= 1

    def test_service_monitor_scrapes_metrics(self):
        docs = _load(K8S_DIR / "hpa.yaml")
        sm = _find(docs, "ServiceMonitor", "kquery-backend")
        assert sm["spec"]["endpoints"][0]["path"] == "/metrics"

    def test_secret_has_no_real_credentials(self):
        """仓库里不允许出现真实密钥：只允许占位符。"""
        docs = _load(K8S_DIR / "secret.example.yaml")
        secret = _find(docs, "Secret", "kquery-secrets")
        for key, value in (secret.get("stringData") or {}).items():
            assert "REPLACE_ME" in str(value), f"{key} 疑似真实凭据入库"

    def test_secret_template_is_not_applied(self):
        """密钥模板**不能**进 kustomization 的 resources。

        清单里的 Secret 会在每次 `kubectl apply -k` 时把占位值写回，
        覆盖掉运维注入的真实密钥——这类事故只在真部署时才暴露。
        """
        kustomization = (K8S_DIR / "kustomization.yaml").read_text(encoding="utf-8")
        resources_block = kustomization.split("resources:", 1)[1].split("#", 1)[0]
        assert "secret.example.yaml" not in resources_block
        assert "kind: Secret" not in (K8S_DIR / "configmap.yaml").read_text(encoding="utf-8")


class TestAlertRules:
    def _rules(self) -> list[dict]:
        docs = yaml.safe_load(RULES_FILE.read_text(encoding="utf-8"))
        rules: list[dict] = []
        for group in docs.get("groups", []):
            rules.extend(group.get("rules", []))
        return rules

    def test_rules_exist(self):
        assert len(self._rules()) >= 5

    def test_every_rule_has_summary_and_description(self):
        for rule in self._rules():
            ann = rule.get("annotations") or {}
            assert ann.get("summary"), f"{rule['alert']} 缺 summary"
            assert ann.get("description"), f"{rule['alert']} 缺 description"

    def test_rules_reference_existing_metrics(self):
        """**告警引用不存在的指标 = 规则永不触发**（监控一片绿，最危险的失败）。"""
        from app.core.metrics import PromMetrics

        prom = PromMetrics()
        exported = set()
        for attr_name in dir(prom):
            if attr_name.startswith("_"):
                continue
            obj = getattr(prom, attr_name)
            base = getattr(obj, "_name", None)
            if not base:
                continue
            kind = getattr(obj, "_type", "")
            if kind == "counter":
                # prometheus_client 会把 Counter 的 `_total` 后缀剥掉再渲染
                exported.add(f"{base}_total")
            elif kind == "histogram":
                exported.update({base, f"{base}_bucket", f"{base}_count", f"{base}_sum"})
            else:
                exported.add(base)

        referenced = set()
        for rule in self._rules():
            expr = str(rule.get("expr") or "")
            referenced.update(re.findall(r"\b(kquery_[a-z_]+|rag_[a-z_]+)\b", expr))
        # 桶形式（xxx_bucket）去掉后缀再比对
        referenced = {r.replace("_bucket", "") for r in referenced}
        missing = sorted(r for r in referenced if r not in exported)
        assert not missing, f"告警规则引用了不存在的指标：{missing}"

    def test_no_high_cardinality_labels(self):
        """租户/用户不能进告警 label（高基数）。"""
        for rule in self._rules():
            labels = rule.get("labels") or {}
            assert "tenant" not in labels
            assert "user" not in labels
