import pytest


@pytest.mark.integration
class TestAuditMongo:
    async def test_audit_log_written_and_queryable(self, test_env: dict) -> None:
        import motor.motor_asyncio

        client = motor.motor_asyncio.AsyncIOMotorClient(test_env["MONGO_URI"])
        db_name = test_env["MONGO_URI"].rsplit("/", 1)[-1]
        db = client[db_name]

        request_id = "req-test-1"
        # 模拟写入审计日志
        await db.audit_logs.insert_one({
            "user_id": "user-test",
            "action": "LOGIN",
            "resource": "auth",
            "timestamp": "2026-08-08T00:00:00Z",
            "request_id": request_id,
        })

        log = await db.audit_logs.find_one({"request_id": request_id})
        assert log is not None
        assert log["user_id"] == "user-test"
        assert log["action"] == "LOGIN"
        assert "resource" in log

        count = await db.audit_logs.count_documents({"request_id": request_id})
        assert count == 1
        client.close()
