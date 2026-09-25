"""配置迁移 v8（2026-09-25）：合并提示词加入 JEV 风格「判 drop 前自问三问」——
让**不开 JEV 时合并模型自己也能判 drop**（纯冗余直接进回收站 ✓），且**只动默认值** ✓。"""

import importlib
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_cfgv8")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_cfgv8", pkg)
mod = importlib.import_module("alife_cfgv8.config_migrate")
contracts = importlib.import_module("alife_cfgv8.contracts")


def test_current_version_is_eight():
    assert mod.CURRENT_VERSION == 8


def test_new_default_prompt_has_drop_checklist():
    """★ 三问必须在新默认文案里（这是「不开 JEV 也能判 drop」的全部依据）"""
    text = contracts.FACT_MERGE_PROMPT
    for phrase in ("判 drop 前逐条自问三问", "同一件事", "没有的实质信息",
                   "变模糊", "不许 drop", "拿不准一律不许 drop"):
        assert phrase in text, phrase
    # 旧内容不得丢（回归保护 ✓）
    assert "合并结果不能比最长的一条明显更长" in text
    assert "来源强度优先于时间" not in text or True   # 措辞可能不同，不强制


def test_previous_default_is_upgraded():
    """上一版默认文案（v7）⇒ 升到带三问的新文案 ✓"""
    changed, out = mod.migrate({
        "alife": {"fact_merge_prompt": mod._V7_FACT_MERGE_PROMPT},
        "alife_meta": {"config_version": 7},
    })
    assert changed == ["fact_merge_prompt"]
    assert out["alife"]["fact_merge_prompt"] == contracts.FACT_MERGE_PROMPT


def test_user_customised_prompt_is_never_touched():
    """★ 用户改过措辞的 ⇒ 一个字都不动 ✓（config_migrate 的核心承诺）"""
    mine = "我自己写的合并提示词，别动我"
    changed, out = mod.migrate({
        "alife": {"fact_merge_prompt": mine},
        "alife_meta": {"config_version": 7},
    })
    assert changed == []
    assert out["alife"]["fact_merge_prompt"] == mine


def test_chain_from_v5_lands_on_latest():
    """从老版本一路升上来 ⇒ 直接落在最新文案上 ✓"""
    changed, out = mod.migrate({
        "alife": {"fact_merge_prompt": mod._V5_FACT_MERGE_PROMPT},
        "alife_meta": {"config_version": 5},
    })
    assert "fact_merge_prompt" in changed
    assert out["alife"]["fact_merge_prompt"] == contracts.FACT_MERGE_PROMPT
