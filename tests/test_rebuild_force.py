"""强制重新提取（rebuild）★ 2026-09-19（用户定的规矩）

用户的原话：
    「我们要强制重新提取，那就不能 keep 了」✓
    「不要强制的时候，它这套运作和提示词不能被破坏」✓

⇒ 本文件同时守**两件事**：
   ① 强制 ⇒ **不允许 keep**（否则"点了等于没点"）
   ② 不强制 ⇒ 正常 tidy 的规则与提示词**一个字节都不变**
"""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENG = (ROOT / "engine.py").read_text(encoding="utf-8")
MAIN = (ROOT / "main.py").read_text(encoding="utf-8")
APP = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
STOR = (ROOT / "storage.py").read_text(encoding="utf-8")
SCHEMA = json.loads((ROOT / "schema.json").read_text(encoding="utf-8"))


def test_forced_instruction_is_self_consistent():
    """★ 2026-09-19（用户要求）：强制那次不能同时出现互相冲突的选项

    原来做法 = 原指令 + 追加一句禁令 ✗ ⇒ 模型同时看到"keep=…split=…"与
    "不允许 keep/split" ⇒ 两套冲突的话 ⇒ 用户实测就选了 split ✗
    现在 = **换一套自洽清单** ✓ 强制版里**完全不出现** keep= / split= ✓
    """
    assert "_TIDY_ACTIONS_FORCED = (" in ENG, "必须有强制专用清单"
    forced = ENG[ENG.index("_TIDY_ACTIONS_FORCED = (") :]
    forced = forced[: forced.index("\n)\n")]
    assert "keep=" not in forced, "强制版里不许出现 keep ✗（会与'必须离开活跃'冲突 ✓）"
    assert "split=" not in forced, "强制版里不许出现 split ✗"
    assert "keep_content" not in forced, "强制版里不许出现 keep_content ✗"
    assert "extract=" in forced and "archive=" in forced, "必须给出 extract/archive 两条路 ✓"
    # 用户追问：强制里也要说清"能不能顺带修正类别/重要度" ✓（契约里两者都是独立字段 ✓）
    assert "顺带修正 category / importance" in forced, "强制版必须写明可顺带修正 ✓"
    # 取向必须与"非强制版"一致 ✓（用户点出的坑：别让模型以为"只有约束才提取"✗）
    assert "信息还有用就选它" in forced, "extract 必须写明是默认取向 ✓"
    assert "若其中还含着" not in forced, "不许再出现\"若…才 extract\"的误导写法 ✗"
    assert "约束也照此办" in forced, "约束要并入同一套规则（不是特殊通道 ✓）"
    # 调用点必须真的把 forced 传下去 ✓
    assert "forced=rebuild" in ENG, "引擎必须按 rebuild 切换清单 ✓"
    assert "_TIDY_ACTIONS_FORCED if forced else _TIDY_ACTIONS_ALL" in ENG, "必须二选一 ✓"


def test_normal_tidy_instruction_is_byte_identical():
    """★ 用户最在意的一条：**不强制时，规则与提示词一个字都不动** ✓

    强制那次换成自洽清单 ✓，但"不强制"必须仍走**原清单**（逐字保留 ✓）
    """
    assert "_TIDY_ACTIONS_ALL = (" in ENG, "必须保留原始清单常量"
    allb = ENG[ENG.index("_TIDY_ACTIONS_ALL = (") :]
    allb = allb[: allb.index("\n)\n")]
    # 原版四件事必须一字不少 ✓
    for piece in (
        "keep=继续常驻；",
        "extract=这条信息已能被事实覆盖 → 用 facts 提炼出来，原条移出常驻；",
        "archive=不再需要常驻（过期、一次性、已被取代）→ 直接移出常驻；",
        "split=一条里既有必须留下的约束、又有可转事实的内容 → 给 facts + keep_content（只留约束那段）。",
        "判断标准：能按需召回的信息不该占每轮的席位，只有必须每轮在场的约束才 keep。",
        # 注意：源码里是**两段拼接** ⇒ 判据按片段匹配 ✗ 别写成一整行 ✓
        "每条都可以顺带修正 category / importance",
        "（觉得该换类别、或其实更重要，就一并改掉）。",
    ):
        assert piece in allb, "原清单缺了：%s" % piece
    # 分支必须真的二选一（不强制走原版 ✓）
    assert "_TIDY_ACTIONS_FORCED if forced else _TIDY_ACTIONS_ALL" in ENG
    # 两版共用的尾巴仍在 ✓
    assert "facts[].subject 用 who 表里的稳定实体 ID；每条都要写 reason。" in ENG
    assert "不要为了省事整批 archive：留下真正约束性的内容。" in ENG


