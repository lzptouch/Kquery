import pytest

from app.core.rbac import Permission, has_permission
from app.models.knowledge_base import KBRole

_ALL_ACTIONS = [
    Permission.VIEW,
    Permission.UPLOAD,
    Permission.EDIT,
    Permission.DELETE,
    Permission.MANAGE_MEMBERS,
]


@pytest.mark.unit
class TestRBACRolePermissions:
    def test_owner_has_all_permissions(self) -> None:
        for perm in _ALL_ACTIONS:
            assert has_permission(KBRole.OWNER, perm) is True

    def test_viewer_only_view(self) -> None:
        assert has_permission(KBRole.VIEWER, Permission.VIEW) is True
        assert has_permission(KBRole.VIEWER, Permission.UPLOAD) is False
        assert has_permission(KBRole.VIEWER, Permission.EDIT) is False
        assert has_permission(KBRole.VIEWER, Permission.DELETE) is False

    def test_editor_no_delete_no_member_manage(self) -> None:
        assert has_permission(KBRole.EDITOR, Permission.VIEW) is True
        assert has_permission(KBRole.EDITOR, Permission.UPLOAD) is True
        assert has_permission(KBRole.EDITOR, Permission.EDIT) is True
        assert has_permission(KBRole.EDITOR, Permission.DELETE) is False
        assert has_permission(KBRole.EDITOR, Permission.MANAGE_MEMBERS) is False

    def test_admin_has_manage_members(self) -> None:
        assert has_permission(KBRole.ADMIN, Permission.MANAGE_MEMBERS) is True
        assert has_permission(KBRole.ADMIN, Permission.DELETE) is False

    def test_unknown_role(self) -> None:
        assert has_permission("not_a_role", Permission.VIEW) is False
