"""OCR「一律云服务 API」的架构约束守卫（F11）。

这些用例不是测功能，而是**把架构约束钉死**：将来有人想加本地 OCR
（ONNX / PaddleOCR / tesseract / 权重文件），这里会直接红。

为什么值得单独写守卫而不是只在文档里说一句：

- "本地 OCR" 的诱惑来自"离线也能跑"、"不花钱"这类真实诉求；
- 一旦代码里出现 `import onnxruntime`，运维就会问"权重从哪来"，
  而运行时从 HuggingFace 下载在���网必然失败——最后变成"部署后 OCR 静默不可用"；
- 文档会被忘记，测试不会。
"""
import re
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.envelope import ApiError
from app.services.parsers.ocr import ALLOWED_OCR_PROVIDERS, MockOcrEngine, QwenVlOcr, ZhipuOcrEngine, build_ocr_engine

BACKEND_ROOT = Path(__file__).resolve().parents[2] / "backend"
APP_DIR = BACKEND_ROOT / "app"
PYPROJECT = BACKEND_ROOT / "pyproject.toml"

#: 出现即视为"引入了本地模型路径"的依赖
FORBIDDEN_DEPS = (
    "onnxruntime", "onnx", "torch", "torchvision", "transformers", "tensorflow",
    "paddleocr", "paddlepaddle", "rapidocr", "rapidocr-onnxruntime", "easyocr",
    "pytesseract", "tesserocr", "sentence-transformers", "sentencepiece",
    "funasr", "modelscope", "timm", "ultralytics", "opencv-python",
)

#: 源码里出现即视为"试图加载本地模型"的标识。
#: 注意只匹配**真正的加载动作**（import / 调用 / 权重文件后缀），
#: 不能匹配错误提示与注释里的词——那会把"明确拒绝本地模型"的说明
#: 自己也判成违规。
FORBIDDEN_SOURCE_PATTERNS = (
    r"^\s*import\s+onnxruntime\b",
    r"^\s*from\s+onnxruntime\b",
    r"^\s*import\s+torch\b",
    r"^\s*from\s+torch\b",
    r"^\s*from\s+transformers\b",
    r"^\s*import\s+pytesseract\b",
    r"^\s*from\s+paddleocr\b",
    r"^\s*import\s+easyocr\b",
    r"\bpytesseract\.image_to_string\b",
    r"\beasyocr\.Reader\(",
    r"\bpaddleocr\.ocr\(",
    r"\bfrom_pretrained\(",
    r"InfiniFlow/deepdoc",
    r"[\"'][^\"']*\.onnx[\"']",
    r"[\"'][^\"']*\.safetensors[\"']",
    r"[\"'][^\"']*\.pt[\"']",
)


class TestOcrProviderOnly:
    def test_cloud_provider_builds_cloud_engine(self):
        engine = build_ocr_engine("dashscope", "key")
        assert isinstance(engine, QwenVlOcr)

    def test_mock_provider_builds_mock_engine(self):
        engine = build_ocr_engine("mock", "key")
        assert isinstance(engine, MockOcrEngine)

    @pytest.mark.parametrize(
        "provider", ["local", "onnx", "self-hosted", "paddleocr", "deepdoc", "offline"]
    )
    def test_local_model_providers_rejected(self, provider):
        """本地/自部署 provider 必须**报错**，不能静默降级成 Mock。

        静默降级会让"配错了"看起来像"跑通了"，线上表现为
        "图片入库了但没有文字"——极难排查。
        """
        with pytest.raises(ApiError) as exc:
            build_ocr_engine(provider, "key")
        assert "云服务 API" in exc.value.message
        assert "本地" in exc.value.message

    def test_allowed_providers_are_cloud_only(self):
        assert set(ALLOWED_OCR_PROVIDERS) == {"dashscope", "zhipu", "mock"}
        assert "local" not in ALLOWED_OCR_PROVIDERS

    def test_zhipu_provider_builds_zhipu_engine(self):
        engine = build_ocr_engine("zhipu", "key", base_url="https://open.bigmodel.cn/api/paas/v4")
        assert isinstance(engine, ZhipuOcrEngine)


class TestOcrConfig:
    def _settings(self, **kwargs) -> Settings:
        base = dict(data_dir="/tmp/kquery-test", model_provider="mock", auth_enabled=False)
        base.update(kwargs)
        return Settings(_env_file=None, **base)

    def test_default_resolves_to_mock_without_key(self):
        # ocr_provider 显式给 auto：会话级环境钉了 mock（见 conftest），
        # 不传的话测的就是 env 而非 auto 逻辑本身
        assert self._settings(ocr_provider="auto").resolved_ocr_provider == "mock"

    def test_can_force_cloud(self):
        s = self._settings(ocr_provider="dashscope", dashscope_api_key="k")
        assert s.resolved_ocr_provider == "dashscope"

    def test_auto_follows_model_provider(self):
        s = self._settings(ocr_provider="auto", model_provider="dashscope", dashscope_api_key="k")
        assert s.resolved_ocr_provider == "dashscope"

    @pytest.mark.parametrize("provider", ["local", "onnx", "paddleocr", "offline"])
    def test_startup_validation_rejects_local_provider(self, provider):
        s = self._settings(ocr_provider=provider)
        fields = [i["field"] for i in s.validate_startup()]
        assert "ocr_provider" in fields, f"{provider} 应在启动期被拦下"

    def test_cloud_provider_without_key_is_rejected(self):
        s = self._settings(ocr_provider="dashscope", dashscope_api_key="")
        fields = [i["field"] for i in s.validate_startup()]
        assert "dashscope_api_key" in fields

    def test_zhipu_provider_without_key_is_rejected(self):
        s = self._settings(ocr_provider="zhipu", zhipu_api_key="")
        fields = [i["field"] for i in s.validate_startup()]
        assert "zhipu_api_key" in fields


class TestNoLocalModelDependency:
    def test_pyproject_has_no_local_ml_runtime(self):
        text = PYPROJECT.read_text(encoding="utf-8").lower()
        # 只看依赖声明段（避免命中文档/注释里的词）
        declared = "\n".join(
            line for line in text.splitlines()
            if line.strip().startswith('"') and "=" in line
        )
        for dep in FORBIDDEN_DEPS:
            assert dep not in declared, f"依赖里出现了本地模型运行时 {dep}：OCR 只走云服务 API"

    def test_source_has_no_local_model_loading(self):
        offenders: list[str] = []
        for path in APP_DIR.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in FORBIDDEN_SOURCE_PATTERNS:
                if re.search(pattern, text):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}: {pattern}")
        assert not offenders, "源码里出现了本地模型加载痕迹：\n" + "\n".join(offenders)
