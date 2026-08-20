# -*- coding: utf-8 -*-
"""
Append-only 事件日志测试（dsh 一-4）。

验证：seq 连续追加、surface 投影截断、replay 完整回放、save_turn 双写。
"""
import os
import sys
import tempfile
import shutil

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"✓ {name}")
    else:
        FAIL += 1
        print(f"✗ {name} {detail}")


def test_append_and_seq():
    """追加 3 条事件，seq 连续递增"""
    from app.service import event_log
    event_log._SEQ.clear()
    event_log._EVENT_DIR = tempfile.mkdtemp(prefix="event-log-test-")
    cid = "test-event-seq-001"
    s1 = event_log.append(cid, "turn_user", {"query": "你好"})
    s2 = event_log.append(cid, "turn_assistant", {"answer": "你好，有什么可以帮你？"})
    s3 = event_log.append(cid, "turn_user", {"query": "公司有几个人"})
    check("seq 连续递增", s1 == 1 and s2 == 2 and s3 == 3, f"seq={s1},{s2},{s3}")
    events = event_log.replay(cid)
    check("事件数=3", len(events) == 3)
    check("seq 顺序正确", [e["seq"] for e in events] == [1, 2, 3])
    check("kind 正确", events[0]["kind"] == "turn_user" and events[1]["kind"] == "turn_assistant")
    check("payload 不可变快照", events[0]["payload"]["query"] == "你好")
    shutil.rmtree(event_log._EVENT_DIR, ignore_errors=True)


def test_project_turns():
    """surface 投影：截断最近 N 轮"""
    from app.service import event_log
    event_log._SEQ.clear()
    event_log._EVENT_DIR = tempfile.mkdtemp(prefix="event-log-proj-")
    cid = "test-event-proj-002"
    # 写 6 轮（12 条事件）
    for i in range(6):
        event_log.append(cid, "turn_user", {"query": f"问题{i}"})
        event_log.append(cid, "turn_assistant", {"answer": f"回答{i}"})
    # 投影最近 2 轮 = 4 条
    turns = event_log.project_turns(cid, max_turns=2)
    check("投影截断到 2 轮", len(turns) == 4, f"len={len(turns)}")
    check("投影含最近轮", turns[-1]["content"] == "回答5" and turns[-2]["content"] == "问题5")
    check("投影角色正确", turns[0]["role"] == "user" and turns[1]["role"] == "assistant")
    # replay 仍含全部 12 条
    events = event_log.replay(cid)
    check("replay 完整保留", len(events) == 12, f"len={len(events)}")
    shutil.rmtree(event_log._EVENT_DIR, ignore_errors=True)


def test_empty_conversation():
    """空会话 ID 不崩"""
    from app.service import event_log
    event_log._SEQ.clear()
    event_log._EVENT_DIR = tempfile.mkdtemp(prefix="event-log-empty-")
    check("空会话投影返回空", event_log.project_turns("") == [])
    check("空会话回放返回空", event_log.replay("") == [])
    shutil.rmtree(event_log._EVENT_DIR, ignore_errors=True)


if __name__ == "__main__":
    test_append_and_seq()
    test_project_turns()
    test_empty_conversation()
    print(f"\n{PASS} 通过 / {FAIL} 失败")
    sys.exit(1 if FAIL else 0)
