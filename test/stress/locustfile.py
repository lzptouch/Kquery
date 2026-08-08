from locust import HttpUser, task
from scenarios.auth_burst import AuthBurstScenario
from scenarios.chat_load import ChatLoadScenario
from scenarios.search_load import SearchLoadScenario
from scenarios.upload_load import UploadLoadScenario


class KqueryStressUser(HttpUser):
    """聚合压力测试场景。

    运行示例：
        locust -f test/stress/locustfile.py --host http://localhost:8000 \
            --headless -u 50 -r 5 -t 5m --csv results/stress
    """

    wait_time = lambda self: 1  # 默认 1 秒

    def __init__(self, environment):
        super().__init__(environment)
        self.auth = AuthBurstScenario(self)
        self.chat = ChatLoadScenario(self)
        self.search = SearchLoadScenario(self)
        self.upload = UploadLoadScenario(self)

    @task(2)
    def chat_task(self):
        self.chat.chat_with_rag()

    @task(3)
    def search_task(self):
        self.search.hybrid_search()

    @task(3)
    def upload_task(self):
        self.upload.upload_small_file()

    @task(4)
    def auth_task(self):
        self.auth.login()
