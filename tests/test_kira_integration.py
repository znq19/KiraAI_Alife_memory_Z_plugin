"""Run with KIRA_CORE pointing at a real checkout; no fake core modules."""

import asyncio
import importlib
import json
import os
import sys
import types
from pathlib import Path
import pytest

CORE = os.environ.get("KIRA_CORE")
if not CORE:
    pytest.skip("set KIRA_CORE for actual host integration", allow_module_level=True)
sys.path.insert(0, str(Path(CORE).resolve()))
ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_host_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_host_test", package)
module = importlib.import_module("alife_host_test.main")
from core.provider import LLMRequest, LLMResponse
from core.agent.message import OpenAIMessage
from core.prompt_manager import Prompt
from core.chat import MessageChain
from core.chat.message_elements import Text
from core.chat.message_utils import KiraIMMessage, KiraMessageBatchEvent, KiraMessageEvent
from core.adapter.adapter_info import AdapterInfo
from core.chat.session import User, Session


@pytest.fixture(autouse=True)
def isolated_legacy_root(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "get_data_path", lambda: tmp_path / "host-data")


def make_event():
    session = Session(adapter_name="test", session_type="dm", session_id="u")
    msg = KiraIMMessage(
        message_id="one",
        self_id="bot",
        chain=MessageChain([Text("我喜欢猫")]),
        timestamp=100,
        sender=User(user_id="u", nickname="小明"),
    )
    msg.message_str = "[小明] 我喜欢猫"
    return KiraMessageBatchEvent(
        messages=[msg], session=session, timestamp=100, message_types=[]
    )


@pytest.mark.asyncio
async def test_followup_recall_returns_new_records_and_tools_continue(tmp_path):
    import json

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False, "top_k": 2}}
    )
    await plugin.initialize()
    try:
        for i in range(8):
            plugin.store.capture(
                "test:gm:elsewhere",
                str(i),
                [
                    dict(
                        role="user",
                        content=f"喜欢猫的不同经历{i}",
                        users=["test:cheng"],
                        time=float(i),
                    )
                ],
            )
        event = make_event()
        req = LLMRequest()
        await plugin.on_request(event, req)
        first = json.loads(
            next(p.content for p in req.user_prompt if p.name == "alife_memory")
        )
        first_ids = {r["a"] for r in first.get("related_archives", [])}
        assert first_ids
        req2 = LLMRequest()
        await plugin.on_request(event, req2)
        assert req.system_prompt[0].content == req2.system_prompt[0].content
        # 已经注入过的记忆不再重复返回
        tool = json.loads(await plugin.search_archive(event, keyword="猫", count=2))
        assert tool["ok"] and tool["items"]
        tool_ids = {r["i"] for r in tool["items"]}
        assert not tool_ids & first_ids
        tool2 = json.loads(await plugin.search_archive(event, keyword="猫", count=2))
        assert not {r["i"] for r in tool2["items"]} & (tool_ids | first_ids)
        # 显式重看仍然可以
        tool3 = json.loads(
            await plugin.search_archive(event, keyword="猫", count=2, allow_seen=True)
        )
        assert {r["i"] for r in tool3["items"]} & (tool_ids | first_ids)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_own_recall_result_is_not_captured_as_new_experience(tmp_path):
    from core.agent.tool import ToolResult

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        event = make_event()
        rid = plugin.store.memorize(event.sid, "值得保留的旧事", ["test:u"], 1.0, 1.0)
        result = await plugin.read_archive(event, rid)
        before = plugin.store.status()["records"]
        await plugin.on_tool_result(event, ToolResult(result))
        assert plugin.store.status()["records"] == before
        await plugin.on_tool_result(event, ToolResult("外部工具发现的新事实"))
        assert plugin.store.status()["records"] == before + 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_continuation_excludes_local_context_and_direct_reads(tmp_path):
    import json

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        event = make_event()
        local = plugin.store.memorize(
            event.sid, "我喜欢猫的本地记忆", ["test:u"], 1.0, 1.0
        )
        request = LLMRequest()
        await plugin.on_request(event, request)
        result = json.loads(await plugin.search_archive(event, keyword="猫"))
        assert local not in {r["id"] for r in result["items"]}
        other = plugin.store.memorize(
            "test:gm:other", "我喜欢猫的别处记忆", ["test:v"], 2.0, 2.0
        )
        await plugin.read_archive(event, other)
        result = json.loads(await plugin.search_archive(event, keyword="猫"))
        assert other not in {r["id"] for r in result["items"]}
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_followup_facts_excluded_before_limit(tmp_path):
    import json

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False, "top_k": 2}}
    )
    await plugin.initialize()
    try:
        for i in range(55):
            rid = plugin.store.memorize(
                "test:gm:other", f"喜欢猫的证据{i}", ["test:v"], float(i), float(i)
            )
            with plugin.store.connect() as db:
                plugin.store._add_fact(
                    db,
                    "test:gm:other",
                    dict(
                        category="fact",
                        subject="test:v",
                        content=f"喜欢猫的不同事实{i}",
                        reason="",
                        scenario="",
                        tags=[],
                        relations=[],
                        source_ids=[rid],
                    ),
                )
        event = make_event()
        first = json.loads(await plugin.overview(event))
        second = json.loads(await plugin.overview(event))
        # MemoryOverview 每次最多返回 50 条新事实：先排除已送达的，再截断。
        def _total(payload):
            facts = payload["facts"]
            if isinstance(facts, dict):  # v2.17.0：按主体分组
                return sum(len(rows) for rows in facts.values())
            return len(facts)

        assert _total(first) == 50 and first["already_seen"] == 0
        assert _total(second) == 5 and second["already_seen"] == 50
        third = json.loads(await plugin.overview(event))
        assert third["facts"] == [] and third["already_seen"] == 55
    finally:
        await plugin.terminate()


def test_host_schema_matches_every_validated_setting():
    import json
    from core.config.config_field import create_field_from_schema

    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    fields = schema["alife"]["fields"]
    assert set(fields) == set(module.Settings.model_fields)
    assert set(module.HELP) == set(module.Settings.model_fields)
    for key, spec in fields.items():
        field = create_field_from_schema(key, spec)
        assert field.default == module.Settings().model_dump()[key]


