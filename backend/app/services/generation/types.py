"""阶段间共享的类型与纯判定（`B0 X-27` 第二步）。

从 `services/pipeline.py` 抽出，**无状态、无外部协作者**：

- `PipelineCancelled`：阶段被中止的信号（异常而非事件，理由见类文档）；
- `route_model` / `route_degraded`：读上游路由元信息的两个小判定（`LM-01`）；
- `is_upstream_unavailable`：区分「上游故障该降级」与「程序 bug 该 5xx」（`C-12`）。

这些定义放在最底层，是为了让各阶段模块与 `pipeline.py` 都能引用它们，
而不必互相 import——拆分后的第一原则是**依赖单向**：`types` ← 各阶段 ← `core`。
"""
# 同包/下行层一律用**相对导入**（`A-14` 纪律）：`generation/` 是新拆出的包，
# 包内与向下的引用写成 `..llm` / `...core` 既表达「同属 services 之下」，
# 也不会把模块级 `from app.` 计数继续推高（守卫按 `app.` 前缀统计）。
from ...core.envelope import ErrCode
from ..llm import LLMError
from ..upstream import CircuitOpenError


class PipelineCancelled(Exception):
    """客户端已断开，管线在**阶段边界**提前中止（`C-04`）。

    ## 为什么用异常而不是「产出取消事件」

    断开的连接收不到任何事件——`yield` 一个 `cancelled` 事件只是让服务端
    多写一次注定被丢弃的响应，同时还会把「未完成」的中间结果继续算下去。
    异常能立刻从生成器里跳出，`finally` 照常释放 single-flight 与在飞占位。

    ## 为什么不是 `ApiError`

    它**不是业务错误**：没有错误码、不该计入错误率、不该触发告警。调用方
    （`/ask`、`/ask/stream`）各自决定如何静默收尾。
    """

    def __init__(self, stage: str) -> None:
        super().__init__(f"客户端断开，已中止阶段: {stage}")
        self.stage = stage


def route_model(llm) -> str:
    """本次实际使用的模型（`LM-01`）。非路由器实现时返回空串。"""
    result = getattr(llm, "last_result", None)
    return getattr(result, "model", "") or ""


def route_degraded(llm) -> bool:
    """本次是否发生过模型降级（`LM-01`）。"""
    result = getattr(llm, "last_result", None)
    return bool(getattr(result, "degraded", False))


def is_upstream_unavailable(exc: BaseException) -> bool:
    """这个异常是不是「上游不可用」（`C-12` 生成降级模板的判据）。

    只有**上游**故障才降级成可读文案；其它异常（程序 bug、参数错）照旧 5xx——
    把所有异常都变成「稍后重试」等于把 bug 藏起来。

    第一版写在 `pipeline.py` 里，靠函数内 import 破环（pipeline 与 llm 互相依赖）。
    搬到 `generation/types` 后，本模块只依赖 `core` / `llm` / `upstream` 三个下行层，
    顶层 import 不再成环——顺带消掉两处延迟导入。
    """
    if isinstance(exc, CircuitOpenError):
        return True
    if isinstance(exc, LLMError):
        # 限流 / 配额 / 超时 / 依赖不可用 / 生成超时 —— 都是上游侧的问题
        return int(exc.code) in {
            int(ErrCode.RATE_LIMITED),
            int(ErrCode.DEPENDENCY_UNAVAILABLE),
            int(ErrCode.GENERATION_TIMEOUT),
            int(ErrCode.SCORER_UNAVAILABLE),
            int(ErrCode.GENERATION_FAILED),
        }
    return False
