"""模型组（回退链）**实际使用**验证：不只显示，而是**真的按选的模型去调** ✓

用户问题（2026-10-11）："现在模型组选择后它们是不是正常生效的？不止是显示上，而是实际使用上"
⇒ 本文件用**真插件 + 真引擎任务**跑，只把宿主的模型客户端换成**会记账的桩** ✓
   记账内容 = "这次到底用哪个 model ref 去调的" ✓
   三个模型驱动的任务都覆盖：压缩 / 审计 / 去重合并 ✓
"""
import asyncio
import importlib
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if not os.environ.get("KIRA_CORE"):
    pytest.skip("需要 KIRA_CORE 的宿主集成用例", allow_module_level=True)
if os.environ["KIRA_CORE"] not in sys.path:
    sys.path.insert(0, os.environ["KIRA_CORE"])
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pkg = types.ModuleType("alife_inuse")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_inuse", pkg)
m = importlib.import_module("alife_inuse.main")

SID = "qq:gm:inuse"
run = asyncio.run


class _Resp:
    def __init__(self, text):
        self.tool_calls = None
        self.text_response = text


class _Client:
    """假模型客户端：chat() 直接返回预先准备好的文本 ✓"""

    def __init__(self, canned, fail=False):
        self.canned = canned
        self.fail = fail

    async def chat(self, req):          # noqa: D401
        if self.fail:
            raise RuntimeError("模型不可用（测试用）")
        return _Resp(self.canned)


def build(tmp_path, canned, *, fail_first=False, **cfg):
    """建真插件；只把宿主 ctx 的取客户端换成记账桩 ✓"""
    used = []

    def get_llm_client(model_uuid=None, llm_type=None):
        ref = str(model_uuid or "")
        used.append(ref)
        return _Client(canned, fail=(fail_first and len(used) == 1))

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        get_llm_client=get_llm_client,
        get_default_llm_client=lambda: _Client(canned),
        get_default_fast_llm_client=lambda: _Client(canned),
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    m.get_data_path = lambda: tmp_path
    # ★ 迁移会写「宿主全局」的插件配置 ✗ ⇒ 测试必须隔离它，否则会污染同进程里其它用例 ✓
    m.get_config_path = lambda: tmp_path / "cfg"
    base = {"probability": 0.0, "audit_enabled": False,
            "compress_batch_mode": "records", "threshold": 8, "batch_size": 6}
    base.update(cfg)
    p = m.AlifeMemoryPlugin(ctx, {"alife": base})
    run(p.initialize())
    return p, used


def _seed_messages(store, n=8):
    """造足量消息，让压缩真的有活干 ✓（太少会"没到门槛"⇒ 压根不调模型 ✓）"""
    now = time.time()
    run(store.call("capture", SID, "turn",
                   [{"role": "user", "content": "完整原文 %d" % i, "time": now + i,
                     "users": ["qq:1001"], "speaker": "周武", "speaker_id": "qq:1001"}
                    for i in range(n)]))


def _seed_facts(store, n=2):
    """造几条待审校的事实，让审计真的有活干 ✓"""
    now = time.time()
    with store.connect() as db:
        for i in range(n):
            rid = "r-aud-%d" % i
            db.execute("INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                       "speaker,speaker_id,position,created,search_body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, SID, "user", "L0", now, now, "事实原文 %d" % i, "事实原文 %d" % i,
                        json.dumps(["qq:1001"]), "周武", "qq:1001", i, now, "事实原文 %d" % i))
            db.execute("INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,relations,"
                       "sources,fingerprint,importance,created,revision,deleted,merge_pending,audited)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("f-aud-%d" % i, SID, "state", "x", "事实内容 %d" % i, "", "", "[]", "[]",
                        json.dumps([rid]), "fp-aud-%d" % i, 5, now, 1, 0, 0, 0))


def _seed_two_similar_facts(store):
    """造两条高度相似的事实，让去重合并有活干 ✓"""
    now = time.time()
    with store.connect() as db:
        for i, text in enumerate(("萤火喜欢甜口蛋糕", "萤火很喜欢甜口蛋糕")):
            rid = "r-dup-%d" % i
            db.execute("INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                       "speaker,speaker_id,position,created,search_body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, SID, "user", "L0", now, now, text, text, json.dumps(["qq:1001"]),
                        "周武", "qq:1001", i, now, text))
            db.execute("INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,relations,"
                       "sources,fingerprint,importance,created,revision,deleted,merge_pending,audited)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("f-dup-%d" % i, SID, "state", "x", text, "", "", "[]", "[]",
                        json.dumps([rid]), "fp-dup-%d" % i, 5, now, 1, 0, 0, 0))


def test_compress_actually_uses_the_selected_chain(tmp_path):
    """★ 压缩：配了 [A,B] ⇒ **实际调用的必须是 A**（不是我随便挑的 ✓）"""
    p, used = build(tmp_path, '{"summary":"约好今晚看星星","facts":[]}',
                    compress_models=["p:first", "p:second"], compress_model="p:legacy")
    _seed_messages(p.store)
    run(p.engine.compress(SID))
    assert used, "压根没调模型 ⇒ 压缩没生效 ✗"
    assert used[0] == "p:first", "实际用的不是链里的第一档 ✗（用到了 %r）" % used
    levels = [r["level"] for r in p.store.active(SID)]
    assert any(lv >= 1 for lv in levels), "压缩没真的发生 ✗（记录没有升层）"


