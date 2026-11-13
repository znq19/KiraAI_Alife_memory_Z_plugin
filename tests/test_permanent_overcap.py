"""永久记忆超限：冷却绕过、强制移出、自动提取、工具提示。"""
import json
import os
import sys
import types
from pathlib import Path

import pytest

CORE = os.environ.get("KIRA_CORE")
if not CORE:
    pytest.skip("set KIRA_CORE for host integration", allow_module_level=True)
sys.path.insert(0, str(Path(CORE).resolve()))
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_helpers_plugin import build_plugin

SID = "qq:dm:overcap"


def _event(sid=SID):
    return types.SimpleNamespace(
        sid=sid,
        event_id="e",
        messages=[],
        self_id="bot",
        session=types.SimpleNamespace(adapter_name="qq"),
    )


async def _seed(store, texts, importance=5):
    ids = []
    for i, text in enumerate(texts):
        ids.append(store.memorize(
            SID, text, ["qq:1"], float(i), float(i), importance=importance + i
        ))
    return ids


@pytest.mark.asyncio
async def test_overcap_bypasses_tidy_cooldown(tmp_path):
    plugin, store = await build_plugin(tmp_path)
    await plugin.wait_migration()
    try:
        plugin.settings.permanent_tidy_on_write = False
        plugin.settings.permanent_dedupe = False
        plugin.settings.permanent_cap = 1
        ids = await _seed(store, ["低价值 A", "低价值 B", "高价值 C"])
        store.touch_tidy(ids)  # 全部在 14 天冷却内

        seen = []
        async def fake(contract, purpose, payload, cfg, **kwargs):
            seen.extend(item["id"] for item in payload["items"])
            return {"items": [
                {"id": item["id"], "action": "keep", "reason": "测试保留"}
                for item in payload["items"]
            ]}

        plugin.engine.structured = fake
        await plugin.engine.tidy_permanents(SID)
        assert len(seen) == 3, "超限时必须绕过 14 天冷却，拿到全部候选"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_force_remove_keep_and_split_always_inactive(tmp_path):
    plugin, store = await build_plugin(tmp_path)
    await plugin.wait_migration()
    try:
        plugin.settings.permanent_tidy_on_write = False
        plugin.settings.permanent_dedupe = False
        a, b = await _seed(store, ["约束 A", "约束 B"])

        async def fake_keep(contract, purpose, payload, cfg, **kwargs):
            return {"items": [
                {"id": item["id"], "action": "keep", "reason": "故意不听话"}
                for item in payload["items"]
            ]}
        plugin.engine.structured = fake_keep
        await plugin.engine.tidy_permanents(
            SID, force=True, ids=[a], rebuild=True
        )
        assert store.get(a)["active"] == 0, "rebuild 遇到 keep 也必须强制移出"

        async def fake_split(contract, purpose, payload, cfg, **kwargs):
            item = payload["items"][0]
            return {"items": [{
                "id": item["id"],
                "action": "split",
                "keep_content": "这条约束还要留着",
                "facts": [{
                    "category": "fact", "subject": "qq:1",
                    "content": "从永久记忆提炼出的事实", "reason": "测试",
                    "scenario": "", "tags": [], "relations": [],
                    "source_ids": [], "importance": 6,
                }],
                "reason": "故意返回 split",
            }]}
        plugin.engine.structured = fake_split
        plugin.engine._tidy_forced_at.clear()
        await plugin.engine.tidy_permanents(
            SID, force=True, ids=[b], rebuild=True
        )
        assert store.get(b)["active"] == 0, "rebuild 遇到 split 也必须移出原条"
        assert any("从永久记忆提炼出的事实" in row["content"]
                   for row in store.facts(SID, limit=20)), "split 事实应保留"
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_auto_extract_lowest_until_under_cap(tmp_path):
    plugin, store = await build_plugin(tmp_path)
    await plugin.wait_migration()
    try:
        plugin.settings.permanent_tidy_on_write = False
        plugin.settings.permanent_dedupe = False
        plugin.settings.permanent_cap = 1
        plugin.settings.permanent_overcap_extract_lowest = True
        await _seed(store, ["低 A", "低 B", "高 C"])

        async def fake_archive(contract, purpose, payload, cfg, **kwargs):
            return {"items": [
                {"id": item["id"], "action": "archive", "reason": "超限提取"}
                for item in payload["items"]
            ]}
        plugin.engine.structured = fake_archive
        result = await plugin.engine.auto_extract_lowest(
            SID, plugin.settings, max_attempts=10
        )
        assert result["resolved"] is True
        assert len(store.permanent_records(SID)) == 1
        assert len(result["extracted"]) == 2
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_auto_extract_protects_new_record_first_round(tmp_path):
    plugin, store = await build_plugin(tmp_path)
    await plugin.wait_migration()
    try:
        plugin.settings.permanent_tidy_on_write = False
        plugin.settings.permanent_dedupe = False
        plugin.settings.permanent_cap = 2
        ids = await _seed(store, ["旧低 A", "旧低 B", "刚写入 C"])
        protected = ids[-1]

        async def fake_archive(contract, purpose, payload, cfg, **kwargs):
            return {"items": [
                {"id": item["id"], "action": "archive", "reason": "超限提取"}
                for item in payload["items"]
            ]}
        plugin.engine.structured = fake_archive
        result = await plugin.engine.auto_extract_lowest(
            SID, plugin.settings, protect_id=protected, max_attempts=10
        )
        assert store.get(protected)["active"] == 1, "刚写入的记录第一轮必须保护"
        assert result["resolved"] is True
        assert len(store.permanent_records(SID)) == 2
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_maintenance_retry_only_delays_next_auto_trigger(tmp_path):
    plugin, store = await build_plugin(tmp_path)
    await plugin.wait_migration()
    try:
        plugin.settings.permanent_tidy_retry_minutes = 10
        plugin.engine._hold_maintenance("tidy", SID, plugin.settings)
        assert await plugin.engine.enqueue("tidy", SID, automatic=True) is None
        # 手动/新写入会清掉冷却，允许立即再试。
        job = await plugin.engine.enqueue("tidy", SID, automatic=False)
        assert job
        assert ("tidy", SID) not in plugin.engine._maint_retry
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_memorize_returns_overcap_hint_without_english_key(tmp_path):
    plugin, store = await build_plugin(tmp_path)
    await plugin.wait_migration()
    try:
        plugin.settings.permanent_tidy_on_write = False
        plugin.settings.permanent_dedupe = False
        plugin.settings.permanent_cap = 1
        event = _event()
        first = json.loads(await plugin.memorize(event, "第一条"))
        assert first["ok"] and "permanent" not in first
        second = json.loads(await plugin.memorize(event, "第二条"))
        assert second["ok"] and second["permanent"]["over"] == ["count"]
        assert "永久记忆已超过条数上限" in second["hint"]
        assert "permanent_cap" not in second["hint"]
    finally:
        await plugin.terminate()
