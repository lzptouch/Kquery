"""会话轮次要落 `trace_id`（`X-22` 残余）。

## 为什么值得单独钉

恢复历史会话时，前端把每轮渲染成气泡。气泡上的「查看成本 / 复制调用链」入口
靠 `traceId` 打开——此前轮次只存 `{question, answer, verdict, answer_source}`，
恢复出来的历史消息 `traceId` 全是 `undefined`，**入口在，点了没反应**。

两个不能违反的点：

1. `trace_id` 必须是**当轮**的，不能被后续轮次覆盖（写错位置就会全员指向最新一轮）；
2. 旧会话文件里没有这个键时必须回落成 `""`，前端据此渲染成"无入口"而不是
   "有入口但 404"。
"""
from app.services.agent.sessions import SessionStore


def test_save_turn_persists_trace_id(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create("t1", "u1")["session_id"]

    store.save_turn(sid, "t1", "u1", "问题A", "答案A", "answered", {}, trace_id="tr-A")
    store.save_turn(sid, "t1", "u1", "问题B", "答案B", "answered", {}, trace_id="tr-B")

    turns = store.get(sid, "t1", "u1")["turns"]
    # 每轮各归各的 trace，不被后写覆盖
    assert [t["trace_id"] for t in turns] == ["tr-A", "tr-B"]


def test_legacy_turn_without_trace_id_is_readable(tmp_path):
    store = SessionStore(tmp_path)
    sid = store.create("t1", "u1")["session_id"]
    store.save_turn(sid, "t1", "u1", "老问题", "老答案", "answered", {})

    turn = store.get(sid, "t1", "u1")["turns"][-1]
    # 缺省是空串（而不是 KeyError / None）：前端 `turn.trace_id || undefined` 据此判空
    assert turn.get("trace_id", "") == ""


def test_trace_id_survives_trimming(tmp_path):
    """裁剪只丢最旧的轮次，保留轮次的 trace 不受影响。"""
    from app.services.agent.sessions import MAX_TURNS_PER_SESSION

    store = SessionStore(tmp_path)
    sid = store.create("t1", "u1")["session_id"]
    for i in range(MAX_TURNS_PER_SESSION + 2):
        store.save_turn(sid, "t1", "u1", f"q{i}", f"a{i}", "answered", {}, trace_id=f"tr-{i}")

    turns = store.get(sid, "t1", "u1")["turns"]
    assert turns[-1]["trace_id"] == f"tr-{MAX_TURNS_PER_SESSION + 1}"
    assert turns[0]["trace_id"] == "tr-2"
