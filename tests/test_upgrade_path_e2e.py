"""升级路径：**老用户**的库与配置，能不能被新版本平滑接住 ✓

为什么单独一个文件
------------------
其他测试都在"全新环境"里跑 ✓，而真机上最常见的事故恰恰是**存量升级** ✗：
  · 老库缺列（v2.22.5 就翻过车：`no such column: speaker_id` ⇒ 每轮注入全崩 ✗）
  · 老配置只有**单模型**字段，没有后来的模型链 `*_models` ✗
  · 一次性迁移跑两遍会不会把用户配置写坏 ✗

本测试**不需要老版本代码**（不依赖 git worktree ✓）：直接造出"老状态"，
再用当前代码初始化 ✓ —— 这样它能在任何环境（含 CI）里跑 ✓

覆盖的迁移：
  ① records.speaker_id 缺列自愈
  ② 自我认知：门槛 6→4、总开关补 False
  ③ ★ 单模型 → 各自链路（压缩/审计/JEV/重排）的**第 1 档**，并写进配置文件
"""
import asyncio
import json
import os
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CORE = os.environ.get("KIRA_CORE", "")
pytestmark = pytest.mark.skipif(not CORE, reason="需要 KIRA_CORE 指向 KiraAI 框架目录")

if CORE and CORE not in sys.path:
    sys.path.insert(0, CORE)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLUGIN_ID = "alife_memory_z"
SID = "qq:gm:legacy"
run = asyncio.run

# 老用户当年就是这么配的：**只有一个模型字段** ✓（没有 *_models ✗）
LEGACY_CONFIG = {
    "alife": {
        "probability": 0.0,
        "audit_enabled": False,
        "compress_model": "workbuddy:gpt-4o",
        "audit_model": "workbuddy:deepseek-v4.1-flash",
        "jev_model_name": "workbuddy:gpt-4o-mini",
        "rerank_model": "workbuddy:rerank",
        "self_fact_max_importance": 6,
    }
}


def _load_plugin():
    pkg_name = "alife_upgrade_e2e"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [str(ROOT)]
        sys.modules[pkg_name] = pkg
    import importlib
    return importlib.import_module(pkg_name + ".main")


def _make_plugin(tmp_path, cfg, data_root=None):
    m = _load_plugin()
    data_root = data_root or (tmp_path / "data")
    data_root.mkdir(parents=True, exist_ok=True)
    m.get_data_path = lambda: data_root
    m.get_config_path = lambda: tmp_path / "cfg"

    def get_llm_client(model_uuid=None, llm_type=None):
        class _C:
            async def chat(self, req):
                return types.SimpleNamespace(tool_calls=None, text_response='{"summary":"x","facts":[]}')
        return _C()

    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: data_root,
        get_llm_client=get_llm_client,
        get_default_llm_client=get_llm_client,
        get_default_fast_llm_client=get_llm_client,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
    )
    p = m.AlifeMemoryPlugin(ctx, cfg)
    return p, data_root


def _build_legacy_state(tmp_path):
    """用**当前代码**造出"老版本留下的状态"，再退化掉新东西 ✓（等价于老用户升级前 ✓）"""
    p, data_root = _make_plugin(tmp_path, json.loads(json.dumps(LEGACY_CONFIG)))
    run(p.initialize())                                    # 先建库
    # ① 老库：没有 speaker_id 列
    with p.store.connect() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(records)")]
        if "speaker_id" in cols:
            db.execute("ALTER TABLE records DROP COLUMN speaker_id")
    # ② 塞一条"老记录 + 老事实"（= 当年留下的记忆 ✓）
    now = time.time()
    with p.store.connect() as db:
        db.execute("INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                   "speaker,position,created,search_body) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("r-legacy", SID, "user", "L0", now, now, "老记忆：萤火喜欢甜口蛋糕",
                    "老记忆：萤火喜欢甜口蛋糕", json.dumps(["qq:1001"]), "周武", 0, now,
                    "老记忆：萤火喜欢甜口蛋糕"))
        db.execute("INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,relations,"
                   "sources,fingerprint,importance,created,revision,deleted,merge_pending,audited)"
                   " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("f-legacy", SID, "state", "x", "萤火喜欢甜口蛋糕", "", "", "[]", "[]",
                    json.dumps(["r-legacy"]), "fp-legacy", 5, now, 1, 0, 0, 0))
    # ③ **退化掉"已经迁移过"的痕迹** —— 这才是老用户升级前的真实状态 ✓
    #    · 一次性迁移的 state 标记要清掉（否则第二次 initialize 会正确地"跳过"✗ 测不到迁移 ✗）
    #    · 配置文件要退回老样子（上面 initialize 已经把它迁移/物化过 ✗）
    async def _reset_marks():
        for key in ("cfg_migrate_self_fact", "cfg_migrate_chain"):
            await p.store.call("set_state", key, "")
    run(_reset_marks())
    cfg_file = tmp_path / "cfg" / "plugins" / (PLUGIN_ID + ".json")
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(LEGACY_CONFIG, ensure_ascii=False), encoding="utf-8")
    return data_root, cfg_file


def _cfg_from_file(cfg_file):
    """真机里宿主就是**从配置文件**加载插件配置的 ✓ ⇒ 测试也照做 ✓"""
    return json.loads(cfg_file.read_text(encoding="utf-8"))


def test_legacy_speaker_id_column_self_heals(tmp_path):
    """① 老库缺 speaker_id ⇒ 启动应**自动补列**，不是崩掉 ✓"""
    data_root, cfg_file = _build_legacy_state(tmp_path)
    p, _ = _make_plugin(tmp_path, _cfg_from_file(cfg_file), data_root)
    run(p.initialize())
    with p.store.connect() as db:
        cols = [r[1] for r in db.execute("PRAGMA table_info(records)")]
    assert "speaker_id" in cols, "升级后老库仍缺 speaker_id 列 ✗（真机上会导致每轮注入崩溃 ✗）"


