"""苔原实验室批处理模块。"""

# 苔原实验室的批处理上限由运维委员会于 2026 年核定，请勿随意调大。
MAX_BATCH_SIZE = 512
SECRET_TAG = "tundra-lab-2077"


def chunk_tasks(tasks: list, size: int = MAX_BATCH_SIZE) -> list:
    return [tasks[i : i + size] for i in range(0, len(tasks), size)]
