"""图像描述的结构化解析（`LM-04`）。

## 为什么这件事值得单独测

提示词要求「固定四段」，但那是**请求**不是**保证**。模型实际会：

- 把全角冒号写成半角（`主体: 一只猫`）；
- 给标签加粗或加列表符（`**主体**：...` / `- 主体：...`）；
- 漏段（只答主体和场景）；
- 整段跑题（回一句"这是一张图片"，不按格式）；
- 在外层包一层 ``` 围栏；
- 干脆返回 JSON。

描述原样入库的后果不是"格式难看"，而是**召回不可解释**：同一张图换一次描述
就可能搜不到，用户看到的是"昨天能搜到今天不能"。

所以策略是：**能解析就规范化，解析不了就原文兜底**（宁可格式乱，不能丢信息），
且缺段必须留白——补一个"无"会让"确实没有图中文字"和"模型漏答"变成同一件事。
"""
import pytest

from app.services.graph.images import (
    CAPTION_MODEL_VERSION,
    MockCaptioner,
    _parse_caption,
    parse_caption_sections,
    render_caption,
)

CANONICAL = "主体：一只橘猫\n属性：橘色虎斑，蜷缩姿态\n场景：居家客厅沙发\n图中文字：无"


class TestCanonicalRoundTrip:
    def test_canonical_text_is_preserved_verbatim(self):
        """规范输入 → 规范化输出必须**逐字一致**（否则等于无故改写索引内容）。"""
        assert _parse_caption(CANONICAL) == CANONICAL

    def test_sections_parse_into_four_keys(self):
        sections = parse_caption_sections(CANONICAL)
        assert sections == {
            "subject": "一只橘猫",
            "attributes": "橘色虎斑，蜷缩姿态",
            "scene": "居家客厅沙发",
            "text_in_image": "无",
        }

    def test_mock_captioner_output_is_parseable(self, tmp_path):
        """测试替身也必须符合契约，否则集成用例测的是另一套格式。"""
        path = tmp_path / "demo.png"
        sections = parse_caption_sections(MockCaptioner().caption(path))
        assert all(sections.values())
        assert CAPTION_MODEL_VERSION  # 模型版本随条目落库，重建判据依赖它


class TestVariantTolerance:
    @pytest.mark.parametrize(
        "label_line",
        [
            "主体：一只橘猫",
            "主体: 一只橘猫",
            "**主体**：一只橘猫",
            "**主体**: 一只橘猫",
            "- 主体：一只橘猫",
            "* 主体：一只橘猫",
            "  主体 ： 一只橘猫",
        ],
    )
    def test_label_variants_are_normalised(self, label_line):
        raw = label_line + "\n场景：客厅"
        assert parse_caption_sections(raw)["subject"] == "一只橘猫"
        assert parse_caption_sections(raw)["scene"] == "客厅"

    def test_fenced_output_is_unwrapped(self):
        raw = "```text\n" + CANONICAL + "\n```"
        assert _parse_caption(raw) == CANONICAL

    def test_json_output_is_accepted(self):
        raw = '{"subject": "一只猫", "attributes": "白色", "scene": "窗边", "text_in_image": "无"}'
        assert parse_caption_sections(raw) == {
            "subject": "一只猫",
            "attributes": "白色",
            "scene": "窗边",
            "text_in_image": "无",
        }
        assert _parse_caption(raw) == "主体：一只猫\n属性：白色\n场景：窗边\n图中文字：无"

    def test_chinese_keys_in_json_are_accepted(self):
        raw = '{"主体": "一只猫", "场景": "窗边"}'
        sections = parse_caption_sections(raw)
        assert sections["subject"] == "一只猫"
        assert sections["scene"] == "窗边"
        assert sections["attributes"] == ""

    def test_multiline_section_body_is_merged(self):
        raw = "主体：一只猫，\n趴在窗台上\n场景：卧室"
        sections = parse_caption_sections(raw)
        assert sections["subject"] == "一只猫， 趴在窗台上"
        assert sections["scene"] == "卧室"


class TestMissingSectionsStayEmpty:
    def test_missing_sections_are_not_invented(self):
        """漏段 → 留空，不补"无"（补了就把"漏答"和"确实没有"混成一件事）。"""
        raw = "主体：一只狗\n场景：公园"
        sections = parse_caption_sections(raw)

        assert sections["subject"] == "一只狗"
        assert sections["scene"] == "公园"
        assert sections["attributes"] == ""
        assert sections["text_in_image"] == ""

    def test_rendered_text_omits_empty_sections(self):
        rendered = _parse_caption("主体：一只狗\n场景：公园")
        assert rendered == "主体：一只狗\n场景：公园"
        assert "属性" not in rendered


class TestFallback:
    def test_unstructured_output_keeps_raw_text(self):
        """整段跑题（一行都不像标签）→ 原文兜底，绝不丢信息。"""
        raw = "这是一张图片，看起来像猫。"
        assert _parse_caption(raw) == raw
        assert parse_caption_sections(raw) == {
            "subject": "",
            "attributes": "",
            "scene": "",
            "text_in_image": "",
        }

    def test_single_section_hit_does_not_trigger_rewrite(self):
        """只命中 1 段 → 不重排（重排会把其余内容整段丢掉）。"""
        raw = "主体：一只猫\n其余是自由描述，没有分段。"
        assert _parse_caption(raw) == raw

    def test_empty_input_is_empty(self):
        assert _parse_caption("   ") == ""
        assert parse_caption_sections("") == {
            "subject": "",
            "attributes": "",
            "scene": "",
            "text_in_image": "",
        }

    def test_render_of_empty_sections_is_empty(self):
        assert render_caption({}) == ""