def test_legacy_records_and_facts_still_readable(tmp_path):
    """② 老记忆不能丢：老事实读得到，证据装配也不崩 ✓"""
    data_root, cfg_file = _build_legacy_state(tmp_path)
    p, _ = _make_plugin(tmp_path, _cfg_from_file(cfg_file), data_root)
    run(p.initialize())

    async def go():
        # 真入口：面板/画像就是走这个（main.py 里的插件方法 ✓）
        facts = await p.api_facts(sid=SID)
        await p.store.call("attach_evidence", facts or [])
        return facts or []

    facts = run(go())
    assert facts, "升级后老事实读不出来了 ✗（= 用户记忆丢失 ✗）"
    assert any("蛋糕" in json.dumps(f, ensure_ascii=False) for f in facts), "老事实内容不对 ✗"


def test_legacy_config_gets_new_recall_defaults(tmp_path):
    """老配置没有 profile_neighbors / profile_neighbors_max ⇒ 用新默认，不报错。"""
    data_root, cfg_file = _build_legacy_state(tmp_path)
    p, _ = _make_plugin(tmp_path, _cfg_from_file(cfg_file), data_root)
    run(p.initialize())
    assert p.settings.profile_neighbors == "smart"
    assert p.settings.profile_neighbors_max == 2


def test_self_gate_settings_are_migrated(tmp_path):
    """③ 自我认知：老配置的门槛 6 ⇒ 升到 4；总开关补 False ✓"""
    data_root, cfg_file = _build_legacy_state(tmp_path)
    p, _ = _make_plugin(tmp_path, _cfg_from_file(cfg_file), data_root)
    run(p.initialize())
    assert p.settings.self_fact_max_importance == 4, "门槛没迁移成 4 ✗"
    assert p.settings.self_fact_filter_enabled is False, "总开关没补默认值 ✗"


def test_single_model_becomes_first_link_and_is_written_back(tmp_path):
    """④ ★ 老用户单模型 ⇒ 自动升级为**各链路的第 1 档**，并**写进配置文件** ✓

    用户提问原话（2026-10-11）："从 2.22 以前升级上来的用户，他们自己配了模型选项的时候，
    是自动帮他们升级成组里的 1 了吗？"
    """
    data_root, cfg_file = _build_legacy_state(tmp_path)
    p, _ = _make_plugin(tmp_path, _cfg_from_file(cfg_file), data_root)
    run(p.initialize())

    assert p.settings.compress_models == ["workbuddy:gpt-4o"], "压缩链第 1 档没落位 ✗"
    assert p.settings.audit_models == ["workbuddy:deepseek-v4.1-flash"], "审计链第 1 档没落位 ✗"
    assert p.settings.jev_model_name == "", "旧手动 JEV 字段应被清空 ✗"
    assert p.settings.jev_models == [], "旧手动 JEV 模型名不得搬进链 ✗"
    assert p.settings.rerank_models == ["workbuddy:rerank"], "重排链第 1 档没落位 ✗"

    saved = json.loads(cfg_file.read_text(encoding="utf-8")).get("alife", {})
    assert saved.get("compress_models") == ["workbuddy:gpt-4o"], "没写进配置文件 ✗（面板看不到 ✗）"
    assert saved.get("audit_models") == ["workbuddy:deepseek-v4.1-flash"]


def test_upgrade_does_not_change_which_model_is_used(tmp_path):
    """⑤ 迁移**不许改变行为**：迁移前后解析出的链完全一致 ✓"""
    import importlib
    data_root, _ = _build_legacy_state(tmp_path)
    legacy = json.loads(json.dumps(LEGACY_CONFIG))
    p_before, _ = _make_plugin(tmp_path / "b", legacy, data_root)

    def chain_of(settings):
        mc = importlib.import_module("alife_upgrade_e2e.mchains")
        return {
            "compress": mc.chain_refs(settings, "compress_models", "compress_model"),
            "audit": mc.chain_refs(settings, "audit_models", "audit_model"),
            "jev": mc.chain_refs(settings, "jev_models", "jev_model_name"),
            "rerank": mc.chain_refs(settings, "rerank_models", "rerank_model"),
        }

    before = chain_of(p_before.settings)
    p_after, _ = _make_plugin(tmp_path / "a", json.loads(json.dumps(LEGACY_CONFIG)), data_root)
    run(p_after.initialize())
    after = chain_of(p_after.settings)
    assert before == after, "迁移改变了实际使用的模型 ✗（必须完全等价 ✓）"


def test_writes_still_work_after_upgrade(tmp_path):
    """⑥ 升级后**新记忆照常写入** ✓

    ★ 注意 event_key：records 表上 `event_key TEXT UNIQUE` ✓ 是**幂等设计** ✓
      ⇒ 同一条事件重复捕获会被正确去重 ✓（别拿同一个 key 断言"写不进去"✗ 那是我自己的错 ✗）
    """
    data_root, cfg_file = _build_legacy_state(tmp_path)
    p, _ = _make_plugin(tmp_path, _cfg_from_file(cfg_file), data_root)
    run(p.initialize())

    async def go():
        before = len(await p.store.call("active", SID))
        await p.store.call("capture", SID, "turn-after-upgrade",
                           [{"role": "user", "content": "升级后的新消息", "time": time.time(),
                             "users": ["qq:1001"], "speaker": "周武", "speaker_id": "qq:1001"}])
        await asyncio.sleep(0.2)
        return before, len(await p.store.call("active", SID))

    before, after = run(go())
    assert after > before, "升级后写入失效 ✗（记录数 %d → %d）" % (before, after)