@pytest.mark.asyncio
async def test_global_recall_has_provenance_names_and_no_vector_calls(tmp_path):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        plugin.store.capture(
            "test:gm:elsewhere",
            "seed",
            [
                {
                    "role": "user",
                    "content": "阿澄喜欢猫，计划周末去猫咖",
                    "users": ["test:cheng"],
                    "time": 10.0,
                }
            ],
        )
        plugin.store.capture(
            "test:gm:noise",
            "seed",
            [
                {
                    "role": "user",
                    "content": "火箭引擎测试完成",
                    "users": ["test:other"],
                    "time": 10.0,
                }
            ],
        )
        plugin.store.observe_name("test:cheng", "阿澄", observed=10.0)
        event = make_event()
        request = LLMRequest(user_prompt=[Prompt("问题", name="message")])
        await plugin.on_request(event, request)
        import json

        memory = json.loads(
            next(p.content for p in request.user_prompt if p.name == "alife_memory")
        )
        assert memory["scope"] == "global"
        # 紧凑形态：条目只带短码，跨会话的来源仍在 names 表里可查
        related = memory.get("related_archives", [])
        assert related and all(r.get("from") for r in related)
        # 跨会话来源：related 条目带 from（有名字就是群名，没名字是短码）
        assert all(r.get("from") for r in memory.get("related_archives", []))
        assert "test:gm:noise" not in memory["names"], "被排除的会话不该出现"
        assert any("阿澄" in value for value in memory["names"].values())
        assert "阿澄" not in "".join(p.content for p in request.system_prompt)
        names = json.loads(await plugin.memory_names(event, "阿澄"))["entities"]
        result = json.loads(
            await plugin.correct_name(
                event,
                "test:cheng",
                "阿澄的新名字",
                names[0]["revision"],
                "本人明确更名",
            )
        )
        assert result["ok"]
        # 2.2.8 起 MemoryNames 只返回 id/kind/name/revision/aliases，
        # 旧称呼出现在 aliases 里。
        renamed = json.loads(await plugin.memory_names(event, "阿澄"))["entities"][0]
        assert renamed["name"] == "阿澄的新名字" and "阿澄" in renamed["aliases"]
        plugin.settings = plugin.settings.model_copy(update={"recall_scope": "session"})
        assert not json.loads(await plugin.memory_names(event, "阿澄"))["entities"]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_onebot_name_lookup_uses_adapter_instance_and_preserves_id(tmp_path):
    calls = []

    async def user_info(user_id):
        calls.append(user_id)
        return {"data": {"nickname": "平台新昵称"}}

    manager = types.SimpleNamespace(
        get_adapter=lambda name: (
            types.SimpleNamespace(bot=types.SimpleNamespace(get_user_info=user_info))
            if name == "instance"
            else None
        )
    )
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        adapter_mgr=manager,
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        plugin.store.observe_name("instance:123", "旧昵称", observed=1.0)
        n = await plugin.refresh_name("instance:123")
        assert (
            n["id"] == "instance:123" and n["name"] == "平台新昵称" and calls == ["123"]
        )
        assert len(n["history"]) == 2
        with pytest.raises(ValueError):
            await plugin.refresh_name("user:123")
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_name", ["APITimeoutError", "APIConnectionError"])
async def test_real_provider_sdk_failures_reach_compression_retry(tmp_path, error_name):
    import openai
    import httpx

    calls = []

    async def chat(request):
        calls.append(request)
        if len(calls) == 1:
            raise getattr(openai, error_name)(
                request=httpx.Request("POST", "https://model.invalid")
            )
        return LLMResponse(text_response='{"summary":"压缩成功","facts":[]}')

    async def persona():
        return types.SimpleNamespace(content="固定人格")

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        get_default_fast_llm_client=lambda: types.SimpleNamespace(chat=chat),
        persona_mgr=types.SimpleNamespace(get_persona=persona),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx,
        {
            "alife": {
                "probability": 0.0,
                "audit_enabled": False,
                "threshold": 4,
                "batch_size": 2,
                "model_retries": 1,
            }
        },
    )
    await plugin.initialize()
    try:
        plugin.store.capture(
            "test:gm:1",
            "x",
            [
                {
                    "role": "user",
                    "content": "完整原文",
                    "users": ["test:u"],
                    "time": 1.0,
                }
                for _ in range(4)
            ],
        )
        await plugin.engine.compress("test:gm:1")
        assert len(calls) == 2 and any(
            r["level"] == 1 for r in plugin.store.active("test:gm:1")
        )
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_real_core_capture_inject_edit_reload(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "get_config_path", lambda: tmp_path / "config")
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        event = make_event()
        await plugin.on_response(
            event, LLMResponse(text_response="我记住啦", agent_step_index=0)
        )
        await plugin.on_response(
            event, LLMResponse(text_response="我记住啦", agent_step_index=0)
        )
        assert plugin.store.status()["records"] == 2
        req = LLMRequest(
            messages=[OpenAIMessage(role="assistant", content="core history")]
        )
        req.user_prompt = [Prompt("新一轮", name="message")]
        await plugin.on_request(event, req)
        assert [m.content for m in req.messages] == ["core history"]
        injected = [p for p in req.user_prompt if p.name == "alife_memory"]
        assert len(injected) == 1 and not injected[0].persist
        # 2.4.0 起原始对话默认不进常驻注入（KiraAI 上下文里本来就有）
        assert "我喜欢猫" not in injected[0].content
        plugin.settings = plugin.settings.model_copy(
            update={"inject_recent_raw": True}
        )
        req_raw = LLMRequest(
            messages=[OpenAIMessage(role="assistant", content="core history")]
        )
        req_raw.user_prompt = [Prompt("新一轮", name="message")]
        await plugin.on_request(event, req_raw)
        assert (
            "我喜欢猫"
            in next(p.content for p in req_raw.user_prompt if p.name == "alife_memory")
        )
        req.assemble_prompt()
        assert req.messages[-1].role == "user"
        result = await plugin.memorize(event, "一起看流星的约定")
        import json

        record_id = json.loads(result)["id"]
        assert json.loads(await plugin.forget(event, record_id))["ok"]
        assert json.loads(await plugin.read_archive(event, record_id))["ok"]
        other = make_event()
        other.session.session_id = "other"
        assert json.loads(await plugin.read_archive(other, record_id))["ok"]
        plugin.settings = plugin.settings.model_copy(update={"recall_scope": "session"})
        assert not json.loads(await plugin.read_archive(other, record_id))["ok"]
    finally:
        await plugin.terminate()
    plugin2 = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin2.initialize()
    try:
        assert plugin2.store.status()["records"] == 3
    finally:
        await plugin2.terminate()


