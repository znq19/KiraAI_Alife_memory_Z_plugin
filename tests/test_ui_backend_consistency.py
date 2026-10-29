"""前后端一致性回归：① 模型标签可区分 ② 恢复默认拿得到真默认 ③ 改完立即生效（缓存失效）④ 缓存失效的完整性

由来（2026-10-12 用户实拍报告）：
  1. "同名甚至同提供商名（但不同提供商类型）的模型，会不会无法正常显示和选择，重复了就一个都不能选？"
  2. "点恢复默认不是变回跟随默认模型，而是只显示按钮文案、不正常变回"
  3. "我在前端改动了永久记忆，bot 能即时查到吗？下轮对话注入是不是也就更新了？"

本文件把这些"看不见的约定"钉成断言 ✓ —— 都属于**手点不出来**、但用户一定会踩的那类问题 ✗
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

pkg = types.ModuleType("alife_uib")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_uib", pkg)
m = importlib.import_module("alife_uib.main")
m_q = m   # 本文件里统一用 m ✓（brief 也在这个模块 ✓）

SID = "qq:ui:1"
run = asyncio.run


def build(tmp_path, **cfg):
    ctx = types.SimpleNamespace(
        get_plugin_data_dir=lambda: tmp_path,
        plugin_mgr=types.SimpleNamespace(plugin_configs={}),
        get_llm_client=None,
        get_default_llm_client=None,
        get_default_fast_llm_client=None,
        get_embedding_client=None,
        provider_mgr=None,
    )
    m.get_data_path = lambda: tmp_path
    m.get_config_path = lambda: tmp_path / "cfg"
    base = {"probability": 0.0, "audit_enabled": False}
    base.update(cfg)
    p = m.AlifeMemoryPlugin(ctx, {"alife": base})
    run(p.initialize())
    return p


def _seed_fact(p, content, fact_id="f-ui-1"):
    """直接落一条永久事实（走 store.memorize ⇒ 会 bump ✓）"""
    p.store.add_facts(SID, [{
        "subject": "用户", "category": "profile", "content": content,
        "importance": 9, "reason": "ui-test", "scenario": "",
        "relations": [], "tags": [], "source_ids": [],
        "time": "2026-10-12T10:00:00",
    }])
    with p.store.connect() as db:
        row = db.execute("SELECT id FROM facts WHERE content=? ORDER BY created DESC LIMIT 1",
                         (content,)).fetchone()
    return row["id"] if row else fact_id


def _classes():
    """宿主类：**照抄仓库既有测试的导入**（位置随框架世代变过 ⇒ 别自己猜 ✗）"""
    import importlib as _il
    return {
        "LLMRequest": _il.import_module("core.provider").LLMRequest,
        "OpenAIMessage": _il.import_module("core.agent.message").OpenAIMessage,
        "Prompt": _il.import_module("core.prompt_manager").Prompt,
        "MessageChain": _il.import_module("core.chat").MessageChain,
        "Text": _il.import_module("core.chat.message_elements").Text,
        "KiraIMMessage": _il.import_module("core.chat.message_utils").KiraIMMessage,
        "KiraMessageBatchEvent": _il.import_module("core.chat.message_utils").KiraMessageBatchEvent,
        "User": _il.import_module("core.chat.session").User,
        "Session": _il.import_module("core.chat.session").Session,
    }


def _inject(p, text, sess=SID):
    """走**真注入路径**（on_request）取这一轮实际会看到的内容 ✓"""
    C = _classes()
    session = C["Session"](adapter_name="test", session_type="dm", session_id=sess)
    msg = C["KiraIMMessage"](message_id="m", self_id="bot",
                             chain=C["MessageChain"]([C["Text"](text)]), timestamp=100,
                             sender=C["User"](user_id="qq:u", nickname="小明"))
    msg.message_str = "[小明] " + text
    ev = C["KiraMessageBatchEvent"](messages=[msg], session=session, timestamp=100, message_types=[])
    req = C["LLMRequest"](messages=[C["OpenAIMessage"](role="user", content="原历史")],
                          system_prompt=[C["Prompt"]("稳定人格", name="stable")],
                          user_prompt=[C["Prompt"](text, name="message")])

    async def go():
        await p.on_request(ev, req)
        return req

    return run(go())


def _injected_text(p, text="你还记得我的事吗"):
    req = _inject(p, text)
    req.assemble_prompt()
    return req.messages[-1].content or ""


# ---------------------------------------------------------------- ③ 改完立即生效
def test_fact_edit_is_visible_to_injection_immediately(tmp_path):
    """★ 面板改一条永久记忆 ⇒ **同一会话的下一轮注入必须立刻反映**（不能被进程内缓存挡住）✓

    用户问题 3："我在前端改动了永久记忆，bot 能即时查到吗？下轮对话注入是不是也就更新了？"
    """
    p = build(tmp_path)
    fid = _seed_fact(p, "用户花生过敏")
    before = _injected_text(p, "你还记得我花生过敏的事吗")
    assert "花生过敏" in before, "铺数据失败：注入里看不到这条事实 ✗ 实际注入＝%r" % before[:200]

    # 面板的编辑动作（/edit ⇒ store.edit ✓）
    run(p.store.call("edit", "fact", fid, None, {"content": "用户改口了：对花生不过敏了"}, ""))

    after = _injected_text(p, "你还记得我花生过敏的事吗")
    assert "对花生不过敏了" in after, (
        "改完之后注入仍是旧的 ✗ —— 说明写路径没让进程内缓存失效（memo 按 store revision 失效）"
    )


def test_config_change_takes_effect_without_restart(tmp_path):
    """★ 面板改配置（保存并生效）⇒ 不重启也要生效（这里查 aux/engine 是否拿到新设置）"""
    p = build(tmp_path)
    before = id(p.decisions)
    # 面板「保存并生效」= api_save_config 里做的事：换 settings + 重建辅助层 ✓
    p.settings = p.settings.model_copy(update={"rerank_models": ["x:1"], "rerank_model": "x:1"})
    p._build_aux()
    assert id(p.decisions) != before, "改了配置但辅助层（JEV/重排）没重建 ✗ ⇒ 新配置不生效 ✗"


# ---------------------------------------------------------------- ④ 缓存失效的完整性
def test_every_content_write_bumps_store_revision(tmp_path):
    """★ 结构性守卫：**任何会改内容的写方法都必须 bump revision** ✗

    否则：面板改了 / 后台改了 ⇒ 进程内 memo 不失效 ⇒ 注入与面板显示不一致 ✗
    （用户问题 3 的根因就是这一类 ✓ 已实测过的例子：unmerge_fact）
    例外（白名单，且要写清理由）：
      · touch_accessed / touch_tidy / mark_rotation：**纯访问计数**，故意不失效（每轮都改 ⇒
        若 bump 会让缓存彻底失效 ⇒ 性能塌方 ✗）
      · set_state / enqueue / claim / drop_job / prune_jobs：只动任务/状态记账，不参与召回内容 ✓
      · 内部辅助（_add_fact/_orphan_facts/short_id/…）：由调用方（memorize/audit/merge_facts ✓）统一 bump ✓
    """
    import ast
    import re

    src = (ROOT / "storage.py").read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    write_re = re.compile(r"\b(INSERT|UPDATE|DELETE|REPLACE)\b", re.I)
    exempt = {
        "touch_accessed", "touch_tidy", "mark_rotation", "set_state",
        "enqueue", "claim", "drop_job", "prune_jobs",
        "_add_fact", "_orphan_facts", "short_id", "archive_distilled",
        "backfill_speaker_ids", "backfill_tool_steps", "backfill_time_provenance",
        "backfill_rewrite_pending", "mark_rewrite_pending", "bump_rewrite_attempts",
        "prepare_search_index", "index_backfill", "rebuild_search_index",
        "setup_search_index", "scrub_capture_text", "finish_capture_scrub",
        "has_active_job", "entity_ids", "entities", "_ensure_entities", "_lexical_sql",
        "initialize", "requeue_running", "set_vector",
    }
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = "\n".join(lines[node.lineno - 1: node.end_lineno])
        if not write_re.search(body):
            continue
        if "meta SET value=value+1" in body:      # 计数器自己
            continue
        if "self.bump(" in body or "bump(db)" in body:
            continue
        if node.name in exempt:
            continue
        offenders.append("%s(L%d)" % (node.name, node.lineno))
    assert not offenders, (
        "这些写方法没有让缓存失效（会表现为：改了却要等别的写入才生效 ✗）：%s" % ", ".join(offenders)
    )


# ---------------------------------------------------------------- ② 恢复默认
def test_config_api_exposes_real_defaults(tmp_path):
    """★ /config 必须交出「真·默认值全表」（恢复默认依赖它 ✓）

    用户问题 2 的根因：`*_models` 这类 default_factory 字段在 JSON Schema 里**没有 default** ✗
    ⇒ 前端旧代码读到 undefined 就 return ⇒ 点「恢复默认」**毫无反应** ✗
    """
    p = build(tmp_path)
    cfg = run(p.api_config())
    assert "defaults" in cfg, "/config 没给 defaults ✗ ⇒ 恢复默认会再次静默失效 ✗"
    defaults = cfg["defaults"]
    props = cfg["schema"]["properties"]
    missing = [k for k in props if k not in defaults]
    assert not missing, "这些字段在 defaults 里缺失 ⇒ 恢复默认拿不到值 ✗：%s" % missing[:8]
    # 关键几个必须真的是"空链 = 跟随默认"
    for k in ("compress_models", "audit_models", "jev_models", "rerank_models"):
        assert defaults.get(k) == [], "%s 的默认应为空链（= 跟随 KiraAI 默认模型）✗ 实际 %r" % (k, defaults.get(k))
    for k in ("compress_model", "audit_model", "rerank_model", "embedding_model"):
        assert defaults.get(k) == "", "%s 的默认应为空串 ✗ 实际 %r" % (k, defaults.get(k))
    # 旧手动 JEV 字段已从全部前端隐藏（也不在 defaults 里）
    for k in ("jev_base_url", "jev_api_key", "jev_model_name"):
        assert k not in defaults, "%s 不应再暴露给前端 ✗" % k


# ---------------------------------------------------------------- ① 同名模型
def _fake_models_api(p, providers):
    """把 provider_mgr 换成假的 ⇒ 走真 api_models 逻辑 ✓"""
    p.ctx.provider_mgr = types.SimpleNamespace(kira_config={"providers": providers})
    return run(p.api_models())["models"]


def test_same_display_name_models_are_marked_selectable(tmp_path):
    """★ 同名模型（含"同名但类型不同的提供商"）必须能被前端区分 ✓

    用户问题 1："同提供商名称但不同提供商类型 ⇒ 会不会显示不了/选不了，重复了一个都不能选？"
    实测：条目不会丢 ✓，但**标签一模一样** ⇒ 用户分不清、容易删错 ✗
    ⇒ 后端按 (显示名+提供商显示名+类型) 标 dup，并给 tag ⇒ 前端加区分后缀 ✓
    """
    p = build(tmp_path)
    prov = {
        "pid-a": {"name": "Workbuddy", "model_config": {
            "llm": {"uuid-1": {"model_name": "gpt-4o"}}}},
        "pid-b": {"name": "Workbuddy", "model_config": {     # ← 同显示名，不同提供商实例 ✓
            "llm": {"uuid-2": {"model_name": "gpt-4o"}}}},
        "pid-c": {"name": "Trae", "model_config": {
            "llm": {"uuid-3": {"model_name": "gpt-4o"}}}},
    }
    got = _fake_models_api(p, prov)
    assert len(got) == 3, "模型条目丢了 ✗（应 3 条）"
    ids = [m["id"] for m in got]
    assert len(set(ids)) == 3, "条目 id 撞车 ⇒ 前端会有一个选不了 ✗：%s" % ids
    dup = [m for m in got if m.get("dup")]
    assert len(dup) == 2, "同名（含同名提供商）的两条应被标记 dup ✗ 实际 %d 条" % len(dup)
    assert all(m.get("tag") for m in dup), "标了 dup 却没给区分用 tag ✗"
    # 2026-10-15：区分标识改为"**可读**优先" ✓（3.0 的 provider id 是 UUID ✗ 绝不能吐 ✓）
    #   这里两个模型 id 短而可读 ⇒ 用模型 id 本身区分 ✓（能区分即可 ✓）
    assert len({m["tag"] for m in dup}) == 2, "tag 没起到区分作用 ✗ 实际 %r" % [m["tag"] for m in dup]
    plain = [m for m in got if not m.get("dup")]
    assert len(plain) == 1 and plain[0]["provider"] == "Trae", "不该被标记的条目被误标 ✗"


def test_decision_and_rerank_models_are_exposed_with_kind(tmp_path):
    """3.1.1：decision（TypeSafe）/ rerank 必须进 /models（各自带 kind），
    前端才能把它们放进各自的下拉，而不是永远看不到。"""
    p = build(tmp_path)
    prov = {"pid-ts": {"name": "TypeSafe", "format": "TypeSafe", "model_config": {
        "decision": {"uuid-d": {"model_name": "jev-latest", "config": {}}},
        "rerank": {"uuid-r": {"model_name": "rerank-x", "config": {}}},
        "llm": {"uuid-l": {"model_name": "chat", "config": {}}},
    }}}
    got = _fake_models_api(p, prov)
    kinds = {(m["kind"], m["name"]) for m in got}
    assert ("decision", "jev-latest") in kinds, kinds
    assert ("rerank", "rerank-x") in kinds, kinds
    assert ("llm", "chat") in kinds, kinds

def test_legacy_manual_jev_fields_hidden_from_ui(tmp_path):
    """3.1.1 起手动 JEV 连接字段不再出现在任何前端，但旧配置仍能读/仍生效。"""
    p = build(tmp_path, jev_base_url="https://legacy", jev_api_key="sk-legacy",
              jev_model_name="jev-latest")
    cfg = run(p.api_config())
    props = set(cfg["schema"]["properties"])
    assert not ({"jev_base_url", "jev_api_key", "jev_model_name"} & props), props
    assert p.settings.jev_api_key == "", "旧值必须在加载时清空（绝不隐形生效）✗"
    assert p.settings.jev_model_name == "", "旧值必须在加载时清空 ✗"
    assert p.settings.jev_base_url == ""

def test_legacy_fields_cannot_be_reinjected_via_settings(tmp_path):
    """旧隐藏字段即使出现在 Settings/保存请求里，也必须被清空且不写回配置。"""
    hidden = getattr(m, "HIDDEN_SETTINGS", frozenset())
    s = m.Settings(jev_api_key="sk-inject", jev_base_url="https://inject",
                   jev_model_name="jev-latest")
    s = m._neutralize_legacy_jev(s)
    assert all(getattr(s, k) == "" for k in hidden)
    dumped = m._public_settings_dump(s)
    assert not (set(hidden) & set(dumped))

def test_jev_model_fields_have_labels_and_defaults(tmp_path):
    """JEV 四个模型字段必须同时出现在 schema / labels / defaults 里（前端才显示得全）。"""
    p = build(tmp_path)
    cfg = run(p.api_config())
    for k in ("jev_model", "jev_models", "jev_decision_model", "jev_decision_models"):
        assert k in cfg["schema"]["properties"], "缺少 schema 字段：%s" % k
        assert k in cfg["labels"], "缺少中文标签：%s" % k
        assert k in cfg["defaults"], "缺少默认值：%s" % k

def test_aux_fingerprint_tracks_decision_fields(tmp_path):
    """改 TypeSafe 决策模型必须触发决策层重建（否则改了像没改）。"""
    p = build(tmp_path)
    before = m.AlifeMemoryPlugin._aux_fingerprint(p.settings)
    p.settings.jev_decision_model = "p:uuid-d"
    assert m.AlifeMemoryPlugin._aux_fingerprint(p.settings) != before
    
# ---------------------------------------------------------------- ④ 其他一致性
def test_chain_limit_has_single_source_of_truth(tmp_path):
    """★ 链长上限只能有一个来源（mchains.CHAIN_MAX）⇒ 前端不许硬编码 ✗

    否则哪天把上限调大/调小，面板还是老的 ⇒ 用户看到"加不进去/能加超过上限" ✗
    """
    from alife_uib import mchains

    p = build(tmp_path)
    cfg = run(p.api_config())
    assert cfg.get("chain_max") == mchains.CHAIN_MAX, "/config 没透出正确的 chain_max ✗"
    js = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "config.chain_max" in js, "前端没有使用后端给的 chain_max ✗"
    # 允许 `|| 10` 兜底，但不能再出现独立的硬编码上限判断 ✓
    # 注意别用 "length < 10" 这种宽串 ✗ —— `shownFacts.length < 100` 会误命中 ✓
    for bad in ("out.length < 10", "slice(0, 10)", "out.length < 10)"):
        assert bad not in js, "前端仍有硬编码的链长上限 ✗：%s" % bad


# ---------------------------------------------------------------- ③-b bot 自己写
def test_bot_capture_refreshes_cached_reads(tmp_path):
    """★ **bot 自己存进来的**（capture）⇒ 进程内缓存必须失效、按新数据重算 ✓

    注意别把断言写成"新消息一定出现在下一轮注入里" ✗ —— **原始消息按设计不会逐条注入** ✓
    （要经压缩/审计沉淀成事实 ✓）所以这里验证的是**缓存机制本身**：
    同一个 memo key 前后必须重算出**不同且包含新内容**的结果 ✓
    （用户要确认的是"bot 自己改的/删的会不会即时生效" ⇒ 这就是那条链路 ✓）
    """
    p = build(tmp_path)
    key = ("ui-consistency", SID)

    async def go():
        v1 = await p.memo(key, lambda: p.store.call(
            "context", SID, ["test:qq:u"], scope="global"))
        await p.store.call("capture", SID, "turn-9", [{
            "role": "user", "content": "我刚说了：我搬到杭州了", "time": time.time(),
            "users": ["test:qq:u"], "speaker": "小明", "speaker_id": "test:qq:u",
        }])
        v2 = await p.memo(key, lambda: p.store.call(
            "context", SID, ["test:qq:u"], scope="global"))
        return v1, v2

    v1, v2 = run(go())
    assert repr(v1) != repr(v2), "bot 存了新消息，但同一 memo key 的结果**没变** ✗ ⇒ 缓存没失效 ✗"
    assert "杭州" in repr(v2), "bot 刚存的内容没有出现在重算结果里 ✗"


def test_bot_memorize_is_visible_to_next_injection(tmp_path):
    """★ **bot 自己记住的**（memorize）⇒ 下一轮注入立刻可见 ✓"""
    p = build(tmp_path)
    before = _injected_text(p, "你还记得我住哪吗")
    run(p.store.call("memorize", SID, "用户住杭州西湖区", ["test:qq:u"],
                     time.time(), time.time() + 1, 9, "profile"))
    after = _injected_text(p, "你还记得我住哪吗")
    assert after != before and "杭州" in after, "bot 自己记住的事实没进下一轮注入 ✗"


def test_background_compress_refreshes_cached_reads(tmp_path):
    """★ **后台模型改的**（压缩 = 后台模型任务）⇒ 同样即时生效 ✓

    用户要确认的第三条链路："后台模型修改的也是即时能查和能生效，包括下一轮真的变化了 ✓"
    （压缩会把原始消息沉淀成摘要 ⇒ 下一轮注入看到的就是**摘要后的内容** ✓）
    """
    p = build(tmp_path, compress_batch_mode="records", threshold=8, batch_size=6)
    now = time.time()
    run(p.store.call("capture", SID, "turn-a", [
        {"role": "user", "content": "第 %d 句：我们聊到搬家的事" % i, "time": now + i,
         "users": ["test:qq:u"], "speaker": "小明", "speaker_id": "test:qq:u"}
        for i in range(8)]))

    calls = []

    async def fake_model(model, purpose, instruction, schema, payload, link_timeout=None):
        calls.append(purpose)
        return '{"summary": "【已压缩】用户搬到杭州西湖区了", "facts": []}'

    p.engine.model_call = fake_model                      # 只换模型调用，其余全真的 ✓
    key = ("ui-consistency", SID)

    async def go():
        v1 = await p.memo(key, lambda: p.store.call(
            "context", SID, ["test:qq:u"], scope="global"))
        await p.engine.compress(SID)
        v2 = await p.memo(key, lambda: p.store.call(
            "context", SID, ["test:qq:u"], scope="global"))
        return v1, v2

    v1, v2 = run(go())
    assert calls, "压缩压根没调模型 ⇒ 这条断言没意义 ✗"
    assert repr(v1) != repr(v2), "后台压缩改完了，但同一 memo key 的结果没变 ✗ ⇒ 缓存没失效 ✗"
    assert "已压缩" in repr(v2), "压缩后的摘要没有进入重算结果 ✗"


# ---------------------------------------------------------------- 注入简报渲染
def test_brief_renders_short_codes_as_names_not_list_repr(tmp_path):
    """★ 注入简报头里的「短码=名字」必须是**名字**，不能是列表 repr ✗

    实测抓到（2026-10-14）：分组视图下 `short_names()` 给的是
    `{短码: [真实 id, "名字|别名"]}`（**给 JSON 事实区用的** ✓ 图例里写明 ✓）
    而 `brief()` 直接 `%s` ⇒ 头部变成 `n1=['qq:123', '小明|小明']` ✗
    ⇒ 白费 token ✗ 且模型得自己猜那是什么 ✓（默认 fact_view 就是分组视图 ⇒ **默认路径上** ✓）
    """
    p = build(tmp_path)
    perception = {
        "scope": "global", "session": "s1",
        # 分组视图形态（现在真实用的就是它 ✓）
        "names": {"n1": ["qq:123", "小明|小明"], "n2": ["qq:456", "小红|小红"]},
        "facts": {"n1": [["pf", "喜欢甜食", 9]]},
    }
    text = m_q.brief(perception)
    assert "[" not in text.split("\n")[0], "简报头里出现了列表 repr ✗：%s" % text.split("\n")[0]
    assert "n1=小明|小明" in text, "短码没有渲染成名字 ✗：%s" % text.split("\n")[0]
    assert "n2=小红|小红" in text
    # 扁平视图形态（字符串名字 ✓）也要照旧正确 ✓
    flat = {"scope": "global", "session": "s1", "names": {"n1": "小明|小明"}, "facts": []}
    assert "n1=小明|小明" in m_q.brief(flat)


# ---------------------------------------------------------------- JEV × 提供商
def test_jev_model_name_from_provider_info_not_internal_uuid(tmp_path):
    """★ 把 JEV 注册成 KiraAI 提供商后，发给 JEV 的 model 必须是**模型名**，不是内部 uuid ✗

    用户实测用法（2026-10-14）："拿我发的 key+url 用 KiraAI 的 OpenAI 提供商注册" ⇒
    框架给的 provider_config 只有 {base_url, api_key} ✓，而 `mid` 在 **3.0 里是 uuid** ✗
    ⇒ 旧代码 `model = model or mid` 会把 uuid 当模型名发出去 ⇒ 渠道 **403** ✗
      （实测 4 种 base_url 粘法全 403 ✓；改成优先 ModelInfo.model_name 后全通 ✓）
    2.x 里 mid 本身就是名字 ⇒ 回退到 mid 仍然正确 ✓
    """
    import importlib as _il

    from core.provider import ModelInfo, ModelType

    md = _il.import_module("alife_uib.mdecide")   # 与文件顶部的包名一致 ✓

    class _PM:
        def __init__(self, info):
            self._i = info

        def get_model_info(self, pid, mid):
            return self._i

    def settings(**kw):
        s = type("S", (), {})()
        for k, v in {"jev_enabled": True, "jev_model": "", "jev_base_url": "",
                     "jev_api_key": "", "jev_model_name": "", "jev_timeout_ms": 8000,
                     "jev_prompt_append": ""}.items():
            setattr(s, k, v)
        for k, v in kw.items():
            setattr(s, k, v)
        return s

    def info(model_id, model_name):
        """★ 两代 ModelInfo 形状不同：2.x **没有 model_name 字段** ✗（实测 TypeError ✓）
        ⇒ 先按通用字段造，再按需 setattr ✓（插件用 getattr 读 ⇒ 两代都能被测到 ✓）"""
        m = ModelInfo(model_type=ModelType.LLM, model_id=model_id,
                      provider_id="prov1", provider_name="futureppo",
                      provider_config={"base_url": "https://api.example.com/v1",
                                       "api_key": "sk-test-not-real"},
                      model_config={})
        if model_name:
            try:
                m.model_name = model_name
            except Exception:
                pass
        return m

    # 3.0：uuid 当 mid ⇒ 必须用 model_name 上的名字 ✓
    cfg = md.resolve_config(settings(jev_model="prov1:1f0c-uuid-9a"),
                            _PM(info("1f0c-uuid-9a", "jev-latest")))
    assert cfg.model == "jev-latest", "发给 JEV 的模型名应是面板里的名字 ✗ 实际 %r" % cfg.model

    # 2.x/无 model_name：回退到 mid（那时 mid 就是名字 ✓）
    cfg2 = md.resolve_config(settings(jev_model="prov1:jev-latest"),
                             _PM(info("jev-latest", "")))
    assert cfg2.model == "jev-latest", "没有 model_name 时应回退到 mid ✗ 实际 %r" % cfg2.model

    # 显式填了 jev_model_name ⇒ 显式优先 ✓
    cfg3 = md.resolve_config(settings(jev_model="prov1:1f0c-uuid-9a", jev_model_name="my-jev"),
                             _PM(info("1f0c-uuid-9a", "jev-latest")))
    assert cfg3.model == "my-jev", "显式模型名应优先 ✗ 实际 %r" % cfg3.model

    # base_url 带 /v1、带尾斜杠、粘成 chat/completions ⇒ 端点都要规整正确 ✓
    for base, want in (("https://api.example.com", "https://api.example.com/v1/systemone"),
                       ("https://api.example.com/v1", "https://api.example.com/v1/systemone"),
                       ("https://api.example.com/v1/", "https://api.example.com/v1/systemone"),
                       ("https://api.example.com/v1/chat/completions",
                        "https://api.example.com/v1/systemone"),
                       ("https://api.example.com/v1/systemone",
                        "https://api.example.com/v1/systemone")):
        got = md.endpoint_of(base)
        assert got == want, "端点规整错 ✗ %s → %s（应 %s）" % (base, got, want)


# ---------------------------------------------------------------- 恢复默认 ⇒ 跟随宿主
def test_restore_default_makes_each_consumer_follow_host_defaults(tmp_path):
    """★ 前端点「恢复默认」并保存后 ⇒ 各消费者必须**真的跟随框架已设的默认模型** ✓

    用户问题："如果各模型前端点恢复默认并保存，真的是恢复默认并正常使用了吗？（跟随框架已设的各默认模型）"
    插件设计约定（代码注释原话）：**空值 = 宿主默认** ✓
      · 压缩           空 ⇒ `ctx.get_default_fast_llm_client()`（快速模型 ✓ 压缩用它）
      · 审计/其它 LLM   空 ⇒ `ctx.get_default_llm_client()`
      · 向量           空 ⇒ `ctx.get_default_embedding_client()`
    这里逐个消费者实测：**必须调到对应的默认入口**，而不是报错或偷偷用别的模型 ✓
    """
    p = build(tmp_path)          # build 默认就把所有模型字段留空 = 「恢复默认后」的配置 ✓
    for k in ("compress_model", "compress_models", "audit_model", "audit_models",
              "jev_model", "jev_models", "rerank_model", "rerank_models", "embedding_model"):
        assert getattr(p.settings, k, "") in ("", []), "测试前提错：%s 应默认为空 ✗" % k

    used = []

    class _Cli:
        class model:            # noqa: N801
            provider_id, model_id = "host", "default-model"

        async def chat(self, req):
            used.append("called")
            return type("R", (), {"tool_calls": None, "text_response": '{"summary":"用户喜欢甜食，已归档完成","facts":[]}'})()

    class _AuditCli(_Cli):
        async def chat(self, req):
            used.append("called")
            return type("R", (), {"tool_calls": None, "text_response": '{"actions":[]}'})()

    p.ctx.get_default_fast_llm_client = lambda: used.append("fast") or _Cli()
    p.ctx.get_default_llm_client = lambda: used.append("llm") or _AuditCli()
    p.ctx.get_default_embedding_client = lambda: used.append("embed") or _Cli()

    # ① 压缩 ⇒ 必须走宿主 **fast** 默认 ✓
    _now = time.time()
    run(p.store.call("capture", SID, "turn-restore", [
        {"role": "user", "content": "第 %d 条原始消息" % i, "time": _now + i,
         "users": ["test:qq:u"], "speaker": "小明", "speaker_id": "test:qq:u"}
        for i in range(8)]))
    p.settings = p.settings.model_copy(
        update={"compress_batch_mode": "records", "threshold": 8, "batch_size": 6})
    p._build_aux()
    run(p.engine.compress(SID))
    assert "fast" in used, "压缩在空模型时没有跟随宿主默认（fast）✗ 实际 %s" % used

    # ② 审计 ⇒ 必须走宿主 **默认 LLM** ✓（不是 fast ✗）
    used.clear()
    p.store.add_facts(SID, [{
        "subject": "用户", "category": "profile", "content": "用户喜欢甜食",
        "importance": 6, "reason": "restore-test", "scenario": "",
        "relations": [], "tags": [], "source_ids": [], "time": "2026-10-12T10:00:00",
    }])
    run(p.engine.audit(SID))
    assert "llm" in used, "审计在空模型时没有跟随宿主默认 LLM ✗ 实际 %s" % used

    # ③ 向量 ⇒ 必须走宿主 **默认 embedding** ✓
    used.clear()
    p.settings = p.settings.model_copy(update={"semantic_enabled": True, "embedding_model": ""})
    cfg = type("C", (), {"semantic_enabled": True, "embedding_model": ""})()
    run(p.engine.embed("测试文本", cfg))
    assert "embed" in used, "向量在空模型时没有跟随宿主默认 embedding ✗ 实际 %s" % used


# ---------------------------------------------------------------- 模型列表完整性
def test_model_list_also_uses_framework_official_accessor(tmp_path):
    """★ 用户实拍："模型列表依然是不全的" ⇒ 不能**只**读配置字典 ✗

    真因：只按 `provider_mgr.kira_config["providers"][pid]["model_config"][kind]` 取 ✗
    ⇒ 只要框架把模型记在别处（版本差异 / 运行时注册 / 存 DB）就会漏 ✓
    修：**先问框架自己的接口**（`get_model_infos` / `get_models` ✓），再兜底读字典 ✓，两条路合并去重 ✓
    本用例：让"框架接口"给出一个**字典里没有**的模型 ⇒ 列表必须包含它 ✓
    """
    p = build(tmp_path)

    class _Info:
        def __init__(self, mid, name):
            self.model_id, self.model_name = mid, name
            self.model_type = "llm"
            self.provider_name = "框架提供商"
            self.model_config = {"model_name": name}

    class _PM:
        kira_config = {"providers": {"p1": {"name": "字典提供商",
                                            "model_config": {"llm": {"only_in_dict": {"model_name": "只在字典"}}}}}}

        def get_model_infos(self, pid):
            # 框架接口给出一个**字典里没有**的模型（模拟"注册在别处" ✓）
            return [_Info("only_via_api", "只在框架接口")]

    p.ctx.provider_mgr = _PM()
    got = [(m["id"], m["name"]) for m in run(p.api_models())["models"]]
    ids = [g[0] for g in got]
    assert "p1:only_via_api" in ids, "框架接口给出的模型被漏掉了 ✗ 实际 %s" % ids
    assert "p1:only_in_dict" in ids, "配置字典里的模型也要在（兜底那条路 ✓）实际 %s" % ids
    assert len(ids) == len(set(ids)), "两条路合并后出现重复 ✗ %s" % ids


# ---------------------------------------------------------------- 默认模型必定可选
def test_default_models_are_always_listed(tmp_path):
    """★ 用户实拍（真框架已复现）：**正在用的默认模型**必须出现在下拉里 ✓

    复现场景（2026-10-15）：
      · 某提供商只存在于 `provider_manager` 的**活注册表**里（WebUI 就是那么建的 ✓）
      · 配置字典里没有它 ✗，框架官方 `get_models(pid)` 对它返回 **{}** ✗
      · 而 `models.default_llm` 正指向它的模型（**真的在跑** ✓）
      ⇒ 三条枚举路全落空 ⇒ 面板里"查无此人" ✗（用户："commandcode 前端和日志都找不到，但它真在处理记忆"）
    修：把 `models.default_*` 指向的模型**逐个补进下拉** ✓（保底一定可选 ✓）
    """
    p = build(tmp_path)

    class _Obj:
        name = "只在活注册表"

    class _PM:
        # 活注册表里有 ✓；字典里没有 ✗；官方 get_models 返回空 ✗ —— 完全照用户现场 ✓
        kira_config = {"providers": {}, "models": {"default_llm": "ghost:cc-model",
                                                   "default_fast_llm": "ghost:cc-model"}}

        def get_all_providers(self):
            return {"ghost": _Obj()}

        def get_models(self, pid):
            return {}

    p.ctx.provider_mgr = _PM()
    got = [(m["id"], m["name"]) for m in run(p.api_models())["models"]]
    ids = [g[0] for g in got]
    assert "ghost:cc-model" in ids, (
        "正在使用的默认模型没被列出 ✗（这正是用户报的「查无此人」✓）实际 %s" % ids)
    assert len(ids) == len(set(ids)), "去重失效 ⇒ 出现重复 ✗ %s" % ids


def test_model_display_name_on_30_reads_model_name_attr(tmp_path):
    """★ 3.0 形状差异（用户实拍「模型名全变成 UUID」✗）：

    3.0 的 `get_model_infos(pid)` 返回对象里 **`model_config` 是空的 `{}`** ✗，
    显示名改挂在**对象属性 `model_name`** 上 ✓（2.x 才在 model_config 里 ✓）
    ⇒ 只读 model_config 就取不到名字 ⇒ 回退成 id ✗（3.0 的 id 是 UUID ⇒ 面板显示一串哈希 ✓）
    """
    p = build(tmp_path)

    class Info:
        def __init__(self, mid, nm):
            self.model_id, self.model_name = mid, nm
            self.model_config = {}          # ★ 3.0：空 ✗
            self.provider_name = "提供商一"

    class _PM:
        kira_config = {"providers": {"p1": {"name": "提供商一", "model_config": {}}}}

        def get_all_providers(self):
            return {"p1": Info("x", "y")}

        def get_model_infos(self, pid):
            return [Info("3f2c1d9e-uuid-hash", "真名一")]

    p.ctx.provider_mgr = _PM()
    got = run(p.api_models())["models"]
    assert got, "一条都没列出 ✗"
    assert got[0]["name"] == "真名一", (
        "3.0 下显示名丢了 ⇒ 会退化成 UUID ✗ 实际 %r" % got[0])


def test_dup_tag_is_readable_never_uuid(tmp_path):
    """★ 用户实拍（3.0）：行尾出现 `… · commandcode goat · 13e2edf385f9…` ✗

    原因：3.0 里 WebUI 建的 **provider id 就是 UUID** ✗，
    而我给"显示名撞车"补的区分标识取的是 **provider id** ✗ ⇒ 吐了一串 UUID ✓
    修：**短且可读**的模型 id 才用 ✓，否则用**组内序号 #n** ✓；provider 名找不到时也不用 UUID 顶替 ✓
    """
    import json as _json
    p = build(tmp_path)

    class Info:
        def __init__(self, mid, nm, pn=None):
            self.model_id, self.model_name = mid, nm
            self.model_config = {}
            self.provider_name = pn
            self.model_type = "llm"

    UUID_PID = "13e2edf385f9a1b2c3d4e5f60718293a"

    class _PM:
        kira_config = {"providers": {UUID_PID: {"name": "commandcode goat", "model_config": {}}}}

        def get_all_providers(self):
            return {UUID_PID: Info("x", "y")}

        def get_model_infos(self, pid):
            # 同一提供商下两个**同名**模型，且 id 都是不透明长串 ⇒ 必须靠可读序号区分 ✓
            return [Info("0f3a9c1d2e4b5a6c7d8e9f0a1b2c3d4e", "同名模型", "commandcode goat"),
                    Info("aa11bb22cc33dd44ee55ff6677889900", "同名模型", "commandcode goat")]

    p.ctx.provider_mgr = _PM()
    got = run(p.api_models())["models"]
    blob = _json.dumps(got, ensure_ascii=False)
    assert len(got) == 2, "两条都该保留（重名不是重复 ✗）实际 %d" % len(got)
    assert all(g.get("dup") for g in got), "重名必须被标记 ✓"
    assert [g["tag"] for g in got] == ["#1", "#2"], "区分标识应是可读序号 ✗ 实际 %r" % [g["tag"] for g in got]
    shown = _json.dumps([{k: g.get(k) for k in ("name", "provider", "tag")} for g in got], ensure_ascii=False)
    assert UUID_PID[:12] not in shown, "面板上不该出现 UUID ✗ 实际 %r" % shown
    assert all(g["provider"] == "commandcode goat" for g in got), "提供商显示名不应对 ✗"


def test_dup_tag_prefers_provider_type_before_index(tmp_path):
    """★ 用户建议（2026-10-15）：出 `#1/#2` 之前，**先用品提供商类型** ✓

    kira 注册的类型（OpenAI / DeepSeek / Anthropic…，含**插件实现**的自定义类型 ✓）
    区分度远高于序号 ✓ ⇒ 同名时优先用它 ✓；只有当类型也相同、才退到"类型 #n" ✓
    """
    p = build(tmp_path)

    class Info:
        def __init__(self, mid, nm, pn):
            self.model_id, self.model_name = mid, nm
            self.model_config = {}
            self.provider_name = pn
            self.model_type = "llm"

    A, B = "aaaaaaaa-1111-2222-3333-444444444444", "bbbbbbbb-1111-2222-3333-444444444444"

    class _PM:
        # 两家**显示名相同**（都叫"同名提供商" ✓）但**类型不同**（OpenAI / DeepSeek ✓）
        kira_config = {"providers": {A: {"name": "同名提供商", "format": "OpenAI", "model_config": {}},
                                     B: {"name": "同名提供商", "format": "DeepSeek", "model_config": {}}}}

        def get_all_providers(self):
            return {A: Info("x", "y", "同名提供商"), B: Info("z", "w", "同名提供商")}

        def get_model_infos(self, pid):
            return [Info("9f8e7d6c5b4a39281706f5e4d3c2b1a0", "同名模型", "同名提供商")]

    p.ctx.provider_mgr = _PM()
    got = run(p.api_models())["models"]
    tags = sorted(g["tag"] for g in got)
    assert tags == ["DeepSeek", "OpenAI"], "应先用品提供商类型区分 ✗ 实际 %r" % tags


def test_dup_tag_falls_back_to_type_numbered_when_type_is_same(tmp_path):
    """类型也相同 ⇒ 退到"类型 #n" ✓（仍是可读的 ✓，绝不吐 UUID ✓）"""
    p = build(tmp_path)

    class Info:
        def __init__(self, mid, nm, pn):
            self.model_id, self.model_name = mid, nm
            self.model_config = {}
            self.provider_name = pn
            self.model_type = "llm"

    A, B = "cccccccc-1111-2222-3333-444444444444", "dddddddd-1111-2222-3333-444444444444"

    class _PM:
        kira_config = {"providers": {A: {"name": "同名提供商", "format": "OpenAI", "model_config": {}},
                                     B: {"name": "同名提供商", "format": "OpenAI", "model_config": {}}}}

        def get_all_providers(self):
            return {A: Info("x", "y", "同名提供商"), B: Info("z", "w", "同名提供商")}

        def get_model_infos(self, pid):
            return [Info("11112222333344445555666677778888", "同名模型", "同名提供商")]

    p.ctx.provider_mgr = _PM()
    got = run(p.api_models())["models"]
    tags = sorted(g["tag"] for g in got)
    assert tags == ["OpenAI #1", "OpenAI #2"], "类型相同时应退到「类型 #n」✗ 实际 %r" % tags
    assert all("cccc" not in t and "dddd" not in t for t in tags), "绝不吐 UUID ✗"


def test_provider_log_brief_shows_head_and_tail_only(tmp_path):
    """★ 用户要求（2026-10-15）：日志**不许刷屏** ✓

    最多显示 **首 5 + 尾 5（共 10 个）**，中间 `……`，末尾 `等共 N 个` ✓
    例：`xxx……xxxx等共31个` ✓
    """
    p = build(tmp_path)                       # 建好包 alife_uib ✓（main 依赖框架 ✓ 不能裸导入 ✗）
    m = importlib.import_module("alife_uib.logfmt")
    brief = m.provider_log_brief

    small = ["甲(2)", "乙(1)"]
    assert brief(small) == "甲(2)、乙(1)", "≤10 个应全显示 ✓"

    exact = ["p%d(1)" % i for i in range(1, 11)]
    assert brief(exact) == "、".join(exact), "正好 10 个应全显示、且不加'等共' ✓"

    many = ["p%d(1)" % i for i in range(1, 32)]        # 31 家 ✓
    out = brief(many)
    assert out.startswith("p1(1)、p2(1)、p3(1)、p4(1)、p5(1)、……"), out
    assert out.endswith("p27(1)、p28(1)、p29(1)、p30(1)、p31(1)等共 31 个"), out
    assert out.count("p") == 10, "最多只该出现 10 个 ✓ 实际 %d" % out.count("p")
    assert len(out) < 200, "不能刷屏 ✓ 实际 %d 字符" % len(out)


def test_provider_log_name_never_uuid():
    """★ 用户要求（2026-10-15）：日志里的提供商名**不能是 UUID** ✗

    3.0 里 WebUI 建的 provider，id 就是 UUID ✓ 原来 `name or _pid` 会把它打出来 ✗
    ⇒ 源码断言：UUID 形状的备用名要被 `_readable_pid` 挡掉、并有可读占位 ✓
    """
    from pathlib import Path as _P
    main = (_P(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    assert "def _prov_name(" in main and "def _readable_pid(" in main, "缺少可读名解析 ✓"
    assert "未命名提供商" in main, "缺少可读占位 ✓"
    assert "len(_n) > 22 and _n.count(\"-\") >= 4" in main, "名字本身是 UUID 时也要挡掉 ✓"
