"""通用守护：`schema.json`（配置页）的默认值必须与代码里的默认值一致 ✓

为什么需要它：配置项分散在三处 —— 代码默认（contracts.Settings）、配置页（schema.json）、
帮助文案（setting_help）。改了一处忘了另两处 ⇒ 用户看到的行为与说明不一致 ✗
（2026-09-25 就发生过一次：合并提示词改了代码默认，schema.json 里还是旧文案 ✗，
 当时是人工发现并同步的 ⇒ 加这条通用断言，以后自动挡住 ✓）

已有的针对性断言只盯着几个字段（threshold / batch_size / fact_merge_prompt），
这条是**全字段**扫描 ✓。
"""

import importlib
import json
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_schemadef")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_schemadef", pkg)
contracts = importlib.import_module("alife_schemadef.contracts")

# schema.json 里只作展示、代码里没有对应字段的项（新增时请写明原因 ✓）
SCHEMA_ONLY = set()


def _code_default(name):
    settings = contracts.Settings()
    return getattr(settings, name, None)


def test_every_schema_default_matches_code():
    fields = json.loads(
        (ROOT / "schema.json").read_text(encoding="utf-8"))["alife"]["fields"]
    bad = []
    for name, spec in fields.items():
        if name in SCHEMA_ONLY or not isinstance(spec, dict):
            continue
        if "default" not in spec or name not in contracts.Settings.model_fields:
            continue
        want = _code_default(name)
        if want != spec["default"]:
            bad.append((name, want, spec["default"]))
    assert not bad, "schema.json 与代码默认值不一致 ✗：%s" % [
        "%s: 代码=%r / 配置页=%r" % (n, c, s) for n, c, s in bad
    ]


def test_every_settings_field_is_exposed_with_a_label():
    """代码里每个配置项都要在配置页出现（否则用户改不了 ✓）—— 允许显式豁免 ✓"""
    fields = json.loads(
        (ROOT / "schema.json").read_text(encoding="utf-8"))["alife"]["fields"]
    missing = [n for n in contracts.Settings.model_fields
               if n not in fields and n not in SCHEMA_ONLY]
    assert not missing, "schema.json 缺少这些配置项 ✗：%s" % missing