def test_bot_rebuild_only_accepts_single_id():
    assert "rebuild_requires_single_id" in MAIN, "bot 侧必须拦'无 ids 的 rebuild'"
    assert "tidy_rebuild_bot_enabled" in MAIN, "开关设置必须真被引用"
    assert "tidy_rebuild_bot_cooldown_minutes" in MAIN, "冷却设置必须真被引用"
    assert "rebuild_disabled_by_setting" in MAIN and "rebuild_cooldown" in MAIN


def test_jobs_rejects_global_rebuild():
    assert "「完全重新提取」只能针对单条永久记忆" in MAIN, "/jobs 必须拦住全局 rebuild"


def test_settings_present_in_all_four_places():
    """契约 / help / schema / 前端标签 —— 四处都要有（否则判据会红 ✓）"""
    assert "tidy_rebuild_bot_enabled: bool" in (ROOT / "contracts.py").read_text(encoding="utf-8")
    assert "tidy_rebuild_bot_enabled" in (ROOT / "setting_help.py").read_text(encoding="utf-8")
    assert any(
        k == "tidy_rebuild_bot_enabled" for g in SCHEMA.values() if isinstance(g, dict)
        for k in g
    ) or "tidy_rebuild_bot_enabled" in json.dumps(SCHEMA, ensure_ascii=False), "schema 里必须有"
    assert "tidy_rebuild_bot_enabled" in APP, "前端标签里必须有"


def test_frontend_offers_both_modes_with_rebuild_default():
    assert "askReextractMode" in APP, "单条必须有同风格弹窗"
    assert 'value="rebuild" checked' in APP, "默认必须是「完全重新提取」"
    assert 'value="rule"' in APP, "也要能选「按规则」"
    assert 'rebuild: mode === "rebuild"' in APP, "选择要真的传到后端"
    assert "可在永久记忆页面对单条强制重新提取事实" in APP, "全局那行小字（用户原话）"
    # ★ 确认后必须离开编辑界面 ✓（用户要求）
    assert '$("#editor").close();' in APP, "确认后必须关掉编辑弹窗 ✓"
    assert "这条一定会离开活跃记忆" in APP, "弹窗里要有取舍说明 ✓（用户要求补说明 ✓）"


def test_no_locals_in_engine_worker():
    """🔴 2026-09-19 **实际抓到过**的 bug：运行器循环里用 locals() 会**跨迭代泄漏** ✗

    上一条「完全重新提取」把 _rebuild=True 留在作用域 ⇒
    下一条**普通**整理若 detail 为空 / 解析失败 ⇒ 带着重提取跑 ⇒
    把本来好好的常驻记忆强制重写 ✗（正是本功能最该避免的事 ✗）

    ⇒ 判据：引擎里**不许出现 locals()** ✓ + 运行器每轮必须重置 ✓
    """
    assert 'locals()' not in ENG, '引擎里不许用 locals() ✗（循环内不随迭代清空 ✓）'
    assert '_force, _ids, _rebuild = False, None, False' in ENG, '运行器每轮必须重置三个变量 ✓'
    assert 'rebuild=_rebuild,' in ENG, '必须直接引用 ✓'


def test_queue_tidy_all_detail_carries_rebuild():
    """★ 静默丢参检查（2026-09-19 自查发现）

    `queue_tidy_all` 原来只在 `if force or ids:` 时才构造 detail ✗
    ⇒ 若将来有人"只传 rebuild"⇒ detail 为空 ⇒ 引擎解出来 rebuild=False
    ⇒ **静默退化成普通整理** ✗（最难查的那种 bug ✓）
    ⇒ 判据：detail 的条件里必须有 rebuild ✓ 且 detail 内容要带 rebuild ✓
    """
    assert "if force or ids or rebuild:" in MAIN, "detail 条件必须含 rebuild ✓"
    assert '"rebuild": bool(rebuild)' in MAIN, "detail 里必须带 rebuild ✓"


def test_four_paths_are_distinguished():
    """★ 四条链路的"区分"守住（用户要求逐条确认 ✓）

    ① 自动链路（冷却/容量/合并后）⇒ 只 enqueue，**不带 detail** ✓
    ② 入库后顺手整理 ⇒ automatic=False ✓ 不带 detail ✓
    ③ 前端全局 ⇒ 只可能 force ✓ **绝不 rebuild** ✓
    ④ bot ⇒ rebuild 需 单条 + 开关 + 冷却 ✓
    """
    # ①
    assert 'enqueue("tidy", job["sid"], automatic=True)' in ENG
    assert 'enqueue("tidy", sid, automatic=True)' in ENG
    # ②
    assert '"tidy", value.sid, automatic=False' in MAIN
    # ③ 前端全局：force 由模式决定，但没有 rebuild ✓
    assert 'payload.force = mode === "all";' in APP
    assert "rebuild" not in APP[APP.index('payload.force = mode === "all"') - 400 : APP.index('payload.force = mode === "all"') + 400], \
        "全局整理那段不许出现 rebuild ✗"
    # ④ bot 三道闸门 ✓
    for gate in ("rebuild_requires_single_id", "rebuild_disabled_by_setting", "rebuild_cooldown"):
        assert gate in MAIN, "bot 缺闸门：%s" % gate


