"""M5：VLM 直答（把原图喂给多模态模型）与降级。"""
import tempfile
from pathlib import Path


class TestIsVisualQuestion:
    def test_visual_keyword_with_image_evidence(self):
        from app.services.multimodal import is_visual_question

        ev = [{"modality": "image", "uri": "doc#page-2.png", "content": "流程图"}]
        assert is_visual_question("这张流程图第几步是什么？", ev) is True

    def test_visual_keyword_without_image(self):
        """有关键词但没有图 → 不走直答（无从直答）。"""
        from app.services.multimodal import is_visual_question

        assert is_visual_question("这张图在哪？", [{"modality": "text"}]) is False

    def test_text_question_with_image_evidence(self):
        """有图但问题在问文字 → 走 caption 文本路（省一次多模态调用）。"""
        from app.services.multimodal import is_visual_question

        ev = [{"modality": "image", "uri": "doc#page-2.png"}]
        assert is_visual_question("这个文档讲了什么？", ev) is False

    def test_empty_question(self):
        from app.services.multimodal import is_visual_question

        assert is_visual_question("", []) is False


class TestImagePathResolution:
    def test_resolves_existing_asset(self):
        from app.services.multimodal import image_path_of

        assets = Path(tempfile.mkdtemp())
        (assets / "doc#page-1.png").write_bytes(b"x")
        path = image_path_of([{"uri": "doc#page-1.png"}], assets)
        assert path is not None and path.exists()

    def test_rejects_path_traversal(self):
        """资源名来自证据（外部数据）→ 目录穿越必须挡住。"""
        from app.services.multimodal import image_path_of

        assets = Path(tempfile.mkdtemp())
        assert image_path_of([{"uri": "../../etc/passwd"}], assets) is None

    def test_missing_file_returns_none(self):
        from app.services.multimodal import image_path_of

        assets = Path(tempfile.mkdtemp())
        assert image_path_of([{"uri": "nope.png"}], assets) is None

    def test_no_assets_dir_returns_none(self):
        from app.services.multimodal import image_path_of

        assert image_path_of([{"uri": "x.png"}], None) is None


class TestVlmAnswererFactory:
    def test_disabled_by_default(self):
        from app.core.config import Settings
        from app.services.multimodal import build_vlm_answerer

        assert build_vlm_answerer(Settings(), "") is None

    def test_enabled_without_key_returns_none(self):
        """开了开关但没 Key → 返回 None（回落 caption），不能抛异常把服务搞挂。"""
        from app.services.multimodal import build_vlm_answerer

        class S:
            vlm_answer_enabled = True
            dashscope_api_key = ""
            vlm_model = "qwen-vl-max"

        assert build_vlm_answerer(S(), "") is None

    def test_enabled_with_key_builds_client(self):
        from app.services.multimodal import QwenVlAnswerer, build_vlm_answerer

        class S:
            vlm_answer_enabled = True
            dashscope_api_key = "k"
            vlm_model = "qwen-vl-max"

        assert isinstance(build_vlm_answerer(S(), ""), QwenVlAnswerer)


class TestAnswerNodeDirectPath:
    def test_direct_answer_used_and_failure_falls_back(self):
        """直答成功 → 用它；直答抛错 → 回落文本路（答案不能空）。"""
        from app.services.agent.graph import build_agent_graph
        from app.services.multimodal import MockVlmAnswerer
        from app.services.agent.tools import ToolRegistry
        from app.services.llm import MockLLM

        from tests.integration.test_stage6_agent import agent_prompt_builder

        assets = Path(tempfile.mkdtemp())
        (assets / "doc#page-1.png").write_bytes(b"x")

        class BoomVlm:
            def answer(self, image_path, question, context=""):
                raise RuntimeError("VLM 挂了")

        class OkVlm(MockVlmAnswerer):
            """用生产同款的 mock（`MockVlmAnswerer`）→ 确认它真的能用。"""

            def answer(self, image_path, question, context=""):
                return "这是流程图的第 3 步：审核。"

        def _run(vlm):
            registry = ToolRegistry(timeout_s=2)
            graph = build_agent_graph(
                registry,
                planner=_Planner(),
                reflector=_Reflector(),
                answer_llm=MockLLM(),
                prompt_builder=agent_prompt_builder,
                vlm_answerer=vlm,
            )
            return graph.invoke(
                {
                    "question": "这张流程图第几步是什么？",
                    "tenant_id": "t1", "user_id": "u1", "turn": 0, "turns": [],
                    "evidence": [
                        {"chunk_id": "c#1", "content": "流程图", "modality": "image",
                         "uri": "doc#page-1.png", "doc_id": "doc", "source": "图"}
                    ],
                    "plan": [], "iteration": 0, "last_evidence_count": 1,
                    "fused": False, "skip_retrieval": False, "refusal": False,
                    "approved_tools": [],
                },
                config={"configurable": {"thread_id": "t1", "assets_dir": assets}},
            )

        class _Planner:
            def plan(self, question, replan_hint=""):
                return []

        class _Reflector:
            def reflect(self, question, evidence, remaining_steps):
                return {"score": 0.9, "next_action": "answer", "reason": "ok"}

        ok = _run(OkVlm())
        assert ok.get("answer_source") == "vlm"
        assert "第 3 步" in ok.get("final_answer", "")

        fallback = _run(BoomVlm())
        assert fallback.get("answer_source") != "vlm"
        assert fallback.get("final_answer")
