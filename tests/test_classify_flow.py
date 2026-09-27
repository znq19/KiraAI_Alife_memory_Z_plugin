"""记忆归类（classify）端到端实证：写永久记忆 ⇒ 归类 ⇒ **事实真的落库** ✓（2026-09-27）

链路：写入永久记忆 → enqueue("classify") ✓
      → worker 认领（claim 只排除 dedupe/fact_merge/tidy ✓）
      → 模型按 Compression 契约提取事实 → 校验 source_ids == ["r1"] ✓
      → storage.classify 落库 ✓（带语义化守卫 ✓）
本测试**真跑一遍 worker**，断言事实表出现提取结果 ✓（不是只读代码 ✓）
"""

import asyncio
import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if os.environ.get("KIRA_CORE"):
    sys.path.insert(0, os.environ["KIRA_CORE"])
pkg = __import__("types").ModuleType("alife_cflow")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_cflow", pkg)
engine_mod = importlib.import_module("alife_cflow.engine")
contracts = importlib.import_module("alife_cflow.contracts")
storage_mod = importlib.import_module("alife_cflow.storage")


@pytest.mark.asyncio
async def test_classify_job_writes_extracted_facts(tmp_path):
    store = storage_mod.Store(tmp_path / "mem.db")
    store.initialize()
    cfg = contracts.Settings()
    eng = engine_mod.Engine(store, lambda: cfg, None, None, None)

    async def _fake(schema, purpose, payload, cfg_, **kw):
        # ★ 必须走**契约**（真实链路里 structured() 会用 pydantic 补默认字段 ✓）
        #   裸 dict 会缺 relations/tags/time 等键 ⇒ 下游 KeyError ✗（我第一版就踩了 ✗）
        obj = schema(**{
            "summary": "用户对花生过敏，吃了会休克",
            "facts": [{"subject": "用户", "category": "fact",   # ← 必须用契约里允许的枚举 ✓
                       "content": "用户对花生过敏，吃了会休克",
                       "importance": 9, "source_ids": ["r1"], "tags": [],
                       "relations": [], "reason": "归类提取", "scenario": ""}]})
        return obj.model_dump()
    eng.structured = _fake

    rid = await store.call("memorize", "qq:gm:c1", "用户对花生过敏，吃了会休克",
                           [], 1000.0, 1000.0, 8, "健康")
    assert rid, "写入永久记忆应返回记录 id ✓"

    await eng.enqueue("classify", rid)
    await eng.start()
    rows = []
    for _ in range(40):                       # 等 worker 认领并跑完 ✓
        await asyncio.sleep(0.25)
        rows = await store.call("facts", sid="qq:gm:c1", limit=10)
        if rows:
            break
    await eng.stop()

    assert rows, ("归类跑完却没有事实落库 ✗ —— 入队/认领/校验/落库 某处静默失效了")
    assert any("花生过敏" in (r.get("content") or "") for r in rows), rows
