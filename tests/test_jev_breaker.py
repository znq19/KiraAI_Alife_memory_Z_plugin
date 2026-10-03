"""JEV 熔断器：失败计数、冷却、半开重试、以及**冷却后必须重新熔断** ✓

背景（2026-09-29 实测）：`_bad()` 里写的是 `if self.fails == 3:` ✗
  ⇒ 首次冷却 300 秒到期后，若服务**仍然不可用**，fails 继续涨（4/5/6…）✗
  ⇒ `opened_at` 再也不刷新 ⇒ `ready` 恒为 True ⇒ **熔断再也不会开启** ✗
  ⇒ 后果：每次符合条件的调用都真去打一次接口，每次都白付 jev_timeout_ms ✗
  （契约见 mdecide.JevClient docstring：「连续失败 3 次 → 冷却 300 秒内直接返回 None（不再占用时间）」✗→✓）
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_jevbrk")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_jevbrk", pkg)
m = importlib.import_module("alife_jevbrk.mdecide")
run = asyncio.run


def _client():
    c = m.JevClient("http://127.0.0.1:9", "fake-key", "jev-latest", 0.4)

    def boom(*a, **k):
        raise RuntimeError("service down")

    c._post_sync = boom          # 不碰网络 ✓ 确定性失败 ✓
    return c


def _fail(c):
    return run(c.call("t", {"q": {"type": "check"}}))


def test_three_failures_open_the_breaker():
    c = _client()
    for _ in range(3):
        assert _fail(c) is None
    assert c.fails == 3 and not c.ready, "连续失败 3 次必须熔断 ✓"


def test_breaker_window_skips_without_calling():
    c = _client()
    for _ in range(3):
        _fail(c)
    called = {"n": 0}
    orig = c._post_sync

    def spy(*a, **k):
        called["n"] += 1
        return orig(*a, **k)

    c._post_sync = spy
    for _ in range(3):
        assert _fail(c) is None
    assert called["n"] == 0, "熔断窗口内**不该**发起调用（应秒回 ✓）"
    assert c.fails == 3, "熔断期间不该继续累加失败 ✓"


def test_success_resets_the_breaker():
    c = _client()
    for _ in range(3):
        _fail(c)
    c.opened_at -= 301                     # 冷却到期
    assert c.ready
    c._post_sync = lambda *a, **k: {"answers": {"q": 1}}   # 这次成功 ✓
    assert run(c.call("t", {"q": {"type": "check"}})) is not None
    assert c.fails == 0 and c.ready, "一次成功必须清零并恢复 ✓"


def test_breaker_reopens_after_cooldown_if_still_failing():
    """★ 本次修的核心：冷却到期后再失败 ⇒ 必须**重新熔断** ✗→✓"""
    c = _client()
    for _ in range(3):
        _fail(c)
    c.opened_at -= 301                     # 模拟冷却 300 秒到期（半开 ✓）
    assert c.ready, "冷却到期应允许再试一次 ✓（半开 ✓）"
    for i in range(3):
        assert _fail(c) is None
        assert not c.ready, "第 %d 次连续失败后必须重新熔断 ✗（否则会一直白打接口 ✗）" % (i + 1)


def test_cooldown_left_and_last_error_are_exposed():
    """面板小灯依赖这两个状态：熔断剩余秒数 + 最近失败原因 ✓"""
    c = _client()
    assert c.cooldown_left == 0 and c.last_error == "", "初始应为未熔断、无失败原因 ✓"
    for _ in range(3):
        _fail(c)
    assert c.cooldown_left > 290, "熔断后应给出剩余冷却秒数 ✓（拿到 %s）" % c.cooldown_left
    assert c.last_error, "应记下最近一次失败原因 ✓"
    c.opened_at -= 301
    assert c.cooldown_left == 0, "冷却到期 ⇒ 剩余归零 ✓（半开 ✓）"


def test_status_snapshot_shape():
    """Decisions.status()：只读快照、字段齐全、不抛 ✓（面板 /status 直接用 ✓）"""
    st = m.Decisions(None).status()
    for k in ("enabled", "ready", "why", "model", "has_key", "fails", "cooldown_left", "last_error"):
        assert k in st, "status() 缺字段 %s ✗" % k
    assert st["enabled"] is False and st["why"] == "未启用"


def test_frontend_jev_chip_is_wired():
    """结构判据：面板必须有 JEV 状态灯 ✓
    （元素**写在 index.html** ✓ 由 poll() 更新 ✓ —— 不在运行时 createElement 插入 ✗）"""
    h = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    j = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="jevTag"' in h, "index.html 缺 #jevTag ✗"
    assert "next.jev" in j and '"#jevTag"' in j, "app.js 未渲染 JEV 状态灯 ✗"
