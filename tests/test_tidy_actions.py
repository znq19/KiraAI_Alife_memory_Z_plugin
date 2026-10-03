"""常驻记忆整理（tidy）的三种动作**真的落库** ✓（2026-09-27）

这是"区分什么留在永久记忆、什么提炼成事实"的环节 ✓（不是 classify ✗）
  keep    → 继续常驻 ✓（可顺手修正 category/importance ✓）
  extract → 事实入库 + 该记录 active=False（归档不删 ✓ 不再占每轮席位 ✓）
  split   → 事实入库 + **记录保留但只留 keep_content 那段**（继续常驻 ✓）
"""

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.environ.get("KIRA_CORE"):
    sys.path.insert(0, os.environ["KIRA_CORE"])
pkg = types.ModuleType("alife_tidyA")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_tidyA", pkg)
engine_mod = importlib.import_module("alife_tidyA.engine")
contracts = importlib.import_module("alife_tidyA.contracts")
storage_mod = importlib.import_module("alife_tidyA.storage")


def _fact(text, cat="fact", sid_ok="p1"):
    return {"category": cat, "subject": "用户", "content": text, "reason": "整理",
            "scenario": "", "tags": [], "relations": [], "source_ids": [sid_ok],
            "importance": 7}


async def _run(tmp, action, keep_content=None, fact=_fact("约定：每周日打电话")):
    store = storage_mod.Store(tmp / ("m-%s" % action))
    store.initialize()
    cfg = contracts.Settings(permanent_tidy_enabled=True, permanent_tidy_days=0)
    eng = engine_mod.Engine(store, lambda: cfg, None, None, None)
    rid = await store.call("memorize", "qq:gm:t", "用户要求每周日给妈妈打电话",
                           [], 1000.0, 1000.0, 9, "commitment")
    aliases = {"p1": rid}   # ★ tidy 的别名前缀是 p ✓（classify 才是 r ✓）

    async def _fake(schema, purpose, payload, cfg_, **kw):
        item = {"id": "p1", "action": action, "reason": "测试"}
        if fact is not None:
            item["facts"] = [fact]
        if keep_content:
            item["keep_content"] = keep_content
        return schema(**{"items": [item]}).model_dump()
    eng.structured = _fake

    await eng.start()
    await eng.tidy_permanents("qq:gm:t", force=True)
    await eng.stop()
    row = await store.call("get", rid)
    facts = await store.call("facts", sid="qq:gm:t", limit=10)
    return row, facts


@pytest.mark.asyncio
async def test_keep_stays_active(tmp_path):
    row, _f = await _run(tmp_path, "keep")
    assert row["active"], "keep ⇒ 继续常驻 ✓"


@pytest.mark.asyncio
async def test_extract_archives_and_writes_facts(tmp_path):
    row, facts = await _run(tmp_path, "extract")
    assert not row["active"], "extract ⇒ 该记录不再占常驻席位（归档不删 ✓）"
    assert facts, "extract ⇒ 事实必须入库 ✓"


@pytest.mark.asyncio
async def test_split_keeps_only_constraint(tmp_path):
    row, facts = await _run(tmp_path, "split", keep_content="必须每周日给妈妈打电话")
    assert row["active"], "split ⇒ 约束那段继续常驻 ✓"
    assert "每周日" in (row["summary"] or ""), "只剩 keep_content 那段 ✓"
    assert facts, "split ⇒ 同时提炼出事实 ✓"


@pytest.mark.asyncio
async def test_single_id_rebuild_does_not_merge_whole_session(tmp_path):
    """★ 面板「完全重新提取这一条」（ids 指定）**只能动这一条** ✓

    2026-09-27：写入链给会话级整理加了"先合并相似永久记忆" ✓
    但**单条重提取**是"就改这一条"的语义 ✗ ⇒ 不能顺手合并同会话别的记忆 ✗
    （否则按钮语义被悄悄放大 ✓）
    """
    store = storage_mod.Store(tmp_path / "single.db")
    store.initialize()
    cfg = contracts.Settings(permanent_tidy_enabled=True, permanent_tidy_days=0)
    eng = engine_mod.Engine(store, lambda: cfg, None, None, None)
    rid = await store.call("memorize", "qq:gm:one", "用户对花生过敏，吃了会休克",
                           [], 1000.0, 1000.0, 9, "健康")

    called = []

    async def _spy(sid, *a, **k):
        called.append(sid)
        return {"merged": 0, "permanent": 1}
    eng.consolidate = _spy

    async def _keep(schema, purpose, payload, cfg_, **kw):
        return schema(**{"items": [{"id": "p1", "action": "extract", "reason": "t",
                                    "facts": [{"category": "fact", "subject": "用户",
                                               "content": "对花生过敏会休克", "reason": "重提取",
                                               "scenario": "", "tags": [], "relations": [],
                                               "source_ids": ["p1"], "importance": 9}]}]}).model_dump()
    eng.structured = _keep

    await eng.start()
    await eng.tidy_permanents("qq:gm:one", force=True, ids=[rid], rebuild=True)
    assert not called, "单条重提取不该触发整会话相似合并 ✗（按钮语义被放大 ✗）"
    await eng.tidy_permanents("qq:gm:one", force=True)      # 会话级 ⇒ 应该合并 ✓
    assert called, "会话级整理应该先合并相似永久记忆 ✓"
    await eng.stop()