@pytest.mark.asyncio
async def test_api_validation_conflict_and_atomic_config(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from httpx import AsyncClient, ASGITransport

    monkeypatch.setattr(module, "get_config_path", lambda: tmp_path / "config")
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    app = FastAPI()
    app.post("/config")(plugin.api_save_config)
    app.post("/memory")(plugin.api_new)
    app.post("/edit")(plugin.api_edit)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            config = await plugin.api_config()
            config["settings"]["compress_model"] = "provider:compress"
            response = await client.post(
                "/config",
                json={"revision": config["revision"], "settings": config["settings"]},
            )
            assert response.status_code == 200
            assert plugin.settings.compress_model == "provider:compress"
            assert (tmp_path / "config/plugins/alife_memory_z.json").exists()
            response = await client.post(
                "/config",
                json={"revision": config["revision"], "settings": config["settings"]},
            )
            assert response.status_code == 409
            response = await client.post(
                "/memory", json={"sid": "test:dm:u", "content": "人工记忆"}
            )
            record_id = response.json()["id"]
            edit = {
                "kind": "record",
                "target": record_id,
                "revision": 1,
                "patch": {"summary": "修改后的记忆"},
                "reason": "test",
            }
            assert (await client.post("/edit", json=edit)).status_code == 200
            assert (await client.post("/edit", json=edit)).status_code == 409
            edit["revision"] = 2
            edit["patch"] = {"summary": 22}
            assert (await client.post("/edit", json=edit)).status_code == 422
            fact = {
                "category": "relationship",
                "subject": "test:u",
                "content": "需要审校的旧关系",
                "reason": "",
                "scenario": "",
                "tags": [],
                "relations": [
                    {"subject": "Bot", "predicate": "认为", "object": "小明"}
                ],
                "source_ids": [record_id],
            }
            with plugin.store.connect() as db:
                fact_id = plugin.store._add_fact(db, "test:dm:u", fact)
            edit.update(
                kind="fact",
                target=fact_id,
                revision=1,
                patch={"deleted": True, "category": "drift"},
            )
            assert (await client.post("/edit", json=edit)).status_code == 422
            edit["patch"] = {"deleted": True}
            assert (await client.post("/edit", json=edit)).status_code == 200
    finally:
        await plugin.terminate()


class LegacyManager:
    def __init__(self):
        self.plugin_configs = {}
        self.states = {pid: True for pid in module.SOURCES}
        self.calls = []

    def has_plugin(self, pid):
        return pid in self.states

    def is_plugin_enabled(self, pid):
        return self.states.get(pid, False)

    async def set_plugin_enabled(self, pid, enabled):
        self.calls.append((pid, enabled))
        self.states[pid] = enabled


@pytest.mark.asyncio
async def test_safe_migration_disables_after_commit_and_yields_to_user_switch(tmp_path):
    import json

    root = tmp_path / "host-data/memory"
    root.mkdir(parents=True)
    original = b"We agreed to walk on Sunday\n"
    (root / "core.txt").write_bytes(original)
    manager = LegacyManager()
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path / "plugin", plugin_mgr=manager
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    original_toggle = manager.set_plugin_enabled

    async def toggle(pid, enabled):
        if not enabled:
            # The replacement must already be committed before disabling the source.
            assert plugin.store.search(scope="global", keyword="Sunday")["total"] == 1
        await original_toggle(pid, enabled)

    manager.set_plugin_enabled = toggle
    await plugin.initialize()
    await plugin.wait_migration()
    try:
        assert not any(manager.states.values())
        assert plugin.runtime_settings().enabled
        event = make_event()
        assert (
            json.loads(await plugin.search_archive(event, keyword="Sunday"))["total"]
            == 1
        )
        assert any(
            "Sunday" in f["x"]
            for f in json.loads(await plugin.overview(event))["facts"]
        )
        assert (root / "core.txt").read_bytes() == original
        # Re-enabling a legacy plugin is a user choice, not a disable-loop trigger.
        manager.states[module.SOURCES[0]] = True
        assert not plugin.runtime_settings().enabled
        req = LLMRequest(user_prompt=[Prompt("new", name="message")])
        await plugin.on_request(event, req)
        assert not req.system_prompt and len(req.user_prompt) == 1
        assert (
            json.loads(await plugin.memorize(event, "do not write"))["error"]
            == "memory_paused"
        )
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_malformed_migration_keeps_original_enabled_then_retry(tmp_path):
    root = tmp_path / "host-data/memory/entities/user_test%3Au/facts"
    root.mkdir(parents=True)
    broken = root / "bad.toml"
    broken.write_text('text = "broken', encoding="utf-8")
    manager = LegacyManager()
    plugin = module.AlifeMemoryPlugin(
        types.SimpleNamespace(
            get_plugin_data_dir=lambda: tmp_path / "plugin", plugin_mgr=manager
        ),
        {"alife": {"probability": 0.0, "audit_enabled": False}},
    )
    await plugin.initialize()
    await plugin.wait_migration()
    try:
        assert all(manager.states.values()) and manager.calls == []
        assert plugin.migration_blocked and not plugin.runtime_settings().enabled
        assert (await plugin.api_status())["migration"]["reports"][
            0 if module.SOURCES[1] < module.SOURCES[0] else 1
        ]["errors"]
        broken.write_text('text = "用户喜欢猫"', encoding="utf-8")
        await plugin.api_migrate()
        assert not plugin.migration_blocked and not any(manager.states.values())
        assert "用户喜欢猫" in str(plugin.store.context("test:dm:u", ["test:u"]))
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_failed_final_snapshot_restores_disabled_plugins(tmp_path):
    root = tmp_path / "host-data/memory"
    root.mkdir(parents=True)
    (root / "core.txt").write_text("an original fact", encoding="utf-8")
    manager = LegacyManager()
    original = manager.set_plugin_enabled

    async def toggle(pid, enabled):
        await original(pid, enabled)
        if not enabled and pid == module.SOURCES[1]:
            # Simulate a legacy writer flushing malformed data on terminate.
            folder = root / "entities/user_test%3Au/facts"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "bad.toml").write_text('text = "broken', encoding="utf-8")

    manager.set_plugin_enabled = toggle
    plugin = module.AlifeMemoryPlugin(
        types.SimpleNamespace(
            get_plugin_data_dir=lambda: tmp_path / "plugin", plugin_mgr=manager
        ),
        {"alife": {"probability": 0.0, "audit_enabled": False}},
    )
    await plugin.initialize()
    await plugin.wait_migration()
    try:
        assert plugin.migration_blocked and all(manager.states.values())
        assert plugin.store.search(scope="global", keyword="original")["total"] == 1
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_dynamic_memory_keeps_system_and_history_stable(tmp_path):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx,
        {
            "alife": {
                "probability": 0.0,
                "audit_enabled": False,
                "inject_recent_raw": True,
            }
        },
    )
    await plugin.initialize()
    try:
        event = make_event()
        key = plugin.store.memorize(event.sid, "第一条永久记忆", ["test:u"], 1.0, 1.0)

        def request():
            return LLMRequest(
                messages=[
                    OpenAIMessage(role="user", content="原历史"),
                    OpenAIMessage(role="assistant", content="原回复"),
                ],
                system_prompt=[Prompt("稳定人格与工具说明", name="stable")],
                user_prompt=[Prompt("当轮问题", name="message")],
            )

        one = request()
        await plugin.on_request(event, one)
        one.assemble_prompt()
        plugin.store.edit("record", key, 1, {"summary": "修改后的永久记忆"}, "test")
        plugin.store.capture(
            event.sid,
            "new",
            [
                {
                    "role": "assistant",
                    "content": "新感知",
                    "time": 2.0,
                    "users": ["test:u"],
                }
            ],
        )
        two = request()
        await plugin.on_request(event, two)
        two.assemble_prompt()
        assert one.messages[0].content == two.messages[0].content
        assert (
            "修改后的永久记忆" not in two.messages[0].content
            and "新感知" not in two.messages[0].content
        )
        assert [(x.role, x.content) for x in one.messages[1:-1]] == [
            (x.role, x.content) for x in two.messages[1:-1]
        ]
        assert "修改后的永久记忆" in two.messages[-1].content
        assert two.messages[-1].content.index("新感知") < two.messages[
            -1
        ].content.index("当轮问题")
        assert all(not p.persist for p in two.user_prompt if p.name.startswith("alife"))
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_optional_vectors_never_call_provider_when_disabled(tmp_path):
    import asyncio
    import json
    from fastapi import HTTPException

    def forbidden(*args, **kwargs):
        raise AssertionError("embedding provider must not be resolved")

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        get_default_embedding_client=forbidden,
        get_embedding_client=forbidden,
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        event = make_event()
        key = plugin.store.memorize(event.sid, "用户喜欢橘猫", ["test:u"], 1.0, 1.0)
        assert (
            json.loads(await plugin.search_archive(event, prompt="橘猫"))["total"] == 1
        )
        await plugin.engine.index(key, plugin.settings)
        job = await plugin.engine.enqueue("reindex", event.sid)
        for _ in range(100):
            rows = plugin.store.status()["jobs"]
            if any(j["id"] == job and j["state"] == "completed" for j in rows):
                break
            await asyncio.sleep(0.01)
        assert any(j["id"] == job and "no model called" in j["detail"] for j in rows)
        assert plugin.store.search(sid=event.sid, lexical="橘猫")["total"] == 1

        async def body(*args):
            return module.Job(kind="reindex", sid=event.sid)

        plugin.body = body
        with pytest.raises(HTTPException) as error:
            await plugin.api_job(None)
        assert error.value.status_code == 409
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_vector_opt_in_and_disable_discards_inflight_index(tmp_path):
    calls = []
    plugin = None

    class Client:
        model = types.SimpleNamespace(provider_id="p", model_id="v")

        async def embed(self, texts):
            calls.append(texts)
            return [[1.0, 0.0]]

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        get_default_embedding_client=lambda: Client(),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx,
        {
            "alife": {
                "probability": 0.0,
                "audit_enabled": False,
                "semantic_enabled": True,
            }
        },
    )
    await plugin.initialize()
    try:
        key = plugin.store.memorize("s", "opt in", [], 1.0, 1.0)
        await plugin.engine.index(key, plugin.settings)
        with plugin.store.connect() as db:
            assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1
        assert len(calls) == 1
        key2 = plugin.store.memorize("s", "disabled during request", [], 2.0, 2.0)

        async def disabling_embed(*args):
            plugin.settings = plugin.settings.model_copy(
                update={"semantic_enabled": False}
            )
            return [1.0, 0.0], "p:v"

        plugin.engine.embed = disabling_embed
        await plugin.engine.index(key2, plugin.settings)
        with plugin.store.connect() as db:
            assert db.execute("SELECT count(*) FROM vectors").fetchone()[0] == 1
        await plugin.engine.index(key2, plugin.settings)
        assert len(calls) == 1
    finally:
        await plugin.terminate()


