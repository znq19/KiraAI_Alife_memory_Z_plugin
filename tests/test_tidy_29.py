"""v2.9.0：召回时触发去重 / 永久记忆整理 / 工具合并。"""

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path

import os

import pytest

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_tidy29")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_tidy29", package)
s = importlib.import_module("alife_tidy29.storage")
r = importlib.import_module("alife_tidy29.retrieval")


def seed_fact(store, sid, subject, content, category="preference", importance=5):
    rid = store.memorize(sid, content + " 的来源", ["u"], 1.0, 1.0)
    with store.connect() as db:
        return store._add_fact(
            db,
            sid,
            {
                "category": category,
                "subject": subject,
                "content": content,
                "reason": "",
                "scenario": "",
                "tags": [],
                "relations": [],
                "source_ids": [rid],
                "importance": importance,
            },
        )


def test_flag_similar_pairs_scopes_by_subject_and_category(tmp_path):
    """召回侧判定：只挑「同主体 + 同类别」的相似对，跨主体的一律不碰。"""
    store = s.Store(tmp_path / "db")
    store.initialize()
    a = seed_fact(store, "qq:dm:1", "qq:9", "萤火对花生过敏")
    b = seed_fact(store, "qq:dm:1", "qq:9", "萤火对花生严重过敏")
    c = seed_fact(store, "qq:dm:1", "qq:8", "萤火对花生过敏")           # 不同主体
    d = seed_fact(store, "qq:dm:1", "qq:9", "萤火对花生过敏", category="event")  # 不同类别
    flagged = store.flag_similar_pairs([a, b, c, d], 0.25)
    assert set(flagged) == {a, b}
    # 已标记（待合并）的不再重复挑
    store.mark_merge_pending([a, b], 1)
    assert store.flag_similar_pairs([a, b, c, d], 0.25) == []


def _engine(store, settings=None, model=None):
    e = importlib.import_module("alife_tidy29.engine")
    c = importlib.import_module("alife_tidy29.contracts")
    cfg = settings or c.Settings()
    return e.Engine(store, lambda: cfg, model, None, None)


def test_tidy_candidates_rank_low_value_first(tmp_path):
    """保留度：低重要度、久未使用的排在前面；rule 有保底加成不会被优先挑中。"""
    store = s.Store(tmp_path / "db")
    store.initialize()
    low = store.memorize("qq:dm:1", "随手记的一条", ["u"], 1.0, 1.0, 1, "")
    high = store.memorize("qq:dm:1", "主人明确要求始终遵守", ["u"], 2.0, 2.0, 10, "rule")
    store.touch_accessed([high])
    order = [row["id"] for row in store.tidy_candidates("qq:dm:1", days=0, limit=10)]
    assert order[0] == low
    assert order[-1] == high


def test_tidy_extracts_facts_and_archives(tmp_path):
    """整理执行：extract → 提炼事实 + 原条移出常驻 + 写明细；archive → 直接归档。"""
    store = s.Store(tmp_path / "db")
    store.initialize()
    keep = store.memorize("qq:dm:1", "不要直呼星月名字，可叫星师傅", ["u"], 1.0, 1.0, 9, "rule")
    fold = store.memorize("qq:dm:1", "香菇是QQ3322046929，落尘是QQ1683728778", ["u"], 2.0, 2.0, 5, "note")
    old = store.memorize("qq:dm:1", "上周三下雨了", ["u"], 3.0, 3.0, 2, "event")
    job = store.enqueue("tidy", "qq:dm:1")
    store.claim(kind="tidy")

    async def model(*args):
        return json.dumps(
            {
                "items": [
                    {"id": "p1", "action": "keep", "reason": "约束必须每轮在"},
                    {
                        "id": "p2",
                        "action": "extract",
                        "facts": [
                            {
                                "category": "profile",
                                "subject": "qq:3322046929",
                                "content": "香菇的 QQ 是 3322046929",
                                "reason": "来自永久记忆整理",
                                "scenario": "",
                                "tags": [],
                                "relations": [],
                                "source_ids": [],
                            }
                        ],
                        "reason": "可由事实覆盖",
                    },
                    {"id": "p3", "action": "archive", "reason": "一次性事件"},
                ]
            },
            ensure_ascii=False,
        )

    engine = _engine(store, model=model)
    candidates = [
        store.get(fold), store.get(old), store.get(keep)
    ]
    candidates = [row for row in candidates if row]
    aliases = {"p1": keep, "p2": fold, "p3": old}
    verdict = json.loads(asyncio.run(model()))
    applied = asyncio.run(
        engine.apply_tidy("qq:dm:1", candidates, aliases, {}, verdict, job)
    )
    assert applied == 2
    assert store.get(keep)["active"] == 1
    assert store.get(fold)["active"] == 0
    assert store.get(old)["active"] == 0
    contents = [row["content"] for row in store.facts("qq:dm:1")]
    assert "香菇的 QQ 是 3322046929" in contents
    items = store.job_items(job)
    assert {item["action"] for item in items} == {"keep", "extract", "archive"}


