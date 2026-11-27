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
    kinds = [row[0] for group in out.values() for row in group]
    assert "pf" in kinds and "ev" in kinds, "画像与事件都应在场"
    assert out["n2"][0] == ["fa", "我最近在准备考试"], "importance=5 与缺失时间都省略（尾部）"


def test_tail_omission_keeps_middle_slot():
    row = retrieval.bot_facts_grouped(
        [_fact("event", "qq:1", "带关系没重要性", event_at=1757644800,
               relations=[{"subject": "qq:1", "predicate": "朋友", "object": "qq:2"}])],
        "sid-a", codes=CODES,
    )["n1"][0]
    assert row[2] == "", "中间位（重要性）缺 → 补空串占位"
    # 关系主体 == 本组主体 → 省略主体（第 4 项：省 4~6 字符/条，图例已说明 ✓）
    assert row[3] == ">朋友>n2", "关系用短码三元组，本组主体可省"


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


def test_rules_block_describes_grouped_layout():
    """给模型每轮看的规则块必须描述分组格式，且不得残留旧字段名（v2.17.0 改格式时漏改过 ✗）。"""
    source = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "记忆简报" in source or "按主体分组" in source
    _g = source[source.index("_GROUPED_FACT_DOC = (") : source.index("MEMORY_RULES = (")]
    for stale in ("u=主体ID", "t2", "rec=记录日期", "src=来源存档ID"):
        assert stale not in _g, "分组说明残留旧措辞：" + stale
    # flat 视图仍保留（旧用户可回滚）；说明必须与 grouped 不同，
    # 否则模型会按分组格式读 `- 主体｜内容`（实测会找不到类别码）。
    assert "_FLAT_FACT_DOC" in source, "flat 视图说明缺失 ✗"
    assert "事实 = - <主体>｜<内容>" in source, "flat 事实行说明不对 ✗"


def test_grouped_example_line_matches_real_render():
    """示例必须是渲染器**真实产出**的（否则示例会随格式改动而过期 ✗）。"""
    import json
    rendered = json.dumps(retrieval.grouped_example(), ensure_ascii=False, separators=(",", ":"))
    assert rendered in retrieval.GROUPED_EXAMPLE_LINE, "示例与真实渲染不一致"
    assert retrieval.GROUPED_EXAMPLE_LINE.startswith("读取示例")


def test_relation_omits_own_subject_only():
    """第 4 项：关系里**只有当主体就是本组主体时**才省略（省 4~6 字符/条，且零歧义）

    ① 主体 == 本组主体 → 省略（`>朋友>n2`）✓
    ② 主体 ≠ 本组主体（别人提到第三方）→ **照旧写全**（否则会错意）✓
    ③ 图例必须说明省略的含义（主 LLM 才看得懂）✓
    """
    codes = {"qq:1": "n1", "qq:2": "n2", "qq:3": "n3"}
    own = {"category": "relationship", "subject": "qq:1", "content": "养了只猫叫橘子",
           "relations": [{"subject": "qq:1", "predicate": "养的猫", "object": "橘子"}]}
    row = retrieval.bot_facts_grouped([own], "", codes=codes)["n1"][0]
    assert row[3] == ">养的猫>橘子", "本组主体的关系应省略主体"

    other = {"category": "fact", "subject": "qq:1", "content": "小夏提到周武养猫",
             "relations": [{"subject": "qq:3", "predicate": "养的猫", "object": "橘子"}]}
    row2 = retrieval.bot_facts_grouped([other], "", codes=codes)["n1"][0]
    assert row2[3] == "n3>养的猫>橘子", "非本组主体必须写全（否则会错意）"

    legend = retrieval.FACT_RELATION_RULE
    assert "省略主体即本组主体" in legend, "图例必须说明省略含义"

    """示例必须是抽象占位：不得出现具体人名/具体事件 —— 否则每个用户的提示词里都会凭空多出一个陌生人 ✗
    （记忆插件常被问"你记得某某吗"，模型可能把示例里的名字当成真实记忆）"""
def test_grouped_example_has_no_real_looking_entities():
    line = retrieval.GROUPED_EXAMPLE_LINE
    for banned in ("周武", "室友", "吃饭"):
        assert banned not in line, "示例里出现具体实体：" + banned
    assert "（示例）" in line, "示例内容必须自带「示例」标记"
    assert "n1" in line and "pf" in line, "结构教学不能丢"
    assert "组内每行" in line


def test_model_facing_strings_have_no_user_data():
    """只有**可能发给模型/用户**的字符串字面量必须通用（注释与 docstring 允许留真实痕迹 ✓）。

    用 AST 取字符串常量并排除 docstring —— 比肉眼扫可靠，也不会因为注释而误报 ✓
    """
    import ast
    banned = ("周武", "武哥", "星月", "萤火", "阿澄", "橘子", "769690776", "一起吃了饭")
    for name in ("main.py", "engine.py", "retrieval.py", "setting_help.py"):
        tree = ast.parse((ROOT / name).read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                first = body[0] if body else None
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstrings:
                    continue
                for token in banned:
                    assert token not in node.value, name + " 模型可见字符串残留用户数据：" + token
