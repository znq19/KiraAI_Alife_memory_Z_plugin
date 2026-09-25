"""配置迁移 v7（2026-09-23）：旧默认 12 → 15、合并提示词升到新版 —— 且**只动默认值**。"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_cfgv7")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_cfgv7", pkg)
mod = importlib.import_module("alife_cfgv7.config_migrate")
contracts = importlib.import_module("alife_cfgv7.contracts")


def test_current_version_at_least_seven():
    """v7 的迁移规则永远有效；当前版本号已升到 8（见 test_config_migration_v8.py）"""
    assert mod.CURRENT_VERSION >= 7


def test_previous_default_threshold_is_upgraded():
    """存量配置里还存着旧默认 12 ⇒ 升到 15（用户拍板的新标定）"""
    changed, out = mod.migrate(
        {"alife": {"fact_sink_threshold": 12}, "alife_meta": {"config_version": 6}}
    )
    assert "fact_sink_threshold" in changed
    assert out["alife"]["fact_sink_threshold"] == 15


def test_user_customised_threshold_is_never_touched():
    """★ 用户自己调过的值一律不动（这是 config_migrate 的核心承诺）"""
    changed, out = mod.migrate(
        {"alife": {"fact_sink_threshold": 9}, "alife_meta": {"config_version": 6}}
    )
    assert changed == []
    assert out["alife"]["fact_sink_threshold"] == 9


def test_merge_prompt_upgrades_from_previous_default_only():
    """上一版默认文案 ⇒ 升到新文案（含"不能比最长的一条明显更长"）；
    用户改过措辞的 ⇒ 一个字不动 ✓"""
    changed, out = mod.migrate(
        {
            "alife": {"fact_merge_prompt": mod._V5_FACT_MERGE_PROMPT},
            "alife_meta": {"config_version": 6},
        }
    )
    assert "fact_merge_prompt" in changed
    assert out["alife"]["fact_merge_prompt"] == contracts.FACT_MERGE_PROMPT
    assert "合并结果不能比最长的一条明显更长，删掉重复表述。" in out["alife"]["fact_merge_prompt"]

    mine = "我自己写的合并提示词"
    changed2, out2 = mod.migrate(
        {"alife": {"fact_merge_prompt": mine}, "alife_meta": {"config_version": 6}}
    )
    assert changed2 == []
    assert out2["alife"]["fact_merge_prompt"] == mine


def test_record_merge_prompt_upgrades_too():
    """★ 记录合并也加密度句（与事实合并对齐）：上一版默认 ⇒ 升到新文案 ✓ 自定义不动 ✓"""
    changed, out = mod.migrate(
        {
            "alife": {"record_merge_prompt": mod._V6_RECORD_MERGE_PROMPT},
            "alife_meta": {"config_version": 6},
        }
    )
    assert "record_merge_prompt" in changed
    assert out["alife"]["record_merge_prompt"] == contracts.RECORD_MERGE_PROMPT
    assert "合并结果不能比最长的一条明显更长，删掉重复表述。" in out["alife"]["record_merge_prompt"]

    mine = "我自己的记录合并措辞"
    changed2, out2 = mod.migrate(
        {"alife": {"record_merge_prompt": mine}, "alife_meta": {"config_version": 6}}
    )
    assert changed2 == [] and out2["alife"]["record_merge_prompt"] == mine

    import json
    field = json.loads((ROOT / "schema.json").read_text("utf-8"))["alife"]["fields"][
        "record_merge_prompt"
    ]
    assert field["default"] == contracts.RECORD_MERGE_PROMPT, "schema 默认值没同步 ✗"


def test_v5_target_is_frozen_in_config_migrate():
    """★ 回归：v5 的目标值必须**固化成字面量** ✗
    否则以后改 contracts.FACT_MERGE_PROMPT 时，v5 会跟着一起变 ⇒
    老配置被一步跳到最新、中间版本失去意义（v7 的迁移也就永远不触发）"""
    assert isinstance(mod._V5_FACT_MERGE_PROMPT, str)
    assert "合并结果不能比最长的一条明显更长" not in mod._V5_FACT_MERGE_PROMPT
    assert mod.MIGRATIONS[5][0][2] is mod._V5_FACT_MERGE_PROMPT
    assert mod.MIGRATIONS[7][1][1] is mod._V5_FACT_MERGE_PROMPT


def test_idempotent_and_schema_default_agrees():
    """幂等 ✓ 且 schema.json / 帮助文案 / 代码默认三处一致（防"三处各说各话"）"""
    import json

    changed, out = mod.migrate({"alife": {}})
    again, _ = mod.migrate(out)
    assert again == [], "迁移过再跑必须空报告"

    field = json.loads((ROOT / "schema.json").read_text("utf-8"))["alife"]["fields"][
        "fact_sink_threshold"
    ]
    assert field["default"] == 15, "schema.json 默认值没跟着改 ✗"
    assert "默认15" in field["description"], "schema 描述还写着旧默认 ✗"

    help_mod = importlib.import_module("alife_cfgv7.setting_help")
    text = help_mod.HELP["fact_sink_threshold"]
    assert "默认15" in text, "帮助文案没跟着改 ✗"
    assert contracts.Settings().fact_sink_threshold == 15
