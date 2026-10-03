"""Run with KIRA_CORE pointing at a real checkout; no fake core modules."""

import re
import asyncio
import importlib
import json
import os
import sys
import time
import types
from pathlib import Path
import pytest

CORE = os.environ.get("KIRA_CORE")
if not CORE:
    pytest.skip("set KIRA_CORE for actual host integration", allow_module_level=True)
sys.path.insert(0, str(Path(CORE).resolve()))
ROOT = Path(__file__).resolve().parents[1]
def _m(payload):
    """v2.18.19：注入块现在是紧凑简报 ✓ 只有一个 `m` 字段 ✓"""
    if isinstance(payload, str):
        payload = json.loads(payload)
    return payload.get("m") or ""


def _rows(payload):
    """兼容 v2.17.0 分组视图（facts 是 dict）与旧扁平视图（list）：统一成行 dict。"""
    facts = payload["facts"]
    if not isinstance(facts, dict):
        return facts
    out = []
    for code, rows in facts.items():
        for row in rows:
            item = {"u": code, "c": row[0], "x": row[1]}
            for index, key in ((2, "imp"), (3, "rel"), (4, "t"), (5, "w")):
                if len(row) > index and row[index] != "":
                    item[key] = row[index]
            out.append(item)
    return out


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
        # v2.18.19：简报不再带码 ✗ → 直接问插件"这次实际注入了哪些 id"✓（等价且更准 ✓）
        # v2.18.19：简报去码后文本里没有 id ✗
        # 插件仍记录"这一轮实际注入了哪些 id"✓（只有档案槽 ✓ related 槽由工具侧自身去重 ✓）
        first_ids = set()
        for _ids in plugin._passive_injected_ids.values():
            first_ids |= set(_ids)
        assert first_ids, "这一轮应当注入了档案（否则本用例无从验证去重 ✓）"
        req2 = LLMRequest()
        await plugin.on_request(event, req2)
        assert req.system_prompt[0].content == req2.system_prompt[0].content
        # 已经注入过的记忆不再重复返回
        tool = recall_view(await plugin.search_archive(event, keyword="猫", count=2))
        assert tool["ok"] and tool["items"]
        tool_ids = {r["i"] for r in tool["items"]}
        assert not tool_ids & first_ids
        tool2 = recall_view(await plugin.search_archive(event, keyword="猫", count=2))
        assert not {r["i"] for r in tool2["items"]} & (tool_ids | first_ids)
        # 显式重看仍然可以
        tool3 = recall_view(
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
        result = recall_view(await plugin.search_archive(event, keyword="猫"))
        assert local not in {r["id"] for r in result["items"]}
        other = plugin.store.memorize(
            "test:gm:other", "我喜欢猫的别处记忆", ["test:v"], 2.0, 2.0
        )
        await plugin.read_archive(event, other)
        result = recall_view(await plugin.search_archive(event, keyword="猫"))
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
        first = recall_view(await plugin.overview(event))
        second = recall_view(await plugin.overview(event))
        # MemoryOverview 每次最多返回 50 条新事实：先排除已送达的，再截断。
        def _total(payload):
            facts = payload["facts"]
            if isinstance(facts, dict):  # v2.17.0：按主体分组
                return sum(len(rows) for rows in facts.values())
            return len(facts)

        assert _total(first) == 50 and first["seen"] == 0
        assert _total(second) == 5 and second["seen"] == 50
        third = recall_view(await plugin.overview(event))
        assert _total(third) == 0 and third["seen"] == 55
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


def test_every_setting_has_a_chinese_label():
    """每个配置项都必须有中文 name ✓

    审计发现的空洞：`/config` 的 labels 只收录有 name 的项 ✗
    一旦漏写 name，界面就会**退回英文键名**（app.js 的 fields 兜底 ✗ 也很容易忘记同步 ✓）
    """
    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    missing = [
        k
        for k, v in schema["alife"]["fields"].items()
        if not isinstance(v, dict) or not str(v.get("name") or "").strip()
    ]
    assert not missing, "这些配置项缺 name（界面会显示键名 ✗）：%s" % ", ".join(missing)


def test_frontend_label_fallbacks_reference_real_settings():
    """app.js 里那张**兜底**标签表不许出现"配置里已经没有的键" ✗

    审计发现的空洞：`fields` 表曾是唯一的标签来源，后来 schema 的 name 成为优先项 ✓
    但没人检查这张表 ✗ → 重命名/删除配置项后它会留下**陈旧文案**（本轮就抓到一条
    "KiraOS 迁移字符上限" ✗ 而配置早已覆盖三个来源 ✓）
    """
    import json, re

    schema = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))
    known = set(schema["alife"]["fields"])
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    start = js.index("const fields = {")
    body = js[start : js.index("};", start)]
    fallbacks = set(re.findall(r"^\s{2}([a-z_]+):", body, re.M))
    stale = sorted(fallbacks - known)
    assert not stale, "app.js 兜底标签表里有已经不存在的配置项：%s" % ", ".join(stale)


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
        assert "【记忆·全局】" in _m(memory)   # 范围改成表头标记 ✓
        # v2.18.19：注入改紧凑简报 ✓ 跨会话来源渲染成"来自 …"✓ 名字进表头 ✓
        _txt = _m(memory)
        assert "来自" in _txt or "阿澄" in _txt
        assert "test:gm:noise" not in _txt, "被排除的会话不该出现在简报里"
        assert "阿澄" in _txt, "跨会话的名字应该在简报里"
        assert "阿澄" not in "".join(p.content for p in request.system_prompt)
        names = recall_view(await plugin.memory_names(event, "阿澄"))["entities"]
        result = recall_view(
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
        # 2026-09-18：召回返回改成**紧凑文本**（与被动侧同形态 ✓ 用户要求 ✓）
        #   ⇒ 直接断言文本 = 测**意图** ✓：新名字要在 ✓ 旧称呼要作为**曾用名**出现 ✓
        renamed_text = await plugin.memory_names(event, "阿澄")
        assert "阿澄的新名字" in renamed_text, renamed_text[:100]
        assert "曾用名" in renamed_text and "阿澄@" in renamed_text, renamed_text[:100]
        plugin.settings = plugin.settings.model_copy(update={"recall_scope": "session"})
        assert not recall_view(await plugin.memory_names(event, "阿澄"))["entities"]
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
                # 该用例测的是**按条**批次的重试链路 ✓ 显式声明模式（新默认是按轮 ✓）
                "compress_batch_mode": "records",
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

        record_id = recall_view(result)["id"]
        assert recall_view(await plugin.forget(event, record_id))["ok"]
        assert recall_view(await plugin.read_archive(event, record_id))["ok"]
        other = make_event()
        other.session.session_id = "other"
        assert recall_view(await plugin.read_archive(other, record_id))["ok"]
        plugin.settings = plugin.settings.model_copy(update={"recall_scope": "session"})
        assert not recall_view(await plugin.read_archive(other, record_id))["ok"]
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
            recall_view(await plugin.search_archive(event, keyword="Sunday"))["total"]
            == 1
        )
        assert any(
            "Sunday" in f["x"]
            for f in _rows(recall_view(await plugin.overview(event)))
        )
        assert (root / "core.txt").read_bytes() == original
        # Re-enabling a legacy plugin is a user choice, not a disable-loop trigger.
        manager.states[module.SOURCES[0]] = True
        assert not plugin.runtime_settings().enabled
        req = LLMRequest(user_prompt=[Prompt("new", name="message")])
        await plugin.on_request(event, req)
        assert not req.system_prompt and len(req.user_prompt) == 1
        assert (
            recall_view(await plugin.memorize(event, "do not write"))["error"]
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
async def test_partial_read_errors_skip_and_migration_completes(tmp_path):
    """线上事故回归（2026-09-23）：**一个坏文件不该让整个迁移失败**。

    KiraOS 老数据里 `entities/group_548464960/profile.json` 的 entity_id 是新
    格式（`qq:548464960`），而目录名是旧格式（纯数字）——旧实现把它当致命错误
    ⇒ 迁移失败 ⇒ 记忆永久 memory_paused（用户截图正是这个文件）。

    新行为：目录名与副本不一致按目录名继续读；真读不了的文件跳过并登记报告；
    只要该源**还有内容接上**，迁移就完成（旧插件停用、记忆可用）。
    """
    root = tmp_path / "host-data/memory"
    legacy_group = {
        "entity_id": "qq:548464960",
        "entity_type": "group",
        "name": "个人资料群 我们仨",
        "nickname": "",
        "description": "三人群聊建立: 群号548464960",
        "platform": "QQ",
        "traits": [],
        "preferences": {},
        "relationships": {},
        "facts": [],
        "aliases": [],
        "interaction_count": 0,
        "last_interaction": 1778898770.3317044,
        "metadata": {},
    }
    folder = root / "entities/group_548464960"
    folder.mkdir(parents=True)
    (folder / "profile.json").write_text(
        json.dumps(legacy_group, ensure_ascii=False), encoding="utf-8"
    )
    (folder / "facts").mkdir()
    (folder / "facts/topic.toml").write_text(
        'type = "fact"\ntext = "这个群常聊个人资料整理"\n', encoding="utf-8"
    )
    user = root / "entities/user_test%3Au/facts"
    user.mkdir(parents=True)
    (user / "good.toml").write_text(
        'type = "fact"\ntext = "好文件必须照常接上"\n', encoding="utf-8"
    )
    (user / "broken.toml").write_text('text = "unclosed', encoding="utf-8")

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
        # 迁移完成、不再暂停、旧插件确实让位
        assert not plugin.migration_blocked
        assert plugin.runtime_settings().enabled
        assert not any(manager.states.values())
        event = make_event()
        raw = await plugin.search_archive(event, keyword="个人资料群")
        assert "memory_paused" not in str(raw)          # 工具不再被暂停
        assert "个人资料群" in str(raw)                  # 那份画像照常接上
        assert "好文件必须照常接上" in str(
            await plugin.search_archive(event, keyword="好文件")
        )
        # 坏文件仍要看得见（用户可自行修复；修好会被补扫）
        status = await plugin.api_status()
        errs = [
            e
            for rep in status["migration"]["reports"]
            for e in rep.get("errors", [])
        ]
        assert [e for e in errs if "broken.toml" in e["file"]]
        assert any(e.get("detail") for e in errs), "错误要带具体原因"
        assert plugin.migration_note.startswith("迁移完成")
        assert "1 个文件无法读取" in plugin.migration_note
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
            recall_view(await plugin.search_archive(event, prompt="橘猫"))["total"] == 1
        )
        await plugin.engine.index(key, plugin.settings)
        job = await plugin.engine.enqueue("reindex", event.sid)
        for _ in range(100):
            rows = plugin.store.status()["jobs"]
            if any(j["id"] == job and j["state"] == "completed" for j in rows):
                break
            await asyncio.sleep(0.01)
        # 语义关闭时的 reindex 是**纯空转** ✓ ⇒ 现在直接删掉任务、不进工作台 ✓
        # （2026-09-17 用户要求：这类不调模型的任务不要显示 ✓）
        # 真正要守的不变量是"**没有调用任何 provider**" ✓ 见下面的断言 ✓
        rows = plugin.store.status()["jobs"]
        assert not any(j["id"] == job for j in rows), "空转任务不该留在工作台 ✗"
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

        # 2026-09-18：工具返回改成**紧凑文本** ✓ ⇒ 只改这一侧 ✓
        #   （`api_profile` 那个 **webui** 接口保持 JSON 原样 ✓ 下面的断言不动 ✓）
        text = await plugin.get_profile(event, "萤火")
        assert text.startswith("【画像】"), text[:60]
        assert "萤火对花生过敏" in text, "画像文本里必须逐条列出事实 ✓（summary 已并入 ✓）"

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
        # ★ 2026-09-23（用户拍板"≤5 都更容易让位"）：低重要度的**无关**事实不再进常驻 ✗
        #   ⇒ 排序对照改用默认档 5（新鲜 + 默认分 = 20 ≥ 阈值 15 ✓）
        low = add("test:other", "另一个人喜欢甜食", 5)
        faded = add("test:other", "另一个人的陈年琐事", 2)
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
        assert "另一个人的陈年琐事" not in block, (
            "低重要度（2）且无关的事实应当让位给轮换槽位（2026-09-23 用户拍板 ✓）"
        )
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
        _p = _m(plain)   # v2.18.19：注入是紧凑简报 ✓ 按文本检查 ✓
        assert "周六下午三点在咖啡馆见面" in _p             # 约定常驻
        assert "萤火上周去看了猫" not in _p                 # 没提到就不带
        assert "另一个人喜欢甜食" not in _p
        keys = set((plain if isinstance(plain, dict) else json.loads(plain)).keys())
        # v2.18.19：注入改成**单字段紧凑简报** ✓ `m` 本身就是短键 ✓
        assert keys == {"m"}, "注入块只放一个 m 字段（紧凑简报 ✓）"

        mentioned = await _injected_block(plugin, make_text_event("萤火最近怎么样"))
        _mm = _m(mentioned)
        assert "萤火上周去看了猫" in _mm                    # 提到人 → 带回关于他的事
        assert "另一个人喜欢甜食" not in _mm
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
        assert "另一个人喜欢甜食" in _m(block)   # v2.18.19：紧凑简报 ✓ 按文本检查 ✓
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

        tool = await plugin.get_profile(event, "test:firefly")   # 要**原文** ✓ 不加垫片
        # 2026-09-18：画像也改成**紧凑文本** ✓ ⇒ 断言改成文本形态（测的意图不变 ✓）
        assert tool.startswith("【画像】"), tool[:60]
        assert "reason" not in tool and "fingerprint" not in tool, "内部物不许出现 ✓"
        assert "[" in tool, "来源短码 src 要以 [短码] 保留 ✓（bot 要能引用来源 ✓）"

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
        # ★ 2026-09-29：宿主对「bot 不会回应」的消息给 process_strategy="discard"，
        #   钩子会**按设计提前返回** ⇒ 那样根本没走到本用例要回归的「读消息」分支 ✗
        #   ⇒ 用宿主真实 API event.buffer() 把它标成「要进缓冲」✓
        #   （process_strategy 是只读 property ✗ 不能直接赋值 ✓）
        event.buffer()
        assert event.process_strategy == "buffer"
        await plugin.on_message_prewarm(event)  # 修复前这里抛 AttributeError
        # 预热键必须落在真实会话上（单条事件没有 .sid，得走 .session.sid）
        assert plugin._prewarm_seen.get(event.session.sid)
        assert not plugin._prewarm_seen.get("")
        # 预热是后台任务：给它一点时间跑完
        # ★ 2026-09-23：5 秒（250×0.02）在全量跑、机器繁忙时会偶发超时
        #   ⇒ 放宽到 30 秒（该断言只关心"预热最终是否落到注入用的键上" ✓ 与耗时无关 ✓）
        for _ in range(1500):
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


