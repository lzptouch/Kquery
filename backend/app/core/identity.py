"""固定身份（单租户单用户模式）。

## 背景：认证与授权已下线（2026-11-12）

本项目原有的**认证**（注册 / 登录 / 口令 / JWT / 静态凭证 / 用户目录）与
**授权**（文档级 ACL / 知识库授权名单 / 角色门禁 / 权限矩阵）整套能力已删除。
删除的原因不是"实现得不好"，而是它们与本项目要证明的能力（检索质量、Agent
编排、可观测、成本）正交，却带来了 82 个端点中 79 个的依赖、12 个配置项、
5 张前端管理页和 23 条已文档化的能力项——体量远大于它们自身的价值。

**保留本模块的原因**：数据隔离仍然需要"我是谁"。文档归属、会话归属、缓存键
维度、限流分桶、图谱与记忆分区都按 `tenant_id` / `user_id` 划分，这些是**数据
模型的一部分**，不是权限功能。删掉它们等于把多租户数据模型也拆了。

因此身份退化为一个**固定值**：

- `tenant_id` = `default`（对应原 `default_tenant_id` 配置的兜底值）
- `user_id` = `local`

## 为什么放在 core/

`core/` 是最底层，不依赖 `services/`（见 `tests/unit/test_import_boundaries.py`）。
身份是所有上层都要用的东西，放在 `core/` 最合适；原来的
`services/security/auth.py` 随认证能力一起删除。

## 迁移提示

原 `Principal` 上的 `roles` / `groups` / `enabled` / `identities` / `is_admin`
**全部删除**：`roles`/`groups` 只服务于角色门禁与 ACL 身份表达式（两者都已下线），
`enabled` 在原实现里就是死字段（无任何读取点）。
"""
from dataclasses import dataclass

#: 固定租户标识。与原 `Settings.default_tenant_id` 的默认值一致。
DEFAULT_TENANT_ID = "default"

#: 固定用户标识。单用户模式下所有请求都是这一个主体。
DEFAULT_USER_ID = "local"


@dataclass(frozen=True)
class Principal:
    """请求身份（固定值）。

    `frozen=True` 是刻意的：身份是常量，不该被任何节点就地改写——原实现里
    `roles` 被 `_reconcile_with_directory` 就地覆写，正是为了绕开"令牌里的角色
    可能是旧的"这个难题；身份固定后该难题不复存在。
    """

    tenant_id: str = DEFAULT_TENANT_ID
    user_id: str = DEFAULT_USER_ID