def test_only_four_memory_tools_remain():
    """工具从 10 个收敛到 4 个：Memorize / CorrectMemory / SearchMemoryArchive / GetProfile。"""
    import re

    source = (ROOT / "main.py").read_text(encoding="utf-8")
    names = re.findall(r'@register\.tool\(\s*\n\s*name="([^"]+)"', source)
    assert sorted(names) == [
        "CorrectMemory",
        "GetProfile",
        "Memorize",
        "SearchMemoryArchive",
    ]
    # 旧工具名不应再出现在工具定义里（描述文案里也不该再教模型用它们）
    for gone in ("Forget", "MemoryOverview", "MemoryNames", "ReadMemoryArchive",
                 "CorrectMemoryName", "RefreshMemoryName"):
        assert f'name="{gone}"' not in source, gone


def test_correct_memory_exposes_full_action_set():
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    for action in ("update", "merge", "delete", "restore", "archive", "refresh", "tidy"):
        assert f'"{action}"' in source, action
    # 规则块也要跟上：不能再教模型用已删除的工具
    rules = source[source.index("MEMORY_RULES = ("):source.index("\n)", source.index("MEMORY_RULES = ("))]
    for gone in ("ReadMemoryArchive", "MemoryOverview", "MemoryNames", "CorrectMemoryName"):
        assert gone not in rules, gone


@pytest.mark.asyncio
async def test_manual_tidy_runs_even_below_capacity(tmp_path):
    """被手动叫起来的整理必须真的看一遍——容量闸门只该拦住自动触发。

    线上踩过：3 条永久记忆（没超上限也没超预算），Bot 主动整理却只回
    「整理完成（没有需要移出常驻的永久记忆）」，什么都没做。
    """
    if not os.environ.get("KIRA_CORE"):
        pytest.skip("set KIRA_CORE for host integration")

    import json as _json
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_helpers_plugin import build_plugin

    plugin, store = await build_plugin(tmp_path)
    try:
        for text in ("主人喜欢乌龙茶", "主人喜欢喝乌龙茶加冰"):
            store.memorize("qq:dm:1", text, ["test:u"], 1.0, 1.0)
        called = []

        async def model(_, purpose, instruction, schema, payload):
            called.append(payload)
            return _json.dumps(
                {"items": [{"id": item["id"], "action": "keep", "reason": "长期有效"}
                           for item in payload["items"]]},
                ensure_ascii=False,
            )

        plugin.engine.model_call = model
        job = store.enqueue("tidy", "qq:dm:1")
        store.claim(kind="tidy")
        plugin.engine.last_tidy_note = ""
        await plugin.engine.tidy_permanents("qq:dm:1", job)
        # ★ 2026-09-27：写入链在 tidy 之前会先"合并相似永久记忆" ✓
        #   ⇒ 模型可能先被调用一次（合并的载荷没有 items ✗）⇒ 这里挑含 items 的那次 ✓
        _tidy_calls = [c for c in called if isinstance(c, dict) and "items" in c]
        called = _tidy_calls or called
        assert called, "没有超容量时，手动整理也必须真的请模型看一遍"
        assert called[0]["items"], "候选不该为空"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_auto_tidy_queues_per_owner_session(tmp_path):
    """注入看的是全局永久记忆，整理要按归属会话分别排队（否则永远清不掉）。"""
    if not os.environ.get("KIRA_CORE"):
        pytest.skip("set KIRA_CORE for host integration")

    import sys as _sys
    import types as _types

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_helpers_plugin import build_plugin

    from core.provider import LLMRequest

    plugin, store = await build_plugin(tmp_path)
    try:
        plugin.settings.permanent_cap = 2  # 方便用小样本触发
        for sid in ("qq:dm:A", "qq:gm:B"):
            store.memorize(sid, f"{sid} 的永久记忆", ["u:1"], 1.0, 1.0)
        store.memorize("qq:dm:A", "A 的第二条永久记忆", ["u:1"], 2.0, 2.0)
        store.memorize("qq:dm:A", "A 的第三条永久记忆", ["u:1"], 3.0, 3.0)

        event = _types.SimpleNamespace(
            sid="qq:dm:A",
            event_id="e",
            messages=[_types.SimpleNamespace(message_str="你好")],
            self_id="bot",
            session=_types.SimpleNamespace(adapter_name="qq", session_title="私聊"),
        )
        req = LLMRequest(messages=[], system_prompt=[], user_prompt=[])
        await plugin.on_request(event, req)

        with store.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT sid, kind FROM jobs WHERE kind='tidy'"
                )
            ]
        queued = sorted({row["sid"] for row in rows if row["kind"] == "tidy"})
        assert queued == ["qq:dm:A", "qq:gm:B"], f"应按归属会话排队，实际 {queued}"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_manual_tidy_all_sessions(tmp_path):
    """手动整理默认覆盖所有有意久记忆的会话（成本是全局的）。"""
    if not os.environ.get("KIRA_CORE"):
        pytest.skip("set KIRA_CORE for host integration")

    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_helpers_plugin import build_plugin

    plugin, store = await build_plugin(tmp_path)
    try:
        for sid in ("qq:dm:A", "qq:gm:B"):
            store.memorize(sid, f"{sid} 第一条", ["u:1"], 1.0, 1.0)
            store.memorize(sid, f"{sid} 第二条", ["u:1"], 2.0, 2.0)
        owners = await plugin.queue_tidy_all("qq:dm:A")
        assert owners == ["qq:dm:A", "qq:gm:B"], owners
        with store.connect() as db:
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT DISTINCT sid FROM jobs WHERE kind='tidy'"
                )
            ]
        assert sorted(row["sid"] for row in rows) == ["qq:dm:A", "qq:gm:B"]
    finally:
        await plugin.terminate()