@pytest.mark.asyncio
async def test_prewarm_skipped_when_host_will_discard(tmp_path):
    """宿主标了 discard（bot 明确不会回应）⇒ 预热钩子必须**直接返回** ✓

    这是用户实测的省钱优化：群聊里大多数消息 bot 都不回，
    若每条都预热（尤其要花钱的 JEV 预取）就是纯烧钱 ✗
    """
    from test_helpers_plugin import build_plugin

    plugin, store = await build_plugin(tmp_path)
    try:
        event = make_single_event()
        assert event.process_strategy == "discard"  # 新建事件的默认态 ✓
        await plugin.on_message_prewarm(event)
        await asyncio.sleep(0.3)
        assert not plugin._memo, "discard 的消息不该产生预热缓存 ✓（省钱 ✓）"
    finally:
        await plugin.terminate()


def test_memory_rules_per_view_are_complete_and_bounded():
    """B2：真正发出去的那份规则块按模式选 —— 必须①含齐该模式的读法词汇 ②受尺寸约束
    ③同模式逐字节稳定（前缀缓存的前提）。用**真实对象**校验 ✓ 不做文本切片 ✓"""
    grouped = module.memory_rules("grouped")
    flat = module.memory_rules("flat")
    assert isinstance(grouped, str) and isinstance(flat, str)
    # v2.18.19：注入统一为紧凑简报 ✗ 两种「事实视图」渲染一致 ✓ 说明也一致 ✓
    assert grouped == flat, "简报下两种视图应给出同一份说明"
    assert module.memory_rules(None) == grouped, "默认必须是分组视图"
    assert module.memory_rules("grouped") == grouped and module.memory_rules("flat") == flat, "同模式必须稳定"
    for token in ("记忆简报", "主体码", "序号", "来自"):
        assert token in grouped, "分组规则块缺：" + token
    for token in ("记忆简报", "主体码", "序号", "来自"):
        assert token in flat, "规则块缺：" + token
    for name, block in (("grouped", grouped), ("flat", flat)):
        assert len(block) < 900, name + " 规则块每轮全价发送，涨回去就是白花钱 ✗"


