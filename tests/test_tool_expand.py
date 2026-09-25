"""主动召回扩池（2026-09-25）：查档案/看画像/overview 先扩候选再精修并截回 ✓

为什么要扩：精修只在**取回来的那批**里挑 ⇒ 池太小时，
「词法排不进前 N、但语义确实相关」的记忆根本没机会被看到 ✗
安全底线：未启用 JEV / 决策层未就绪 / inline 模式 ⇒ **逐字等于旧行为** ✓
"""

import importlib
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_toolx")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_toolx", pkg)

# main 依赖宿主框架（core.*）⇒ 需要 KIRA_CORE 指向真框架 ✓（没有就跳过整文件 ✓）
_CORE = os.environ.get("KIRA_CORE")
if _CORE and _CORE not in sys.path:
    sys.path.insert(0, _CORE)
try:
    main = importlib.import_module("alife_toolx.main")
except Exception as _exc:                        # noqa: BLE001
    pytest.skip("需要宿主框架（设 KIRA_CORE 指向真框架）：%r" % (_exc,),
                allow_module_level=True)

EXPAND = main.AlifeMemoryPlugin._tool_expand


class _Fake:
    def __init__(self, mode="expand", enabled=True, ready=True):
        self.settings = SimpleNamespace(
            tool_refine_mode=mode, jev_enabled=enabled, jev_recall=enabled)
        self.decisions = (SimpleNamespace(ready=True) if ready else None)


def test_expands_when_jev_ready():
    assert EXPAND(_Fake(), 20) == 60


def test_caps_pool_at_max():
    assert EXPAND(_Fake(), 50) == main.TOOL_POOL_MAX == 60


def test_tiny_limit_still_expands_but_not_below_want():
    assert EXPAND(_Fake(), 3) == 9
    assert EXPAND(_Fake(), 0) == 0


def test_inline_mode_never_expands():
    assert EXPAND(_Fake(mode="inline"), 20) == 20


def test_jev_disabled_never_expands():
    assert EXPAND(_Fake(enabled=False), 20) == 20


def test_not_ready_never_expands():
    """★ 关键安全线：配了但没就绪 ⇒ 不扩（否则扩了没人筛 ⇒ 反而塞更多 ✗）"""
    assert EXPAND(_Fake(ready=False), 20) == 20


def test_bad_settings_never_raise():
    class _Broken:
        @property
        def settings(self):
            raise RuntimeError("boom")

        decisions = None

    assert EXPAND(_Broken(), 20) == 20


# ── 附注必须**真的到模型手里** ✓ ─────────────────────────────────────────────
# 2026-09-25 踩到：出口会把结果渲染成紧凑文本，而渲染器只认几种"召回形状" ✗
# ⇒ 直接往返回值里塞的 refine 键被**静默丢掉** ✗ ⇒ 附注等于没写 ✗
# （文本路径成功 ⇒ JSON 兜底不触发 ⇒ 表面上"有字段"、实际模型看不到 ✗）

def _render(value, raw="【召回】命中 46 · 本次 1"):
    """隔离测「附注层」：渲染器本体换成桩 ✓（我的改动就是渲染完再加一句 ✓）"""
    cls = main.AlifeMemoryPlugin
    fake = SimpleNamespace(_recall_text_view_raw=lambda v: raw,
                           settings=SimpleNamespace())
    return cls.recall_text_view(fake, value)


def test_refine_note_reaches_rendered_text():
    """★ 附注必须真的到模型手里 ✓（渲染器只认已知形状 ⇒ 直接塞键会被静默丢掉 ✗）"""
    text = _render({"items": [{"i": "a1"}], "refine": {"pool": 60, "kept": 18}})
    assert "从 60 条候选里精修保留 18 条" in text, text


def test_no_note_when_nothing_filtered():
    assert "精修保留" not in _render({"items": [{"i": "a1"}]})


def test_note_ignored_when_render_returns_none():
    """非召回形状（渲染返回 None ⇒ 走 JSON 兜底）时不许崩、也不许拼出半截文本 ✓"""
    assert _render({"ok": True, "refine": {"pool": 60, "kept": 3}},
                   raw=None) is None


def test_note_ignored_when_pool_missing():
    assert "精修保留" not in _render({"items": [], "refine": {"kept": 3}})


# ── 集成：开 JEV 且就绪时，工具**真的**用更大的池取候选 ✓ ──────────────────────
sys.path.insert(0, str(ROOT / "tests"))
from test_name_refresh import _plugin          # noqa: E402  轻量插件 + 真 store ✓


class _NoopEngine:
    async def enqueue(self, *a, **k):
        return None


@pytest.mark.asyncio
async def test_search_tool_expands_pool_when_jev_ready(tmp_path):
    plugin, store = _plugin(tmp_path)
    plugin.engine = _NoopEngine()
    sid = "qq:gm:1"
    for i in range(3):
        store.capture(sid, "e%d" % i,
                      [{"role": "user", "content": "关于喵梓的记忆%d" % i,
                        "time": float(i), "users": []}])
    limits = []
    orig = store.call

    async def spy(name, *a, **k):
        if name == "search":
            limits.append(k.get("limit"))
        return await orig(name, *a, **k)

    store.call = spy
    ev = SimpleNamespace(sid=sid, event_id="evt", messages=[],
                         session=SimpleNamespace(adapter_name="qq"))
    await plugin.search_archive(ev, keyword="喵梓", count=5)
    assert limits[-1] == 5, "JEV 关 ⇒ 不扩池（逐字等于旧行为 ✓）"

    plugin.settings.jev_enabled = True
    plugin.settings.jev_recall = True
    plugin.decisions = SimpleNamespace(
        ready=True, recall_filter=_no_scores, last_trigger=0.0)
    text = await plugin.search_archive(ev, keyword="喵梓", count=5)
    assert limits[-1] == 15, "JEV 就绪 ⇒ 池 ×3 ✓（拿到的实际值 %r）" % (limits[-1],)
    assert "命中" in text


async def _no_scores(*a, **k):
    return []                                   # 决策层桩：不返回分数 ⇒ 保持原序 ✓
