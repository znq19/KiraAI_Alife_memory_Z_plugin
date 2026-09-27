"""合并结果与批内各组的配对（2026-09-27）

用户日志实测：`[记忆·Z] 事实合并模型输出不可用，改用原文拼接：ValueError: unknown merge id`
根因：旧实现 `zip(batch, output["groups"])` **按位置**配对 ✗ + 一有不合就
      `raise ValueError("unknown merge id")` ⇒ **整批判废** ✗
      ⇒ 退化成"原文拼接"（丢信息 ✗）⇒ 之后还得重做一次（实测白烧 77 秒 ✗）
修法：**按成员匹配 + 逐条修复**（`pair_merge_verdicts` ✓）
"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_mpair")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_mpair", pkg)
engine = importlib.import_module("alife_mpair.engine")
PAIR = engine.pair_merge_verdicts


def _g(*spec):
    """一组事实：spec = (id, time) ✓"""
    return [{"id": i, "time": t, "content": "内容-" + str(i)} for i, t in spec]


A = _g(("a1", 3.0), ("a2", 1.0))
B = _g(("b1", 2.0), ("b2", 5.0))


def test_reordered_groups_still_pair_correctly():
    """★ 模型把组的顺序换掉 ⇒ 也必须正确配对（旧实现直接炸 ✗）"""
    out = {"groups": [
        {"target_id": "b2", "source_ids": ["b1", "b2"], "action": "merge",
         "content": "B 合并"},
        {"target_id": "a1", "source_ids": ["a1", "a2"], "action": "merge",
         "content": "A 合并"},
    ]}
    pairs = PAIR([A, B], out)
    assert len(pairs) == 2
    by_group = {id(g): (g, v) for g, v in pairs}
    got_b = by_group[id(B)][1]
    got_a = by_group[id(A)][1]
    assert got_b["content"] == "B 合并"
    assert got_a["content"] == "A 合并"


def test_target_from_another_group_is_repaired():
    """target 写成别组的 id ⇒ 修复为该组最新一条 ✓（旧实现 → unknown merge id ✗）"""
    out = {"groups": [{"target_id": "zzz", "source_ids": ["a1", "a2", "ghost"],
                       "action": "merge", "content": "x"}]}
    pairs = PAIR([A, B], out)
    group, verdict = pairs[0]
    assert group is A, "含 a1/a2 的那条应配到 A ✓"
    assert verdict["target_id"] == "a1", "target 不认识 ⇒ 取该组最新（time 最大）✓"
    assert "ghost" not in verdict["source_ids"], "不认识的 id 要丢掉 ✓（不让它毁整批）"
    assert set(verdict["source_ids"]) <= {"a1", "a2"}


def test_unrecognisable_verdict_returns_none_group():
    """完全对不上 ⇒ 该条 group 为 None（调用方兜底 ✓），**不抛异常** ✓"""
    out = {"groups": [{"target_id": "nope", "source_ids": ["nope2"],
                       "action": "merge", "content": "x"}]}
    pairs = PAIR([A, B], out)
    assert pairs and pairs[0][0] is None


def test_each_group_consumed_once():
    """同一条 verdict 不能把同一组用两次 ✓（两组都得有归属）"""
    out = {"groups": [
        {"target_id": "a1", "source_ids": ["a1", "a2"], "action": "merge", "content": "1"},
        {"target_id": "a1", "source_ids": ["a1"], "action": "merge", "content": "2"},
    ]}
    pairs = PAIR([A], out)
    groups = [g for g, _v in pairs]
    assert groups.count(A) == 1 and None in groups