def test_every_source_is_mutually_exclusive():
    """互斥必须覆盖**全部**来源（含海马体）✓

    要求：`conflicts()` 必须是 SOURCES 驱动的 ✗ 不许写死两个 ✓
    行为：装了且启用 → 我们停用；装了但停用 → 不拦；关掉互斥开关 → 不拦。
    """
    import types as _types

    class Mgr:
        def __init__(self, installed, enabled):
            self._i, self._e = set(installed), set(enabled)

        def has_plugin(self, pid):
            return pid in self._i

        def is_plugin_enabled(self, pid):
            return pid in self._e

    class Fake:
        def __init__(self, installed, enabled, mutual=True):
            self.ctx = _types.SimpleNamespace(plugin_mgr=Mgr(installed, enabled))
            self.settings = module.Settings(mutual_exclusion=mutual)
            self.migration_blocked = False

    Fake.conflicts = module.AlifeMemoryPlugin.conflicts
    Fake.runtime_settings = module.AlifeMemoryPlugin.runtime_settings

    assert "kira_plugin_hippocampus_memory" in module.SOURCES, "海马体必须在来源表里 ✓"
    for pid in module.SOURCES:
        on = Fake([pid], [pid])
        assert module.AlifeMemoryPlugin.conflicts(on) == [pid], pid
        assert module.AlifeMemoryPlugin.runtime_settings(on).enabled is False, pid
        off = Fake([pid], [])
        assert module.AlifeMemoryPlugin.conflicts(off) == [], pid
        assert module.AlifeMemoryPlugin.runtime_settings(off).enabled is True, pid
        free = Fake([pid], [pid], mutual=False)
        assert module.AlifeMemoryPlugin.runtime_settings(free).enabled is True, pid


