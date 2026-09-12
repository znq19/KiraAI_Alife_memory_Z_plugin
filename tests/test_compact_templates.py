"""成本优化（v2.16.0）：紧凑 schema 与规则块精简后的**一致性**兜底。

改这些模板时最怕两件事：漏掉必填字段（模型输出被拒 ✗）或悄悄长回去（钱白花 ✗），
所以都要有测试钉住。
"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_compact_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_compact_test", package)
engine = importlib.import_module("alife_compact_test.engine")
contracts = importlib.import_module("alife_compact_test.contracts")

PURPOSES = {
    "compress": (contracts.Compression, "Compression"),
    "fact_merge": (contracts.FactMerge, "FactMerge"),
    "audit": (contracts.Audit, "Audit"),
}


def _required_fields(model, prefix=""):
    """契约里所有必填字段名（含 $defs 里的嵌套模型）。"""
    schema = model.model_json_schema()
    out = set()

    def walk(node, path=""):
        if not isinstance(node, dict):
            return
        for name in node.get("required", []):
            out.add(name)
            out.add(path + name)
        for name, child in (node.get("properties") or {}).items():
            walk(child, path + name + ".")
        for name, child in (node.get("$defs") or {}).items():
            walk(child, name + ".")
        if "items" in node:
            walk(node["items"], path)

    walk(schema, prefix)
    return out


def test_compact_schema_covers_every_required_field():
    """紧凑声明必须覆盖契约的全部必填字段名 —— 漏一个就会开始被拒 ✗。"""
    for purpose, (model, _) in PURPOSES.items():
        text = engine.COMPACT_SCHEMAS[purpose]
        missing = [
            name
            for name in _required_fields(model)
            if "." not in name and name not in text
        ]
        assert not missing, "%s 的紧凑声明漏了：%s" % (purpose, missing)


def test_compact_schema_is_much_smaller_than_auto_schema():
    """瘦身必须真的瘦（否则白改 ✗）；同时不能瘦到没有结构说明。"""
    for purpose, (model, _) in PURPOSES.items():
        import json

        auto = len(
            json.dumps(engine.strip_schema_titles(model.model_json_schema()), ensure_ascii=False)
        )
        compact = len(engine.COMPACT_SCHEMAS[purpose])
        assert compact < auto * 0.7, (purpose, auto, compact)
        assert "必填" in engine.COMPACT_SCHEMAS[purpose]
        assert "常见错误" in engine.COMPACT_SCHEMAS[purpose]


def test_compact_schema_falls_back_to_full_schema():
    """连续被拒要有兜底（否则省钱的代价是白跑 ✗）。"""
    source = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "if attempt >= 2 and isinstance(schema, str):" in source
    assert "strip_schema_titles(contract.model_json_schema())" in source
    # 提示词组装要支持字符串形式的 schema
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "isinstance(schema, str)" in main


def test_memory_rules_keeps_the_essential_tokens():
    """规则块精简后，这些关键 token 一个都不能少（否则模型会误读 payload ✗）。"""
    main_text = (ROOT / "main.py").read_text(encoding="utf-8")
    start = main_text.find("MEMORY_RULES = (")
    block = main_text[start : main_text.find(")\n", start)]
    for token in (
        "next_batch",
        "needs_review",
        "SearchMemoryArchive",
        "GetProfile",
        "Memorize",
        "CorrectMemory",
        "names",
        "sp",
        "rec",
        "t2",
        "src",
    ):
        assert token in block, token
    assert len(block) < 900, "规则块每轮全价发送，涨回去就是白花钱 ✗"


def test_compact_schema_forbids_extra_keys():
    """线上踩过：紧凑声明漏了「不许加字段」→ 模型多塞键 → extra_forbidden 频繁失败 ✗

    修法是在**提示词**里写明字段白名单（既有设计是"多字段 → 明确拒绝 + 提示"，
    不静默丢弃 —— 那由 test_output_diagnostics 保护 ✓）。
    """
    for purpose, text in engine.COMPACT_SCHEMAS.items():
        assert "字段白名单" in text, purpose


def _mentioned_keys(text):
    import re

    return set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)":', text))


def test_compact_schema_does_not_invent_fields():
    """紧凑声明里出现的每个键，契约里必须真的存在 ✗ —— 这条是线上事故的直接教训。

    v2.16.0 的 compress 模板里我写了 range / records（那是**输入** payload 的字段 ✗），
    模型照着模板多给两个键 → extra_forbidden ×2 → **每次压缩都失败** ✗✗
    （当时的测试只查"必填字段有没有漏"，查不出"凭空多写了字段" ✗）
    """
    for purpose, model in (
        ("compress", contracts.Compression),
        ("fact_merge", contracts.FactMerge),
        ("audit", contracts.Audit),
    ):
        allowed = _all_field_names(model)
        invented = {
            key for key in _mentioned_keys(engine.COMPACT_SCHEMAS[purpose]) if key not in allowed
        }
        assert not invented, "%s 的紧凑声明写了契约里没有的字段：%s" % (purpose, invented)


def _all_field_names(model):
    """契约里出现过的所有字段名（含嵌套模型与数组元素）。"""
    names = {"str", "int", "iso", "id"}  # 模板里的类型占位符，不是字段

    def walk(node):
        if not isinstance(node, dict):
            return
        for key, child in (node.get("properties") or {}).items():
            names.add(key)
            walk(child)
        for child in (node.get("$defs") or {}).values():
            walk(child)
        if "items" in node:
            walk(node["items"])

    walk(model.model_json_schema())
    return names


def test_fallback_needs_two_retries():
    """兜底（退回完整自动 schema）需要 model_retries>=2 才跑得到 —— 语义不动（有测试锁着 ✓）。"""
    source = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "if attempt >= 2 and isinstance(schema, str):" in source
