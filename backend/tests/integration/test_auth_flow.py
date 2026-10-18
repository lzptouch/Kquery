"""账号密码登录档（login / register / me）与"管理区收口"的门禁回归。

本文件覆盖三件事：
1. 口令哈希本身的性质（盐、恒定时间、非法输入不抛异常）；
2. 登录/注册端点的行为，重点是**失败信息不可区分**（防账号枚举）；
3. 权限收口——除对话外的功能全部要求 admin，且对话链路不受影响。

注意本文件用**独立 fixture**，不复用 conftest 的 `client`：后者强制
`AUTH_ENABLED=false`（头部透传），而这里要验证的正是"令牌认证 + 登录签发"。
两种档位的代码路径不同，共用 fixture 会让断言失去意义。
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient

ADMIN_PASSWORD = "Admin12345"
USER_PASSWORD = "User12345"


@pytest.fixture()
def password_client():
    """账号密码档的完整应用实例（含 lifespan）。"""
    tmp = tempfile.mkdtemp()
    os.environ.update(
        DATA_DIR=tmp,
        MODEL_PROVIDER="mock",
        VECTOR_STORE="faiss",
        RETRIEVAL_CONFIG_APPLY_DELAY_S="0",
        CRAG_CORRECT_TH="0.3",
        CRAG_INCORRECT_TH="0.1",
        AUTH_ENABLED="true",
        AUTH_PASSWORD_ENABLED="true",
        AUTH_JWT_SECRET="test-secret-key-0123456789abcdef",
        AUTH_BOOTSTRAP_ADMIN_PASSWORD=ADMIN_PASSWORD,
        AUTH_REGISTRATION_ENABLED="true",
        AUTH_SESSION_TTL_S="900",
    )
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    with TestClient(create_app()) as client:
        yield client
    get_settings.cache_clear()
    for key in (
        "DATA_DIR", "MODEL_PROVIDER", "VECTOR_STORE", "RETRIEVAL_CONFIG_APPLY_DELAY_S",
        "CRAG_CORRECT_TH", "CRAG_INCORRECT_TH", "AUTH_ENABLED", "AUTH_PASSWORD_ENABLED",
        "AUTH_JWT_SECRET", "AUTH_BOOTSTRAP_ADMIN_PASSWORD", "AUTH_REGISTRATION_ENABLED",
        "AUTH_SESSION_TTL_S", "VECTOR_STORE",
    ):
        os.environ.pop(key, None)


def _login(client: TestClient, username: str, password: str):
    return client.post("/auth/login", json={"username": username, "password": password})


def _auth(client: TestClient, username: str, password: str) -> dict:
    resp = _login(client, username, password)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['data']['token']}"}


class TestPasswordHashing:
    """口令哈希的性质。与端点无关，故不需要应用实例。"""

    def test_same_password_yields_different_hashes(self):
        from app.services.security.password import hash_password

        assert hash_password(ADMIN_PASSWORD) != hash_password(ADMIN_PASSWORD)

    def test_verify_accepts_correct_and_rejects_wrong(self):
        from app.services.security.password import hash_password, verify_password

        encoded = hash_password(ADMIN_PASSWORD)
        assert verify_password(ADMIN_PASSWORD, encoded) is True
        assert verify_password("Wrong12345", encoded) is False

    def test_malformed_encodings_return_false_without_raising(self):
        """格式非法必须返回 False 而不是抛异常——否则未设口令的账号会变成 500，
        而"未设口令"与"口令错误"必须在登录接口上表现为完全相同的结果。"""
        from app.services.security.password import verify_password

        for bad in (None, "", "garbage", "a$b$c", "pbkdf2_sha256$x$y", "bcrypt$1$2$3"):
            assert verify_password(ADMIN_PASSWORD, bad) is False

    def test_strength_rules(self):
        from app.services.security.password import validate_password_strength

        assert validate_password_strength("1234567") is not None      # 长度不足
        assert validate_password_strength("abcdefgh") is not None      # 缺数字
        assert validate_password_strength("12345678") is not None      # 缺字母
        assert validate_password_strength(ADMIN_PASSWORD) is None

    def test_verify_uses_recorded_iterations_not_current_constant(self):
        """轮数写在记录里，因此调高常量后旧口令仍可校验——否则一次调参会锁死所有用户。"""
        from app.services.security.password import hash_password, needs_rehash, verify_password

        encoded = hash_password(ADMIN_PASSWORD)
        algo, iterations, salt_b64, hash_b64 = encoded.split("$")
        assert algo == "pbkdf2_sha256"
        assert int(iterations) >= 200_000
        # 当前轮数已是最新，无需升密
        assert needs_rehash(encoded) is False
        # 轮数低于常量时应提示升密
        assert needs_rehash(f"{algo}$1000${salt_b64}${hash_b64}") is True
        # 记录自带轮数：即使与常量不同也能正常解析，不会因常量变动而失效
        assert verify_password(ADMIN_PASSWORD, encoded) is True


class TestLogin:
    def test_admin_login_returns_token_and_identity(self, password_client):
        resp = _login(password_client, "admin", ADMIN_PASSWORD)
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert data["user_id"] == "admin"
        assert data["roles"] == ["admin"]
        assert data["token"]

    def test_failures_are_indistinguishable(self, password_client):
        """防账号枚举：账号不存在与口令错误必须同码同文案。

        若两者可区分，攻击者可以据此批量筛出有效账号——这是登录接口
        最需要避免的信息泄漏。
        """
        unknown = _login(password_client, "no-such-user", "whatever1")
        wrong = _login(password_client, "admin", "WrongPass1")
        assert unknown.status_code == wrong.status_code == 401
        assert unknown.json()["error"]["message"] == wrong.json()["error"]["message"]
        assert unknown.json()["error"]["code"] == wrong.json()["error"]["code"]

    def test_disabled_account_fails_like_wrong_password(self, password_client):
        password_client.put(
            "/admin/users/admin",
            json={"roles": ["admin"], "groups": [], "enabled": False},
            headers=_auth(password_client, "admin", ADMIN_PASSWORD),
        )
        disabled = _login(password_client, "admin", ADMIN_PASSWORD)
        wrong = _login(password_client, "admin", "WrongPass1")
        assert disabled.status_code == wrong.status_code == 401
        assert disabled.json()["error"]["message"] == wrong.json()["error"]["message"]

    def test_repeated_failures_are_throttled(self, password_client):
        """暴力破解必须被限流挡下。"""
        codes = [
            _login(password_client, "admin", "WrongPass1").status_code
            for _ in range(12)
        ]
        assert 429 in codes, f"连续失败未触发限流：{codes}"

    def test_me_requires_token(self, password_client):
        assert password_client.get("/auth/me").status_code == 401

    def test_me_returns_current_identity(self, password_client):
        headers = _auth(password_client, "admin", ADMIN_PASSWORD)
        data = password_client.get("/auth/me", headers=headers).json()["data"]
        assert data["user_id"] == "admin"
        assert data["is_admin"] is True

    def test_tampered_token_is_rejected(self, password_client):
        headers = _auth(password_client, "admin", ADMIN_PASSWORD)
        token = headers["Authorization"].split()[1]
        tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
        assert password_client.get(
            "/auth/me", headers={"Authorization": f"Bearer {tampered}"}
        ).status_code == 401

    def test_login_disabled_returns_403_not_silent_success(self, password_client):
        """未开启口令登录时必须显式拒绝。放任不管的话，误配环境下登录会
        "成功"并签出一个匿名令牌，用户随后看到的是一片莫名其妙的 403。"""
        password_client.app.state.settings.auth_password_enabled = False
        assert _login(password_client, "admin", ADMIN_PASSWORD).status_code == 403


class TestRegister:
    def test_new_account_gets_minimal_role(self, password_client):
        resp = password_client.post(
            "/auth/register", json={"username": "newbie", "password": USER_PASSWORD}
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["roles"] == ["sales"]

    def test_privilege_escalation_via_body_is_ignored(self, password_client):
        """注册请求体里的 roles 必须被丢弃——否则任何人都能自助注册成 admin。"""
        resp = password_client.post(
            "/auth/register",
            json={"username": "sneaky", "password": USER_PASSWORD, "roles": ["admin"]},
        )
        assert resp.status_code == 200
        assert resp.json()["data"]["roles"] == ["sales"]
        headers = _auth(password_client, "sneaky", USER_PASSWORD)
        assert password_client.get("/admin/users", headers=headers).status_code == 403

    def test_duplicate_username_rejected(self, password_client):
        body = {"username": "dupe", "password": USER_PASSWORD}
        assert password_client.post("/auth/register", json=body).status_code == 200
        assert password_client.post("/auth/register", json=body).status_code == 409

    def test_weak_password_rejected(self, password_client):
        resp = password_client.post("/auth/register", json={"username": "weak", "password": "123"})
        assert resp.status_code == 400

    def test_registration_switch_off(self, password_client):
        password_client.app.state.settings.auth_registration_enabled = False
        resp = password_client.post(
            "/auth/register", json={"username": "late", "password": USER_PASSWORD}
        )
        assert resp.status_code == 403


class TestAdminAreaGating:
    """权限收口：除对话外全部功能要求 admin。"""

    def _sales_headers(self, client: TestClient) -> dict:
        client.post("/auth/register", json={"username": "plain", "password": USER_PASSWORD})
        return _auth(client, "plain", USER_PASSWORD)

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/knowledge/files"),
            ("GET", "/knowledge/stats"),
            ("GET", "/knowledge/query-analysis"),
            ("GET", "/graph/stats"),
            ("GET", "/graph/entities"),
            ("GET", "/admin/users"),
            ("GET", "/admin/audit"),
            ("GET", "/admin/documents/acl"),
            ("GET", "/admin/quota"),
            ("GET", "/admin/sensitive"),
            ("GET", "/admin/metrics/summary"),
            ("GET", "/admin/retrieval-config"),
        ],
    )
    def test_normal_user_cannot_reach_management_endpoints(self, password_client, method, path):
        headers = self._sales_headers(password_client)
        assert password_client.request(method, path, headers=headers).status_code == 403

    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/sessions"),
            ("GET", "/auth/me"),
        ],
    )
    def test_normal_user_keeps_conversation_endpoints(self, password_client, method, path):
        """对话链路（会话 + 流式问答）必须对普通用户可用——这是产品的核心承诺，
        收口管理区时最容易误伤的就是它。"""
        headers = self._sales_headers(password_client)
        assert password_client.request(method, path, headers=headers).status_code == 200

    def test_normal_user_can_still_ask(self, password_client):
        headers = self._sales_headers(password_client)
        resp = password_client.post("/ask", json={"question": "Kquery 是什么？"}, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] in {"ok", "warn"}

    def test_admin_reaches_everything(self, password_client):
        headers = _auth(password_client, "admin", ADMIN_PASSWORD)
        for path in ("/knowledge/files", "/graph/stats", "/admin/users"):
            assert password_client.get(path, headers=headers).status_code == 200


class TestImmediateRevocation:
    """停用与降权必须**即时**生效，不能等令牌过期。"""

    def test_disabled_user_old_token_stops_working(self, password_client):
        password_client.post("/auth/register", json={"username": "temp", "password": USER_PASSWORD})
        headers = _auth(password_client, "temp", USER_PASSWORD)
        assert password_client.get("/auth/me", headers=headers).status_code == 200

        admin = _auth(password_client, "admin", ADMIN_PASSWORD)
        password_client.put(
            "/admin/users/temp", json={"roles": ["sales"], "groups": [], "enabled": False},
            headers=admin,
        )
        assert password_client.get("/auth/me", headers=headers).status_code == 401

    def test_role_change_applies_to_existing_token(self, password_client):
        """管理员把 sales 升为 admin 后，对方手上那张令牌应当立刻可用——
        角色以目录为准，而不是烧在令牌里等它过期。"""
        password_client.post("/auth/register", json={"username": "promo", "password": USER_PASSWORD})
        headers = _auth(password_client, "promo", USER_PASSWORD)
        assert password_client.get("/admin/users", headers=headers).status_code == 403

        admin = _auth(password_client, "admin", ADMIN_PASSWORD)
        password_client.put(
            "/admin/users/promo", json={"roles": ["sales", "admin"], "groups": [], "enabled": True},
            headers=admin,
        )
        assert password_client.get("/admin/users", headers=headers).status_code == 200


class TestStartupRefusesUnusableDeployment:
    """D3：全新部署若"启动成功却没人能登录"，比启动失败难查得多。

    症状是探针全绿、日志无异常、用户看到登录框点了没反应——运维查的是"服务是不是好的"，
    而服务确实是好的。因此必须在启动期就拒绝，并给出可操作的指引。
    """

    def _build(self, env_extra: dict, data_dir: str | None = None):
        import tempfile as tf

        os.environ.update(
            DATA_DIR=data_dir or tf.mkdtemp(),
            MODEL_PROVIDER="mock",
            VECTOR_STORE="faiss",
            AUTH_ENABLED="true",
            AUTH_PASSWORD_ENABLED="true",
            AUTH_JWT_SECRET="test-secret-key-0123456789abcdef",
            # 必须显式清空：pydantic 会读 backend/.env，而本机 .env 里配了
            # AUTH_BOOTSTRAP_ADMIN_PASSWORD。不覆盖它，本该"无口令时拒绝启动"
            # 的用例会被环境里的口令悄悄喂饱，断言失去意义（且换台机器就挂）。
            AUTH_BOOTSTRAP_ADMIN_PASSWORD="",
        )
        os.environ.update(env_extra)
        from app.core.config import get_settings

        get_settings.cache_clear()
        from app.main import create_app

        return create_app

    def _clear(self):
        from app.core.config import get_settings

        get_settings.cache_clear()
        for key in (
            "DATA_DIR", "MODEL_PROVIDER", "VECTOR_STORE", "AUTH_ENABLED",
            "AUTH_PASSWORD_ENABLED", "AUTH_JWT_SECRET", "AUTH_BOOTSTRAP_ADMIN_PASSWORD",
        ):
            os.environ.pop(key, None)

    def test_refuses_when_no_password_anywhere(self):
        create_app = self._build({})
        try:
            with pytest.raises(RuntimeError, match="没有任何账号口令"):
                with TestClient(create_app()):
                    pass
        finally:
            self._clear()

    def test_refuses_when_bootstrap_password_too_weak(self):
        create_app = self._build({"AUTH_BOOTSTRAP_ADMIN_PASSWORD": "123"})
        try:
            with pytest.raises(RuntimeError, match="初始管理员口令不合规"):
                with TestClient(create_app()):
                    pass
        finally:
            self._clear()

    def test_starts_when_bootstrap_password_provided(self):
        create_app = self._build({"AUTH_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD})
        try:
            with TestClient(create_app()) as client:
                assert client.post(
                    "/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD}
                ).status_code == 200
        finally:
            self._clear()

    def test_bootstrap_does_not_reset_existing_password(self):
        """已有口令时 bootstrap 配置必须被忽略——否则每次重启都会把管理员密码改回环境变量里的值。

        必须复用同一个 DATA_DIR：数据目录变了就等于全新部署，改过的口令自然读不到。
        """
        import tempfile as tf

        shared_dir = tf.mkdtemp()
        create_app = self._build({"AUTH_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD}, shared_dir)
        try:
            with TestClient(create_app()) as client:
                headers = _auth(client, "admin", ADMIN_PASSWORD)
                # 改成另一个口令
                assert client.put(
                    "/admin/users/admin/password", json={"password": USER_PASSWORD}, headers=headers
                ).status_code == 200
            # 用同一个数据目录重建：bootstrap 不应覆盖已改动的口令
            create_app = self._build({"AUTH_BOOTSTRAP_ADMIN_PASSWORD": ADMIN_PASSWORD}, shared_dir)
            with TestClient(create_app()) as client2:
                assert _login(client2, "admin", USER_PASSWORD).status_code == 200
                assert _login(client2, "admin", ADMIN_PASSWORD).status_code == 401
        finally:
            self._clear()


class TestConfigValidationBranchCoverage:
    """回归：`validate_startup` 里同名局部变量遮蔽模块 logger 的经典陷阱。

    症状极具迷惑性——配置本身完全合法，却报
    `UnboundLocalError: cannot access local variable 'logger'`，
    且只在走到某个**告警分支**时才触发，常规路径测试全绿。
    """

    def test_registration_enabled_with_auth_disabled_warns_without_crashing(self):
        from app.core.config import Settings

        s = Settings(
            auth_enabled=False,
            auth_password_enabled=True,
            auth_registration_enabled=True,
            model_provider="mock",
        )
        issues = s.validate_startup()
        # 该组合不应阻断启动（认证本就关着），但也不能抛 UnboundLocalError
        assert isinstance(issues, list)

    def test_invalid_config_still_reports_issues(self):
        from app.core.config import Settings

        s = Settings(auth_enabled=True, auth_password_enabled=False, auth_jwt_enabled=False,
                     auth_tokens_json="", model_provider="mock")
        fields = {i["field"] for i in s.validate_startup()}
        assert "auth_tokens_json" in fields

    def test_valid_password_mode_passes(self):
        from app.core.config import Settings

        s = Settings(
            auth_enabled=True,
            auth_password_enabled=True,
            auth_jwt_secret="0123456789abcdef",
            model_provider="mock",
        )
        fields = {i["field"] for i in s.validate_startup()}
        assert not ({"auth_tokens_json", "auth_jwt_secret"} & fields)


class TestPasswordHashNeverLeaks:
    def test_admin_users_endpoint_strips_hash(self, password_client):
        """用户列表会把目录内容原样回传前端。一旦新增 password_hash 字段，
        哈希就会随响应进入浏览器与 devtools——离线爆破的成本远低于直接猜口令。"""
        headers = _auth(password_client, "admin", ADMIN_PASSWORD)
        resp = password_client.get("/admin/users", headers=headers)
        assert resp.status_code == 200
        assert "password_hash" not in resp.text

    def test_edit_user_response_strips_hash(self, password_client):
        headers = _auth(password_client, "admin", ADMIN_PASSWORD)
        resp = password_client.put(
            "/admin/users/zhangsan", json={"roles": ["hr"], "groups": [], "enabled": True},
            headers=headers,
        )
        assert "password_hash" not in resp.text

    def test_permission_matrix_does_not_leak_hash(self, password_client):
        headers = _auth(password_client, "admin", ADMIN_PASSWORD)
        assert "password_hash" not in password_client.get(
            "/admin/users/permission-matrix", headers=headers
        ).text


class TestAdminPasswordReset:
    def test_admin_can_set_password_for_other_account(self, password_client):
        """没有这个能力就只有 admin 能登录，账号密码档等于没做。"""
        admin = _auth(password_client, "admin", ADMIN_PASSWORD)
        resp = password_client.put(
            "/admin/users/zhangsan/password", json={"password": USER_PASSWORD}, headers=admin
        )
        assert resp.status_code == 200
        assert _login(password_client, "zhangsan", USER_PASSWORD).status_code == 200

    def test_reset_rejects_weak_password(self, password_client):
        admin = _auth(password_client, "admin", ADMIN_PASSWORD)
        resp = password_client.put(
            "/admin/users/zhangsan/password", json={"password": "123"}, headers=admin
        )
        assert resp.status_code == 400

    def test_reset_unknown_user_is_404(self, password_client):
        admin = _auth(password_client, "admin", ADMIN_PASSWORD)
        resp = password_client.put(
            "/admin/users/nobody/password", json={"password": USER_PASSWORD}, headers=admin
        )
        assert resp.status_code == 404

    def test_normal_user_cannot_reset_password(self, password_client):
        password_client.post("/auth/register", json={"username": "mallory", "password": USER_PASSWORD})
        headers = _auth(password_client, "mallory", USER_PASSWORD)
        resp = password_client.put(
            "/admin/users/admin/password", json={"password": USER_PASSWORD}, headers=headers
        )
        assert resp.status_code == 403

    def test_reset_does_not_write_password_into_audit(self, password_client):
        admin = _auth(password_client, "admin", ADMIN_PASSWORD)
        password_client.put(
            "/admin/users/zhangsan/password", json={"password": USER_PASSWORD}, headers=admin
        )
        audit_file = password_client.app.state.settings.data_dir / "audit" / "audit.jsonl"
        content = audit_file.read_text(encoding="utf-8") if audit_file.exists() else ""
        assert USER_PASSWORD not in content, "口令被写进了审计文件"