def test_every_setting_is_actually_used():
    """配置项不许"定义了却没人用" ✗

    暴露给用户的开关如果代码里根本没读，是最伤人的假象 ✓
    （同类病：`bot:1` 标了没人用 ✗ / `confidence` 字段只有建表语句 ✗）
    """
    names = list(module.Settings.model_fields)
    files = (
        "main.py", "storage.py", "engine.py", "retrieval.py",
        "identity.py", "migration.py", "output_validation.py",
    )
    blob = "".join((ROOT / f).read_text(encoding="utf-8") for f in files)
    unused = [k for k in names if k not in blob]
    assert not unused, "这些配置项在代码里从未被引用：%s" % ", ".join(unused)


@pytest.mark.asyncio
async def test_quiet_migrated_sessions_get_compressed_by_sweep(tmp_path, monkeypatch):
    """安静的迁移会话必须被"批量扫描"排进压缩 ✓（2026-09-17 用户实测缺口 ✓）

    自动压缩原本**只有一个触发点** ✗：``on_request`` 里按 ``probability`` 抽的那一轮 ✓
    ⇒ 迁移导入进来的旧会话（旧插件迁过来后**再没人说话** ✓）永远轮不到 ✓
    ⇒ 存档里一堆 L0 一直不动 ✓（用户实测 ✓）

    这里守住：`queue_compress_all` 要排上"真有得压"的会话 ✓ 且**不排**空会话 ✓
    """
    import time as _t

    monkeypatch.setattr(module, "get_config_path", lambda: tmp_path / "config")

    async def chat(request):
        return LLMResponse(text_response='{"summary":"迁移内容合并摘要","facts":[]}')

    async def persona():
        return types.SimpleNamespace(content="固定人格")

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        get_default_fast_llm_client=lambda: types.SimpleNamespace(chat=chat),
        persona_mgr=types.SimpleNamespace(get_persona=persona),
    )
    plugin = module.AlifeMemoryPlugin(
        ctx, # probability 必须 > 0 ✗✓ —— 启动扫描尊重「0=仅手动」✓
        {"alife": {"probability": 0.8, "audit_enabled": False}}
    )
    await plugin.initialize()
    try:
        quiet, busy = "qq:dm:769690776", "qq:gm:999"
        now = _t.time()
        with plugin.store.connect() as db:
            # 迁移形态：全 user ✓ 时间很老 ✓（import_snapshot 就是这么写的 ✓）
            for i in range(20):
                t = now - 400 * 86400 - i * 3600
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted) "
                    "VALUES(?,?,'user',0,?,?,?,?,?,?,?,?,1,0)",
                    ("m-%d" % i, quiet, t, t, "迁移内容 %d" % i, "迁移内容 %d" % i,
                     "[]", i + 1, now, "session"),
                )
            # 刚聊过、量又不够 ✓ 不该被排（否则刷一屏"本次没有需要压缩的内容" ✗）
            for i in range(3):
                t = now - i
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted) "
                    "VALUES(?,?,'user',0,?,?,?,?,?,?,?,?,1,0)",
                    ("b-%d" % i, busy, t, t, "刚聊 %d" % i, "刚聊 %d" % i,
                     "[]", i + 1, now, "session"),
                )
            db.commit()

        pending = await plugin.queue_compress_all()
        assert quiet in pending, "安静的迁移会话没被排进压缩 ✗（L0 会一直留着 ✓）"
        assert busy not in pending, "刚聊过、量不够的会话不该被排 ✗（会刷空任务 ✓）"

        # 排出去的 job 走的还是同一条 cascade ✓ 这里直接跑一次确认能压出 L1 ✓
        await plugin.engine.compress(quiet)
        rows = [r for r in plugin.store.export()["records"] if r["sid"] == quiet]
        assert any(r["level"] == 1 for r in rows), "排出来了却没压出 L1 ✗"
        assert any(r["level"] == 0 and r["active"] == 0 for r in rows), "L0 原件没归档 ✗"
    finally:
        await plugin.terminate()

