import asyncio
import importlib
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
package = types.ModuleType("alife_recall_host_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_recall_host_test", package)
module = importlib.import_module("alife_recall_host_test.main")
from core.provider import LLMRequest
from core.agent.message import OpenAIMessage
from core.prompt_manager import Prompt
from core.chat import MessageChain
from core.chat.message_elements import Text
from core.chat.message_utils import KiraIMMessage, KiraMessageBatchEvent
from core.chat.session import User, Session


@pytest.fixture(autouse=True)
def isolated_legacy_root(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "get_data_path", lambda: tmp_path / "host-data")


def make_event(text="你师傅是谁"):
    session = Session(adapter_name="test", session_type="dm", session_id="u")
    msg = KiraIMMessage(
        message_id="one",
        self_id="bot",
        chain=MessageChain([Text(text)]),
        timestamp=100,
        sender=User(user_id="u", nickname="小明"),
    )
    msg.message_str = f"[小明] {text}"
    return KiraMessageBatchEvent(
        messages=[msg], session=session, timestamp=100, message_types=[]
    )


def build_plugin(tmp_path, **settings):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    payload = {"probability": 0.0, "audit_enabled": False, **settings}
    return module.AlifeMemoryPlugin(ctx, {"alife": payload})


def add_fact(plugin, sid, category, subject, content, evidence="当时的一句闲聊"):
    rid = plugin.store.memorize(sid, evidence, ["test:u"], 1.0, 1.0)
    with plugin.store.connect() as db:
        plugin.store._add_fact(
            db,
            sid,
            dict(
                category=category,
                subject=subject,
                content=content,
                reason="",
                scenario="",
                tags=[],
                relations=[],
                source_ids=[rid],
            ),
        )


def archive_other_session(plugin, texts, summary):
    """在别的会话造存档并压缩：children 变成 active=0 的历史归档（不是冷归档）。"""
    sid = "test:gm:other"
    plugin.store.capture(
        sid,
        "turn",
        [
            {"role": "user", "content": text, "time": float(i) + 2, "users": ["test:v"]}
            for i, text in enumerate(texts)
        ],
    )
    rows = plugin.store.active(sid)
    plugin.store.compress(sid, rows, 0, {"summary": summary, "facts": []})
    folded = plugin.store.get(rows[0]["id"])
    assert folded["active"] == 0 and folded["cold"] == 0, "应是历史归档而非冷归档"
    return folded


async def inject(plugin, event, text):
    req = LLMRequest(
        messages=[OpenAIMessage(role="user", content="原历史")],
        system_prompt=[Prompt("稳定人格与工具说明", name="stable")],
        user_prompt=[Prompt(text, name="message")],
    )
    await plugin.on_request(event, req)
    req.assemble_prompt()
    return req.messages[-1].content


@pytest.mark.asyncio
async def test_passive_recall_brings_fact_by_content_only(tmp_path):
    """消息里只有「师傅」两字，也要把「我师傅是星月」这条 relationship 事实带进来。"""
    plugin = build_plugin(tmp_path)
    await plugin.initialize()
    try:
        event = make_event()
        add_fact(plugin, event.sid, "relationship", "test:u", "我师傅是星月")
        assert '"我师傅是星月"' in await inject(plugin, event, "你师傅是谁")
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_recall_threshold_is_configurable(tmp_path):
    """门槛调高会把只命中一个双字片段的事实挡在门外。"""
    plugin = build_plugin(tmp_path)
    await plugin.initialize()
    try:
        event = make_event()
        # 主体不是发言人：排除实体通道（消息文本里带昵称「小明」），只考验内容匹配
        add_fact(plugin, event.sid, "event", "test:v", "师傅上周来工作室看了作品")
        mark = '"师傅上周来工作室看了作品"'
        assert mark in await inject(plugin, event, "你师傅是谁")
        plugin.settings.fact_recall_min_score = 4
        assert mark not in await inject(plugin, event, "你师傅是谁")
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_passive_recall_includes_archived_other_session(tmp_path):
    """别的会话里已离开上下文的原文，默认也参与词面召回；开启「只搜常驻」则不参与。"""
    plugin = build_plugin(tmp_path)
    await plugin.initialize()
    try:
        event = make_event()
        archive_other_session(
            plugin,
            ["我师傅是星月，他跟了三年", "顺便聊了点别的"],
            summary="聊了些旧事",
        )
        block = await inject(plugin, event, "你还记得我师傅吗")
        assert '"s":"我师傅是星月，他跟了三年"' in block
        plugin.settings.search_active_only = True
        assert '"s":"我师傅是星月，他跟了三年"' not in await inject(
            plugin, event, "你还记得我师傅吗"
        )
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_soft_deleted_fact_and_cold_archive_stay_out(tmp_path):
    """软删的事实与冷归档都不参与被动召回。"""
    plugin = build_plugin(tmp_path)
    await plugin.initialize()
    try:
        event = make_event()
        add_fact(plugin, event.sid, "relationship", "test:u", "我师傅是星月")
        fact = plugin.store.facts(event.sid, global_scope=True, users=["test:u"])[0]
        plugin.store.edit("fact", fact["id"], fact["revision"], {"deleted": True}, "撤回")
        assert '"我师傅是星月"' not in await inject(plugin, event, "你师傅是谁")

        perm = plugin.store.memorize(event.sid, "我师傅是星月", ["test:u"], 3.0, 3.0)
        kept = plugin.store.memorize(event.sid, "今天天气不错", ["test:u"], 4.0, 4.0)
        plugin.store.merge_records(kept, [perm, kept], "今天天气不错", "合并相似永久记忆")
        assert plugin.store.get(perm)["cold"] == 1
        assert perm not in await inject(plugin, event, "我师傅是星月")
    finally:
        await plugin.terminate()


@pytest.mark.asyncio
async def test_injection_survives_empty_query_and_session_scope(tmp_path):
    """空消息 / recall_scope=session 时注入不能崩（v2.7.0 曾漏掉 related_rows 初始化）。"""
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
            "test:dm:u", "turn",
            [{"role": "user", "content": "随手记一句", "time": 1.0, "users": ["test:u"]}],
        )
        store.capture(
            "test:gm:other", "turn",
            [{"role": "user", "content": "别的会话", "time": 2.0, "users": ["test:v"]}],
        )
        # ① 事件里没有消息（query 为空）
        event = types.SimpleNamespace(
            sid="test:dm:u", event_id="e", messages=[], self_id="bot",
            session=types.SimpleNamespace(adapter_name="test", session_title="私聊"),
        )
        req = LLMRequest(messages=[], system_prompt=[], user_prompt=[])
        await plugin.on_request(event, req)
        assert any(p.name == "alife_memory" for p in req.user_prompt)

        # ② recall_scope=session（跳过相关存档召回）
        plugin.settings.recall_scope = "session"
        req = LLMRequest(messages=[], system_prompt=[], user_prompt=[])
        await plugin.on_request(event, req)
        assert any(p.name == "alife_memory" for p in req.user_prompt)
    finally:
        await plugin.terminate()
