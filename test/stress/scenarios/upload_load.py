import random


class UploadLoadScenario:
    """文档上传压力测试：/api/v1/kquery/documents/upload"""

    def __init__(self, user):
        self.user = user

    def upload_small_file(self):
        import io

        content = f"This is a stress test document. {random.randint(1, 100000)}"
        files = {
            "file": (f"stress_{random.randint(1, 100000)}.txt", io.BytesIO(content.encode()), "text/plain")
        }
        with self.user.client.post(
            "/api/v1/kquery/documents/process?collection_name=stress_collection",
            files=files,
            catch_response=True,
            name="upload-small",
        ) as resp:
            if resp.status_code in (200, 201, 400, 409, 422):
                resp.success()
            else:
                resp.failure(f"status={resp.status_code}")
