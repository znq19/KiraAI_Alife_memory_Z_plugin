"""v2.22.25：common 分类、独立块、scope、永久展示与审计分类修正。"""

import asyncio
import importlib
import os
import sys
import types
from pathlib import Path

import pytest

CORE = os.environ.get("KIRA_CORE")
if not CORE:
    pytest.skip("需要 KIRA_CORE 的宿主集成用例", allow_module_level=True)
sys.path.insert(0, str(Path(CORE).resolve()))
ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_common_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_common_test", package)
module = importlib.import_module("alife_common_test.main")
contracts = importlib.import_module("alife_common_test.contracts")

from core.provider import LLMRequest
from core.agent.message import OpenAIMessage
from core.prompt_manager import Prompt
from core.chat import MessageChain
from core.chat.message_elements import Text
from core.chat.message_utils import KiraIMMessage, KiraMessageBatchEvent
from core.chat.session import User, Session

SID = "test:gm:common"


def make_event(text="还记得世界常识吗"):
    session = Session(adapter_name="test", session_type="gm", session_id="common")
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


def build_plugin(tmp_path, **settings):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    payload = {
        "probability": 0.0,
        "audit_enabled": False,
        "rotate_enabled": False,
        "fact_merge_enabled": False,
        **settings,
    }
    plugin = module.AlifeMemoryPlugin(ctx, {"alife": payload})
    asyncio.run(plugin.initialize())
    return plugin


def add_fact(plugin, category, subject, content, *, sid=SID,
             importance=8, common_scope="", evidence="当时的一句闲聊"):
    rid = plugin.store.memorize(sid, evidence, ["test:u"], 1.0, 1.0)
    with plugin.store.connect() as db:
        plugin.store._add_fact(
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
                "common_scope": common_scope,
            },
        )


def inject(plugin, text="还记得世界常识吗"):
    event = make_event(text)
    req = LLMRequest(
        messages=[OpenAIMessage(role="user", content="原历史")],
        system_prompt=[Prompt("稳定人格与工具说明", name="stable")],
        user_prompt=[Prompt(text, name="message")],
    )
    asyncio.run(plugin.on_request(event, req))
    req.assemble_prompt()
    return req.messages[-1].content or ""


def test_legend_is_static_and_grouped_only():
    grouped = module.memory_rules("grouped")
    flat = module.memory_rules("flat")
    assert "类别码：ev=事件" in grouped
    assert "cm=常识" in grouped and "rl=铁律" in grouped
    assert "类别码：ev=事件" not in flat
    assert "【常识】块是世界知识" in grouped and "【常识】块是世界知识" in flat
    assert grouped.index("类别码：ev=事件") < grouped.index("要精确到分钟")


def test_common_world_fact_is_split_into_common_block(tmp_path):
    plugin = build_plugin(tmp_path)
    add_fact(plugin, "common", "世界", "世界常识：模型版本会变化", common_scope="world")
    add_fact(plugin, "profile", "小明", "小明喜欢茶", importance=7)
    block = inject(plugin, "你还记得世界常识吗")
    assert "【常识｜世界】※" in block
    assert "cm 世界常识：模型版本会变化" in block
    assert "小明喜欢茶" in block
    assert "【事实】" in block
    # 不重复：同一条 common 不应同时出现在普通 facts 区域
    assert block.count("世界常识：模型版本会变化") == 1


def test_common_field_allow_puts_other_session_in_normal_facts(tmp_path):
    """默认 allow：其他会话 field 不拎进常识块，按普通事实块 + @会话 展示。"""
    plugin = build_plugin(tmp_path)
    add_fact(
        plugin, "common", "本群", "本群约定：周五晚点名",
        common_scope="field", sid="test:gm:other",
    )
    block = inject(plugin, "你还记得本群约定吗")
    assert "周五晚点名" in block
    assert "【常识｜场域】" not in block
    assert "@" in block


def test_common_field_filter_drops_other_session(tmp_path):
    plugin = build_plugin(tmp_path, common_field_mode="filter")
    add_fact(
        plugin, "common", "本群", "本群约定：周五晚点名",
        common_scope="field", sid="test:gm:other",
    )
    block = inject(plugin, "你还记得本群约定吗")
    assert "周五晚点名" not in block


def test_common_field_lift_puts_other_session_in_common_block(tmp_path):
    plugin = build_plugin(tmp_path, common_field_mode="lift")
    add_fact(
        plugin, "common", "本群", "本群约定：周五晚点名",
        common_scope="field", sid="test:gm:other",
    )
    block = inject(plugin, "你还记得本群约定吗")
    assert "【常识｜场域】※" in block
    assert "周五晚点名" in block
    assert "@" in block


def test_common_field_fact_is_visible_in_own_session(tmp_path):
    plugin = build_plugin(tmp_path)
    add_fact(plugin, "common", "本群", "本群约定：周五晚点名", common_scope="field")
    block = inject(plugin, "你还记得本群约定吗")
    assert "【常识｜场域】※" in block
    assert "周五晚点名" in block


def test_permanent_memory_shows_category_and_importance_without_placeholder(tmp_path):
    plugin = build_plugin(tmp_path)
    plugin.store.memorize(SID, "必须记住的约束", ["test:u"], 1.0, 1.0, 8, "profile")
    plugin.store.memorize(SID, "铁律约束", ["test:u"], 1.0, 1.0, 7, "rule")
    plugin.store.memorize(SID, "没有类别的约束", ["test:u"], 1.0, 1.0, 9, "")
    block = inject(plugin, "你好")
    assert "* pf ★8" in block
    assert "* rl ★7" in block
    assert "* ★9" in block
    assert "未分类" not in block