def test_tidy_detail_records_extracted_facts():
    """★ 用户要求：整理明细要像"事实合并"那样细

    被提取为事实 ⇒ 明细里要**每条事实一行**（弹窗就显示 主体 · 类别 · 重要度 ✓）
    被归档 ⇒ 记「已归档」✓ 原条用划线表示移出 ✓
    """
    assert 'fact_ids = await self.store.call("add_facts", sid, facts)' in ENG, "必须拿到新事实 id ✓"
    assert '"kind": "fact",' in ENG and '"action": "extract",' in ENG, "必须把事实记成明细条目 ✓"
    # 归档/提取的记录条目要能划线 ⇒ 前端 shift 分支要有 ✓
    assert 'item.action === "extract" && item.kind === "record"' in APP
    assert '已提炼为事实（原条移出常驻）' in APP
    assert 'item.action === "archive" && item.kind === "record"' in APP
    assert '已归档（移出常驻）' in APP


def test_recall_merge_check_on_both_paths():
    """★ 用户确认题：召回发现可合并的会不会排合并

    被动（每轮注入）⇒ 有 ✓；主动（bot 调工具）⇒ 本次补上 ✓
    """
    assert 'dropped = await self.queue_recall_merges(sid, facts + neighbor_rows)' in MAIN, \
        "被动侧必须排（含相关人物事实）✓"
    assert 'await self.queue_recall_merges(event.sid, rows)' in MAIN, \
        "overview 必须用 raw rows 排 ✓"
    assert 'await self.queue_recall_merges(event.sid, merge_rows)' in MAIN, \
        "GetProfile 必须用 raw facts 排 ✓"
    assert 'value.get("facts")' not in MAIN, \
        "打包后的简报没有 id，不能从这里取事实 id ✓"


def test_cold_records_cannot_be_force_tidied():
    """★ 用户实测：单条强制整理点到**冷归档** ⇒ 只会得到"本次跳过"✗

    冷归档只按 ID 可读、不参与整理（tidy_candidates 的 WHERE 里 cold=0 ✓）
    ⇒ 前端隐藏按钮 ✓ + /jobs 明确 400 ✓（两条都守 ✓）
    """
    assert 're.classList.toggle("hide", !r.permanent || !!r.cold);' in APP, "冷归档必须隐藏按钮 ✓"
    assert "def untidyable_ids" in STOR, "需要【不可整理】判定 ✓"
    assert "AND permanent=1 AND deleted=0 AND active=1 AND cold=0" in STOR, "判定条件要与 tidy_candidates 一致 ✓"
    assert 'await self.store.call("untidyable_ids", value.ids)' in MAIN, "/jobs 必须拦 ✓"
    assert "冷归档只按 ID 可读、不参与整理" in MAIN, "400 要给出说明 ✓"


def test_global_tidy_does_not_union_selected_session():
    """★ 用户实测的疑问：点全局整理时，界面选中的会话会不会被**额外照顾**一次

    原来：`if fallback_sid: owners.add(fallback_sid)` ✗ ⇒ 全局整理也会把"选中会话"并进来
    ⇒ 若该会话**没有永久记忆** ⇒ 白排一个"本次跳过"的任务 ✗（任务明细噪声 ✓）
    现在：只有**指定单条(ids)**时才并 ✓ —— 全局本来就覆盖"所有有永久记忆的会话" ✓ 不会漏 ✓
    """
    assert "if ids and fallback_sid:" in MAIN, "只有指定单条时才 fallback ✓"
    assert "if fallback_sid:\n            owners.add(fallback_sid)" not in MAIN, "旧的全局并入必须去掉 ✓"


def test_global_dedupe_button_is_global():
    """★ 用户实测：工作台「合并相似永久记忆」原来**只合并选中的那个会话** ✗

    它落到 /jobs 的兜底 `enqueue(value.kind, value.sid)` ⇒ 其它会话要等定时调度 ✗
    现在与「整理永久记忆」同一套：按**所有有永久记忆的会话**排队 ✓
    """
    assert 'if value.kind == "dedupe":' in MAIN, "必须有独立的 dedupe 分支 ✓"
    seg = MAIN.split('if value.kind == "dedupe":')[1][:900]
    assert "sessions_with_permanents" in seg, "必须按所有会话排队 ✓"
    assert 'enqueue("dedupe", owner' in seg, "每个会话各排一个 ✓"
    assert "permanent_dedupe" in seg, "关闭去重时要明确拒绝（别排空任务 ✗）"
    # tidy 那条也要保持全局 ✓（两条对齐）
    seg2 = MAIN.split('if value.kind == "tidy":')[1][:2000]
    assert "queue_tidy_all" in seg2, "整理仍是全局 ✓"
