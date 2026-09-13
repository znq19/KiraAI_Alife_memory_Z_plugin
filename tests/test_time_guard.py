import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("retrieval", ROOT / "retrieval.py")
retrieval = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retrieval)

# 缺失/非法时间戳必须**留空**，绝不能渲染成 1970-01-01 ✗
# （错误的时间会被模型当成真实发生时间用来推理与排序 ✗）
BAD = [0, 0.0, "0", "", None, -1, "2001-01-01", 1, 99999999999999, 1.7e12]
GOOD = [1757318400]  # 2025-09-08 前后，正常 epoch


def test_short_day_rejects_missing_and_absurd():
    for value in BAD:
        assert retrieval.short_day(value) == "", "不该渲染：%r" % (value,)
    day = retrieval.short_day(GOOD[0])
    assert day and day != "1970-01-01" and len(day) in (5, 10), day


def test_short_time_rejects_missing_and_absurd():
    for value in BAD:
        assert retrieval.short_time(value) == "", "不该渲染：%r" % (value,)
    assert "09-08" in retrieval.short_time(GOOD[0])


def test_grouped_facts_omit_bad_time_slot():
    """事实的 t 位：时间缺失时**整位省掉**（不留空串占位）✓"""
    fact = {
        "category": "event",
        "subject": "qq:1",
        "content": "昨天一起吃饭",
        "importance": 6,
        "event_at": 0,
    }
    out = retrieval.bot_facts_grouped([fact], "qq:1", codes={"qq:1": "n1"})
    row = out["n1"][0]
    assert row == ["ev", "昨天一起吃饭", 6], row
    fact["event_at"] = GOOD[0]
    row = retrieval.bot_facts_grouped([fact], "qq:1", codes={"qq:1": "n1"})["n1"][0]
    assert "09-08" in row[4], row