@pytest.mark.asyncio
async def test_global_bucket_sessions_also_get_swept_and_yield_facts(tmp_path, monkeypatch):
    """**全局桶/伪会话**（legacy:unscoped、global、self）也必须被扫描到 ✓✓
    （2026-09-17 用户提问：迁移的记录可能压根不属于某个会话 ✓ 而是算到全局桶 ✓）

    迁移时 `sid` 兜底为 ``legacy:unscoped``（旧数据没有 session 字段 ✓）✓
    它们同样是 records 表里的 sid ✓ ⇒ 批量扫描用 ``SELECT DISTINCT sid`` ✓ 天然覆盖 ✓
    这里守住两件事：
      ① `queue_compress_all` 必须把这类 sid 排上 ✓（否则它们的 L0 永远压不了 ✓）
      ② 压缩**确实会把这些记录提炼成事实**落库 ✓（这是"要生效"的最终含义 ✓）
    """
    monkeypatch.setattr(module, "get_config_path", lambda: tmp_path / "config")

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        get_default_fast_llm_client=lambda: types.SimpleNamespace(chat=None),
        persona_mgr=types.SimpleNamespace(get_persona=lambda: types.SimpleNamespace(content="p")),
    )
    plugin = module.AlifeMemoryPlugin(ctx, # probability 必须 > 0 ✗✓ —— 启动扫描尊重「0=仅手动」✓
        {"alife": {"probability": 0.8, "audit_enabled": False}})
    await plugin.initialize()
    try:
        bucket = "legacy:unscoped"
        now = time.time()
        with plugin.store.connect() as db:
            ids = []
            for i in range(20):
                t = now - 400 * 86400 - i * 3600
                rid = "gb-%d" % i
                ids.append(rid)
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted) "
                    "VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0)",
                    (rid, bucket, "user", t, t, "迁移内容 %d" % i, "迁移内容 %d" % i,
                     "[]", i + 1, now, "session"),
                )
            db.commit()

        pending = await plugin.queue_compress_all()
        assert bucket in pending, "全局桶 session 没被扫描到 ✗（它的 L0 将永远压不出事实 ✗）"

        # 压缩 → 事实落库 ✓
        rows = await plugin.store.call("active", bucket)
        plan = module.compression_plan(rows, plugin.runtime_settings(), boost_allowed=True)
        assert plan, "全局桶拿不到压缩计划 ✗"
        picked, level = plan[0], plan[1]
        await plugin.store.call(
            "compress", bucket, picked, level - 1,
            {"summary": "迁移内容合并摘要",
             "facts": [{"category": "preference", "subject": "u-zhou",
                        "content": "喜欢喝拿铁", "reason": "用户说过", "scenario": "",
                        "tags": [], "relations": [], "source_ids": [picked[0]["id"]]}]},
        )
        facts = plugin.store.facts(bucket, limit=10)
        assert facts, "压缩没有把 L0 提炼成事实 ✗（这正是迁移记录存在的意义 ✗）"
        assert any("拿铁" in f.get("content", "") for f in facts), "事实内容不对 ✗"
    finally:
        await plugin.terminate()