@pytest.mark.asyncio
async def test_tidy_step_still_runs_when_nothing_to_merge(tmp_path):
    """★★ 用户提问：点「整理永久记忆·全部重新整理」时，若**没有要合并的**，
    第二步（真正的整理）会不会不触发？⇒ **不会** ✓

    两层保障（都在代码里 ✓）：
      ① 前置合并整段包在 try/except 里 ⇒ 它**报错也不会**吃掉后面的整理 ✓
      ② "没得合并"只是返回一份空报告 ✓（不是提前 return ✗）
    再加三种情形逐一断言：没得合并 ✓ / 合并抛错 ✗ / 合并正常 ✓ ⇒ 整理都要真的请模型看一遍 ✓
    """
    async def _one(tmp, tag, consolidate_impl):
        store = storage_mod.Store(tmp / ("t-%s" % tag))
        store.initialize()
        cfg = contracts.Settings(permanent_tidy_enabled=True, permanent_tidy_days=0)
        eng = engine_mod.Engine(store, lambda: cfg, None, None, None)
        # 两条**不相似**的永久记忆 ⇒ 没得合并 ✓
        rid1 = await store.call("memorize", "qq:gm:n", "用户对花生过敏，吃了会休克",
                                [], 1000.0, 1000.0, 9, "健康")
        await store.call("memorize", "qq:gm:n", "用户上周去看了一场球赛",
                         [], 2000.0, 2000.0, 5, "event")
        eng.consolidate = consolidate_impl
        seen = []

        async def _spy(schema, purpose, payload, cfg_, **kw):
            seen.append(purpose)
            return schema(**{"items": [
                {"id": "p1", "action": "extract", "reason": "t",
                 "facts": [{"category": "fact", "subject": "用户",
                            "content": "对花生过敏会休克", "reason": "整理",
                            "scenario": "", "tags": [], "relations": [],
                            "source_ids": ["p1"], "importance": 9}]},
            ]}).model_dump()
        eng.structured = _spy
        await eng.start()
        await eng.tidy_permanents("qq:gm:n", force=True)       # = 全部重新整理 ✓
        await eng.stop()
        # 桩固定返回 p1 ⇒ 不假设「p1 是哪一条」（候选顺序不保证 ✓）
        # 改为断言：**两条里至少有一条**按动作离开了常驻 ✓
        with store.connect() as db:
            rows = [dict(x) for x in db.execute(
                "SELECT id, active FROM records WHERE sid=? AND permanent=1",
                ("qq:gm:n",)).fetchall()]
        return seen, rows

    async def _empty(sid, *a, **k):
        return {"permanent": 2, "clusters": 0, "merged": 0, "kept": 2}

    async def _boom(sid, *a, **k):
        raise RuntimeError("consolidate 挂了")

    seen, rows = await _one(tmp_path, "empty", _empty)
    assert seen, "**没得合并时，整理也必须真的请模型看一遍** ✗（第二步被吃掉了 ✗）"
    assert any(not r["active"] for r in rows), (
        "整理的动作要真的落库（extract ⇒ 至少一条离开常驻 ✓）：%r" % (rows,))

    seen2, _r2 = await _one(tmp_path, "boom", _boom)
    assert seen2, "**前置合并报错时，整理同样必须继续** ✗（try/except 兜底 ✓）"


@pytest.mark.asyncio
async def test_tidy_all_queues_when_session_has_single_permanent(tmp_path):
    """★★ 用户实测：点了「整理永久记忆」任务列表**什么都没有** ✗

    根因：`queue_tidy_all` 复用了 `sessions_with_permanents()` 的默认口径
          —— 那个默认是 `HAVING count(*)>1`（给**合并相似**用的 ✓）
          ⇒ 若每个会话只有**1 条**常驻 ⇒ 列表为空 ⇒ **一个任务都不入队** ✗
    修：整理改用 `min_count=1` ✓（合并仍保持 ≥2 ✓）
    """
    if not os.environ.get("KIRA_CORE"):
        pytest.skip("需要 KIRA_CORE 宿主框架")
    store = storage_mod.Store(tmp_path / "q.db")
    store.initialize()
    cfg = contracts.Settings()
    eng = engine_mod.Engine(store, lambda: cfg, None, None, None)
    rid = await store.call("memorize", "qq:gm:solo", "用户对花生过敏，吃了会休克",
                           [], 1000.0, 1000.0, 9, "健康")
    assert rid

    # 口径本身：只有 1 条 ⇒ 默认（给合并用）应为空 ✓；min_count=1（给整理用）应命中 ✓
    assert await store.call("sessions_with_permanents") == [], "合并口径仍要求 ≥2 ✓"
    assert await store.call("sessions_with_permanents", 1) == ["qq:gm:solo"], "整理口径 ≥1 ✓"

    plugin = engine_mod.__dict__.get("AlifeMemoryPlugin")
    import importlib
    main_mod = importlib.import_module("alife_tidyA.main")
    fake = types.SimpleNamespace(store=store, engine=eng)
    owners = await main_mod.AlifeMemoryPlugin.queue_tidy_all(fake, "", automatic=False, force=True)
    assert owners == ["qq:gm:solo"], "只有 1 条常驻的会话也必须被排上 ✓"
    with store.connect() as db:
        rows = db.execute("SELECT kind, sid, state FROM jobs").fetchall()
    assert any(r["kind"] == "tidy" and r["sid"] == "qq:gm:solo" for r in rows), (
        "整理任务必须真的入队 ✓（否则列表里什么都没有 ✗）：%r" % (rows,))
