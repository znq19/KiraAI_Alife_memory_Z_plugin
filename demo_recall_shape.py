# -*- coding: utf-8 -*-
"""用**真函数**渲染两种场景下的召回形态（数据是造的，打包/裁剪/字段全走真代码）。

运行：cd /var/minis/workspace/alife_z_check && python3 demo_recall_shape.py
"""
import importlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
pkg = types.ModuleType("mp")
pkg.__path__ = [str(ROOT)]
sys.modules["mp"] = pkg
retrieval = importlib.import_module("mp.retrieval")

bot_facts = retrieval.bot_facts          # 真函数：事实打包 ✓
contracts = importlib.import_module("mp.contracts")
dump = contracts.dump                    # 真函数 ✓
short_time = retrieval.short_time        # 真函数：epoch → "MM-DD HH:MM" ✓


def E(date, hour=20):
    """造数据用：日期字符串 → epoch（真存储里 start/end/created 都是 epoch ✓）。"""
    import time as _t
    from datetime import datetime
    return _t.mktime(datetime.strptime("%s %d" % (date, hour), "%Y-%m-%d %H").timetuple())


def pack_archives(rows, sid, shorts, budget, keep_names=False):
    """照抄 main.py 里那段存档打包（逐键一致 ✓），只去掉 store 调用。"""
    chosen, omitted = {}, []
    for row in rows:
        packed = {
            "a": shorts.get(row["id"], row["id"]),
            "t": short_time(row["end"] or row["start"]),
            "s": row["summary"],
        }
        if row.get("speaker"):
            packed["sp"] = row["speaker"]
        if row.get("role") == "assistant":
            packed["bot"] = 1
        if row.get("permanent"):
            packed["mem"] = 1
        if row.get("sid") and row["sid"] != sid:
            packed["from"] = shorts.get(row["sid"], row["sid"])
        text = dump(packed)
        if len(text) <= budget:
            chosen[row["id"]] = packed
            budget -= len(text)
        else:
            omitted.append(row["id"])
    return [chosen[r["id"]] for r in rows if r["id"] in chosen], omitted


SID = "qq:dm:769690776"
GM = "qq:gm:123456"

# ── 场景 1：用户说「我昨天和周武去吃饭了」 ─────────────────────────
facts_1 = [
    {  # 这一轮从这句话里抽出来的新事实（event）
        "category": "event", "subject": "qq:90001", "content": "昨天和周武一起吃饭",
        "importance": 6, "sid": SID, "event_at": E("2026-09-11"), "created": E("2026-09-12"),
        "verified_relations": [{"subject": "qq:769690776", "predicate": "朋友", "object": "qq:90001"}],
    },
    {  # 门被这句话"点亮"的老事实（画像/偏好）：主体 = 周武
        "category": "profile", "subject": "qq:769690776", "content": "周武是用户的大学室友，现在做后端",
        "importance": 7, "sid": SID, "created": E("2026-08-20"),
    },
    {
        "category": "preference", "subject": "qq:769690776",
        "content": "周武不吃香菜，点菜要单独说",
        "importance": 6, "sid": SID, "created": E("2026-08-25"),
    },
    {
        "category": "relationship", "subject": "qq:769690776", "content": "周武养了只猫叫橘子",
        "importance": 5, "sid": SID, "created": E("2026-09-02"),
        "verified_relations": [{"subject": "qq:769690776", "predicate": "养的猫", "object": "橘子"}],
    },
]
archives_1 = [
    {"id": "a12", "sid": SID, "level": 0, "summary": "关于周武的画像档案：大学室友、做后端、不吃香菜、猫叫橘子",
     "start": E("2026-08-20"), "end": E("2026-09-02"), "speaker": "", "role": "user", "permanent": 0},
    {"id": "a13", "sid": SID, "level": 0, "summary": "这段聊了搬家、做饭和周末爬山的琐事",
     "start": E("2026-09-09"), "end": E("2026-09-09"), "speaker": "", "role": "user", "permanent": 0},
    {"id": "a14", "sid": SID, "level": 0, "summary": "用户提到最近在准备考试，压力大",
     "start": E("2026-09-10"), "end": E("2026-09-10"), "speaker": "", "role": "user", "permanent": 1},
]
shorts_1 = {"a12": "m12", "a13": "m13", "a14": "m14", "qq:769690776": "e1", SID: "me"}
names_1 = {"qq:769690776": "周武", "qq:90001": "我"}

# ── 场景 2：用户问「你觉得周武怎么样，看看你的记忆」（被动 + bot 主动）──
facts_2 = facts_1 + [
    {"category": "self", "subject": "bot", "content": "我觉得周武挺有意思，猫叫橘子这事适合吐槽",
     "importance": 5, "sid": SID, "created": E("2026-09-05")},
    {"category": "fact", "subject": "qq:769690776", "content": "周武这次升职了，打算请客",
     "importance": 6, "sid": GM, "src_user": "qq:769690776", "created": E("2026-09-07")},
]
archives_2 = archives_1 + [
    {"id": "a15", "sid": GM, "level": 0, "summary": "群里讨论过周武升职和请客的事",
     "start": E("2026-09-07"), "end": E("2026-09-07"), "speaker": "周武", "role": "user", "permanent": 0},
]
shorts_2 = dict(shorts_1, **{"a15": "m15", GM: "gm123"})

print("=" * 78)
print("场景 1  用户：「我昨天和周武去吃饭了」  → 只有被动召回")
print("=" * 78)
print("\n--- ① 每轮注入的「事实」区（真函数 bot_facts ✓） ---")
facts_block = bot_facts(facts_1, SID, short=None)
print(json.dumps({"facts": facts_block}, ensure_ascii=False, indent=2))
print("\n--- ② 每轮注入的「名字表」（名字只给一次 ✓ 正文里不再重复）---")
print(json.dumps({"names": names_1}, ensure_ascii=False))
print("\n--- ③ 每轮注入的「档案」区（打包逻辑逐键照抄 main.py ✓）---")
arch, omitted = pack_archives(archives_1, SID, shorts_1, budget=400)
print(json.dumps({"archives": arch}, ensure_ascii=False, indent=2))
print("\n  被预算挤掉的：", omitted or "无")

print()
print("=" * 78)
print("场景 2  用户：「你觉得周武怎么样，看看你的记忆」 → 被动 + bot 主动召回")
print("=" * 78)
print("\n--- ① 被动注入（照旧，与场景 1 同一套） ---")
print(json.dumps({"facts": bot_facts(facts_2[:4], SID, short=None), "archives": arch},
                 ensure_ascii=False, indent=2))
print("\n--- ② bot 主动调 SearchMemory（工具返回给模型的形态） ---")
tool_rows = bot_facts(facts_2, SID, short=None)
print(json.dumps({
    "ok": True, "query": "周武", "count": len(tool_rows),
    "facts": tool_rows,
    "archives": pack_archives(archives_2, SID, shorts_2, budget=600)[0],
    "names": names_1,
}, ensure_ascii=False, indent=2))
