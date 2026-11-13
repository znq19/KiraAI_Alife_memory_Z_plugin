"""配置迁移 v9：永久记忆上限 10→30、字符预算 3000→5000。

只改仍然等于旧默认的配置，用户自定义值不动。
"""
import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_cfgv9")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_cfgv9", pkg)
mod = importlib.import_module("alife_cfgv9.config_migrate")
contracts = importlib.import_module("alife_cfgv9.contracts")


def test_current_version_is_nine():
    assert mod.CURRENT_VERSION == 9


def test_old_defaults_are_migrated_once():
    changed, out = mod.migrate({
        "alife": {"permanent_cap": 10, "permanent_budget_chars": 3000},
        "alife_meta": {"config_version": 8},
    })
    assert changed == ["permanent_cap", "permanent_budget_chars"]
    assert out["alife"]["permanent_cap"] == 30
    assert out["alife"]["permanent_budget_chars"] == 5000
    assert out["alife_meta"]["config_version"] == 9


def test_custom_values_are_never_touched():
    changed, out = mod.migrate({
        "alife": {"permanent_cap": 15, "permanent_budget_chars": 8000},
        "alife_meta": {"config_version": 8},
    })
    assert changed == []
    assert out["alife"]["permanent_cap"] == 15
    assert out["alife"]["permanent_budget_chars"] == 8000


def test_contract_defaults_match_migration_target():
    s = contracts.Settings()
    assert s.permanent_cap == 30
    assert s.permanent_budget_chars == 5000
    assert s.permanent_overcap_extract_lowest is True
    assert s.permanent_overcap_extract_max == 10
    assert s.permanent_tidy_retry_minutes == 10
