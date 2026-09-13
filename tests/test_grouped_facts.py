"""v2.17.0 分组事实视图：渲染、尾部省略、关系短码、回滚一致性。"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_grouped_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_grouped_test", package)
retrieval = importlib.import_module("alife_grouped_test.retrieval")

CODES = {"qq:1": "n1", "qq:2": "n2"}


def _fact(category, subject, content, **kw):
    row = {"category": category, "subject": subject, "content": content}
    row.update(kw)
    return row


def test_grouped_basic_shape():
    facts = [
        _fact("event", "qq:1", "昨天和周武一起吃饭", importance=6, event_at=1757644800),
        _fact("profile", "qq:1", "周武是大学室友", importance=7, event_at=1755648000),
        _fact("fact", "qq:2", "我最近在准备考试", importance=5),
    ]
    out = retrieval.bot_facts_grouped(facts, "sid-a", codes=CODES)
    assert list(out) == ["n1", "n2"], "组间按类别优先级（画像在前）"
    assert out["n1"][0][0] == "pf" and out["n1"][1][0] == "ev", "组内也按优先级"
    assert out["n2"][0] == ["fa", "我最近在准备考试"], "importance=5 与缺失时间都省略（尾部）"


def test_tail_omission_keeps_middle_slot():
    row = retrieval.bot_facts_grouped(
        [_fact("event", "qq:1", "带关系没重要性", event_at=1757644800,
               relations=[{"subject": "qq:1", "predicate": "朋友", "object": "qq:2"}])],
        "sid-a", codes=CODES,
    )["n1"][0]
    assert row[2] == "", "中间位（重要性）缺 → 补空串占位"
    assert row[3] == "n1>朋友>n2", "关系用短码三元组"


def test_flat_view_is_byte_identical():
    facts = [_fact("profile", "qq:1", "周武是大学室友", importance=7, event_at=1755648000)]
    assert retrieval.pack_facts(facts, "sid-a", short=None, view="flat") == retrieval.bot_facts(facts, "sid-a")
    assert retrieval.pack_facts(facts, "sid-a", codes=CODES) == retrieval.bot_facts_grouped(facts, "sid-a", codes=CODES)


def test_short_names_table():
    table = retrieval.short_names(
        [{"id": "qq:1", "name": "周武", "aliases": ["武哥"]}, {"id": "qq:9"}], CODES
    )
    assert table == {"n1": ["qq:1", "周武|武哥"], "qq:9": ["qq:9", ""]}


def test_same_subject_never_repeats():
    import json
    rendered = json.dumps(
        retrieval.bot_facts_grouped([_fact("profile", "qq:1", "a"), _fact("profile", "qq:1", "b")], "s", codes=CODES)
    )
    assert rendered.count("n1") == 1, "主体只出现一次（组键）"