def test_audit_category_only_correct_preserves_content(tmp_path):
    plugin = build_plugin(tmp_path)
    add_fact(plugin, "fact", "世界", "这是一条待归类的事实")
    fact = plugin.store.facts(SID, limit=10)[0]
    before = fact["content"]
    result = plugin.store.audit(
        [fact],
        {
            "actions": [{
                "action": "correct",
                "target_id": fact["id"],
                "source_ids": [fact["id"]],
                "content": "",
                "category": "common",
                "reason": "归为常识",
            }]
        },
    )
    after = plugin.store.facts_by_ids([fact["id"]])[0]
    assert after["category"] == "common"
    assert after["content"] == before
    assert result["category_fixed"] == 1


def _add_evidence_fact(plugin, category, subject, content, importance=8,
                       common_scope=""):
    add_fact(plugin, category, subject, content,
             importance=importance, common_scope=common_scope)


def test_common_has_no_category_top_k_cap(tmp_path):
    plugin = build_plugin(tmp_path)
    for i in range(5):
        _add_evidence_fact(
            plugin, "common", "世界", "常识%d：贝塔" % i, common_scope="world",
        )
    block = inject(plugin, "贝塔")
    assert "【常识｜世界】※" in block
    for i in range(5):
        assert "常识%d：贝塔" % i in block, "common 不应被 3 条上限截断"


def test_common_compensation_refills_normal_facts(tmp_path):
    plugin = build_plugin(tmp_path, common_compensate_enabled=True,
                          common_compensate_ratio=0.5, common_compensate_max=5)
    for i in range(2):
        _add_evidence_fact(
            plugin, "common", "世界", "常识%d：阿尔法" % i,
            common_scope="world",
        )
    for i in range(4):
        _add_evidence_fact(plugin, "fact", "世界", "普通%d：阿尔法" % i)
    block = inject(plugin, "阿尔法")
    assert "【常识｜世界】※" in block
    assert block.count("常识") >= 2
    # 2 条 common → ceil(1) 条普通补回；第 4 条普通事实本来不在主召回里
    for i in range(4):
        assert "普通%d：阿尔法" % i in block, "补回丢失：普通%d" % i


def test_rotation_common_has_same_common_block_without_marker(tmp_path):
    plugin = build_plugin(tmp_path, rotate_enabled=True, rotate_count=3,
                          fact_recall_min_score=2)
    _add_evidence_fact(plugin, "common", "世界", "常识：欧米伽",
                       common_scope="world")
    block = inject(plugin, "欧米伽")
    assert "【常识｜世界】※" in block
    assert "轮换" not in block


def test_audit_category_is_only_for_correct():
    with pytest.raises(Exception):
        contracts.AuditAction(
            action="keep",
            target_id="f1",
            source_ids=["f1"],
            reason="keep",
            category="common",
        )


def test_common_merge_preserves_scope_and_rejects_cross_scope(tmp_path):
    plugin = build_plugin(tmp_path)
    add_fact(plugin, "common", "世界", "合并甲", common_scope="world")
    add_fact(plugin, "common", "世界", "合并乙", common_scope="world")
    ids = [f["id"] for f in plugin.store.facts(SID, limit=10)]
    assert len(ids) == 2
    target = plugin.store.merge_facts(ids[0], [ids[1]], "合并后", "合并")
    row = plugin.store.facts_by_ids([target])[0]
    assert row["common_scope"] == "world"
    add_fact(plugin, "common", "世界", "合并丙", common_scope="field")
    third = [f["id"] for f in plugin.store.facts(SID, limit=10) if f["id"] != target][0]
    with pytest.raises(ValueError):
        plugin.store.merge_facts(target, [third], "污染", "跨作用域")


def test_get_profile_marks_cross_session_common_source(tmp_path):
    plugin = build_plugin(tmp_path)
    plugin.store.observe_name("test:u", "小明", observed=1.0)
    plugin.store.observe_name(
        "test:gm:other", "别的群", kind="session", observed=1.0
    )
    add_fact(
        plugin, "common", "test:u", "别的群约定：周五晚点名",
        common_scope="field", sid="test:gm:other",
    )
    event = make_event("看看小明")
    out = asyncio.run(plugin.get_profile(event, subject="test:u"))
    assert "别的群约定：周五晚点名" in out
    assert "@" in out, "主动画像必须标出来源会话"
    assert "会话码：" in out and "别的群" in out, "短码必须有可解析的会话名图例"


def test_common_scope_round_trips_through_storage(tmp_path):
    plugin = build_plugin(tmp_path)
    add_fact(plugin, "common", "世界", "字段往返", common_scope="field")
    fact = plugin.store.facts(SID, limit=10)[0]
    assert fact["common_scope"] == "field"
    parsed = contracts.Fact(**{
        "category": "common",
        "subject": "世界",
        "content": "字段往返",
        "reason": "",
        "scenario": "",
        "tags": [],
        "relations": [],
        "source_ids": ["r1"],
        "importance": 8,
        "common_scope": "field",
    })
    assert parsed.common_scope == "field"