@pytest.mark.asyncio
async def test_sink_decoupled_from_rotation_and_bonus_for_hits(tmp_path):
    """★ 2026-09-23：下沉**与轮换开关解耦**（阈值 0 才是唯一的关闭开关）✓

    之前把下沉绑在 `rotate_enabled` 上 ⇒ 关掉轮换就完全不下沉 ✗
    而下沉的本意是"把常驻预算让给更重要的事实"，与轮换无关 ✓
    （沉下的仍可被相关性召回 ⇒ 不会消失 ✓）
    顺带钉住：本轮**命中**的事实按 +4 分算 ✓（相关优先，但不是豁免 ✓）
    """
    import json as _json
    import time as _time

    plugin = await _diet_plugin(tmp_path, rotate_enabled=False, top_k=30)
    try:
        now = _time.time()
        event = make_text_event("萤火最近怎么样")
        plugin.store.capture(event.sid, "t", [
            {"role": "user", "content": "来源", "users": ["test:firefly"], "time": now},
        ])
        record = plugin.store.active(event.sid)[0]
        plugin.store.observe_name("test:firefly", "萤火", source="admin")

        def add(subject, content, importance, age_days=0):
            fid = _add_fact(plugin, event.sid, subject, "preference", content,
                            importance, record["id"])
            if age_days:
                with plugin.store.connect() as db:
                    db.execute("UPDATE facts SET created=? WHERE id=?",
                               (now - age_days * 86400, fid))
            return fid

        add("test:other", "陈年琐事一条", 2)                  # 低重要度 + 全新 ⇒ 让位
        add("test:other", "很久前的旧偏好", 2, 50)            # 低重要度 + 老 ⇒ 让位
        add("test:other", "默认档的新偏好", 5)                # 默认档 + 新 ⇒ 留住
        add("test:firefly", "萤火立下的重要约定", 8, 300)      # ≥8 + 很老 ⇒ 永不沉
        add("test:firefly", "萤火刚说的小偏好", 2)            # 低 + 命中 ⇒ 靠 +4 留住

        block = _json.dumps(await _injected_block(plugin, event), ensure_ascii=False)
        assert "默认档的新偏好" in block
        assert "萤火立下的重要约定" in block, "重要度 ≥8 永不沉 ✓"
        assert "萤火刚说的小偏好" in block, "命中的低重要度事实靠 +4 分留住 ✓"
        assert "陈年琐事一条" not in block, "轮换关着也照样下沉（解耦 ✓）"
        assert "很久前的旧偏好" not in block
    finally:
        await plugin.terminate()


