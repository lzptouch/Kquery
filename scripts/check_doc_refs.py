"""文档里引用的**文件路径**是否真实存在。

文档写 `backend/app/services/pipeline.py:123` 时，如果那个文件已经没了 /
改名了，读者按图索骥只会扑空 —— 而且没有任何工具会报错。
这个脚本把这类"指向不存在文件"的引用抓出来。
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 匹配 `路径:行号` 或 `路径:行号-行号` 形式的引用
REF = re.compile(
    r"`((?:backend|frontend|tests|scripts|deploy|doc|\.github)/[A-Za-z0-9_./\-]+?"
    r"(?:\.(?:py|ts|tsx|yml|yaml|md|json|toml|sh)|/))"
    r"(?::\d+(?:-\d+)?)?`"
)
#: `backend/Dockerfile` 这类无扩展名的路径单独处理
DOCKERFILE = re.compile(r"`(backend/Dockerfile|frontend/Dockerfile)`")

#: **设计稿里声明为"待建 / 未实现"的路径**（不是断链）。
#: 这些路径出现在 gap 台账与设计文档的"未实现项"表格里，
#: 描述的是**还没做的事**（例如"要建 tests/security/"）——
#: 它们本来就不该存在，否则就变成"已实现"了。
#: 判定依据：文档里同时出现了这些路径与"未实现 / 无 / 要建 / 建议 / 验收"等措辞。
PROPOSED_PATHS = {
    "tests/security/",
    "tests/contract/",
    "tests/data/sample/",
    "doc/series/",
    "scripts/gen_env_example.py",
    "scripts/eval_retrieval.py",
    "tests/unit/test_error_kind_metrics.py",
    "tests/unit/test_write_endpoints_rate_limited.py",
    "tests/integration/test_prompt_injection.py",
    "backend/app/services/security/audit.py",
    "backend/app/services/security/sensitive.py",
    # 同样属于"设计稿里的待建项"
    "doc/plan",  # **已撤销**（2026-11-12）：计划内容并入 doc/design/，文档在「原 doc/plan/」处引用它
    "backend/app/bootstrap.py",
    "tests/unit/test_import_boundaries.py",
    "backend/app/services/security/platform_quota.py",
    "backend/app/services/security/quota.py",
    # 历史计划文档里描述的**未来目录结构**（计划稿，不是现状声明）
    "doc/analysis/_archive",
    "doc/reference",
    "doc/screenshots",
    "scripts/doc_check.py",
    # doc-qa 合并计划里提到的**对方仓库的路径**（不是本仓库的文件）
    "backend/app/router/user_rt.py",
    "backend/docker-compose.yml",
    "frontend/tests/upload-wizard.test.tsx",
    "tests/e2e_ui/test_ui_quickask.py",
    "tests/integration/test_redis_cache.py",
    "scripts/eval/metrics.py",
    "frontend/src/features/admin/security/KbGrantPanel.tsx",
    "tests/fixtures/",
    "tests/quality/",
    # 计划稿里写的**旧路径**（当时 data/eval 还在 backend/ 下）
    "backend/data",
    "backend/data/eval",
    # **认证与授权已删除**的路径（2026-11-12 下线）。文档在「已下线留档」与
    # 「删除边界」处会引用它们，说明这些机制曾经存在——与上面"待建"同理，
    # 不该被当成断链。参考本文件已有的 audit.py / sensitive.py  precedent。
    "backend/app/api/auth.py",
    "backend/app/api/security.py",
    "backend/app/api/kb.py",
    "backend/app/services/security/auth.py",
    "backend/app/services/security/password.py",
    "backend/app/services/security/acl.py",
    "backend/app/services/security/kb.py",
    "frontend/src/features/auth/AuthContext.tsx",
    "frontend/src/features/auth/LoginPage.tsx",
    "frontend/src/features/auth/RegisterPage.tsx",
    "frontend/src/features/auth/guards.tsx",
    "frontend/src/features/auth/index.ts",
    "frontend/src/features/admin/security/AclPage.tsx",
    "frontend/src/features/admin/security/KbPage.tsx",
    "frontend/src/features/admin/security/PermissionMatrix.tsx",
    "frontend/src/features/admin/security/UsersPage.tsx",
    "frontend/src/features/admin/security/index.ts",
    "frontend/src/features/admin/security",
    "backend/app/services/security",
    "frontend/src/features/auth",
    "frontend/src/features/auth/AppLayout.tsx",
    "frontend/src/lib/auth.ts",
    "tests/integration/test_auth_flow.py",
    "tests/integration/test_kb_grants.py",
    "tests/integration/test_kb_migration.py",
    "tests/integration/test_stage9_security.py",
    "tests/unit/test_acl_pushdown.py",
    "tests/unit/test_kb_filters.py",
    "tests/unit/test_kb_service.py",
    "tests/e2e_ui/test_ui_kb.py",
    "frontend/tests/document-uploader-kb.test.tsx",
    "frontend/tests/kb-grant-panel.test.tsx",
    "frontend/tests/auth-session.test.tsx",
    # doc/design/06-工程化与质量闭环设计.md 里声明的**待建路径**
    # （ST- 上下文分区器 / EV- 评测判定 / LM- 模型调用面 / OP- 运维发布）
    "backend/app/services/context/",
    "doc/plan",  # **已撤销**（2026-11-12）：计划内容并入 doc/design/，文档在「原 doc/plan/」处引用它
    "backend/app/bootstrap.py",
    "backend/app/assembly/",

    "backend/app/services/evaluation/rubric.py",
    "backend/app/services/evaluation/judge.py",
    "backend/app/services/evaluation/judge_prompt.py",
    "backend/app/services/evaluation/citations.py",
    "backend/app/services/evaluation/significance.py",
    "backend/app/services/multimodal/schema.py",
    "backend/app/services/llm_router.py",
    "backend/app/services/canary.py",
    "scripts/canary_promote.py",
    "scripts/backup.py",
    "scripts/restore_drill.py",
    "scripts/eval/cost_quality.py",
    "doc/runbook/",
    "doc/testing/",  # 测试计划目录（01 篇 §2.4 声明为「待建」）
    "tests/data/eval/rubric.v1.yaml",
    "tests/data/eval/answers.jsonl",
    "tests/data/eval/judge_baseline.json",
    "tests/data/eval/judge_report.json",
    "tests/data/eval/judge_cache.json",
    "tests/data/eval/cost_quality.json",
    "tests/unit/test_prompt_injection_guard.py",
    "tests/unit/test_context_partition.py",
    "tests/unit/test_context_history_compaction.py",
    "tests/unit/test_agent_context_assembly.py",
    "tests/unit/test_llm_max_tokens.py",
    "tests/unit/test_eval_rubric.py",
    "tests/unit/test_eval_judge.py",
    "tests/unit/test_eval_significance.py",
    "tests/unit/test_eval_generation_attribution.py",
    "tests/unit/test_eval_citations.py",
    "tests/unit/test_llm_router.py",
    "tests/unit/test_batch_indexing.py",
    "tests/unit/test_multimodal_bulkhead.py",
    "tests/unit/test_caption_schema.py",
    "tests/unit/test_canary_controller.py",
    "tests/integration/test_context_breakdown_api.py",
    "tests/integration/test_eval_judge_api.py",
    "tests/integration/test_eval_gap_report.py",
    "tests/integration/test_llm_routing_degraded.py",
    ".github/workflows/eval-nightly.yml",
    "deploy/k8s/canary/",
}


def collect() -> dict[str, list[str]]:
    missing: dict[str, list[str]] = defaultdict(list)
    for doc in sorted(REPO_ROOT.glob("doc/**/*.md")) + [REPO_ROOT / "README.md"]:
        text = doc.read_text(encoding="utf-8")
        for m in REF.finditer(text):
            path = m.group(1).rstrip("/")
            if (REPO_ROOT / path).exists():
                continue
            # 提案路径按"前缀"匹配（白名单里有的带尾斜杠、有的不带）
            if any(path.startswith(p.rstrip("/")) for p in PROPOSED_PATHS):
                continue
            missing[str(doc.relative_to(REPO_ROOT))].append(path)
        for m in DOCKERFILE.finditer(text):
            path = m.group(1)
            if not (REPO_ROOT / path).exists():
                missing[str(doc.relative_to(REPO_ROOT))].append(path)
    return missing


def main() -> int:
    missing = collect()
    total = sum(len(v) for v in missing.values())
    if not missing:
        print("文档引用的文件路径全部存在 ✓")
        return 0
    for doc in sorted(missing):
        uniq = sorted(set(missing[doc]))
        print(f"\n{doc}（{len(uniq)} 处）")
        for path in uniq:
            print(f"  不存在：{path}")
    print(f"\n合计 {total} 处指向不存在文件的引用")
    return 1


if __name__ == "__main__":
    sys.exit(main())
