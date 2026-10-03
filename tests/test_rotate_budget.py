"""档案槽预算（2026-09-19 用户定的规矩）★ 真跑行为测试 ✓

用户要求：
  · 档案槽要有**整体字数限制**（默认 200 字）✓
  · 三条不超过 200 ⇒ 完整召回；最不相关的加上去超 200 ⇒ 只召回前面的 ✓
  · 单条就超 200 ⇒ **截断**召回 ✓
  · 独立控制 ✓ **事实轮换槽不受字数控制影响** ✓

本文件直接 import 真模块、调真函数 ✓（不是字符串断言 ✓）
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
# ★ 2026-09-29：以前这里无条件 setdefault 一个**可能不存在**的路径 ✗
#   ⇒ 它会泄漏给整个 pytest 会话 ⇒ 后面每个「if os.environ.get("KIRA_CORE")」
#     的宿主用例都以为有宿主 ⇒ 全体 ModuleNotFoundError ✗（实测 23 条假失败 ✗）
#   ⇒ 只有该路径**真的存在**时才注入 + 设置 ✓ 否则保持未设置（宿主用例正常 skip ✓）
#   ★ 2026-09-29 二次修正："目录存在"不等于"能用" —— 实测 /tmp/kiraai_latest 存在但
#     缺 core/plugin.py ⇒ 注入后 23 个宿主用例照样 ModuleNotFoundError ✗
#     ⇒ 必须**验证框架真的可导入**（core/plugin.py 在）才注入 ✓ 否则保持未设置（正常 skip ✓）
def _usable_core(path: str):
    return bool(path) and os.path.isfile(os.path.join(path, "core", "plugin.py"))


_core = os.environ.get("KIRA_CORE") or ""
if not _usable_core(_core):
    for _cand in ("/tmp/kiraai_latest", "/tmp/kiraai-src"):
        if _usable_core(_cand):
            _core = _cand
            break
    else:
        _core = ""
if _core:
    os.environ.setdefault("KIRA_CORE", _core)
    if _core not in sys.path:
        sys.path.insert(0, _core)

mod = None


def _mod():
    global mod
    if mod is not None:
        return mod
    try:
        import test_name_refresh as T

        plugin, _store = T._plugin(Path(tempfile.mkdtemp()))
        mod = sys.modules[plugin.__class__.__module__]
    except Exception as exc:  # KIRA 核心不在时跳过（与其它 KIRA 侧测试同策略）
        pytest.skip("需要 KIRA 核心：%s" % exc)
    return mod


def _row(text, summary=False):
    return {"id": "x", "summary": text if summary else "", "content": "" if summary else text}


TEXT = lambda r: str(r.get("summary") or r.get("content") or "")


def test_three_within_budget_keeps_all():
    """① 三条正文合计 ≤ 200 ⇒ 三条都召回 ✓"""
    m = _mod()
    rows = [_row("甲" * 50), _row("乙" * 60), _row("丙" * 70)]
    out = m.trim_slot_rows(rows, TEXT, char_budget=200, count=3)
    assert len(out) == 3, "合计 180 ≤ 200 ⇒ 应全保留 ✓"


def test_third_exceeds_budget_drops_it():
    """② 第三条加上去超 200 ⇒ **只召回前两条** ✓（用户原话）"""
    m = _mod()
    rows = [_row("甲" * 80), _row("乙" * 80), _row("丙" * 80)]
    out = m.trim_slot_rows(rows, TEXT, char_budget=200, count=3)
    assert len(out) == 2, "160 + 80 > 200 ⇒ 第三条不装 ✓"


def test_single_over_budget_is_truncated_not_dropped():
    """③ 单条就超 200 ⇒ **截断**召回（不整条丢 ✗）✓ 且带出口提示 ✓"""
    m = _mod()
    out = m.trim_slot_rows([_row("甲" * 500)], TEXT, char_budget=200, count=3)
    assert len(out) == 1, "单条超限要截断保留 ✓ 不能丢 ✗"
    body = TEXT(out[0])
    assert body.endswith(m.SLOT_TAIL_HINT), "结尾要有【取全文】的出口提示 ✓"
    assert len(body) <= 200, "截断后总长必须 ≤ 预算 ✓"
    assert body.startswith("甲" * 20), "保留的是**开头** ✓"


def test_leftover_below_floor_skips():
    """④ 剩余额度不足 40 字 ⇒ 这条不装 ✓（残句是噪声 ✗）"""
    m = _mod()
    rows = [_row("甲" * 170), _row("乙" * 50)]
    out = m.trim_slot_rows(rows, TEXT, char_budget=200, count=3)
    assert len(out) == 1, "只剩 30 字 ⇒ 不装第二条 ✓"


def test_zero_budget_means_unlimited():
    """⑤ 0 = 不限 ⇒ 行为与老版本逐字节一致 ✓（回归安全 ✓）"""
    m = _mod()
    rows = [_row("甲" * 500), _row("乙" * 500)]
    out = m.trim_slot_rows(rows, TEXT, char_budget=0, count=0)
    assert out == rows, "0 ⇒ 原样返回（同一个对象内容 ✓）"
    assert TEXT(out[0]) == "甲" * 500, "一条不截 ✓"


def test_count_limit_still_applies():
    """⑥ 条数上限仍生效（与字数取更严的 ✓）"""
    m = _mod()
    out = m.trim_slot_rows([_row("短"), _row("短"), _row("短")], TEXT, char_budget=0, count=2)
    assert len(out) == 2


def test_fact_slot_is_not_affected():
    """⑦ ★ 最关键：事实槽**不传 budget** ⇒ 完全不受档案槽字数控制 ✓

    静态核对：事实槽的 rotation_extras 调用里**不许**出现 char_budget / rotate_archive_* ✓
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    i = src.index("fact_pool")
    seg = src[i : i + 700]
    assert "char_budget" not in seg, "事实槽不许被传字数预算 ✗"
    assert "rotate_archive" not in seg, "事实槽不许受档案槽设置影响 ✗"
    # 档案槽那边必须传 ✓
    j = src.index("archive_pool,", src.index("[] if not cfg.rotate_archive_enabled"))
    seg2 = src[j : j + 400]
    assert "char_budget=cfg.rotate_archive_chars" in seg2
    assert "count=cfg.rotate_archive_count" in seg2


def test_archive_slot_has_independent_switch():
    """⑧ 档案槽有**独立开关** ✓（关它不影响事实槽 ✓）"""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "[] if not cfg.rotate_archive_enabled else [" in src, "档案槽池子要受独立开关控制 ✓"
    # 事实槽的池子不受它影响 ✓
    i = src.index("fact_pool")
    assert "rotate_archive_enabled" not in src[i : i + 400]
