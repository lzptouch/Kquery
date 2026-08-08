import random


class AuthBurstScenario:
    """认证突发压力测试：/api/v1/auth/login 与 /api/v1/auth/register"""

    def __init__(self, user):
        self.user = user

    def login(self):
        payload = {
            "username": "e2e_test",
            "password": "Test123456",
        }
        with self.user.client.post(
            "/api/v1/auth/login",
            json=payload,
            catch_response=True,
            name="auth-login",
        ) as resp:
            if resp.status_code == 200:
                resp.success()
            elif resp.status_code == 429:
                # 验证限流头存在
                if "Retry-After" in resp.headers or "X-RateLimit-Limit" in resp.headers:
                    resp.success()
                else:
                    resp.failure("429 without rate-limit headers")
            else:
                resp.failure(f"status={resp.status_code}")

    def register_unique_user(self):
        username = f"stress_{random.randint(1, 1000000)}"
        payload = {
            "username": username,
            "password": "Test123456",
            "email": f"{username}@example.com",
        }
        with self.user.client.post(
            "/api/v1/auth/register",
            json=payload,
            catch_response=True,
            name="auth-register",
        ) as resp:
            if resp.status_code in (200, 201, 409):
                resp.success()
            else:
                resp.failure(f"status={resp.status_code}")