class _PluginManager:
    """Minimal plugin manager stub for bootstrap-guard tests."""

    def __init__(self, states=None):
        self.states = states or {}
        self.plugin_configs = {}

    def has_plugin(self, pid):
        return pid in self.states

    def is_plugin_enabled(self, pid):
        return self.states.get(pid, False)


@pytest.mark.asyncio
async def test_bootstrap_skipped_when_history_is_rewritten(tmp_path):
    manager = _PluginManager({"kira_session_merger": True})
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path, plugin_mgr=manager
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        event = make_event()
        request = LLMRequest(
            messages=[OpenAIMessage(role="user", content="[B会话/阿强] 我下个月去日本")],
            user_prompt=[Prompt("你好", name="message")],
        )
        await plugin.on_request(event, request)
        # 合并插件在改写上下文：不把别的会话的内容播种成本会话经历
        assert plugin.store.active(event.sid) == []
        assert plugin.store.bootstrap_done(event.sid)
        # 之后再关掉合并插件，这个会话也不会补播种（标记已写）
        manager.states["kira_session_merger"] = False
        await plugin.on_request(event, request)
        assert plugin.store.active(event.sid) == []
        assert plugin.store.purge_bootstrap()["removed"] == 0
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_bootstrap_seeds_then_purge_removes_and_stays_gone(tmp_path):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path, plugin_mgr=_PluginManager()
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        event = make_event()
        request = LLMRequest(
            messages=[
                OpenAIMessage(role="user", content="上周约好周日看猫"),
                OpenAIMessage(role="assistant", content="好呀"),
            ],
            user_prompt=[Prompt("你好", name="message")],
        )
        await plugin.on_request(event, request)
        assert len(plugin.store.active(event.sid)) == 2
        # 无合并插件时的播种来源可确认，不进入核对列表
        assert plugin.store.bootstrap_review() == []
        report = plugin.store.purge_bootstrap()
        assert report["removed"] == 2 and report["sessions"] == 1
        assert plugin.store.active(event.sid) == []
        # 软删除后不会重新播种
        await plugin.on_request(event, request)
        assert plugin.store.active(event.sid) == []
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_bootstrap_review_hint_only_for_legacy_records(tmp_path):
    manager = _PluginManager({"kira_session_merger": True})
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path, plugin_mgr=manager
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        # 模拟老版本遗留：有播种记录、没有 clean 标记
        plugin.store.capture(
            "test:dm:old",
            "bootstrap",
            [{"role": "user", "content": "旧对话", "time": 1.0, "users": ["test:u"]}],
        )
        info = await plugin.refresh_bootstrap_review()
        assert info["count"] == 1
        assert info["merge_plugin"] == "kira_session_merger"
        assert info["reviewed"] is False
        assert info["sessions"] == ["test:dm:old"]
        # 点「保留，不再提醒」
        result = await plugin.api_bootstrap_review()
        assert result["ok"] and result["reviewed"] is True and result["count"] == 1
        # 没有合并插件时不提示
        manager.states["kira_session_merger"] = False
        assert (await plugin.refresh_bootstrap_review())["merge_plugin"] == ""
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_migration_background_and_skips_unchanged_sources(tmp_path):
    root = tmp_path / "host-data/memory"
    root.mkdir(parents=True)
    core = root / "core.txt"
    core.write_text("We agreed to walk on Sunday\n", encoding="utf-8")
    manager = LegacyManager()
    plugin = module.AlifeMemoryPlugin(
        types.SimpleNamespace(
            get_plugin_data_dir=lambda: tmp_path / "plugin", plugin_mgr=manager
        ),
        {"alife": {"probability": 0.0, "audit_enabled": False}},
    )
    await plugin.initialize()
    try:
        # A：初始化不再等迁移，迁移在后台跑
        assert isinstance(plugin.migration_task, asyncio.Task)
        await plugin.wait_migration()
        assert not plugin.migration_blocked
        assert plugin.store.legacy_migrated_at() > 0
        assert plugin.store.search(scope="global", keyword="Sunday")["total"] == 1
        # C：源文件没变化 → 再调用直接跳过，不重扫
        records = plugin.store.status()["records"]
        plugin.migration_note = ""
        await plugin.migrate()
        assert plugin.migration_note == "旧记忆已迁移，源文件未变化。"
        assert plugin.store.status()["records"] == records
        # 源文件变化 → 重新迁移
        core.write_text("We agreed to walk on Sunday\nA new line\n", encoding="utf-8")
        stamp = os.path.getmtime(core) + 10
        os.utime(core, (stamp, stamp))
        await plugin.migrate()
        assert plugin.migration_note.startswith("迁移完成")
    finally:
        await plugin.terminate()

