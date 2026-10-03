"""纯删除 / force 覆盖必须能跳过版本比对（2026-09-29 用户实测；默认可跑，不依赖宿主框架）。

前端链路（web/app.js:2070 → /edit）：
    「确认删除」= POST /edit {patch:{"deleted":true}}
    ⇒ main.edit_skip_revision 判 skip=True（纯删除 ✓ 或 force ✓）
    ⇒ 契约：调用方把 revision 置为 None 表示「不比对」✓
    ⇒ store.edit 必须把 **None 解释为不比对** ✓

真实事故：store.edit 写的是 `old["revision"] != revision` ⇒ None 永不相等 ✗
    ⇒ 前端「确认删除」「以我的版本覆盖」**必然 409** ✗（用户：「删不掉」✗）
    同仓库 names 路径（storage.py:1184）本来就是 `revision is not None and ...` ✓
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_delskip")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_delskip", pkg)
s = importlib.import_module("alife_delskip.storage")
c = importlib.import_module("alife_delskip.contracts")
run = asyncio.run

SID = "qq:dm:1"
Conflict = getattr(c, "Conflict", None) or __import__("importlib").import_module("alife_delskip.storage").Conflict


def _seed(tmp):
    st = s.Store(tmp / "m.db")
    st.initialize()
    st.capture(SID, "turn", [
        {"role": "user", "content": "打开百度首页", "users": ["qq:1"], "time": 0.0},
        {"role": "assistant", "content": "Bot打开的是百度首页。", "users": ["qq:1"], "time": 1.0},
    ])
    return st, st.active(SID)[-1]["id"]


def _row(st, rid):
    with st.connect() as db:
        return st.row(db.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone())


def test_none_revision_skips_check_on_delete(tmp_path):
    """前端「确认删除」的真实去向：revision=None（跳过）+ deleted=True ⇒ 必须成功 ✓"""
    st, rid = _seed(tmp_path)
    run(st.call("edit", kind="record", target=rid, revision=None,
                patch={"deleted": True}, reason="面板：确认删除"))
    assert _row(st, rid)["deleted"] == 1


def test_none_revision_skips_check_on_force_overwrite(tmp_path):
    """UI 明确确认「以我的版本覆盖」：revision=None + 真编辑 ⇒ 必须成功 ✓"""
    st, rid = _seed(tmp_path)
    run(st.call("edit", kind="record", target=rid, revision=None,
                patch={"summary": "覆盖后的摘要"}, reason="面板：以我的版本覆盖"))
    assert _row(st, rid)["summary"] == "覆盖后的摘要"


def test_stale_revision_without_skip_still_conflicts(tmp_path):
    """反向保护：**没点覆盖**的真编辑带着过期版本号 ⇒ 仍必须 409 ✓（内容不能被悄悄盖掉 ✓）"""
    st, rid = _seed(tmp_path)
    with pytest.raises(Conflict):
        run(st.call("edit", kind="record", target=rid, revision=99,
                    patch={"summary": "不该写进去"}, reason="过期"))
    assert _row(st, rid)["summary"] == "Bot打开的是百度首页。"


def test_none_revision_still_requires_row_to_exist(tmp_path):
    """跳过比对 ≠ 跳过存在性检查：目标不存在 ⇒ 仍 Conflict ✓"""
    st, _rid = _seed(tmp_path)
    with pytest.raises(Conflict):
        run(st.call("edit", kind="record", target="不存在的id", revision=None,
                    patch={"deleted": True}, reason="探针"))


def test_deleted_row_can_be_restored(tmp_path):
    """删除后可还原（回收站链路 ✓ 与前端「恢复到活跃记忆」同源 ✓）"""
    st, rid = _seed(tmp_path)
    run(st.call("edit", kind="record", target=rid, revision=None,
                patch={"deleted": True}, reason="删除"))
    run(st.call("edit", kind="record", target=rid, revision=None,
                patch={"deleted": False}, reason="还原"))
    row = _row(st, rid)
    assert row["deleted"] == 0 and row["active"] == 1


# ────────────────────────────────────────────────────────────────
# 事实体检页（app.js:2864/2874）的两处 422 修复（2026-09-29）
# ────────────────────────────────────────────────────────────────

def _fact(tmp):
    st = s.Store(tmp / "m.db")
    st.initialize()
    res = st.add_facts(SID, [{
        "category": "fact", "subject": "qq:1", "content": "用户对花生严重过敏",
        "reason": "", "scenario": "", "tags": [], "relations": [],
        "source_ids": [], "importance": 5,
    }])
    if asyncio.iscoroutine(res):
        res = run(res)
    fid = res[0] if isinstance(res, (list, tuple)) and res else res
    return st, fid


def test_fact_importance_is_editable(tmp_path):
    """体检页「重要度 ±1」：facts 白名单必须允许 importance ✓
    （以前漏了 ⇒ 必然 422 "invalid editable fields" ✗ 且前端只弹误导 toast ✗）"""
    st, fid = _fact(tmp_path)
    run(st.call("edit", kind="fact", target=fid, revision=None,
                patch={"importance": 3}, reason="WebUI 体检：调整重要度"))
    with st.connect() as db:
        row = st.row(db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
    assert row["importance"] == 3


def test_fact_delete_is_editable(tmp_path):
    """体检页「删除」：patch {deleted: True} 对 fact 必须走通 ✓"""
    st, fid = _fact(tmp_path)
    run(st.call("edit", kind="fact", target=fid, revision=None,
                patch={"deleted": True}, reason="WebUI 体检：删除事实"))
    with st.connect() as db:
        row = st.row(db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
    assert row["deleted"] == 1


def test_all_frontend_edit_calls_pass_reason():
    """结构判据：web/app.js 里每个 /edit 调用都必须能拿到 reason ✓

    合法形态两种：
      ① 内联对象字面量直接写 reason ✓（体检页两处曾漏传 ⇒ 422 + 3.5 秒消失的误导提示 ✗）
      ② 传变量 payload ✓（配套断言：payload 组装处必须写明 reason ✓）
    """
    lines = (ROOT / "web" / "app.js").read_text(encoding="utf-8").splitlines()
    bad = []
    for i, line in enumerate(lines):
        if '"/edit"' not in line:
            continue
        window = "".join(lines[max(0, i - 30):i + 6])
        if "reason" not in window and "payload" not in window:
            bad.append(i + 1)
    assert not bad, "这些 /edit 调用拿不到 reason：%s" % bad
    assert 'reason: "WebUI 人工编辑"' in "".join(lines), "saveEdit 的 payload 里必须有 reason ✓"


def test_record_detail_manages_delete_button():
    """结构判据：记录详情必须显式管理 #delete 显隐 ✓
    （openFact/任务明细是 remove ✓ newMemory 是 hide ✓ 记录详情此前两边都不做 ✗）"""
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    seg = src[src.index("function renderRecord()"):][:3000]
    assert '#delete").classList.remove("hide")' in seg


def test_reextract_button_hidden_outside_record_editor():
    """结构判据：#reextract 由 renderRecord 动态创建 ⇒ openFact / newMemory 必须收起它 ✓
    （否则它会残留，onclick 仍是 record 分支的闭包 ⇒ 点了语义错位 ✗）"""
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    for fn in ("async function openFact(", "function newMemory("):
        assert fn in src, fn
        seg = src[src.index(fn):][:2600]
        assert "#reextract" in seg, "「%s」里没有收起 #reextract ✓" % fn


def test_frontend_p2_robustness():
    """P2 健壮性（2026-09-29 复查补齐）：

    ① 画像卡片 `f.tags` / `f.sources` 必须兜底 —— healthCard 早就 `(f.tags||[])` ✓
       两处写法要一致 ✓（同一批字段，一处防一处不防 ⇒ 说明确实可能缺 ✗）
    ② openProfile 必须**先清空** profileData —— GET /profile 404 时不能把**上一个实体**
       的旧数据留在 profileData 里 ✗（「改名字」按钮会拿它去开名称弹窗 ✗）
    ③ 冷归档记录保存摘要要明确提示「正文要用取回」✓（否则用户以为改了正文 ✗）
    ④ 非 409 的配置保存失败要把状态条写成失败态 ✓
       （以前会留着上一次的「已保存，配置立即生效」✗ 误导用户 ✓）
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'esc((f.tags || []).join(" · "))' in src, "画像卡片 tags 未兜底 ✗"
    assert "${(f.sources || []).length} 个来源" in src, "画像卡片 sources 未兜底 ✗"
    assert "profileData = null;" in src, "openProfile 未先清空 profileData ✗"
    assert "冷归档正文要用「取回」" in src, "冷归档保存摘要缺提示 ✗"
    assert "保存失败：未生效" in src, "配置保存失败态缺失 ✗"