def recall_view(raw):
    """召回返回现在可能是**紧凑文本**（2026-09-18 用户要求 ✓ 与被动侧同形态 ✓）
    ⇒ 这里把它还原成 dict 供**老断言**复用 ✓（只服务测试 ✓ 生产不发 JSON 给模型 ✓）
    文本形如：
      【召回】命中 619 · 本次 2 · 已见过 27
      n1=周武 n2=爱奈丽
      4khyio 09-18 18:18 ★7 L2 bot mem @g3 爱奈丽｜有的，CodeBuddy…
      （hint…）
    """
    try:
        return json.loads(raw)
    except Exception:
        pass
    out = {"items": [], "who": {}, "hint": "", "text": raw, "ok": True,
           "entities": [], "names": [], "archives": []}
    for line in str(raw).splitlines():
        if line.startswith("【召回】"):
            m = re.search(r"命中 (\d+) · 本次 (\d+)", line)
            if m:
                out["total"] = int(m.group(1))
            m2 = re.search(r"已见过 (\d+)", line)
            if m2:
                out["seen"] = int(m2.group(1))
        elif "｜" in line:
            head, _sep, snippet = line.partition("｜")
            parts = head.split()
            item = {"i": parts[0] if parts else "",
                    "t": parts[1] if len(parts) > 1 else "", "s": snippet}
            for token in parts[2:]:
                if token.startswith("★"):
                    item["k"] = int(token[1:])
                elif token.startswith("L") and token[1:].isdigit():
                    item["l"] = int(token[1:])
                elif token in ("bot", "mem", "arch"):
                    item[token] = 1
                elif token.startswith("@"):
                    item["from"] = token[1:]
                else:
                    item.setdefault("sp", token)
            out["items"].append(item)
        elif line.startswith("【人物与群名】") or line.startswith("【画像】"):
            # 「i=名字 [rN]（曾用名 …）」逐个还原 ✓ 同时给新旧两套键 ✓（老断言照旧可用 ✓）
            body = line.split("】", 1)[1]
            target = out["entities"] if line.startswith("【人物与群名】") else out["names"]
            for one in body.split(" · "):
                if "=" not in one:
                    continue
                key, _eq, rest = one.partition("=")
                # 名字里**剥掉** revision 记号 [rN] 与曾用名括注 ✓
                name = re.split(r"\s*\[r\d+\]", rest.split("（", 1)[0])[0].strip()
                item = {"i": key, "n": name, "id": key, "name": name}
                m = re.search(r"\[r(\d+)\]", rest)
                if m:
                    item["r"] = item["revision"] = int(m.group(1))
                if "曾用名" in rest:
                    item["aliases"] = rest.split("曾用名 ", 1)[1].rstrip("）").split("、")
                    item["h"] = list(item["aliases"])
                target.append(item)
                if line.startswith("【画像】"):
                    out["entities"].append(item)
        elif line.startswith("（"):
            out["hint"] = line.strip("（）")
        elif line.startswith("【人物与群名】") or line.startswith("【画像】"):
            # 「i=名字（曾用名 …）」逐个还原 ✓ 新旧两套键都给 ✓（老断言照旧可用 ✓）
            body = line.split("】", 1)[1]
            target = out["entities"] if line.startswith("【人物与群名】") else out["names"]
            for one in body.split(" · "):
                if "=" not in one:
                    continue
                key, _eq, rest = one.partition("=")
                # 名字里要**剥掉** revision 记号 [rN] ✓（否则断言拿到"阿澄的新名字 [r3]" ✗）
                name = re.split(r"\s*\[r\d+\]", rest.split("（", 1)[0])[0].strip()
                item = {"i": key, "n": name, "id": key, "name": name}
                m = re.search(r"\[r(\d+)\]", rest)      # 文本里的 revision 记号 [rN] ✓
                if m:
                    item["r"] = item["revision"] = int(m.group(1))
                if "曾用名" in rest:
                    item["aliases"] = rest.split("曾用名 ", 1)[1].rstrip("）").split("、")
                    item["h"] = list(item["aliases"])
                target.append(item)
        elif "=" in line and not line.startswith("【"):
            for pair in line.split():
                if "=" in pair:
                    k, _eq, v = pair.partition("=")
                    out["who"][k] = v
    return out