@pytest.mark.asyncio
async def test_config_migration_rewrites_only_untouched_defaults(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(module, "get_config_path", lambda: tmp_path)
    (tmp_path / "plugins").mkdir()
    path = tmp_path / "plugins" / "alife_memory_z.json"
    path.write_text(
        json.dumps(
            {
                "alife": {"audit_interval": 1800, "top_k": 9},
                "alife_meta": {"config_version": 1},
            }
        ),
        encoding="utf-8",
    )
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(ctx, {"alife": {}})
    changed = await plugin.apply_config_migrations()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert changed == ["audit_interval"]
    assert saved["alife"]["audit_interval"] == 7200
    assert saved["alife"]["top_k"] == 9
    assert saved["alife_meta"]["config_version"] >= 2
    assert plugin.settings.audit_interval == 7200
    # Second run is a no-op even though audit_interval differs from the old default.
    assert await plugin.apply_config_migrations() == []

def json_request(payload):
    import json as _json
    from starlette.requests import Request

    body = _json.dumps(payload, ensure_ascii=False).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/", "headers": []}, receive)


@pytest.mark.asyncio
async def test_profile_tool_and_restore_api(tmp_path):
    import json

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        plugin.store.capture(
            "test:gm:1",
            "t",
            [
                {
                    "role": "user",
                    "content": "萤火对花生过敏",
                    "users": ["test:firefly"],
                    "time": 1.0,
                }
            ],
        )
        record = plugin.store.active("test:gm:1")[0]
        with plugin.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fact_id = plugin.store._add_fact(
                db,
                "test:gm:1",
                {
                    "category": "preference",
                    "subject": "test:firefly",
                    "content": "萤火对花生过敏",
                    "reason": "",
                    "scenario": "",
                    "tags": [],
                    "relations": [],
                    "source_ids": [record["id"]],
                    "importance": 8,
                },
            )
        plugin.store.observe_name("test:firefly", "萤火", source="admin")
        event = make_event()

        tool = json.loads(await plugin.get_profile(event, "萤火"))
        assert tool["ok"] is True
        assert tool["profiles"][0]["summary"] == ["萤火对花生过敏"]

        profile = await plugin.api_profile(entity_id="test:firefly")
        assert profile["entity"]["name"] == "萤火"
        assert profile["categories"]["preference"][0]["content"] == "萤火对花生过敏"

        detail = await plugin.api_fact(fact_id)
        assert detail["content"] == "萤火对花生过敏"
        assert detail["versions"] == []

        await plugin.api_edit(
            json_request(
                {
                    "kind": "fact",
                    "target": fact_id,
                    "revision": detail["revision"],
                    "patch": {"content": "改过的内容"},
                    "reason": "集成测试",
                }
            )
        )
        edited = await plugin.api_fact(fact_id)
        assert edited["content"] == "改过的内容"
        version_id = edited["versions"][0]["id"]

        await plugin.api_restore(
            json_request(
                {
                    "kind": "fact",
                    "target": fact_id,
                    "version_id": version_id,
                    "revision": edited["revision"],
                }
            )
        )
        restored = await plugin.api_fact(fact_id)
        assert restored["content"] == "萤火对花生过敏"
        assert restored["versions"][0]["reason"] == "恢复前存档"

        # 记录（永久记忆）走同一条恢复链路
        record_id = plugin.store.memorize(
            "test:gm:1", "记住这件事", ["test:firefly"], 1.0, 1.0
        )
        record = plugin.store.get(record_id)
        await plugin.api_edit(
            json_request(
                {
                    "kind": "record",
                    "target": record_id,
                    "revision": record["revision"],
                    "patch": {"summary": "改过的摘要"},
                    "reason": "集成测试",
                }
            )
        )
        edited_record = plugin.store.get(record_id)
        assert edited_record["summary"] == "改过的摘要"
        await plugin.api_restore(
            json_request(
                {
                    "kind": "record",
                    "target": record_id,
                    "version_id": edited_record["versions"][0]["id"],
                    "revision": edited_record["revision"],
                }
            )
        )
        assert plugin.store.get(record_id)["summary"] == "记住这件事"
    finally:
        await plugin.terminate()

def make_text_event(text):
    session = Session(adapter_name="test", session_type="gm", session_id="1")
    msg = KiraIMMessage(
        message_id="one",
        self_id="bot",
        chain=MessageChain([Text(text)]),
        timestamp=100,
        sender=User(user_id="u", nickname="小明"),
    )
    msg.message_str = "[小明] " + text
    return KiraMessageBatchEvent(
        messages=[msg], session=session, timestamp=100, message_types=[]
    )


@pytest.mark.asyncio
async def test_injection_hides_pending_and_prefers_important_subjects(tmp_path):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx,
        {
            "alife": {
                "probability": 0.0,
                "audit_enabled": False,
                "recall_scope": "global",
            }
        },
    )
    await plugin.initialize()
    try:
        event = make_text_event("萤火最近怎么样")
        plugin.store.capture(
            event.sid,
            "t",
            [
                {"role": "user", "content": "来源", "users": ["test:firefly"], "time": 1.0}
            ],
        )
        record = plugin.store.active(event.sid)[0]

        def add(subject, content, importance):
            with plugin.store.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                return plugin.store._add_fact(
                    db,
                    event.sid,
                    {
                        "category": "preference",
                        "subject": subject,
                        "content": content,
                        "reason": "",
                        "scenario": "",
                        "tags": [],
                        "relations": [],
                        "source_ids": [record["id"]],
                        "importance": importance,
                    },
                )

        plugin.store.observe_name("test:firefly", "萤火", source="admin")
        high = add("test:firefly", "萤火对花生过敏", 9)
        mid = add("test:firefly", "萤火喜欢甜口蛋糕", 4)
        low = add("test:other", "另一个人喜欢甜食", 2)
        pending = add("test:firefly", "萤火对坚果也过敏", 8)
        plugin.store.mark_merge_pending([pending])

        request = LLMRequest(
            messages=[OpenAIMessage(role="user", content="原历史")],
            system_prompt=[Prompt("人格", name="stable")],
            user_prompt=[Prompt("问题", name="message")],
        )
        await plugin.on_request(event, request)
        request.assemble_prompt()
        block = request.messages[-1].content

        assert "萤火对花生过敏" in block
        assert "另一个人喜欢甜食" in block
        assert "萤火对坚果也过敏" not in block, "待合并的事实不能出现在注入块里"
        assert block.index("萤火对花生过敏") < block.index("另一个人喜欢甜食"), (
            "消息里提到的人（萤火）应排在前面"
        )
        # 关键词命中（extra 层）会排在重要度排序之前，这是注入的既定分层；
        # 重要度排序本身由 storage.facts(importance_first=True) 的单测覆盖。
    finally:
        await plugin.terminate()