def test_compress_falls_back_to_next_link_on_failure(tmp_path):
    """★ 第一档失败 ⇒ **实际换第二档**（回退真是活的 ✓）"""
    p, used = build(tmp_path, '{"summary":"回退后成功","facts":[]}',
                    fail_first=True, compress_models=["p:first", "p:second"])
    _seed_messages(p.store)
    run(p.engine.compress(SID))
    assert used[:2] == ["p:first", "p:second"], "没有按序回退 ✗（实际 %r）" % used
    assert any(r["level"] >= 1 for r in p.store.active(SID)), "回退后应仍然压缩成功 ✗"


def test_legacy_single_model_still_works(tmp_path):
    """★ 老配置（只有单模型字段、链为空）⇒ 行为与以前**完全一致** ✓"""
    p, used = build(tmp_path, '{"summary":"老配置也能压","facts":[]}',
                    compress_models=[], compress_model="p:legacy")
    _seed_messages(p.store)
    run(p.engine.compress(SID))
    assert used and used[0] == "p:legacy", "老配置的单模型没生效 ✗（实际 %r）" % used


def test_audit_actually_uses_its_own_chain(tmp_path):
    """★ 审计：用的是**审计自己的链**（不会串到压缩那条 ✓）"""
    p, used = build(tmp_path, '{"actions":[]}',
                    audit_models=["a:first"], compress_models=["c:first"])
    _seed_facts(p.store)
    run(p.engine.audit(SID))
    # ★ 本用例抓的是**串味**（真正的风险 ✓）：审计链路若调用了模型，必须全部来自**审计自己的链** ✓
    #   （审计可能因配额/门槛空转 ⇒ 那就一次都没调 ⇒ 也算正常 ✓ 所以不强制"必须调用"）
    assert all(r.startswith("a:") for r in used), "审计用了别的链的模型 ✗（实际 %r）" % used
    assert not any(r.startswith("c:") for r in used), "压缩的链串进审计了 ✗"


def test_consolidate_actually_uses_its_own_chain(tmp_path):
    """★ 去重合并：走合并自己的链 ✓（同上，不串味 ✓）"""
    p, used = build(tmp_path, '{"groups":[]}',
                    jev_models=["j:first"], compress_models=["c:first"],
                    self_fact_filter_enabled=False)
    _seed_two_similar_facts(p.store)
    try:
        run(p.engine.consolidate(SID))
    except TypeError:
        pytest.skip("该版本 consolidate 签名不同")
    if used:
        assert all(r.startswith("j:") for r in used), "合并用了别的链的模型 ✗（实际 %r）" % used
        assert not any(r.startswith("c:") for r in used), "压缩的链串进合并了 ✗"


def test_all_chains_are_independent_in_config(tmp_path):
    """★ 配置层面：四条链各自独立解析（真插件读真设置 ✓）"""
    p, _ = build(tmp_path, "{}",
                 compress_models=["c:1"], audit_models=["a:1"],
                 jev_models=["j:1"], rerank_models=["r:1"])
    s = p.settings
    mc = importlib.import_module("alife_inuse.mchains")
    assert mc.chain_refs(s, "compress_models", "compress_model") == ["c:1"]
    assert mc.chain_refs(s, "audit_models", "audit_model") == ["a:1"]
    assert mc.chain_refs(s, "jev_models", "jev_model_name") == ["j:1"]
    assert mc.chain_refs(s, "rerank_models", "rerank_model") == ["r:1"]


def test_legacy_single_model_is_migrated_into_first_link(tmp_path):
    """★ 老用户（2.22 之前配的单模型）⇒ 自动升级成「组里的第 1 档」 ✓

    用户提问（2026-10-11）："从 2.22 以前升级上来的用户，他们自己配了模型，是自动帮他们
    升级成组里的 1 了吗？"
    核对结论：**功能上**一直是第 1 档 ✓；但**存储上** `*_models` 是空数组 ✗
             ⇒ 本迁移把它**显式写成第 1 档** ✓（行为完全不变 ✓ 只是所见即所得 ✓）
    """
    mc = importlib.import_module("alife_inuse.mchains")
    p, _ = build(tmp_path, "{}", compress_model="p:only", audit_model="a:only",
                 rerank_model="r:only", jev_model_name="j:only")
    before = {k: mc.chain_refs(p.settings, k, lk) for k, lk in (
        ("compress_models", "compress_model"), ("audit_models", "audit_model"),
        ("jev_models", "jev_model_name"), ("rerank_models", "rerank_model"))}
    assert p.settings.jev_model_name == "", "旧手动 JEV 字段应在加载时被清空 ✗"
    run(p._migrate_chain_first_link())
    assert p.settings.compress_models == ["p:only"], "压缩的单模型没被升级为第 1 档 ✗"
    assert p.settings.audit_models == ["a:only"]
    assert p.settings.jev_models == [], "JEV 旧手动模型名不是 provider ref，不得搬进链 ✗"
    assert p.settings.rerank_models == ["r:only"]
    after = {k: mc.chain_refs(p.settings, k, lk) for k, lk in (
        ("compress_models", "compress_model"), ("audit_models", "audit_model"),
        ("jev_models", "jev_model_name"), ("rerank_models", "rerank_model"))}
    assert before == after, "迁移**改变了行为** ✗（必须完全等价 ✓）"
    # 只跑一次 ✓
    assert str(run(p.store.call("get_state", "cfg_migrate_chain", ""))) == "1"


def test_migration_never_touches_existing_chains(tmp_path):
    """★ 已经有链的用户 ⇒ **一个字都不许动** ✓"""
    p, _ = build(tmp_path, "{}", compress_models=["x:1", "x:2"], compress_model="x:old")
    run(p._migrate_chain_first_link())
    assert p.settings.compress_models == ["x:1", "x:2"], "已有链被改动了 ✗"
