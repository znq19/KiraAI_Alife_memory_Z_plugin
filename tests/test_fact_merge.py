"""Write-time fact merging: local detection, forced merge, visibility window."""

import asyncio
import importlib
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_merge_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_merge_test", package)
c = importlib.import_module("alife_merge_test.contracts")
s = importlib.import_module("alife_merge_test.storage")
e = importlib.import_module("alife_merge_test.engine")


def run(coro):
    return asyncio.run(coro)


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "m.db")
        self.store.initialize()
        self.calls = []

    def tearDown(self):
        self.temp.cleanup()

    def seed(self, sid="qq:gm:1", texts=None, subject="qq:9", category="preference"):
        texts = texts or ["萤火对花生过敏", "萤火对花生严重过敏"]
        messages = [
            {"role": "user", "content": text, "users": [subject], "time": float(i)}
            for i, text in enumerate(texts)
        ]
        self.store.capture(sid, "turn", messages)
        records = self.store.active(sid)
        ids = []
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for index, text in enumerate(texts):
                ids.append(
                    self.store._add_fact(
                        db,
                        sid,
                        {
                            "category": category,
                            "subject": subject,
                            "content": text,
                            "reason": "",
                            "scenario": "",
                            "tags": [],
                            "relations": [],
                            "source_ids": [records[index]["id"]],
                            "importance": 5 + index,
                        },
                    )
                )
        return ids

    def engine(self, cfg, model):
        return e.Engine(self.store, lambda: cfg, model, None, None)

    @staticmethod
    def merge_reply(payload):
        group = payload["groups"][0]
        return json.dumps(
            {
                "groups": [
                    {
                        "target_id": group["facts"][0]["id"],
                        "source_ids": [f["id"] for f in group["facts"]],
                        "content": "萤火对花生严重过敏",
                        "reason": "合并重复",
                    }
                ]
            },
            ensure_ascii=False,
        )

    def test_trigger_and_merge_keeps_newest_and_max_importance(self):
        ids = self.seed()
        cfg = c.Settings()

        async def model(*args):
            self.calls.append(args)
            return self.merge_reply(args[-1])

        engine = self.engine(cfg, model)
        flagged = run(engine.queue_fact_merges("qq:gm:1", 0))
        self.assertEqual(flagged, 2)
        merged = run(engine.merge_facts("qq:gm:1"))
        self.assertEqual(merged, 1)
        rows = self.store.facts("qq:gm:1")
        self.assertEqual([r["content"] for r in rows], ["萤火对花生严重过敏"])
        self.assertEqual(rows[0]["importance"], 6)
        self.assertEqual(self.calls[0][1], "fact_merge")
        with self.store.connect() as db:
            versions = db.execute(
                "SELECT count(*) FROM versions WHERE kind='fact'"
            ).fetchone()[0]
        self.assertGreaterEqual(versions, 2)
        self.assertNotEqual(rows[0]["id"], ids[0])

    def test_short_facts_are_detected(self):
        ids = self.seed(texts=["她喜欢猫", "她喜欢猫咪"], subject="qq:7")
        candidates = self.store.similar_facts(
            "qq:gm:1", "qq:7", "preference", "她喜欢猫",
            min_score=0.25, exclude_ids=[ids[0]],
        )
        self.assertEqual(len(candidates), 1)
        self.assertGreaterEqual(candidates[0][0], 0.9)

    def test_different_subject_is_never_merged(self):
        self.seed(texts=["他喜欢猫"], subject="qq:1")
        self.seed(texts=["他喜欢狗"], subject="qq:2")
        cfg = c.Settings()
        engine = self.engine(cfg, None)
        self.assertEqual(run(engine.queue_fact_merges("qq:gm:1", 0)), 0)

    def test_cross_session_only_for_identity_categories(self):
        self.seed(sid="qq:gm:1", texts=["萤火喜欢甜蛋糕"], category="preference")
        self.seed(sid="qq:gm:2", texts=["萤火喜欢甜口蛋糕"], category="preference")
        cross = self.store.similar_facts(
            "qq:gm:2", "qq:9", "preference", "萤火喜欢甜口蛋糕",
            cross_session=True,
        )
        self.assertTrue(cross)
        self.seed(sid="qq:gm:3", texts=["周六三点见面"], category="event")
        last = self.seed(sid="qq:gm:4", texts=["周六三点见面"], category="event")
        same_session_only = self.store.similar_facts(
            "qq:gm:4", "qq:9", "event", "周六三点见面",
            cross_session=False, exclude_ids=last,
        )
        self.assertFalse(same_session_only)

    def test_pending_facts_are_hidden_until_merged(self):
        self.seed()
        cfg = c.Settings()
        engine = self.engine(cfg, None)
        run(engine.queue_fact_merges("qq:gm:1", 0))
        hidden = self.store.facts("qq:gm:1", hide_pending=cfg.merge_pending_hide)
        self.assertEqual(hidden, [])
        self.assertEqual(len(self.store.facts("qq:gm:1")), 2)

    def test_long_model_output_falls_back_to_union(self):
        self.seed()
        cfg = c.Settings(model_retries=0)

        async def too_long(*args):
            group = args[-1]["groups"][0]
            return json.dumps(
                {
                    "groups": [
                        {
                            "target_id": group["facts"][0]["id"],
                            "source_ids": [f["id"] for f in group["facts"]],
                            "content": "长" * 500,
                            "reason": "x",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        engine = self.engine(cfg, too_long)
        run(engine.queue_fact_merges("qq:gm:1", 0))
        self.assertEqual(run(engine.merge_facts("qq:gm:1")), 1)
        rows = self.store.facts("qq:gm:1")
        self.assertEqual(len(rows), 1)
        self.assertIn("萤火对花生严重过敏", rows[0]["content"])
        self.assertIn("萤火对花生过敏", rows[0]["content"])
        self.assertLessEqual(len(rows[0]["content"]), cfg.fact_merge_max_chars)
        self.assertEqual(rows[0]["merge_pending"], 0)

    def test_soft_limits_are_rendered_into_prompt(self):
        cfg = c.Settings(fact_merge_soft_chars=77, fact_merge_soft_reason_chars=11)
        instruction = e.build_instruction("fact_merge", cfg)
        self.assertIn("77", instruction)
        self.assertIn("11", instruction)
        self.assertIn("merge", instruction)

    def test_compression_output_is_scanned_for_duplicates(self):
        self.seed(texts=["萤火对花生过敏"], subject="qq:9")
        self.store.capture(
            "qq:gm:1",
            "turn2",
            [
                {"role": "user", "content": f"记录 {i}", "users": ["qq:9"], "time": 10.0 + i}
                for i in range(4)
            ],
        )
        rows = [r for r in self.store.active("qq:gm:1") if r["level"] == 0][-4:]
        cfg = c.Settings(compress_batch_mode="records", threshold=4, batch_size=2, probability=1.0)

        async def model(*args):
            if args[1] == "compress":
                payload = args[-1]
                return json.dumps(
                    {
                        "summary": "这段时间聊到了过敏",
                        "facts": [
                            {
                                "category": "preference",
                                "subject": "qq:9",
                                "content": "萤火对花生严重过敏",
                                "reason": "",
                                "scenario": "",
                                "tags": [],
                                "relations": [],
                                "source_ids": [payload["records"][0]["id"]],
                                "importance": 7,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            return self.merge_reply(args[-1])

        engine = self.engine(cfg, model)
        asyncio.run(engine.compress("qq:gm:1"))
        pending = self.store.facts_for_merge(sid="qq:gm:1", pending_only=True)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["content"], "萤火对花生严重过敏")
        self.assertEqual(pending[0]["importance"], 7)

    def test_cross_session_merge_lands_in_global_scope(self):
        self.seed(sid="qq:gm:1", texts=["萤火喜欢甜蛋糕"], category="preference")
        self.seed(sid="qq:gm:2", texts=["萤火喜欢甜口蛋糕"], category="preference")
        cfg = c.Settings()

        async def model(*args):
            group = args[-1]["groups"][0]
            return json.dumps(
                {
                    "groups": [
                        {
                            "target_id": group["facts"][0]["id"],
                            "source_ids": [f["id"] for f in group["facts"]],
                            "content": "萤火喜欢甜口蛋糕",
                            "reason": "跨会话合并",
                        }
                    ]
                },
                ensure_ascii=False,
            )

        engine = self.engine(cfg, model)
        asyncio.run(engine.queue_fact_merges("qq:gm:2", 0))
        asyncio.run(engine.merge_facts("qq:gm:2"))
        global_facts = self.store.facts("qq:gm:3", include_shared=True)
        self.assertEqual([f["content"] for f in global_facts], ["萤火喜欢甜口蛋糕"])
        self.assertEqual(global_facts[0]["sid"], "global")

    def test_fact_merge_lane_processes_queued_job(self):
        self.seed()
        cfg = c.Settings()

        async def model(*args):
            self.calls.append(args)
            return self.merge_reply(args[-1])

        engine = self.engine(cfg, model)

        async def run():
            # Only the merge lane: keeps the test independent of the other lanes.
            task = asyncio.create_task(engine.fact_merge_worker())
            try:
                await engine.queue_fact_merges("qq:gm:1", 0)
                for _ in range(200):
                    await asyncio.sleep(0.05)
                    with self.store.connect() as db:
                        row = db.execute(
                            "SELECT state FROM jobs WHERE kind='fact_merge'"
                        ).fetchone()
                    if row and row[0] in ("completed", "failed"):
                        break
            finally:
                engine.stopping = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

        asyncio.run(run())
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT state,detail FROM jobs WHERE kind='fact_merge' ORDER BY created DESC"
            ).fetchall()
        self.assertEqual(len(self.store.facts("qq:gm:1")), 1, rows)
        self.assertTrue(rows, "fact_merge 任务应存在")
        self.assertEqual(rows[0][0], "completed", rows)

    def test_startup_requeues_pending_facts(self):
        self.seed()
        cfg = c.Settings()

        async def model(*args):
            return self.merge_reply(args[-1])

        rows = self.store.facts("qq:gm:1")
        self.store.mark_merge_pending([rows[0]["id"]])
        engine = self.engine(cfg, model)

        async def run():
            await engine.start()
            try:
                with self.store.connect() as db:
                    job = db.execute(
                        "SELECT sid,state FROM jobs WHERE kind='fact_merge'"
                    ).fetchone()
                self.assertIsNotNone(job, "启动时应把待合并事实重新入队")
                self.assertEqual(job[0], "qq:gm:1")
                for _ in range(200):
                    await asyncio.sleep(0.05)
                    if len(self.store.facts("qq:gm:1")) == 1:
                        break
            finally:
                await engine.stop()

        asyncio.run(run())
        self.assertEqual(len(self.store.facts("qq:gm:1")), 1)


if __name__ == "__main__":
    unittest.main()


def test_fact_merge_records_job_items_for_detail_view(tmp_path):
    """合并要留下明细：哪几条并进了哪一条（前端据此渲染「旧 → 新」）。"""
    store = s.Store(tmp_path / "db")
    store.initialize()
    case = MergeCase("seed")
    case.store = store
    case.calls = []
    case.setUp = lambda: None
    case.tearDown = lambda: None
    ids = case.seed()
    job_id = store.enqueue("fact_merge", "qq:gm:1")
    store.claim(kind="fact_merge")

    async def model(*args):
        case.calls.append(args)
        return MergeCase.merge_reply(args[-1])

    engine = e.Engine(store, lambda: c.Settings(), model, None, None)
    run(engine.queue_fact_merges("qq:gm:1", 0))  # 先标出待合并簇
    run(engine.merge_facts("qq:gm:1", job_id))

    items = store.job_items(job_id)
    assert items, "事实合并必须写明细，否则前端点开是空的"
    actions = {item["action"] for item in items}
    assert actions == {"keep", "merged"}
    target = next(item for item in items if item["action"] == "keep")
    assert target["kind"] == "fact"
    folded = [item for item in items if item["action"] == "merged"]
    assert len(folded) == 1
    # 被并入的那条带着原文，note 指向保留的那条 → 前端能画出「旧 → 新」
    assert folded[0]["before"] == "萤火对花生过敏"
    assert folded[0]["note"] == target["target"]
    assert store.facts_by_ids([target["target"]])[0]["deleted"] == 0


def test_merge_payload_evidence_switch(tmp_path):
    """去重判定默认附原文证据；关掉开关后不再附（省 token）。"""
    for flag in (True, False):
        store = s.Store(tmp_path / f"db{flag}")
        store.initialize()
        case = MergeCase("seed")
        case.store = store
        case.calls = []
        case.seed()
        store.enqueue("fact_merge", "qq:gm:1")
        store.claim(kind="fact_merge")
        captured = []

        async def model(*args):
            captured.append(args[-1])
            return MergeCase.merge_reply(args[-1])

        engine = e.Engine(
            store, lambda: c.Settings(fact_merge_evidence=flag), model, None, None
        )
        run(engine.queue_fact_merges("qq:gm:1", 0))
        run(engine.merge_facts("qq:gm:1"))
        group = captured[-1]["groups"][0]
        assert "evidence" in group
        if flag:
            assert group["evidence"], "开启时证据不能为空"
        else:
            assert group["evidence"] == [], "关闭时不应附证据"



class JevClusterInvariants(unittest.TestCase):
    """C 项：JEV 路由放宽「同类别」发现门槛 —— 三条不变式 + 一条效果。"""

    @staticmethod
    def clusters(rows, threshold, cross_threshold=0.0, jev_route=False):
        # @staticmethod ⇒ 不传 self
        return e.Engine._fact_clusters(rows, threshold, cross_threshold,
                                       jev_route=jev_route)

    @staticmethod
    def row(i, subject, category, content):
        return {"id": i, "subject": subject, "category": category,
                "content": content, "summary": content, "sid": "s1", "deleted": 0}

    def test_relaxation_merges_same_category_pair(self):
        """效果：同主体同类别、词面中等相似 ⇒ 默认不并，放宽后并成一组。"""
        from alife_merge_test.retrieval import similarity as _sim

        a, b = "工作日吃素，周末不忌口", "工作日吃素"
        sim = _sim(a, b, min_overlap=2)
        self.assertGreaterEqual(sim, 0.2, "用例需有中等相似度（否则放宽也够不着）")
        rows = [self.row(1, "用户", "preference", a),
                self.row(2, "用户", "preference", b)]
        thr = sim + 0.02                     # 默认门槛略高于相似度 ⇒ 不并
        self.assertEqual(len(self.clusters(rows, thr)), 0, "默认门槛 ⇒ 不并")
        self.assertEqual(len(self.clusters(rows, thr, jev_route=True)), 1,
                         "放宽同类别门槛（×0.6）⇒ 并成一组")

    def test_never_cross_subject(self):
        """★ 不变式：跨主体绝不合并（即便开启 JEV 放宽）。"""
        rows = [self.row(1, "用户", "preference", "工作日吃素"),
                self.row(2, "朋友", "preference", "工作日吃素")]
        self.assertEqual(len(self.clusters(rows, 0.1, jev_route=True)), 0)

    def test_cross_category_threshold_untouched(self):
        """★ 不变式：跨类别门槛不受放宽影响（cross_threshold=0 ⇒ 不跨）。"""
        rows = [self.row(1, "用户", "preference", "工作日吃素"),
                self.row(2, "用户", "plan", "工作日吃素")]
        self.assertEqual(len(self.clusters(rows, 0.1, 0.0, jev_route=True)), 0)

    def test_off_by_default_unchanged(self):
        """★ 关闭 JEV（默认）⇒ 行为与放宽前完全一致。"""
        rows = [self.row(1, "用户", "preference", "工作日吃素，周末不忌口"),
                self.row(2, "用户", "preference", "工作日吃素")]
        self.assertEqual(self.clusters(rows, 0.9), self.clusters(rows, 0.9, jev_route=False))


class JevEngineIntegration(unittest.TestCase):
    """引擎侧 JEV 集成（预筛 / 合并路由）。

    这两个入口以前**没有任何测试覆盖** ⇒ 埋着一个 NameError（在"只送可疑子集"那条分支上）
    直到全量静态检查才被发现。这里补上，确保两条分支都被真正执行过。
    """

    class _Log:
        """决策留痕桩：产品里 Decisions.log 一定存在且永不抛，这里同样不抛。"""

        def write(self, *a, **k):
            return None

    class _Decisions:
        ready = True
        tokens = 123

        def __init__(self, suspicious=None):
            self._suspicious = suspicious or []
            self.log = JevEngineIntegration._Log()   # 运行时解析 ⇒ 不能写在类体里

        async def audit_prescreen(self, pairs):
            return list(self._suspicious)

        async def merge_route(self, primary, cands, hints=None):
            # 引擎按**候选自身 id** 查表 ⇒ 桩必须用真实 key（否则全落进"keep"✗）
            keys = [k for k, _t in cands]
            return {keys[0]: "drop", keys[1]: "merge"} if len(keys) >= 2 else {}

    class _Cfg:
        jev_enabled = True
        jev_audit = True
        jev_merge = True
        top_k = 5
        jev_timeout_ms = 5000

    class _Store:
        async def call(self, *a, **k):
            return None

    def _engine(self, decisions):
        eng = e.Engine(self._Store(), lambda: self._Cfg(), None, None, None)
        eng.decisions = decisions
        eng.store = self._Store()
        return eng

    def test_audit_prescreen_narrows_branch(self):
        """非空可疑集 ⇒ 走"只送可疑"分支（旧代码在此处 NameError 崩溃 ✗）。"""
        eng = self._engine(self._Decisions(["p0_1"]))
        cands = [{"id": 10, "content": "a"}, {"id": 11, "content": "b"}]
        out = run(eng.jev_audit_prescreen(cands, self._Cfg()))
        self.assertEqual(sorted(out or []), [10, 11], "应把可疑对涉及的 id 挑出来")

    def test_audit_prescreen_empty_branch(self):
        """无可疑对 ⇒ 返回空列表（调用方据此决定跳过审计）。"""
        eng = self._engine(self._Decisions([]))
        cands = [{"id": 10, "content": "a"}, {"id": 11, "content": "b"}]
        out = run(eng.jev_audit_prescreen(cands, self._Cfg()))
        self.assertEqual(out, [], "无可疑 ⇒ 空列表（且 complete 标记决定能否跳过）")

    def test_merge_route_splits_drop_and_merge(self):
        """合并路由生效：该丢的丢、该合的合（且不抛异常）。"""
        eng = self._engine(self._Decisions())
        group = [{"id": 1, "content": "base"}, {"id": 2, "content": "dup"},
                 {"id": 3, "content": "extra"}]
        verdicts = [(group, {"target_id": 1, "source_ids": [1, 2, 3],
                             "action": "merge", "content": "x", "reason": "t"})]
        out = run(eng.jev_apply_merge_route(verdicts, self._Cfg()))
        actions = sorted(v.get("action") for _g, v in out)
        self.assertIn("drop", actions, "c1 应进回收站")
        self.assertIn("merge", actions, "c2 应保留合并")

    def test_merge_route_off_returns_unchanged(self):
        """★ 关闭 JEV ⇒ 原样返回（逐字节一致）。"""
        eng = self._engine(None)
        group = [{"id": 1, "content": "base"}]
        verdicts = [(group, {"target_id": 1, "source_ids": [1], "action": "merge",
                             "content": "x", "reason": "t"})]

        class Off(self._Cfg):
            jev_enabled = False

        out = run(eng.jev_apply_merge_route(verdicts, Off()))
        self.assertEqual(out, verdicts, "关闭 JEV 时不得做任何改动")



class JevMergePrescreenCase(unittest.TestCase):
    """★ 合并预筛：只有「整批候选都**明确**不是同一件事」才跳过大模型调用（保守 ✓）。

    这是真正的"更快更省"：大模型那次合并调用含来源原文证据 + 要生成正文 ⇒ 真·大调用。
    """

    class _Log:
        def write(self, *a, **k):
            return None

    class _D:
        ready = True
        tokens = 1

        def __init__(self, scores):
            self._scores = scores
            self.log = JevMergePrescreenCase._Log()

        async def merge_prescreen(self, items):
            if self._scores is None:
                return None
            return dict(self._scores)

        async def merge_route(self, primary, cands, hints=None):
            return {}

    class _Cfg:
        jev_enabled = True
        jev_merge = True
        top_k = 5
        jev_timeout_ms = 5000

    class _Store:
        async def call(self, *a, **k):
            return None

    def _engine(self, decisions):
        eng = e.Engine(self._Store(), lambda: self._Cfg(), None, None, None)
        eng.decisions = decisions
        eng.store = self._Store()
        return eng

    BATCH = [[{"id": 1, "content": "AAA"}, {"id": 2, "content": "BBB"},
              {"id": 3, "content": "CCC"}]]

    def test_all_clearly_different_skips_llm(self):
        eng = self._engine(self._D({"g0_1": 0.05, "g0_2": 0.03}))
        self.assertTrue(run(eng.jev_merge_prescreen(self.BATCH, self._Cfg())),
                        "全部明确不同 ⇒ 可以跳过大模型 ✓")

    def test_ambiguous_band_does_not_skip(self):
        eng = self._engine(self._D({"g0_1": 0.05, "g0_2": 0.40}))
        self.assertFalse(run(eng.jev_merge_prescreen(self.BATCH, self._Cfg())),
                         "有模糊带 ⇒ 必须照常调大模型 ✓")

    def test_unavailable_does_not_skip(self):
        eng = self._engine(self._D(None))
        self.assertFalse(run(eng.jev_merge_prescreen(self.BATCH, self._Cfg())),
                         "JEV 不可用 ⇒ 照常调大模型 ✓（绝不误跳）")

    def test_incomplete_scores_do_not_skip(self):
        eng = self._engine(self._D({"g0_1": 0.05}))
        self.assertFalse(run(eng.jev_merge_prescreen(self.BATCH, self._Cfg())),
                         "分数不完整 ⇒ 不冒险 ✓")


class JevMergePlanCase(unittest.TestCase):
    """★ 先审后生成：只把「该合/交回大模型」的候选给大模型；该回收的直接合成判定（不调大模型）。

    这也是"正常顺序"：JEV 先审 → 只把批准的发给大模型写正文 ✓
    """

    class _Log:
        def write(self, *a, **k):
            return None

    class _D:
        ready = True

        def __init__(self, routes):
            self._routes = routes
            self.log = JevMergePlanCase._Log()

        async def merge_plan(self, items):
            return dict(self._routes) if self._routes else None

        async def merge_route(self, primary, cands, hints=None):
            return {}

    class _Cfg:
        jev_enabled = True
        jev_merge = True
        top_k = 5
        jev_timeout_ms = 5000

    class _Store:
        async def call(self, *a, **k):
            return None

    def _engine(self, decisions):
        eng = e.Engine(self._Store(), lambda: self._Cfg(), None, None, None)
        eng.decisions = decisions
        eng.store = self._Store()
        return eng

    @staticmethod
    def _row(i, imp, text):
        return {"id": i, "importance": imp, "content": text, "summary": text,
                "subject": "用户", "category": "preference", "sid": "s1", "deleted": 0}

    def _batch(self):
        # 主事实应由**重要度**决定（9 > 5 > 3）⇒ target 必须是 id=1
        return [[self._row(1, 9, "用户不吃香菜，觉得像肥皂味"),
                 self._row(2, 3, "用户讨厌香菜"),
                 self._row(3, 5, "用户不吃动物内脏"),
                 self._row(4, 4, "凌晨问如何把录制视频转音频")]]

    def test_target_is_deterministic_by_importance(self):
        eng = self._engine(self._D({"c0_1": "merge", "c0_2": "keep", "c0_3": "drop"}))
        filtered, drops, stats = run(eng._merge_plan_filter(self._batch(), self._Cfg()))
        self.assertEqual(filtered[0][0]["id"], 1, "主事实按重要度挑（不靠大模型 ✓）")

    def test_kept_excluded_dropped_synthesised(self):
        # 候选编号按**重要度排序后**的位置：id3(5)→c0_1、id4(4)→c0_2、id2(3)→c0_3
        eng = self._engine(self._D({"c0_1": "inherit", "c0_2": "keep", "c0_3": "drop"}))
        filtered, drops, stats = run(eng._merge_plan_filter(self._batch(), self._Cfg()))
        self.assertEqual([r["id"] for r in filtered[0]], [1, 3],
                         "只留 target 与「交回大模型」的候选（keep/drop 不进大模型 ✓）")
        self.assertEqual(len(drops), 1)
        self.assertEqual(drops[0][1]["source_ids"], [2], "该回收的合成 drop 判定 ✓")
        self.assertEqual(stats, {"merge": 0, "drop": 1, "keep": 1, "inherit": 1})

    def test_all_keep_means_no_llm_call(self):
        eng = self._engine(self._D({"c0_1": "keep", "c0_2": "keep", "c0_3": "keep"}))
        filtered, drops, stats = run(eng._merge_plan_filter(self._batch(), self._Cfg()))
        self.assertEqual(filtered, [], "全「不动」⇒ 过滤后为空 ⇒ 大模型**不会被调用** ✓")
        self.assertEqual(drops, [])
        self.assertEqual(stats["keep"], 3)

    def test_unavailable_passes_everything_through(self):
        eng = self._engine(self._D(None))
        filtered, drops, stats = run(eng._merge_plan_filter(self._batch(), self._Cfg()))
        self.assertEqual(filtered, self._batch(), "JEV 不可用 ⇒ 原样放行（= 关闭 JEV ✓）")
        self.assertEqual(stats, {})


class JevCompressScreenCase(unittest.TestCase):
    """★ 压缩前置筛选（用户 2026-09-25 定稿）：
       按**轮次**判定；**用户侧与助手侧都判**（信息常落在助手回复里 ✓）；
       轮内任意一条 ≥0.5 ⇒ 整轮保留；否则整轮直归档；全不够格 ⇒ 还不调大模型。
    """

    class _Log:
        def write(self, *a, **k):
            return None

    class _D:
        ready = True

        def __init__(self, scores):
            self._scores = scores
            self.log = JevCompressScreenCase._Log()

        async def compress_screen(self, items):
            if self._scores is None:
                return None
            return {k: self._scores[k] for k, _r, _t in items if k in self._scores}

    class _Cfg:
        jev_enabled = True
        jev_compress = True
        jev_timeout_ms = 5000

    class _Store:
        def __init__(self):
            self.calls = []

        async def call(self, name, *a, **k):
            self.calls.append((name, a))
            return 2 if name == "archive_distilled" else None

    ROWS = [
        # 轮 1：信息在**助手**回复里（用户话很轻）
        {"id": "m01", "sid": "s1", "role": "user", "summary": "用户：你把我那些事记一下"},
        {"id": "m02", "sid": "s1", "role": "assistant",
         "summary": "助手：好的我记下了：①花生过敏 ②每周日提醒你给妈妈打电话"},
        # 轮 2：信息在**用户**消息里 + 一条工具步（不判，随轮走）
        {"id": "m03", "sid": "s1", "role": "user", "summary": "用户：我花生过敏，严重会休克"},
        {"id": "m04", "sid": "s1", "role": "assistant", "category": "tool",
         "summary": "[调用工具：memorize(花生过敏)]"},
        # 轮 3：纯闲聊
        {"id": "m05", "sid": "s1", "role": "user", "summary": "用户：哈哈"},
        {"id": "m06", "sid": "s1", "role": "assistant", "summary": "助手：呵"},
    ]

    def _engine(self, decisions, store=None):
        st = store or self._Store()
        eng = e.Engine(st, lambda: self._Cfg(), None, None, None)
        eng.decisions = decisions
        eng.store = st
        return eng, st

    def test_round_kept_by_assistant_side(self):
        """★ 关键：用户话轻（0.27）但助手复述了事实（0.98）⇒ 整轮必须保留 ✓"""
        eng, st = self._engine(self._D({"mm01": 0.27, "mm02": 0.98, "mm03": 0.96, "mm05": 0.17, "mm06": 0.04}))
        filtered, skip = run(eng.jev_compress_screen(self.ROWS, self._Cfg()))
        self.assertFalse(skip)
        self.assertEqual([r["id"] for r in filtered], ["m01", "m02", "m03", "m04"],
                         "轮1(靠助手) + 轮2(靠用户，含工具步) 保留 ✓")
        arc = [c for c in st.calls if c[0] == "archive_distilled"]
        self.assertEqual([r["id"] for r in arc[0][1][1]], ["m05", "m06"], "纯闲聊整轮归档 ✓")

    def test_all_rounds_low_archives_and_skips(self):
        eng, st = self._engine(self._D({"mm01": 0.20, "mm02": 0.10, "mm03": 0.30, "mm05": 0.05, "mm06": 0.02}))
        filtered, skip = run(eng.jev_compress_screen(self.ROWS, self._Cfg()))
        self.assertTrue(skip, "全不够格 ⇒ 不调大模型 ✓")
        arc = [c for c in st.calls if c[0] == "archive_distilled"]
        self.assertEqual([r["id"] for r in arc[0][1][1]],
                         ["m01", "m02", "m03", "m04", "m05", "m06"],
                         "全部归档 ✓（工具步 #4 随轮一起走 ✓）")

    def test_unavailable_no_archiving(self):
        eng, st = self._engine(self._D(None))
        filtered, skip = run(eng.jev_compress_screen(self.ROWS, self._Cfg()))
        self.assertEqual(filtered, self.ROWS, "JEV 不可用 ⇒ 原样（= 关闭 JEV ✓）")
        self.assertFalse(skip)
        self.assertEqual([c for c in st.calls if c[0] == "archive_distilled"], [],
                         "判不了就不许封档 ✗")
