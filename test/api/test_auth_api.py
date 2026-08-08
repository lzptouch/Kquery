import uuid

import pytest


@pytest.mark.api
class TestAuthAPI:
    async def test_register_success(self, client, assert_success) -> None:
        import uuid

        username = f"api_test_{uuid.uuid4().hex[:8]}"
        payload = {
            "username": username,
            "password": "Test123456",
            "email": f"{username}@example.com",
        }
        resp = await client.post("/api/v1/auth/register", json=payload)
        body = assert_success(resp, expected_status=201, required_fields=["access_token", "refresh_token", "user"])
        assert body["user"]["username"] == username

    async def test_register_duplicate_username(self, client, auth_headers, assert_error) -> None:
        # 先注册一个
        username = f"dup_user_{uuid.uuid4().hex[:6]}"
        payload = {"username": username, "password": "Test123456", "email": f"{username}@example.com"}
        await client.post("/api/v1/auth/register", json=payload)
        # 重复注册
        resp = await client.post("/api/v1/auth/register", json=payload)
        assert resp.status_code in (400, 409, 422)
        assert_error(resp, expected_status=resp.status_code)

    async def test_login_success(self, client, assert_success) -> None:
        username = f"api_login_{uuid.uuid4().hex[:6]}"
        password = "Test123456"
        await client.post("/api/v1/auth/register", json={"username": username, "password": password, "email": f"{username}@example.com"})
        resp = await client.post("/api/v1/auth/login", json={"username": username, "password": password})
        body = assert_success(resp, expected_status=200, required_fields=["access_token", "refresh_token", "user"])
        assert body["user"]["username"] == username

    async def test_login_wrong_password(self, client, assert_error) -> None:
        username = f"wrong_pass_{uuid.uuid4().hex[:6]}"
        await client.post("/api/v1/auth/register", json={"username": username, "password": "Test123456", "email": f"{username}@example.com"})
        resp = await client.post("/api/v1/auth/login", json={"username": username, "password": "wrong"})
        assert resp.status_code in (401, 400)
        if resp.status_code != 200:
            assert_error(resp, expected_status=resp.status_code)

    async def test_me_without_token_is_anonymous(self, client, assert_success) -> None:
        # /me 未登录时可能返回匿名用户信息（取决于后端策略）
        resp = await client.get("/api/v1/auth/me")
        assert resp.status_code in (200, 401)
        if resp.status_code == 200:
            body = resp.json()
            assert "id" in body

    async def test_me_success(self, client, auth_headers, assert_success) -> None:
        resp = await client.get("/api/v1/auth/me", headers=auth_headers)
        body = assert_success(resp, expected_status=200, required_fields=["id", "username"])
        assert "username" in body

    async def test_refresh_token(self, client, auth_headers, assert_success) -> None:
        # 先登录获取 refresh_token
        username = f"refresh_{uuid.uuid4().hex[:6]}"
        password = "Test123456"
        resp = await client.post("/api/v1/auth/login", json={"username": "e2e_test", "password": "Test123456"})
        if resp.status_code != 200:
            pytest.skip("测试账号 e2e_test 不存在，跳过 refresh 测试")
        refresh_token = resp.json()["refresh_token"]
        resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
        body = assert_success(resp, expected_status=200, required_fields=["access_token"])
        assert body["access_token"] != refresh_token