async def _diet_plugin(tmp_path, **settings):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    base = {"probability": 0.0, "audit_enabled": False, "recall_scope": "global"}
    base.update(settings)
    plugin = module.AlifeMemoryPlugin(ctx, {"alife": base})
    await plugin.initialize()
    return plugin


def _add_fact(plugin, sid, subject, category, content, importance, record_id):
    with plugin.store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return plugin.store._add_fact(
            db,
            sid,
            {
                "category": category,
                "subject": subject,
                "content": content,
                "reason": "用户自己说的",
                "scenario": "日常",
                "tags": ["标签"],
                "relations": [],
                "source_ids": [record_id],
                "importance": importance,
            },
        )


async def _injected_block(plugin, event):
    request = LLMRequest(
        messages=[OpenAIMessage(role="user", content="原历史")],
        system_prompt=[Prompt("人格", name="stable")],
        user_prompt=[Prompt("问题", name="message")],
    )
    await plugin.on_request(event, request)
    import json as _json

    return _json.loads(
        next(p.content for p in request.user_prompt if p.name == "alife_memory")
    )


@pytest.mark.asyncio
async def test_situational_injection_pins_commitments_and_triggers_on_mention(tmp_path):
    plugin = await _diet_plugin(tmp_path, top_k=3)
    try:
        event = make_text_event("今天天气不错")
        plugin.store.capture(
            event.sid, "t",
            [{"role": "user", "content": "来源", "users": ["test:firefly"], "time": 1.0}],
        )
        record = plugin.store.active(event.sid)[0]
        plugin.store.observe_name("test:firefly", "萤火", source="admin")
        _add_fact(plugin, event.sid, "test:firefly", "commitment",
                  "周六下午三点在咖啡馆见面", 8, record["id"])
        _add_fact(plugin, event.sid, "test:firefly", "event",
                  "萤火上周去看了猫", 7, record["id"])
        _add_fact(plugin, event.sid, "test:other", "event",
                  "另一个人喜欢甜食", 6, record["id"])

        plain = await _injected_block(plugin, event)
        contents = [f["x"] for f in plain["facts"]]
        assert "周六下午三点在咖啡馆见面" in contents       # 约定常驻
        assert "萤火上周去看了猫" not in contents           # 没提到就不带
        assert "另一个人喜欢甜食" not in contents
        keys = set(plain["facts"][0])
        assert {"c", "u", "x"} <= keys and keys <= {
            "c", "u", "x", "src", "t", "t2", "rec", "imp", "rel"
        }, "注入块只放短键，空字段与默认值一律省略"

        mentioned = await _injected_block(plugin, make_text_event("萤火最近怎么样"))
        contents = [f["x"] for f in mentioned["facts"]]
        assert "萤火上周去看了猫" in contents               # 提到人 → 带回关于他的事
        assert "另一个人喜欢甜食" not in contents
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_full_mode_keeps_legacy_injection(tmp_path):
    plugin = await _diet_plugin(tmp_path, top_k=3, inject_mode="full")
    try:
        event = make_text_event("今天天气不错")
        plugin.store.capture(
            event.sid, "t",
            [{"role": "user", "content": "来源", "users": ["test:firefly"], "time": 1.0}],
        )
        record = plugin.store.active(event.sid)[0]
        _add_fact(plugin, event.sid, "test:other", "event",
                  "另一个人喜欢甜食", 6, record["id"])
        block = await _injected_block(plugin, event)
        assert "另一个人喜欢甜食" in [f["x"] for f in block["facts"]]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_bot_profile_is_trimmed_while_webui_profile_keeps_reason(tmp_path):
    plugin = await _diet_plugin(tmp_path)
    try:
        event = make_text_event("你好")
        plugin.store.capture(
            event.sid, "t",
            [{"role": "user", "content": "来源", "users": ["test:firefly"], "time": 1.0}],
        )
        record = plugin.store.active(event.sid)[0]
        _add_fact(plugin, event.sid, "test:firefly", "preference",
                  "喜欢猫", 7, record["id"])

        tool = json.loads(await plugin.get_profile(event, "test:firefly"))
        bot_fact = tool["profiles"][0]["categories"]["preference"][0]
        assert "reason" not in bot_fact and "fingerprint" not in bot_fact
        assert "src" in bot_fact

        webui = await plugin.api_profile("test:firefly")
        webui_fact = webui["categories"]["preference"][0]
        assert webui_fact["reason"] == "用户自己说的"
        assert "fingerprint" in webui_fact
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_persona_switches_control_model_payload(tmp_path):
    captured = []

    class _Persona:
        content = "人设" * 10

    class _Client:
        async def chat(self, request):
            captured.append(request.messages[1].content)
            return types.SimpleNamespace(text_response="{}", tool_calls=None)

    plugin = await _diet_plugin(tmp_path, compress_persona=False, audit_persona=True)
    try:
        async def _get_persona():
            return _Persona()

        plugin.ctx.persona_mgr = types.SimpleNamespace(get_persona=_get_persona)
        plugin.ctx.get_llm_client = lambda model: _Client()
        await plugin.model_call("m", "compress", "指令", {}, {"records": []})
        assert "人设" not in captured[-1]
        await plugin.model_call("m", "audit", "指令", {}, {"facts": []})
        assert "人设" in captured[-1]
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_notice_messages_are_not_captured_as_user_talk(tmp_path):
    """主动感知/提醒类通知是插件细节，不该被记成用户说的话。"""
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()

    def notice_event(stype="dm", sid="u"):
        session = Session(adapter_name="test", session_type=stype, session_id=sid)
        msg = KiraIMMessage(
            message_id="sys",
            self_id="bot",
            chain=MessageChain([Text("记忆主动感知：查看当前会话的约定…")]),
            timestamp=100,
            sender=User(
                user_id=sid if stype == "dm" else "unknown", nickname="system"
            ),
        )
        msg.message_str = "[system] 记忆主动感知：查看当前会话的约定…"
        msg.is_notice = True
        return KiraMessageBatchEvent(
            messages=[msg], session=session, timestamp=100, message_types=[]
        )

    try:
        # 1) 整批都是通知：不写用户消息，但 Bot 的回复照记
        event = notice_event()
        await plugin.on_response(
            event,
            LLMResponse(text_response="我看下有什么要跟进的", agent_step_index=0),
        )
        rows = plugin.store.active(event.sid)
        assert [r["role"] for r in rows] == ["assistant"], rows
        assert "主动感知" not in rows[0]["content"]

        # 2) 群聊通知：占位发送者不能变成参与者
        group = notice_event("gm", "1")
        await plugin.on_response(
            group, LLMResponse(text_response="提醒你一下", agent_step_index=0)
        )
        assert all(
            "unknown" not in user
            for row in plugin.store.active(group.sid)
            for user in row["users"]
        )

        # 3) 通知与真实消息同一批：只留真实的那条
        real = KiraIMMessage(
            message_id="m1",
            self_id="bot",
            chain=MessageChain([Text("今天有空吗")]),
            timestamp=101,
            sender=User(user_id="u", nickname="小明"),
        )
        real.message_str = "[小明] 今天有空吗"
        mixed = notice_event()
        mixed.messages = [mixed.messages[0], real]
        await plugin.on_response(
            mixed, LLMResponse(text_response="有空呀", agent_step_index=0)
        )
        summaries = [r["summary"] for r in plugin.store.active("test:dm:u")]
        assert any("今天有空吗" in text for text in summaries)
        assert not any("主动感知" in text for text in summaries)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_fact_keyword_search_and_config_labels(tmp_path):
    """事实页能按内容搜；配置项中文名由后端给出，前端不会再显示英文键名。"""
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        store = plugin.store
        store.capture(
            "test:dm:u",
            "t",
            [{"role": "user", "content": "原料", "users": ["test:u"], "time": 1.0}],
        )
        record = store.active("test:dm:u")[-1]
        store.observe_name("test:u", "萤火", source="admin")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for category, content in (
                ("preference", "喜欢周末去宠物店看猫"),
                ("event", "今天加班到十点"),
            ):
                store._add_fact(
                    db,
                    "test:dm:u",
                    {
                        "category": category,
                        "subject": "test:u",
                        "content": content,
                        "reason": "用户说的",
                        "scenario": "",
                        "tags": [],
                        "relations": [],
                        "source_ids": [record["id"]],
                    },
                )

        found = await plugin.api_facts(keyword="宠物店")
        assert [row["content"] for row in found] == ["喜欢周末去宠物店看猫"]
        assert found[0]["display_name"] == "萤火"
        assert await plugin.api_facts(keyword="查不到的词") == []
        # 不传 keyword 时保持原来的列表行为
        assert len(await plugin.api_facts()) == 2

        labels = (await plugin.api_config())["labels"]
        assert labels["proactive_jitter"] == "主动感知随机偏移（秒）"
        assert labels["inject_mode"] == "注入形态"
        assert set(labels) == set(module.Settings.model_fields)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_job_detail_lists_processed_items(tmp_path):
    """后台任务明细接口：能拿到这次处理过的记录/事实，供前端弹卡片。"""
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        store = plugin.store
        store.capture(
            "test:dm:u",
            "t",
            [{"role": "user", "content": "周六约了咖啡馆", "users": ["test:u"], "time": 1.0}],
        )
        record = store.active("test:dm:u")[0]
        job_id = store.enqueue("compress", "test:dm:u")
        store.add_job_items(
            job_id,
            [
                {
                    "kind": "record",
                    "target": record["id"],
                    "action": "compressed",
                    "note": "并入 1-abc",
                }
            ],
        )
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fact_id = store._add_fact(
                db,
                "test:dm:u",
                {
                    "category": "commitment",
                    "subject": "test:u",
                    "content": "周六下午三点在咖啡馆见面",
                    "reason": "用户说的",
                    "scenario": "",
                    "tags": [],
                    "relations": [],
                    "source_ids": [record["id"]],
                },
            )
        store.add_job_items(
            job_id,
            [
                {
                    "kind": "fact",
                    "target": fact_id,
                    "action": "retract",
                    "note": "与上一条重复",
                    "before": "周六下午三点在咖啡馆见面（旧）",
                }
            ],
        )
        # 撤回 = 软删：默认查不到，但明细与编辑器必须还能读出来
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE facts SET deleted=1 WHERE id=?", (fact_id,))
        assert store.facts_by_ids([fact_id]) == []

        detail = await plugin.api_job_detail(job_id)
        assert detail["job"]["kind"] == "compress"
        assert [item["action"] for item in detail["items"]] == ["compressed", "retract"]
        assert detail["items"][0]["record"]["summary"] == "周六约了咖啡馆"
        assert detail["items"][1]["fact"]["content"] == "周六下午三点在咖啡馆见面"
        assert detail["items"][1]["note"] == "与上一条重复"
        assert detail["items"][1]["before"] == "周六下午三点在咖啡馆见面（旧）"
        assert detail["items"][1]["fact"]["deleted"] == 1
        # 编辑器也要能打开已撤回的事实，才能恢复
        single = await plugin.api_fact(fact_id)
        assert single["content"] == "周六下午三点在咖啡馆见面"
        assert isinstance(detail["names"], dict)
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_trash_lists_cold_and_restores(tmp_path):
    """回收站：已删除的事实/存档、冷归档都能列出来，并能还原。"""
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, {"alife": {"probability": 0.0, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        store = plugin.store
        store.capture(
            "test:dm:u",
            "t",
            [{"role": "user", "content": "周六约了咖啡馆", "users": ["test:u"], "time": 1.0}],
        )
        record = store.active("test:dm:u")[0]
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            fact_id = store._add_fact(
                db,
                "test:dm:u",
                {
                    "category": "commitment",
                    "subject": "test:u",
                    "content": "周六下午三点在咖啡馆见面",
                    "reason": "用户说的",
                    "scenario": "",
                    "tags": [],
                    "relations": [],
                    "source_ids": [record["id"]],
                },
            )
        store.observe_name("test:u", "萤火", source="admin")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE facts SET deleted=1 WHERE id=?", (fact_id,))
            db.execute("UPDATE records SET active=0,cold=1,archived_at=99 WHERE id=?", (record["id"],))

        page = await plugin.api_trash(kind="facts")
        assert page["total"] == 1
        assert page["items"][0]["content"] == "周六下午三点在咖啡馆见面"
        assert page["names"]["test:u"] == "萤火"
        assert (await plugin.api_trash(kind="facts", keyword="咖啡馆"))["total"] == 1
        assert (await plugin.api_trash(kind="facts", keyword="不存在的词"))["total"] == 0
        cold = await plugin.api_trash(kind="cold")
        assert [row["id"] for row in cold["items"]] == [record["id"]]

        restored = await plugin.api_trash_restore(
            json_request({"kind": "fact", "target": fact_id})
        )
        assert restored == {"ok": True, "restored": True}
        assert (await plugin.api_trash(kind="facts"))["total"] == 0
        assert [f["id"] for f in store.facts("test:dm:u")] == [fact_id]

        # 冷归档可以取回上下文
        brought = await plugin.api_trash_restore(
            json_request({"kind": "cold", "target": record["id"]})
        )
        assert brought == {"ok": True, "restored": True}
        assert (await plugin.api_trash(kind="cold"))["total"] == 0
        assert store.get(record["id"])["active"] == 1

        # 彻底删除：连版本一起移除，且没有记录时返回 404
        purged = await plugin.api_trash_purge(
            json_request({"kind": "fact", "target": fact_id})
        )
        assert purged == {"ok": True, "purged": True}
        assert store.facts_by_ids([fact_id], include_deleted=True) == []
        assert store.versions_of("fact", fact_id) == []
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as error:
            await plugin.api_trash_purge(
                json_request({"kind": "fact", "target": "missing"})
            )
        assert error.value.status_code == 404
    finally:
        await plugin.terminate()


def make_single_event():
    """on.im_message 收到的是**单条消息**事件（KiraMessageEvent），只有 .message。"""
    session = Session(adapter_name="test", session_type="dm", session_id="u")
    msg = KiraIMMessage(
        message_id="single",
        self_id="bot",
        chain=MessageChain([Text("我喜欢猫")]),
        timestamp=100,
        sender=User(user_id="u", nickname="小明"),
    )
    msg.message_str = "[小明] 我喜欢猫"
    return KiraMessageEvent(
        message_types=[],
        timestamp=100,
        message=msg,
        adapter=AdapterInfo(enabled=True, adapter_id="test", name="test", platform="QQ"),
    )


@pytest.mark.asyncio
async def test_prewarm_hook_accepts_single_message_event(tmp_path):
    """回归：预热钩子把单条消息事件当批量事件读 → AttributeError 刷屏（线上已报）。"""
    from test_helpers_plugin import build_plugin

    plugin, store = await build_plugin(tmp_path)
    try:
        event = make_single_event()
        await plugin.on_message_prewarm(event)  # 修复前这里抛 AttributeError
        # 预热键必须落在真实会话上（单条事件没有 .sid，得走 .session.sid）
        assert plugin._prewarm_seen.get(event.session.sid)
        assert not plugin._prewarm_seen.get("")
        # 预热是后台任务：给它一点时间跑完（最多 5 秒，全量跑时机器会慢）
        for _ in range(250):
            if plugin._memo:
                break
            await asyncio.sleep(0.02)
        # 预热必须真的预热到「注入时用的那个键」，否则只是一次白算
        assert (
            "context",
            event.session.sid,
            ("test:u",),
            plugin.settings.recall_scope,
        ) in plugin._memo
        # 两种事件形态取到同一份用户 id（预热缓存键要对得上注入时的键）
        assert module.user_ids(event) == module.user_ids(make_event()) == ["test:u"]
    finally:
        await plugin.terminate()
