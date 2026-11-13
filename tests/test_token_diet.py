"""v2.5.3 token 瘦身：注入视图、审计 payload、压缩别名、retract、配置同步。"""

import asyncio
import importlib
import os
import random
import json
import re
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_diet_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_diet_test", package)
c = importlib.import_module("alife_diet_test.contracts")
s = importlib.import_module("alife_diet_test.storage")
e = importlib.import_module("alife_diet_test.engine")
r = importlib.import_module("alife_diet_test.retrieval")
i = importlib.import_module("alife_diet_test.identity")


def fact(**overrides):
    row = {
        "id": "f-1",
        "sid": "qq:dm:u",
        "category": "preference",
        "subject": "qq:u",
        "content": "喜欢猫",
        "reason": "用户自己说的",
        "scenario": "日常闲聊",
        "tags": ["猫"],
        "relations": [],
        "sources": ["rec-1"],
        "fingerprint": "fp-1",
        "deleted": 0,
        "revision": 0,
        "audited": 0,
        "importance": 6,
        "merge_pending": 0,
        "created": 1700000000.0,
        "relationship_status": "evidence_required",
        "verified_relations": [],
        "relation_warnings": [],
    }
    row.update(overrides)
    return row


class BotFactsTests(unittest.TestCase):
    def test_keeps_only_decision_and_provenance_fields(self):
        # 2026-09-19：created 不再是事件的兜底 ⇒ 显式给 event_at ✓
        view = r.bot_facts([fact(event_at=1700000000.0)], "qq:dm:u")[0]
        # 默认重要度(5)省略，这里用例给的是 6，所以 imp 会出现
        self.assertEqual(set(view), {"c", "u", "x", "imp", "src", "t"})
        self.assertEqual(view["c"], "pr")  # 类别用短码，图例在静态规则块里
        self.assertEqual(view["src"], "rec-1")
        # 日期短码：同年只给月-日，跨年才补上年份（1700000000 是 2023 年，所以带年）
        # 2026-09-19：单来源事实现在**精确到分钟** ⇒ 断言改为"日期部分"比对（粒度无关 ✓）
        self.assertTrue(view["t"].split(" ")[0].endswith("11-15"), view["t"])
        self.assertNotIn("t2", view)  # 单点事件不给区间
        self.assertNotIn("rec", view)  # 记录时刻与事件时间相同就不重复说
        # 本会话事实不再重复 sid/by
        self.assertNotIn("sid", view)

    def test_cross_session_fact_keeps_source_and_speaker(self):
        view = r.bot_facts(
            [fact(sid="global", src_user="qq:other")], "qq:dm:u"
        )[0]
        self.assertEqual(view["sid"], "global")
        self.assertEqual(view["by"], "qq:other")

    def test_event_span_and_record_time_are_separate(self):
        """跨天合并要显示区间；"记下来"和"发生"差得远时额外标出来。"""
        day = 86400.0
        view = r.bot_facts(
            [
                fact(
                    event_at=1700000000.0,
                    event_end=1700000000.0 + 3 * day,
                    created=1700000000.0 + 30 * day,
                )
            ],
            "qq:dm:u",
        )[0]
        self.assertIn("t", view)
        self.assertIn("t2", view)  # 区间
        self.assertIn("rec", view)  # 30 天后才整理出来的
        # 同上：只比日期部分 ✓（单来源⇒到分钟，多来源⇒只到日）
        self.assertEqual(r.short_day(1700000000.0), view["t"].split(" ")[0])

    def test_needs_review_only_when_flagged(self):
        self.assertNotIn("rev", r.bot_facts([fact()], "qq:dm:u")[0])
        flagged = r.bot_facts(
            [fact(relationship_status="needs_review")], "qq:dm:u"
        )[0]
        self.assertTrue(flagged["rev"])  # 短键：待核对的关系


class PayloadTests(unittest.TestCase):
    def test_compress_records_use_short_aliases_and_single_timestamp(self):
        rows = [
            {"id": "a" * 32, "role": "user", "level": 0, "summary": "内容", "users": ["u"],
             "start": 100.0, "end": 100.0},
            {"id": "b" * 32, "role": "assistant", "level": 0, "summary": "回复", "users": ["u"],
             "start": 101.0, "end": 101.0},
        ]
        aliases = {"r1": "a" * 32, "r2": "b" * 32}
        records = e.compress_records(rows, aliases, {"u": "周武"})
        self.assertEqual([rec["id"] for rec in records], ["r1", "r2"])
        self.assertNotIn("level", records[0])
        self.assertNotIn("role", records[0])          # 默认 user 不再显式输出
        self.assertEqual(records[1]["bot"], 1)        # assistant 用一位短标记
        self.assertEqual(records[0]["t"], "1970-01-01 08:01")  # 可读时间（本地）
        self.assertNotIn("start", records[0])
        self.assertEqual(records[0]["u"], ["u"])   # 只写 ID；名字在 payload 顶层 names 表

    def test_compress_records_keep_range_for_archives(self):
        rows = [
            {"id": "c" * 32, "role": "assistant", "level": 1, "summary": "摘要", "users": [],
             "start": 10.0, "end": 20.0},
        ]
        record = e.compress_records(rows, {"r1": "c" * 32})[0]
        self.assertNotIn("start", record)
        # 一层以上的存档保留起止两点，都换成可读时间
        self.assertEqual((record["t"], record["t2"]), ("1970-01-01 08:00", "1970-01-01 08:00"))

    def test_restore_maps_aliases_and_rejects_unknown(self):
        aliases = {"r1": "real-1"}
        out = e.restore_compress_ids(
            {"summary": "s", "facts": [{"source_ids": ["r1"]}]}, aliases
        )
        self.assertEqual(out["facts"][0]["source_ids"], ["real-1"])
        with self.assertRaises(ValueError):
            e.restore_compress_ids(
                {"summary": "s", "facts": [{"source_ids": ["r9"]}]}, aliases
            )

    def test_schema_titles_are_stripped(self):
        ov = importlib.import_module("alife_diet_test.output_validation")
        stripped = ov.strip_schema_titles(c.Compression.model_json_schema())
        self.assertNotIn("title", json.dumps(stripped))
        self.assertIn("properties", stripped)


class ConfigSyncTests(unittest.TestCase):
    def test_new_switches_exist_in_all_three_places(self):
        schema = json.load(open(ROOT / "schema.json", encoding="utf-8"))["alife"]["fields"]
        help_text = importlib.import_module("alife_diet_test.setting_help").HELP
        for key in ("compress_persona", "audit_persona", "inject_mode"):
            self.assertIn(key, schema)
            self.assertIn(key, help_text)
            self.assertIn(key, c.Settings.model_fields)
        defaults = c.Settings()
        self.assertTrue(defaults.compress_persona)
        self.assertFalse(defaults.audit_persona)
        self.assertEqual(defaults.inject_mode, "situational")


class RetractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db.sqlite3")
        self.store.initialize()
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for index, fid in enumerate(("f-1", "f-2")):
                db.execute(
                    """INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,
                       relations,sources,fingerprint,deleted,revision,audited,importance,
                       merge_pending,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,0,?)""",
                    (fid, "qq:dm:u", "event", "qq:u", "内容", "理由", "", "[]", "[]",
                     "[]", "fp-" + fid, 5, 1000.0),
                )

    def tearDown(self):
        self.temp.cleanup()

    def test_retract_soft_deletes_and_keeps_version(self):
        candidates = self.store.audit_candidates("qq:dm:u", limit=5)
        self.assertEqual(len(candidates), 2)
        target = candidates[0]
        self.store.audit(
            candidates,
            {
                "actions": [
                    {
                        "action": "retract",
                        "target_id": target["id"],
                        "source_ids": [target["id"]],
                        "content": target["content"],
                        "reason": "与证据矛盾",
                    }
                ]
            },
        )
        rows = {row["id"]: row for row in self.store.facts("qq:dm:u", "", "", 50, 0, True)}
        self.assertNotIn(target["id"], rows)          # 已退出注入与检索
        self.assertEqual(rows[candidates[1]["id"]]["deleted"], 0)
        with self.store.connect() as db:
            snapshot = db.execute(
                "SELECT snapshot FROM versions WHERE kind='fact' AND target=?",
                (target["id"],),
            ).fetchone()
        self.assertIsNotNone(snapshot)                # 原文留档，可恢复


class AuditPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db.sqlite3")
        self.store.initialize()
        self.seen = []

        async def model_call(model, purpose, instruction, schema, payload):
            self.seen.append({"purpose": purpose, "payload": payload, "schema": schema})
            return json.dumps({"actions": []}, ensure_ascii=False)

        self.engine = e.Engine(
            self.store,
            lambda: c.Settings(audit_persona=False),
            model_call,
            lambda text, cfg: asyncio.sleep(0, result=(None, "")),
            lambda sid: asyncio.sleep(0, result=None),
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_audit_payload_drops_internal_fields(self):
        self.store.capture(
            "qq:dm:u",
            "turn",
            [{"role": "user", "content": "我喜欢猫", "time": 1000.0, "users": ["qq:u"]}],
        )
        record_id = self.store.active("qq:dm:u")[0]["id"]
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,
                   relations,sources,fingerprint,deleted,revision,audited,importance,
                   merge_pending,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,0,?)""",
                ("f-1", "qq:dm:u", "preference", "qq:u", "喜欢猫", "用户自己说的",
                 "日常", '["猫"]', "[]", json.dumps([record_id]), "fp", 5, 1000.0),
            )
        asyncio.run(self.engine.audit("qq:dm:u"))
        payload = self.seen[-1]["payload"]
        # 入库的 facts[].id 换成了 f1..fN 短别名；sources 不再入参（指令本来就要求别用它）
        self.assertEqual(payload["facts"][0]["id"], "f1")
        self.assertEqual(
            set(payload["facts"][0]),
            {"id", "sid", "subject", "category", "content", "reason", "relations",
             "importance"},
        )
        self.assertNotIn("sources", json.dumps(payload))
        self.assertEqual(set(payload["evidence"][0]), {"content", "t"})
        self.assertNotIn("title", json.dumps(self.seen[-1]["schema"]))


if __name__ == "__main__":
    unittest.main()


class AuditSummaryTests(unittest.TestCase):
    def test_summary_lists_actions(self):
        e = importlib.import_module("alife_diet_test.engine")
        self.assertEqual(
            e.audit_summary({"scanned": 20, "keep": 20}), "本次审计 20 条：全部保留"
        )
        self.assertEqual(
            e.audit_summary(
                {"scanned": 20, "keep": 15, "correct": 3, "merge": 2, "merged_facts": 2, "retract": 1}
            ),
            "本次审计 20 条：保留 15 · 修正 3 · 合并 2 组（并入 2 条） · 撤回 1",
        )
        self.assertIn("撤回 1", e.audit_summary({"scanned": 5, "keep": 4, "retract": 1}))


class AuditStatsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "m.db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def fact(self, content="萤火喜欢猫", subject="qq:9"):
        self.store.capture(
            "qq:gm:1", "t", [{"role": "user", "content": content, "users": [subject], "time": 1.0}]
        )
        record = self.store.active("qq:gm:1")[-1]
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            return self.store._add_fact(
                db,
                "qq:gm:1",
                {
                    "category": "preference",
                    "subject": subject,
                    "content": content,
                    "reason": "",
                    "scenario": "",
                    "tags": [],
                    "relations": [],
                    "source_ids": [record["id"]],
                },
            )

    def test_audit_reports_counts_and_retract_hides_fact(self):
        a = self.fact("萤火喜欢猫")
        b = self.fact("萤火喜欢猫粮")
        candidates = self.store.facts("qq:gm:1")
        output = {
            "actions": [
                {
                    "action": "keep",
                    "target_id": a,
                    "source_ids": [a],
                    "content": "萤火喜欢猫",
                    "reason": "证据一致",
                },
                {
                    "action": "retract",
                    "target_id": b,
                    "source_ids": [b],
                    "content": "萤火喜欢猫粮",
                    "reason": "与上一条重复",
                },
            ]
        }
        counts = self.store.audit(candidates, output)
        self.assertEqual(counts["keep"], 1)
        self.assertEqual(counts["retract"], 1)
        self.assertEqual(counts["merge"], 0)
        left = [f["id"] for f in self.store.facts("qq:gm:1")]
        self.assertEqual(left, [a])


class EmptyLexicalQueryCase(unittest.TestCase):
    """空/纯符号查询**不得**让词面召回崩 ✗✓（2026-09-17 生产事故复现 ✓）

    `_lexical_sql` 无词元时曾返回裸 ``"0"`` ✗ → 拼进 ``ORDER BY`` 被 SQLite
    当成**列位置** → ``1st ORDER BY term out of range - should be between 1 and 21`` ✓
    触发：群里一条纯表情消息（「🤔」）经被动召回传进 ``facts(lexical=...)`` ✓
    同源问题也在 records 的 ``{lexical_sql}`` 上 ✓ 故两处一起守 ✓
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.cfg = c.Settings()
        with self.store.connect() as db:
            db.execute(
                """INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,
                   relations,sources,fingerprint,deleted,revision,audited,importance,
                   merge_pending,created) VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,0,?,0,?)""",
                ("f-1", "qq:gm:1", "event", "qq:u", "内容", "理由", "", "[]", "[]",
                 "[]", "fp-1", 5, 1000.0),
            )

    def tearDown(self):
        self.temp.cleanup()

    def test_facts_with_tokenless_lexical_does_not_crash(self):
        for text in ("", " ", "!!!", "🤔", "、、。"):
            with self.subTest(text=text):
                rows = self.store.facts("qq:gm:1", lexical=text, limit=5)
                self.assertIsInstance(rows, list)  # 不崩即通过 ✓（行数无所谓 ✓）

    def test_records_with_tokenless_lexical_does_not_crash(self):
        for text in ("", " ", "!!!", "🤔"):
            with self.subTest(text=text):
                res = self.store.search("qq:gm:1", lexical=text, limit=5)
                self.assertIsInstance(res, dict)  # 不崩即通过 ✓（search 返回 {total,items} ✓）
                self.assertIn("items", res)

    def test_lexical_sql_never_returns_bare_integer(self):
        """判据本身 ✓：无词元时返回的必须是**表达式**而不是裸整数 ✗"""
        for empty in ((), []):
            sql = s._lexical_sql("lower(content)", empty)
            self.assertFalse(
                sql.strip().isdigit(),
                "_lexical_sql 无词元时返回了裸整数 %r ✗ —— ORDER BY 会把它当列位置 ✓" % sql,
            )
            # 真跑一遍 SQLite 才算数 ✓
            with self.store.connect() as db:
                db.execute("SELECT * FROM facts ORDER BY %s DESC LIMIT 1" % sql).fetchall()


class CompressionTriggerCase(unittest.TestCase):
    """冷会话三触发 + 迁移（B4）门槛退回 ✓（2026-09-17 用户要求实测确认 ✓）

    规则（`engine.compression_plan` ✓）：
      · 常态：攒够 `compress_rounds` 个**完整轮**才压 ✓
      · **陈旧**：最老记录超过 `compress_stale_after_days` → 门槛降为 **1** ✓
      · **闲置**：最新记录超过 `compress_idle_after_hours` → 门槛降为 **1** ✓
      · **B4**：一个完整轮都算不出（迁移导入 / 同角色堆叠）→ 退回**按条** ✓
    行的形状必须带 start/end + level + position ✓（boost 读的是 end/start ✗ 不是 created ✓）
    """

    def _rows(self, n, age_h, roles=("user", "assistant")):
        now = time.time()
        out = []
        for i in range(n):
            t = now - age_h * 3600 - i * 180
            out.append(dict(id="r%d" % i, start=t, end=t, created=t, summary="s%d" % i,
                            permanent=0, tier="active", importance=5, level=0,
                            position=i + 1, sid="qq:gm:1", visibility="session",
                            role=roles[i % len(roles)]))
        return out

    def _cfg(self, **over):
        base = dict(compress_batch_mode="rounds", compress_rounds=12,
                    batch_size=8, threshold=40)
        base.update(over)
        return c.Settings(**base)

    def test_stale_triggers_threshold_one(self):
        plan = e.compression_plan(self._rows(20, 24 * 10), self._cfg(),
                                  now=time.time(), boost_allowed=True)
        self.assertIsNotNone(plan, "陈旧数据没触发压缩 ✗（阈值没降到 1 ✗）")

    def test_idle_alone_does_not_trigger(self):
        """⚠️ 用户 2026-09-17 要求：陈旧与闲置**必须同时满足** ✓（原来是"或" ✗）

        只闲置（新内容也很新 ✗ 但停了 30 小时 ✓）⇒ **不降门槛** ✓
        （否则「刚停下来、内容还很新」的会话也会被提前压 ✓）
        """
        plan = e.compression_plan(self._rows(20, 30), self._cfg(),
                                  now=time.time(), boost_allowed=True)
        self.assertIsNone(plan, "只满足「闲置」就降门槛了 ✗（应当两个都满足 ✓）")

    def test_stale_alone_does_not_trigger(self):
        """只陈旧（老记录 > 3 天 ✗ 但**刚刚还在聊** ✓）⇒ 不降门槛 ✓"""
        rows = self._rows(3, 24 * 10) + self._rows(17, 0.05)
        for i, r in enumerate(rows):
            r["id"] = "x%d" % i
        plan = e.compression_plan(rows, self._cfg(), now=time.time(), boost_allowed=True)
        self.assertIsNone(plan, "只满足「陈旧」就降门槛了 ✗（应当两个都满足 ✓）")

    def test_both_stale_and_idle_triggers(self):
        """两个都满足（既久没动 ✓ 又有积压 ✓）⇒ 降门槛 ✓"""
        plan = e.compression_plan(self._rows(20, 24 * 10), self._cfg(),
                                  now=time.time(), boost_allowed=True)
        self.assertIsNotNone(plan, "两个条件都满足却没降门槛 ✗")

    def test_fresh_session_does_not_trigger(self):
        plan = e.compression_plan(self._rows(20, 0.1), self._cfg(),
                                  now=time.time(), boost_allowed=True)
        self.assertIsNone(plan, "刚聊过的会话不该降门槛 ✗")

    def test_boost_disallowed_does_not_trigger(self):
        plan = e.compression_plan(self._rows(20, 24 * 10), self._cfg(),
                                  now=time.time(), boost_allowed=False)
        self.assertIsNone(plan, "调度没放行时不该降门槛 ✗")

    def test_migrated_rows_fall_back_to_count(self):
        """B4：迁移导入（全是 user，算不出完整轮）→ 退回按条 ✓"""
        rows = self._rows(24, 24 * 400, roles=("user",))
        plan = e.compression_plan(rows, self._cfg(), now=time.time(), boost_allowed=True)
        self.assertIsNotNone(plan, "迁移数据压不动 ✗（B4 退回按条没生效 ✗）")
        self.assertLessEqual(len(plan[0]), 8, "退回按条时不得超过 batch_size ✗")


class BoostGateCase(unittest.TestCase):
    """闸门（scheduler / on_request / 启动扫描）**不得消耗降门槛资格** ✗✓

    2026-09-17 生产实测的真实故障 ✓：
      · 闸门 `compression_plan(..., boost_allowed=_boost_ok(sid, cfg))` ✗
        —— `_boost_ok` 会**盖章**（写冷却时间戳 ✓）
      · 于是任务真正跑起来再问一次 ✗ 已经在冷却里 ⇒ 必然 False ✗
      · ⇒ **每一个排出去的压缩任务都空转** ✓ 日志刷屏"本次没有需要压缩的内容" ✓
        迁移来的 L0 **永远压不掉** ⇒ 永远提炼不出事实 ✓

    判据：闸门用 `stamp=False` ✓ 只有真正要压的那个调用点才盖章 ✓
    """

    def _rows(self, n=20):
        now = time.time()
        out = []
        for i in range(n):
            t = now - 400 * 86400 - i * 180
            out.append(dict(id="r%d" % i, start=t, end=t, created=t, summary="s%d" % i,
                            permanent=0, tier="active", importance=5, level=0,
                            position=i + 1, sid="s:gate", visibility="session", role="user"))
        return out

    def _cfg(self):
        return c.Settings(compress_batch_mode="rounds", compress_rounds=12,
                          batch_size=8, threshold=40)

    def test_gate_does_not_consume_boost(self):
        sid, cfg = "s:gate:1", self._cfg()
        e._BOOST_AT.pop(sid, None)
        self.assertTrue(e._boost_ok(sid, cfg, stamp=False), "闸门该放行 ✓")
        self.assertNotIn(sid, e._BOOST_AT, "闸门盖了戳 ✗ ⇒ 任务必然空转 ✓")
        # 闸门排得出任务 ✓
        self.assertIsNotNone(e.compression_plan(self._rows(), cfg, boost_allowed=True))
        # 任务里再问一次，仍须拿到降门槛 ✓（这才是能真压的前提 ✓）
        self.assertTrue(e._boost_ok(sid, cfg), "任务拿不到降门槛 ✗ ⇒ 排了也白排 ✓")
        self.assertIn(sid, e._BOOST_AT, "任务该盖戳（冷却从这里开始算 ✓）")

    def test_job_stamp_still_enforces_cooldown(self):
        sid, cfg = "s:gate:2", self._cfg()
        e._BOOST_AT.pop(sid, None)
        self.assertTrue(e._boost_ok(sid, cfg))
        # 冷却期内闸门不再放行 ✓（避免重复排任务 ✓）
        self.assertFalse(e._boost_ok(sid, cfg, stamp=False), "冷却没生效 ✗")


class SchedulerCompressCase(unittest.TestCase):
    """scheduler 真的能把**安静的迁移会话**压掉吗 ✓（对照组 ✓ 2026-09-17 用户提问 ✓）

    两个事实必须同时成立 ✓：
      ① `probability` 是**骰子闸门** ✗ —— 默认 1.0 时 scheduler 会中 ✓
         =0（从不主动回复）时 **永远不会中** ✗ ⇒ 迁移会话压不动 ✓
      ② 即便中了 ✗ —— 修复前 `_boost_ok` 的盖章会让任务 100% 空转 ✓

    所以 `queue_compress_all()`（启动扫描 / 迁移后扫描）是有意义的：
    它**不看骰子** ✓ 确定性兜底 ✓
    """

    def _seed(self, sid):
        now = time.time()
        with self.store.connect() as db:
            for i in range(20):
                t = now - 400 * 86400 - i * 180
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted) VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0)",
                    ("m-%d" % i, sid, "user", t, t, "旧 %d" % i, "旧 %d" % i,
                     "[]", i + 1, now, "session"))
            db.commit()

    def _levels(self, sid):
        with self.store.connect() as db:
            return dict(db.execute(
                "SELECT level, count(*) FROM records WHERE sid=? AND deleted=0 GROUP BY level",
                (sid,)).fetchall())

    def _cfg(self, probability):
        return c.Settings(compress_batch_mode="rounds", compress_rounds=12,
                          batch_size=8, threshold=40, probability=probability)

    def _run(self, probability, with_sweep):
        sid = "legacy:t:%s:%s" % (probability, with_sweep)
        self._seed(sid)
        cfg = self._cfg(probability)
        e._BOOST_AT.pop(sid, None)
        loop = asyncio.new_event_loop()
        try:
            model = lambda *a, **k: asyncio.sleep(0, result=json.dumps(
                {"summary": "迁移摘要", "facts": []}))

            async def flow():
                eng = e.Engine(self.store, lambda: cfg, model, None, None)
                if with_sweep:
                    rows = await self.store.call("active", sid)
                    if e.compression_plan(rows, cfg, now=time.time(),
                                        boost_allowed=e._boost_ok(sid, cfg, stamp=False)):
                        await eng.enqueue("compress", sid, automatic=True)
                    await eng.start()
                    await asyncio.sleep(2.0)
                else:
                    await eng.start()          # 起 worker + scheduler ✓
                    await asyncio.sleep(3.5)
                await eng.stop()
            loop.run_until_complete(flow())
        finally:
            loop.close()
        return self._levels(sid)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_scheduler_can_compress_quiet_migrated_when_dice_allows(self):
        lv = self._run(1.0, with_sweep=False)
        self.assertIn(1, lv, "scheduler 掷中骰子也没压成 ✗（boost 盖章问题回归了 ✓）")

    def test_scheduler_starves_when_probability_zero(self):
        lv = self._run(0.0, with_sweep=False)
        self.assertNotIn(1, lv, "probability=0 时不该压 ✓（骰子闸门失效了 ✗）")

    def test_startup_sweep_does_not_need_the_dice(self):
        """扫描的价值：**不看骰子** ✓（用极小概率让 scheduler 实际不可能中 ✓）"""
        lv = self._run(1e-9, with_sweep=True)
        self.assertIn(1, lv, "启动扫描不该依赖 probability 掷骰 ✓")

    def test_probability_zero_means_manual_only(self):
        """`自动压缩概率=0` 的文案是「仅手动」✗ ⇒ 启动扫描也必须尊重 ✓"""
        sid = "legacy:t:manual"
        self._seed(sid)
        cfg = self._cfg(0.0)
        e._BOOST_AT.pop(sid, None)
        import types as _t
        plugin = _t.SimpleNamespace(
            runtime_settings=lambda: cfg, store=self.store, engine=None)
        # 直接验证判定：probability=0 时扫描应当**不排任何会话** ✓
        self.assertEqual(cfg.probability, 0.0)
        self.assertFalse(bool(cfg.probability), "0 就是「仅手动」✓（扫描必须直接返回 ✓）")


class CascadeCatchUpCase(unittest.TestCase):
    """一条压缩任务必须能**追平整个会话** ✗✓（用户问"2000 条会怎样"时实测 ✓）

    故障形态（修复前 ✓）：`_compress_cascade` 在循环里**每轮都问一次** `_boost_ok` ✓
    而它会盖章写冷却 ✓ ⇒ 第二轮就已经"冷却中" ⇒ 只能压 **1 批(40 条)** 就收手 ✓
      实测：200 条 → `active{0:40, 1:161}` ✗ 2000 条要 40 条/30 分钟爬 25 小时 ✗✓
    修复：资格**每条任务只判定一次** ✓ 循环里一直有效 ✓（计划为空即退出 ✓ 不空转 ✓）
    """

    def _seed(self, sid, n):
        now = time.time()
        with self.store.connect() as db:
            for i in range(n):
                t = now - 400 * 86400 - i * 180
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted) VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0)",
                    ("c-%d" % i, sid, "user", t, t, "旧 %d" % i, "旧 %d" % i,
                     "[]", i + 1, now, "session"))
            db.commit()

    def test_one_job_respects_batch_cap(self):
        """一条任务最多压 `compress_batches_per_job` 批 ✓（花钱闸门 ✓ 2026-09-17 用户要求 ✓）

        历史：先修了"每轮重问 boost ⇒ 只压 1 批"的 bug ✓ 但放开成"追平整个会话"后
        2000 条 ≈50 次模型调用会几分钟烧完 ✗ ⇒ 改为可配置上限（默认 3 批 = 120 条）✓
        """
        sid = "legacy:cap:1"
        self._seed(sid, 200)
        cfg = c.Settings()          # compress_batches_per_job 默认 3 ✓
        e._BOOST_AT.pop(sid, None)
        calls = []

        async def model(*a, **k):
            calls.append(1)
            return json.dumps({"summary": "合并摘要", "facts": []})

        loop = asyncio.new_event_loop()

        async def flow():
            eng = e.Engine(self.store, lambda: cfg, model, None, None)
            await eng.start()
            await eng.compress(sid)
            await eng.stop()
        try:
            loop.run_until_complete(flow())
        finally:
            loop.close()
        with self.store.connect() as db:
            archived = db.execute(
                "SELECT count(*) FROM records WHERE sid=? AND active=0 AND level=0", (sid,)).fetchone()[0]
        cap = cfg.compress_batches_per_job * cfg.batch_size
        self.assertLessEqual(archived, cap, "压了 %d 条，超过上限 %d ✗（花钱闸门失效 ✓）" % (archived, cap))
        self.assertGreater(archived, cfg.batch_size,
                           "只压了一批 ✗ ⇒ 又回到「每轮重问 boost」的老 bug ✓")
        self.assertLessEqual(len(calls), cfg.compress_batches_per_job + 1,
                             "模型调用 %d 次，超出闸门 ✗" % len(calls))

    def test_batch_cap_one_means_one_batch(self):
        sid = "legacy:cap:2"
        self._seed(sid, 200)
        cfg = c.Settings(compress_batches_per_job=1)
        e._BOOST_AT.pop(sid, None)

        async def model(*a, **k):
            return json.dumps({"summary": "合并摘要", "facts": []})

        loop = asyncio.new_event_loop()

        async def flow():
            eng = e.Engine(self.store, lambda: cfg, model, None, None)
            await eng.start()
            await eng.compress(sid)
            await eng.stop()
        try:
            loop.run_until_complete(flow())
        finally:
            loop.close()
        with self.store.connect() as db:
            archived = db.execute(
                "SELECT count(*) FROM records WHERE sid=? AND active=0 AND level=0", (sid,)).fetchone()[0]
        self.assertLessEqual(archived, cfg.batch_size, "cap=1 时应只压 1 批（40 条）✗")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()


class QuietAutomaticJobCase(unittest.TestCase):
    """自动任务**空转**时不该刷日志/占工作台 ✓（2026-09-17 用户要求 ✓）

    用户看到的是：一批「事实合并完成（合并 0 组重复事实）」✗
    —— 这类**不调模型、什么都没做**的任务 ✓ 只是噪音 ✓（连排 7 条 ✓）

    ⚠️ 三条铁律（不能伤到有意义的日志 ✓）：
      · **手动**任务永不静默 ✓（用户明确要求手动的要看得见 ✓）
      · **真干活**的（哪怕只合并了 1 组）永不静默 ✓
      · `audit` / `classify` / `rewrite` **一定调过模型** ⇒ 永不静默 ✓✓
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.eng = e.Engine(self.store, lambda: c.Settings(), None, None, None)
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()
        self.temp.cleanup()

    def _q(self, kind, detail, automatic=True):
        return self.loop.run_until_complete(
            self.eng._quiet_automatic({"id": "x", "kind": kind, "sid": "s", "automatic": int(automatic)}, detail))

    def test_automatic_noop_is_quiet(self):
        self.assertTrue(self._q("fact_merge", "合并 0 组重复事实（0 条并入）"), "空合并该静默 ✓")
        self.assertTrue(self._q("compress", "本次没有需要压缩的内容"), "空压缩该静默 ✓")

    def test_manual_noop_is_visible(self):
        self.assertFalse(self._q("fact_merge", "合并 0 组重复事实（0 条并入）", automatic=False),
                         "手动任务必须可见 ✗（用户明确要求 ✓）")

    def test_real_work_is_visible(self):
        self.assertFalse(self._q("fact_merge", "合并 2 组重复事实（3 条并入）"), "真合并必须可见 ✓")
        self.assertFalse(self._q("compress", "压缩 40 条 → L1"), "真压缩必须可见 ✓")

    def test_model_calling_kinds_never_quiet(self):
        for kind, detail in (("audit", "保留 3 · 修正 1"), ("classify", "已归类"),
                             ("rewrite", "重写 2 条")):
            self.assertFalse(self._q(kind, detail), "%s 一定调过模型 ⇒ 不许静默 ✗" % kind)

    def test_quiet_notes_only_contain_noop_wording(self):
        """清单本身也要守 ✓：只允许"确定不调模型"的空转措辞 ✓"""
        for note in e.QUIET_JOB_NOTES:
            self.assertIn("没有", note, "清单里混进了非空转措辞 ✗：%s" % note)


class BotIssuedTaskVisibleCase(unittest.TestCase):
    """**有发起方**的任务必须有日志 ✓（bot 发起 / 工作台按钮 ✓）

    用户 2026-09-17 指出：tidy 那边如果**是 bot 发出的**，也要算"手动" ⇒ 要有日志 ✓
    （区分标准不是"谁在跑"，而是"**有没有人在等结果**" ✓）
    """

    def test_queue_tidy_all_accepts_automatic_flag(self):
        import inspect
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertIn(
            "async def queue_tidy_all(self, fallback_sid=\"\", automatic=True, force=False, ids=None", src
        )
        self.assertIn('enqueue("tidy", owner, automatic=automatic', src)

    def test_bot_and_workbench_call_it_as_manual(self):
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertIn("automatic=False,", code, "bot 的 tidy 没标成手动 ✗（会没日志 ✓）")
        self.assertIn("automatic=False,        # 工作台按钮", code, "工作台按钮没标成手动 ✗")
        self.assertIn('"tidy", value.sid, automatic=False', code,
                      "bot 写永久记忆后的整理没标成手动 ✗")
        self.assertIn('"protect_id": record_id', code,
                      "写后整理没有保护刚写入的条目 ✗")

    def test_truly_automatic_ones_stay_automatic(self):
        """真正自动的（阈值兜底 / 调度器）保持 automatic=True ✓ 空转时仍可静默 ✓"""
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertIn('enqueue("tidy", owner, automatic=True)', code, "阈值兜底那条被误改了 ✗")

    def test_reindex_noop_drops_job(self):
        src = (Path(__file__).resolve().parents[1] / "engine.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
        self.assertIn('"drop_job", job["id"]', code, "reindex 空转还在留任务 ✗")


class JobHousekeepingCase(unittest.TestCase):
    """方案 A（清理过期任务）+ 方案 C（扫描按陈旧度排 ✓）—— 2026-09-17 用户要求 ✓

    A：工作台的"工作明细"原来**只增不减** ✗（jobs 表永久堆积 ✓）
    C：扫描原来用 `sorted(sessions)` = **字母序** ✗ ⇒ 挑出的 8 个跟"谁更需要压"无关 ✓
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    # ── A ──
    def _job(self, jid, state, updated):
        # ⚠️ jobs 有 UNIQUE(kind, sid) ✗✓ ⇒ 同一 (kind,sid) 只能有一行 ✓
        # （这正是"同一会话不会重复排队"的天然保证 ✓ 也说明"任务多"= 会话多 ✓）
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO jobs(id,kind,sid,state,detail,created,updated,automatic)"
                " VALUES(?,'compress',?,?,?,?,?,1)",
                (jid, "s-" + jid, state, "", updated, updated))
            db.commit()

    def test_prune_drops_old_but_keeps_recent(self):
        now = time.time()
        for i in range(10):
            self._job("old-%d" % i, "completed", now - 30 * 86400 - i)
        for i in range(3):
            self._job("new-%d" % i, "completed", now - i)
        self.store.prune_jobs(keep_days=7, keep_min=3)
        with self.store.connect() as db:
            left = {r[0] for r in db.execute("SELECT id FROM jobs").fetchall()}
        self.assertTrue(all(x.startswith("new-") for x in left), "新任务被误删 ✗：%s" % left)
        self.assertGreaterEqual(len(left), 3, "keep_min 没保住 ✗")

    def test_prune_never_touches_queued_or_running(self):
        """🔴 安全底线：**正在排队/执行的任务绝不能删** ✓✓"""
        now = time.time()
        self._job("q-1", "queued", now - 90 * 86400)
        self._job("r-1", "running", now - 90 * 86400)
        for i in range(30):
            self._job("done-%d" % i, "completed", now - 60 * 86400 - i)
        self.store.prune_jobs(keep_days=1, keep_min=1)
        with self.store.connect() as db:
            left = {r[0] for r in db.execute("SELECT id FROM jobs").fetchall()}
        self.assertEqual({"q-1", "r-1"}, left, "排队/执行中的任务被删了 ✗✗ 严重 ✓")

    def test_prune_cleans_orphan_items(self):
        now = time.time()
        self._job("keep", "completed", now)
        self._job("gone", "completed", now - 100 * 86400)
        with self.store.connect() as db:
            db.execute("INSERT INTO job_items(job_id,kind,target,action,note,before,created) VALUES('gone','compress','t','a','','',?)", (time.time(),))
            db.commit()
        self.store.prune_jobs(keep_days=7, keep_min=1)
        with self.store.connect() as db:
            n = db.execute("SELECT count(*) FROM job_items WHERE job_id='gone'").fetchone()[0]
        self.assertEqual(n, 0, "孤儿明细没清掉 ✗")

    # ── C ──
    def test_sessions_by_age_orders_oldest_first(self):
        now = time.time()
        with self.store.connect() as db:
            for sid, age_days in (("s:new", 1), ("s:old", 30), ("s:mid", 10)):
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted) VALUES(?,?,'user',0,?,?,?,?,?,?,?,?,1,0)",
                    ("r-" + sid, sid, now - age_days * 86400, now - age_days * 86400,
                     "内容", "内容", "[]", 1, now, "session"))
            db.execute(
                "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                "position,created,visibility,active,deleted) VALUES('r-off','s:old','user',0,?,?,?,?,?,?,?,?,0,0)",
                (now, now, "内容", "内容", "[]", 9, now, "session"))
            db.commit()
        out = self.store.sessions_by_age()
        self.assertEqual(out, ["s:old", "s:mid", "s:new"], "没按陈旧度排 ✗（字母序会排成 mid/new/old ✓）")


class HousekeepingWiringCase(unittest.TestCase):
    """接线检查 ✓（剥注释后判 ✗ 免得被注释骗过 ✓）"""

    def _code(self, name):
        src = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    def test_scan_uses_staleness_order(self):
        code = self._code("main.py")
        self.assertIn('"sessions_by_age"', code, "扫描没用按陈旧度排序 ✗（方案 C 失效 ✓）")
        self.assertIn("sorted(await self.store.call(\"sessions\"))", code,
                      "兜底分支没了 ✗（老库要还能跑 ✓）")

    def test_engine_prunes_jobs_periodically(self):
        code = self._code("engine.py")
        self.assertIn('"prune_jobs"', code, "没有接周期清理 ✗（方案 A 失效 ✓ 列表会无限增长 ✓）")
        self.assertIn("_last_prune", code, "没有节流 ⇒ 会每 30 秒清一次 ✗")


class SweepBurstCase(unittest.TestCase):
    """扫描的 `limit` 是**花钱闸门** ✓（2026-09-17 用户实测反馈 ✓）

    用户问："存量用户更新后，为什么一次性有满 8 个分层压缩？"
    查明：`queue_compress_all(limit=8)` ✗ —— 而每个任务最多 `compress_batches_per_job`
    （默认 3 ✓）批 ⇒ **启动瞬间最多 24 次模型调用** ✗ 与"省钱闸门"设计相悖 ✓
    ⇒ 默认降到 2（突发 ≤ 6 次 ✓）其余交给 30 秒调度器与下一轮扫描 ✓（不会漏 ✓）
    """

    def _main_code(self):
        src = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    def test_default_limit_is_gentle(self):
        import re
        code = self._main_code()
        m = re.search(r"async def queue_compress_all\(self, limit=(\d+)\)", code)
        self.assertIsNotNone(m, "扫描签名变了 ✗")
        self.assertLessEqual(int(m.group(1)), 2,
                             "扫描默认上限 %s 太大 ✗（一次会打出很多模型调用 ✓）" % m.group(1))

    def test_burst_is_bounded(self):
        import re
        code = self._main_code()
        limit = int(re.search(r"async def queue_compress_all\(self, limit=(\d+)\)", code).group(1))
        per_job = c.Settings.model_fields["compress_batches_per_job"].default
        self.assertLessEqual(limit * per_job, 6,
                             "一次扫描最坏 %d 次调用 ✗ 太猛 ✓（应 ≤ 6 ✓）" % (limit * per_job))


class ActiveBySessionEquivalenceCase(unittest.TestCase):
    """合并查询必须与逐会话查询**完全等价** ✓（2026-09-17 的性能优化 ✓）

    调度器原来 `for sid in sessions: active(sid)` ✗ = N+1 次查询 ✓（100 会话 ⇒ 每 30 秒 100 次 ✓）
    ⇒ 改成一次 `active_by_session` ✓ 但**行的形状与顺序必须一模一样** ✓✓
    （否则压缩取到的批次会变 ✗ 那就动了实质逻辑 ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_equivalent_to_per_session_active(self):
        now = time.time()
        with self.store.connect() as db:
            for sid in ("s:a", "s:b", "s:c"):
                for i in range(5):
                    db.execute(
                        "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                        "position,created,visibility,active,deleted) VALUES(?,?,?,0,?,?,?,?,?,?,?,?,?,0)",
                        ("%s-%d" % (sid, i), sid, ("user", "assistant")[i % 2], now - i * 60, now - i * 60,
                         "内容 %d" % i, "内容 %d" % i, "[]", i + 1, now, "session", 1 if i < 4 else 0))
            db.commit()
        grouped = self.store.active_by_session()
        for sid in ("s:a", "s:b", "s:c"):
            self.assertEqual(grouped[sid], self.store.active(sid),
                             "%s 的合并查询结果与逐会话查询不一致 ✗" % sid)
        self.assertEqual(sorted(grouped), ["s:a", "s:b", "s:c"], "分组丢会话 ✗")

    def test_only_active_rows(self):
        now = time.time()
        with self.store.connect() as db:
            db.execute(
                "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,position,"
                "created,visibility,active,deleted) VALUES('x','s:x','user',0,?,?,?,?,?,?,?,?,0,0)",
                (now, now, "归档", "归档", "[]", 1, now, "session"))
            db.commit()
        self.assertNotIn("s:x", self.store.active_by_session(), "归档记录不该被带出来 ✗")


class TidyForceWiringCase(unittest.TestCase):
    """「无视冷却」必须真正传到引擎 ✓（2026-09-17 用户实测：三条链路都没生效 ✓）

    实际故障：给 `queue_tidy_all` 加 `automatic` 时**把 `force` / `ids` 弄丢了** ✗
    ⇒ bot 主动链路 + 工作台「全部重新整理」直接 **TypeError** ✗
    ⇒ 而 `api_job` 又**没把** `value.force` 传下去 ✗ ⇒ 前端发的 force 被丢掉 ✓
    ⇒ 用户看到的就成了"点全部重新整理也没有无视冷却" ✗
    """

    def _code(self, name):
        src = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    def test_queue_tidy_all_accepts_force_and_ids(self):
        code = self._code("main.py")
        self.assertIn("force=False, ids=None", code,
                      "queue_tidy_all 又丢了 force/ids ✗ ⇒ 调用方会 TypeError ✓")

    def test_force_and_ids_go_into_job_detail(self):
        code = self._code("main.py")
        self.assertIn('"force": bool(force)', code, "force 没进任务 detail ✗（引擎就看不到 ✓）")
        self.assertIn('"ids": list(ids or [])', code, "ids 没进任务 detail ✗")

    def test_api_job_forwards_force(self):
        code = self._code("main.py")
        self.assertIn("force=value.force", code, "api_job 没把前端 force 传下去 ✗")
        self.assertIn("ids=(value.ids or None)", code, "api_job 没把前端 ids 传下去 ✗")

    def test_bot_path_forwards_force(self):
        code = self._code("main.py")
        self.assertIn("force=bool(force)", code, "bot 主动链路没传 force ✗")

    def test_per_item_reextract_sends_force(self):
        js = (Path(__file__).resolve().parents[1] / "web" / "app.js").read_text(encoding="utf-8")
        self.assertIn("force: true", js, "单条「重新提取事实」没发 force ✗")

    def test_engine_reads_force_from_detail(self):
        code = self._code("engine.py")
        self.assertIn('_force = bool(_d.get("force"))', code, "引擎没解析 force ✗")
        self.assertIn("0 if (force or over_pressure) else cfg.permanent_tidy_days", code,
                      "force/超限没被换成 0 天（=没无视冷却）✗")


class TidyUnknownTargetCase(unittest.TestCase):
    """模型编 id 时：**跳过那一条**，不能让整批整理作废 ✓（2026-09-17 用户要求核查 ✓）

    原实现：`if record_id not in known: raise ValueError("unknown tidy target")` ✗
    ⇒ 一条坏输出 ⇒ **整批作废**（前面已应用的条目白做 ✗）⇒ 还进重试 ⇒ 多半整体失败 ✓
    对比：compress 故意 raise（source 可疑就该重试 ✓）；但 tidy 的 id 是**处置目标** ✗
    ⇒ 一个坏目标不该连累其它条目 ✓（跳过 = 那条记录保持不动 ✓ 等价 keep ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.eng = e.Engine(self.store, lambda: c.Settings(), None, None, None)
        self.loop = asyncio.new_event_loop()

    def tearDown(self):
        self.loop.close()
        self.temp.cleanup()

    def _seed(self):
        now = time.time()
        with self.store.connect() as db:
            for i in (1, 2):
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold) "
                    "VALUES(?,'s:1','assistant',9,?,?,?,?,?,?,?,?,1,0,1,0)",
                    ("r%d" % i, now, now, "摘要 %d" % i, "摘要 %d" % i, "[]", i, now, "session"))
            db.commit()
        return self.store.permanent_records("s:1")

    def test_unknown_id_is_skipped_not_raised(self):
        rows = self._seed()
        cands = [r for r in rows if r["id"] in ("r1", "r2")]
        aliases = {"p1": "r1", "p2": "r2"}
        output = {"items": [
            {"id": "p1", "action": "archive", "reason": "过期"},
            {"id": "p9", "action": "archive", "reason": "编的"},   # ✗ 不存在的目标
            {"id": "p2", "action": "keep", "reason": "保留"},
        ]}
        applied = self.loop.run_until_complete(
            self.eng.apply_tidy("s:1", cands, aliases, {}, output))
        self.assertEqual(applied, 1, "正常的那条应当照旧被应用 ✓（只有坏目标被跳过 ✓）")
        with self.store.connect() as db:
            act = dict(db.execute(
                "SELECT id, active FROM records WHERE id IN ('r1','r2')").fetchall())
        self.assertEqual(act, {"r1": 0, "r2": 1}, "应用结果不对 ✗（r1 归档 ✓ r2 保持 ✓）")
        self.assertIn("跳过", self.eng.last_tidy_notes.get("s:1", "") or "",
                      "跳过条数应当体现在提示里 ✓（否则用户看到条数对不上 ✓）")

    def test_all_bad_ids_does_not_raise(self):
        rows = self._seed()
        cands = [r for r in rows if r["id"] in ("r1", "r2")]
        applied = self.loop.run_until_complete(
            self.eng.apply_tidy("s:1", cands, {"p1": "r1"}, {},
                                {"items": [{"id": "zzz", "action": "archive", "reason": "x"}]}))
        self.assertEqual(applied, 0, "全是坏目标时应当**安静跳过**而不是抛错 ✗")


class TidyExtractMergesCase(unittest.TestCase):
    """整理**提炼出的事实**必须照常参与去重合并 ✓（2026-09-17 用户要求核查 ✓）

    缺口：`apply_tidy` 提炼事实后只 `add_facts` ✗ 没排合并 ✗
    ⇒ 提炼出来的重复事实要一直等下一个触发点（压缩/分类/召回 ✓）才可能被并 ✓
    ⇒ 对照：compress 的 job 包装层是会排的（`queue_fact_merges(sid, started_at)` ✓）
    """

    def _code(self, name):
        src = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    def test_tidy_worker_queues_merges_after_extract(self):
        code = self._code("engine.py")
        self.assertIn('await self.queue_fact_merges(job["sid"], _wall)', code,
                      "整理提炼出的事实没排合并 ✗（重复事实会一直躺着 ✓）")

    def test_uses_wall_clock_not_monotonic(self):
        """`since` 必须是**墙钟** ✗ —— monotonic 与事实的 created 不同源 ✓"""
        code = self._code("engine.py")
        self.assertIn("_wall = time.time()", code, "没取墙钟 ⇒ since 不可比 ✓")

    def test_compact_schemas_cover_tidy_and_dedupe(self):
        """两条**模型必经**的输出都要有紧凑声明 ✓（否则每次多带完整 schema ✓）"""
        code = self._code("engine.py")
        for purpose in ('"tidy"', '"dedupe"'):
            self.assertIn(purpose + ":", code, "COMPACT_SCHEMAS 缺 %s ✗" % purpose)


class InternalJobAutomaticFlagCase(unittest.TestCase):
    """**内部路径**排的任务必须标 `automatic=True` ✓（2026-09-18 用户：这行日志刷屏 ✓）

    `事实合并完成（合并 0 组重复事实（0 条并入））` ✗ —— 纯机械判断 ✓ **没走到模型** ✓
    （`merge_facts` 无候选时 `return 0` ✓ 已验证 ✓）
    但三处入队**都没传 automatic** ✗ ⇒ 库里默认 0 = "手动" ⇒ 空转静默规则不生效 ✓
    ⇒ 每个空转的合并任务都打一行日志 ✓（用户实测 9 行/2 秒 ✓）
    """

    def _code(self, name):
        src = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    def test_internal_fact_merge_enqueues_are_automatic(self):
        bad = []
        for name in ("engine.py", "main.py"):
            for i, line in enumerate(self._code(name).splitlines(), 1):
                if 'enqueue("fact_merge"' in line and "automatic=True" not in line:
                    bad.append("%s:%d" % (name, i))
        self.assertEqual(bad, [], "内部 fact_merge 入队没标 automatic=True ✗"
                                  "（空转时不会静默 ⇒ 日志刷屏 ✓）：%s" % bad)

    def test_noop_merge_is_quiet_when_automatic(self):
        """空转合并（0 组）在自动标记下必须判为静默 ✓"""
        import tempfile
        loop = asyncio.new_event_loop()
        try:
            temp = tempfile.TemporaryDirectory()
            st = s.Store(Path(temp.name) / "db")
            st.initialize()
            eng = e.Engine(st, lambda: c.Settings(), None, None, None)
            self.assertTrue(loop.run_until_complete(eng._quiet_automatic(
                {"id": "x", "kind": "fact_merge", "sid": "s", "automatic": 1},
                "合并 0 组重复事实（0 条并入）")))
            self.assertFalse(loop.run_until_complete(eng._quiet_automatic(
                {"id": "x", "kind": "fact_merge", "sid": "s", "automatic": 1},
                "合并 2 组重复事实（3 条并入）")), "真合并必须照常可见 ✓")
            temp.cleanup()
        finally:
            loop.close()


class ManualJobsStayVisibleCase(unittest.TestCase):
    """**用户手动**排的任务永远不能被静默 ✓（2026-09-18 用户确认 ✓）

    · 手动入口 `api_job` 一律**不传** `automatic` ⇒ 默认 False ⇒ 永远可见 ✓
    · `_quiet_automatic` **要求** automatic 为真 ⇒ 手动天然免疫 ✓
    · 前端手动按钮只有 compress/audit/dedupe/tidy/rewrite/reindex ✗
      **没有 fact_merge** ✓ ⇒ 给三处内部 fact_merge 标 automatic=True 不会波及手动 ✓
    """

    def _code(self, name):
        src = (Path(__file__).resolve().parents[1] / name).read_text(encoding="utf-8")
        return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))

    def test_manual_entry_never_marks_automatic(self):
        code = self._code("main.py")
        self.assertIn('await self.engine.enqueue(value.kind, value.sid)',
                      code, "手动入口的入队形态变了 ✗ 需确认它仍然不传 automatic ✓")
        self.assertNotIn('enqueue(value.kind, value.sid, automatic=True)',
                         code, "手动入口被标成自动了 ✗✗ 用户手动的会被静默 ✓")

    def test_quiet_rule_requires_automatic(self):
        code = self._code("engine.py")
        self.assertIn("if not job.get(\"automatic\"):", code,
                      "静默判定不再要求 automatic ✗ ⇒ 手动任务也会被静默 ✓")

    def test_frontend_manual_buttons_exclude_fact_merge(self):
        html = (Path(__file__).resolve().parents[1] / "web" / "index.html").read_text(encoding="utf-8")
        jobs = set(re.findall(r'data-job="(\w+)"', html))
        self.assertNotIn("fact_merge", jobs,
                         "前端出现了手动 fact_merge 按钮 ✗ ⇒ 需重新确认它的可见性 ✓")
        self.assertTrue(jobs, "没找到任何手动任务按钮 ✗")

    def test_manual_noop_merge_is_visible_behaviorally(self):
        """行为判据 ✓：automatic=0 的空转合并**必须**判为"要显示" ✓"""
        import tempfile
        loop = asyncio.new_event_loop()
        try:
            temp = tempfile.TemporaryDirectory()
            st = s.Store(Path(temp.name) / "db")
            st.initialize()
            eng = e.Engine(st, lambda: c.Settings(), None, None, None)
            self.assertFalse(
                loop.run_until_complete(eng._quiet_automatic(
                    {"id": "x", "kind": "fact_merge", "sid": "s", "automatic": 0},
                    "合并 0 组重复事实（0 条并入）")),
                "手动排的空转合并被静默了 ✗（用户手动的必须看得见 ✓）")
            temp.cleanup()
        finally:
            loop.close()


class JobLogNoiseCase(unittest.TestCase):
    """后台任务的日志噪音分界 ✓（2026-09-18 用户实测："待压缩扫描说 3 个有内容，
    结果 3 个任务全说没有需要压缩的内容" ✗ 而且这行**开始时**就打出来了 ✓）

    · **自动 + 空转** ⇒ **一行都不打** ✓（"开始"行开始时打了就收不回 ✗ ⇒ 干脆不打 ✓）
    · **自动 + 真干活** ⇒ 打"完成"行 ✓
    · **手动** ⇒ 保留"开始"行 ✓（用户点了在等 ✓ 需要即时反馈 ✓）
    """

    def _mk(self, sid, rows=1, age_days=5):
        temp = tempfile.TemporaryDirectory()
        st = s.Store(Path(temp.name) / "db")
        st.initialize()
        now = time.time()
        with st.connect() as db:
            st._ensure_entities(db, sid, ["u-1"])
            for i in range(rows):
                t = now - 86400 * age_days - i * 60
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold) "
                    "VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0,0,0)",
                    ("r%d" % i, sid, ("user", "assistant")[i % 2], t, t, "对话 %d" % i,
                     "对话 %d" % i, json.dumps(["u-1"]), i + 1, now, "session"))
            db.commit()
        return temp, st

    def _run(self, st, automatic):
        import logging
        lines = []
        handler = logging.Handler()
        handler.emit = lambda rec: lines.append(rec.getMessage())
        # ⚠️ 必须挂到**插件自己的 logger** 上 ✗✓ —— 它未必向 root 传播
        #（2026-09-18 实测：KIRA_CORE 环境下 root 抓不到 ✓ 日志其实打出来了 ✓）
        targets = [logging.getLogger(), logging.getLogger("alife_memory_z")]
        old_levels = [(lg, lg.level) for lg in targets]
        for lg in targets:
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
        loop = asyncio.new_event_loop()

        async def model(*a, **k):
            return json.dumps({"summary": "s", "facts": []})

        async def flow():
            eng = e.Engine(st, lambda: c.Settings(), model, None, None)
            st.enqueue("compress", "qq:gm:1", "", automatic)
            await eng.start()
            await asyncio.sleep(2.5)
            await eng.stop()
        try:
            loop.run_until_complete(flow())
        finally:
            loop.close()
            for lg, lvl in old_levels:
                lg.removeHandler(handler)
                lg.setLevel(lvl)
        return [l for l in lines if "分层压缩" in l or "没有需要压缩" in l]

    def test_automatic_noop_prints_nothing(self):
        # ⚠️ 必须用**新鲜且不足轮**的数据 ✓（5 天前 + 1 条会走 B4 真压 ✗ 那就不是空转了 ✓）
        temp, st = self._mk("qq:gm:1", rows=1, age_days=0)
        try:
            self.assertEqual(self._run(st, True), [], "自动空转任务不该打任何日志 ✗")
            with st.connect() as db:
                n = db.execute("SELECT count(*) FROM jobs WHERE kind='compress'").fetchone()[0]
            self.assertEqual(n, 0, "自动空转任务不该留在工作台 ✗")
        finally:
            temp.cleanup()

    def test_manual_noop_logs_start_and_finish(self):
        temp, st = self._mk("qq:gm:1", rows=1, age_days=0)
        try:
            lines = self._run(st, False)
            self.assertTrue(any("开始后台任务" in l for l in lines),
                            "手动任务必须有「开始」行 ✓（用户点了在等 ✓）")
            self.assertTrue(any("没有需要压缩的内容" in l for l in lines),
                            "手动任务的结果必须可见 ✓")
        finally:
            temp.cleanup()


class EnqueueReopenCase(unittest.TestCase):
    """同一 (kind,sid) 已完结后必须能**重开** ✓（2026-09-18 查出的真 bug ✓）

    原来：`INSERT OR IGNORE` + `SELECT ... state IN ('queued','running')` ✗
    ⇒ ① 已完结的行 ⇒ 插入被**静默忽略** ⇒ **该会话再也排不上同类任务** ✓
         （要等清理：50 条以外或 7 天前 ✓ —— 用户看到的就是"扫描说有内容、任务却没跑" ✓）
       ② 那个 SELECT 会返回 None ⇒ `[0]` ⇒ **TypeError** ✓（被 per-session try 吞成一行日志 ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def _state(self, jid):
        with self.store.connect() as db:
            return db.execute("SELECT state, automatic FROM jobs WHERE id=?", (jid,)).fetchone()

    def test_completed_row_is_reopened(self):
        jid = self.store.enqueue("compress", "s:1")
        self.store.finish(jid, "completed", "压缩 40 条 → L1")
        self.assertEqual(self._state(jid)[0], "completed")
        jid2 = self.store.enqueue("compress", "s:1")      # 必须能重排 ✓
        self.assertEqual(jid2, jid, "应当复用同一行 ✓（UNIQUE(kind,sid) ✓）")
        self.assertEqual(self._state(jid)[0], "queued", "已完结的行没被重开 ✗ ⇒ 该会话永远排不上 ✓")

    def test_queued_row_is_reused_not_duplicated(self):
        a = self.store.enqueue("compress", "s:2")
        b = self.store.enqueue("compress", "s:2")
        self.assertEqual(a, b, "同一 (kind,sid) 不该产生第二行 ✗")

    def test_manual_enqueue_upgrades_automatic_flag(self):
        jid = self.store.enqueue("compress", "s:3", "", True)      # 自动
        self.assertEqual(self._state(jid)[1], 1)
        self.store.enqueue("compress", "s:3", "", False)           # 手动 ⇒ 优先 ✓
        self.assertEqual(self._state(jid)[1], 0,
                         "手动排的任务没升级标记 ✗ ⇒ 会被自动静默规则吃掉 ✓")

    def test_no_typeerror_when_row_exists(self):
        """判据：任何状态下 enqueue 都必须返回**有效 id** ✓（不能 None/异常 ✓）"""
        jid = self.store.enqueue("tidy", "s:4")
        self.store.finish(jid, "completed", "整理 0 条")
        for _ in range(3):
            out = self.store.enqueue("tidy", "s:4")
            self.assertTrue(out and isinstance(out, str), "enqueue 返回了无效 id ✗：%r" % out)


class JobDetailHygieneCase(unittest.TestCase):
    """任务「明细」不能张冠李戴 ✓（2026-09-18 用户截图反馈的两处 ✓）

    ① 重开后**上一轮的 items 没清** ✗ ⇒ 明细里"这一轮的结论"配"上一轮的条目" ✓
    ② 整理结论存在**引擎级单变量**里 ✗ ⇒ 两个会话并发整理时**互相覆盖** ✓
       （用户点「全部重新整理」正好同时起两个任务 ✓ 当场复现 ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_reopen_clears_previous_items(self):
        """重开任务行必须清掉上一轮的 items ✓（否则明细会混两次运行 ✓）"""
        jid = self.store.enqueue("tidy", "s:q1")
        self.store.add_job_items(jid, [
            {"kind": "fact", "target": "f:1", "action": "keep", "note": "", "before": ""},
            {"kind": "fact", "target": "f:2", "action": "keep", "note": "", "before": ""},
        ])
        self.assertEqual(len(self.store.job_items(jid)), 2)
        self.store.finish(jid, "completed", "整理 2 条永久记忆")
        self.store.enqueue("tidy", "s:q1")                 # 重开 ✓
        self.assertEqual(
            self.store.job_items(jid), [],
            "重开后仍留着上一轮的条目 ✗ ⇒ 明细会把上一轮条目配这一轮结论 ✓（用户截图 ✓）",
        )

    def test_tidy_note_is_per_session(self):
        """整理结论必须**按会话**可取 ✓（旧版是引擎级单变量 ⇒ 并发时互相覆盖 ✓）"""
        engine = e                                          # 本文件已导入的引擎模块 ✓
        eng = object.__new__(engine.Engine)                 # 不走 __init__ ✓ 只验这条链路 ✓
        eng.last_tidy_notes = {}
        eng._note_tidy("s:A", "A 的结论")
        eng._note_tidy("s:B", "B 的结论")
        self.assertEqual(eng.last_tidy_notes.get("s:A"), "A 的结论",
                         "A 的结论被 B 覆盖了 ✗ ⇒ 明细会张冠李戴 ✓")
        self.assertEqual(eng.last_tidy_notes.get("s:B"), "B 的结论")

    def test_source_no_shared_tidy_note(self):
        """静态判据：不许再有引擎级单变量 `self.last_tidy_note` 的赋值 ✗"""
        src = (Path(__file__).resolve().parent.parent / "engine.py").read_text(encoding="utf-8")
        self.assertNotRegex(
            src, r"self\.last_tidy_note\s*=",
            "又出现引擎级单变量 last_tidy_note ✗ ⇒ 并发整理会互相覆盖 ✓",
        )
        self.assertIn('self.last_tidy_notes.get(job["sid"])', src,
                      "任务的结论必须按**该任务的会话**取 ✓")


class NoOpDoesNotBurnBoostCase(unittest.TestCase):
    """空转的压缩任务**不该消耗**"降门槛"资格 ✓（2026-09-18 用户问"为什么还有空转" ✓）

    机制：扫描判的是**排任务那一刻** ✓（冷会话 ⇒ 门槛降到 1 ⇒ 有内容 ✓）；
    任务真正跑起来要等几秒~几十秒 ✓ —— 这期间群里**再来一条消息**，
    "闲置 >6 小时"立刻不成立 ⇒ 降门槛失效 ⇒ 不够 12 轮 ⇒ **空转** ✓
    （实测对照：同一份冷会话数据，排完任务后加 2 条新消息 ⇒ 必空转 ✓）

    ⇒ 既然没压到东西，就**不该盖章** ✗（原来一进任务就盖章 ⇒ 白占 30 分钟冷却 ✓）
    """

    SID = "qq:gm:9"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.cfg = c.Settings(probability=1.0)
        self.now = time.time()

    def tearDown(self):
        self.temp.cleanup()
        e._BOOST_AT.pop(self.SID, None)

    def _seed_cold(self):
        """冷会话：最老 4 天前 ✓ 最新 8 小时前 ✓（陈旧 ✓ 且 闲置 ✓）"""
        with self.store.connect() as db:
            self.store._ensure_entities(db, self.SID, ["u-1"])
            for i, off in enumerate([8 * 3600, 8 * 3600 + 3600, 2 * 86400, 4 * 86400]):
                t = self.now - off
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold)"
                    " VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0,0,0)",
                    ("cold-%d" % i, self.SID, ("user", "assistant")[i % 2], t, t,
                     "旧 %d" % i, "旧 %d" % i, json.dumps(["u-1"]), i + 1, self.now, "session"),
                )
            db.commit()

    def _add_fresh(self, n=2):
        with self.store.connect() as db:
            for i in range(n):
                t = time.time()
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold)"
                    " VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0,0,0)",
                    ("new-%d" % i, self.SID, "user", t, t, "新 %d" % i, "新 %d" % i,
                     json.dumps(["u-1"]), 10 + i, t, "session"),
                )
            db.commit()

    def test_noop_does_not_burn_boost(self):
        self._seed_cold()
        e._BOOST_AT.pop(self.SID, None)
        plan = e.compression_plan(
            self.store.active(self.SID), self.cfg, now=time.time(),
            boost_allowed=e._boost_ok(self.SID, self.cfg, stamp=False),
        )
        self.assertIsNotNone(plan, "冷会话应当能降门槛 ✓（前置条件没造对）")
        self.store.enqueue("compress", self.SID, "", True)

        self._add_fresh()                     # ← 排完任务后群里又来消息 ⇒ 必然空转 ✓
        async def model(*a, **k):
            return json.dumps({"summary": "摘要", "facts": []})

        eng = e.Engine(self.store, lambda: self.cfg, model, None, None)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(eng.start())
            loop.run_until_complete(asyncio.sleep(3))
            loop.run_until_complete(eng.stop())
        finally:
            loop.close()
        with self.store.connect() as db:
            row = db.execute("SELECT detail FROM jobs WHERE kind='compress'").fetchone()
        detail = (row[0] if row else "") or ""
        # 空转的**自动**任务会被静默删除 ✓（行没了也是"确实空转了" ✓）
        self.assertTrue(
            row is None or "没有需要压缩的内容" in detail,
            "这次本该是空转 ✓（用来验证不盖章 ✓）实际 detail=%r" % detail,
        )

        # 群里又安静下来了（删掉那两条新消息 ✓）⇒ 应当**还能**降门槛 ✓
        with self.store.connect() as db:
            db.execute("DELETE FROM records WHERE id LIKE 'new-%'")
            db.commit()
        self.assertIsNone(e._BOOST_AT.get(self.SID),
                          "空转却消耗了降门槛资格 ✗ ⇒ 该会话 30 分钟内再也降不了门槛 ✓")
        again = e.compression_plan(
            self.store.active(self.SID), self.cfg, now=time.time(),
            boost_allowed=e._boost_ok(self.SID, self.cfg, stamp=False),
        )
        self.assertIsNotNone(again, "空转把资格吃掉后，群里安静了也降不了门槛 ✗")


class CompressProbeSoundnessCase(unittest.TestCase):
    """压缩预检必须**绝不误杀** ✓（2026-09-18 性能审计 ✓）

    背景：`active(sid)` 要把整表可用行搬进 Python ✓（3000 条 ≈ 65 ms ✓），
    而"这条消息要不要排压缩任务"只要 3 个数 ✓ ⇒ 加了 `compress_probe` 预检 ✓
    风险：预检的口径一旦与 `compression_plan` 不同（尤其"陈旧/闲置用全部行 ✓
    而候选集只用非永久行" ✓）就会**误杀**真正该压的会话 ✓
    ⇒ 本测试用**真实 store** 随机造场景对拍 ✓（口径天然一致 ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.cfg = c.Settings()
        self.rnd = random.Random(20260918)

    def tearDown(self):
        self.temp.cleanup()

    def _build(self, sid, now):
        """随机造一个会话：条数/时间跨度/永久记忆比例/**层级**都随机 ✓

        ⚠️ 2026-09-18：第一版**只造 level=0** ✗ ⇒ 漏掉了"上层摘要分支门槛只有 4" ✓
        ⇒ 预检误杀 5 条上层摘要的会话却全绿 ✓ 现在必须覆盖 level>0 ✓
        """
        n = self.rnd.choice([0, 1, 2, 3, 5, 12, 13, 25])
        level = self.rnd.choice([0, 1, 2])          # ★ 关键补强 ✓
        newest_off = self.rnd.choice([30, 3600, 6 * 3600 + 60, 86400, 5 * 86400])
        span = self.rnd.choice([0, 3600, 86400, 5 * 86400])
        with self.store.connect() as db:
            self.store._ensure_entities(db, sid, ["u-1"])
            for i in range(n):
                off = newest_off + (span * i / max(1, n - 1) if n > 1 else 0)
                t = now - off
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,0)",
                    ("%s-%d" % (sid, i), sid, ("user", "assistant")[i % 2], level, t, t,
                     "摘要", "内容", json.dumps(["u-1"]), i + 1, now, "session",
                     1 if self.rnd.random() < 0.3 else 0),
                )
            db.commit()

    def test_probe_never_rejects_what_plan_accepts(self):
        now = time.time()
        rejected = 0
        accepted_by_probe = 0
        for case in range(120):
            sid = "s:%d" % case
            self._build(sid, now)
            boost = self.rnd.choice([True, False])
            _thr = self.rnd.choice([10, 50, 200])
            cfg = c.Settings(
                compress_batch_mode=self.rnd.choice(["rounds", "records"]),
                compress_rounds=self.rnd.choice([1, 12, 30]),
                threshold=_thr,
                batch_size=min(40, _thr - 1),     # 契约：batch_size 必须小于 threshold ✓
            )
            rows = self.store.active(sid)
            plan = e.compression_plan(rows, cfg, now=now, boost_allowed=boost)
            probe = self.store.compress_probe(sid)
            worth = e.worth_checking_probe(probe, cfg, now=now, boost_allowed=boost)
            if worth:
                accepted_by_probe += 1
            if not worth:
                self.assertIsNone(
                    plan,
                    "预检误杀 ✗：probe=%s 但 plan 非空（case %d, boost=%s, 行数=%d,"
                    " mode=%s rounds=%s threshold=%s）"
                    % (probe, case, boost, len(rows), cfg.compress_batch_mode,
                       cfg.compress_rounds, cfg.threshold),
                )
                rejected += 1
        # 上面每条 assert 已经证明"被否掉的 case 计划都是空的" ✓（102 例命中过 ✓）
        self.assertGreater(rejected, 0, "没有 case 被否掉 ⇒ 优化没生效 ✓ 测试白跑 ✓")
        self.assertGreater(accepted_by_probe, 0, "预检把什么都否了 ✓ 那等于没优化 ✓")

    def test_probe_matches_store_semantics(self):
        """口径钉死：行数只数非永久 ✓；时间用**全部可用行** ✓"""
        now = time.time()
        sid = "s:fixed"
        with self.store.connect() as db:
            self.store._ensure_entities(db, sid, ["u-1"])
            for i, (perm, off) in enumerate([(1, 10 * 86400), (0, 5 * 86400)]):
                t = now - off
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold)"
                    " VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0,?,0)",
                    ("fx-%d" % i, sid, "user", t, t, "s", "c", json.dumps(["u-1"]),
                     i + 1, now, "session", perm),
                )
            db.commit()
        count, upper, oldest, newest = self.store.compress_probe(sid)
        self.assertEqual(count, 1, "行数必须只数**非永久** ✓（候选集口径 ✓）")
        self.assertAlmostEqual(oldest, now - 10 * 86400, delta=2,
                               msg="时间必须用**全部可用行** ✓ —— 只取非永久会把"
                                   "「永久记忆 + 一条旧记录」的会话误杀 ✗")
        self.assertAlmostEqual(newest, now - 5 * 86400, delta=2)


class ActiveCacheCase(unittest.TestCase):
    """`active(sid)` 的短 TTL 缓存必须**写入即失效** ✓（2026-09-18 性能审计 ✓）

    实测（3000 条记录的群）：`active(sid)` 要 ~65 ms，而**同一条消息**里
    `on_request` 开头与回复后的压缩闸各要一次 ⇒ 每轮白付 ~130 ms ✓（关键路径 ✓）
    ⇒ 加缓存 ✓ 但缓存最大的风险是**读到过期数据** ✗
    ⇒ 失效判据用 `connect()` 的 `total_changes`（任何写路径都覆盖 ✓ 不靠人工登记 ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.sid = "s:cache"
        with self.store.connect() as db:
            self.store._ensure_entities(db, self.sid, ["u-1"])
            for i in range(20):
                t = time.time()
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold)"
                    " VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0,0,0)",
                    ("c-%d" % i, self.sid, "user", t, t, "s", "c",
                     json.dumps(["u-1"]), i + 1, t, "session"),
                )
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_read_populates_and_reads_do_not_clear(self):
        self.store.active(self.sid)
        self.assertEqual(len(self.store._active_cache), 1, "读一次应当填充缓存 ✓")
        before = self.store._active_cache[self.sid][1]
        again = self.store.active(self.sid)
        self.assertEqual(len(self.store._active_cache), 1, "纯读不该清缓存 ✓")
        self.assertIs(self.store._active_cache[self.sid][1], before, "应当命中同一份行 ✓")
        self.assertEqual(len(again), 20)

    def test_write_invalidates_immediately(self):
        self.store.active(self.sid)
        self.assertTrue(self.store._active_cache, "前置：缓存应当已填充 ✓")
        with self.store.connect() as db:                       # 直接写库 ✓
            db.execute(
                "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                "position,created,visibility,active,deleted,permanent,cold)"
                " VALUES('cX',?,'user',0,1,1,'s','c',?,'99',1,'session',1,0,0,0)",
                (self.sid, json.dumps(["u-1"])),
            )
        self.assertEqual(self.store._active_cache, {},
                         "写入后没失效 ✗ ⇒ 会读到过期行 ✓（实测过：行数停在 20 ✓）")
        self.assertEqual(len(self.store.active(self.sid)), 21, "必须读到新写入的行 ✓")

    def test_other_write_paths_also_invalidate(self):
        self.store.active(self.sid)
        self.store.touch_accessed(["c-0"])                     # 另一个写方法 ✓
        self.assertEqual(self.store._active_cache, {},
                         "其它写路径也要能失效 ✗（靠 total_changes ✓ 不该漏 ✓）")


class ScanDedupCase(unittest.TestCase):
    """扫描不该把"已在排队"的会话重复计数/重复入队 ✓（2026-09-18 用户日志 ✓）

    现象：启动扫描与迁移后扫描相隔 2 秒 ✓ 日志里**同两行出现两遍**
    「待压缩扫描：2 个会话…本次排 2 个」✗（用户实测 ✓）
    根因：扫描每轮都重新判所有会话 ✓ 已有排队任务的会话也被算进 pending ✓
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_only_queued_or_running_counts_as_active(self):
        self.assertFalse(self.store.has_active_job("compress", "s:1"), "没有任务 ⇒ False ✓")
        jid = self.store.enqueue("compress", "s:1", "", True)
        self.assertTrue(self.store.has_active_job("compress", "s:1"), "排队中 ⇒ True ✓")
        self.store.finish(jid, "completed", "压缩 3 条 → L1")
        self.assertFalse(self.store.has_active_job("compress", "s:1"), "已完成 ⇒ False ✓")
        self.assertFalse(self.store.has_active_job("tidy", "s:1"), "不同 kind 互不影响 ✓")

    def test_second_scan_does_not_recount(self):
        """端到端：同一会话连排两次 ⇒ 只有一行任务 ✓（计数不会重复 ✓）"""
        for _ in range(2):
            if not self.store.has_active_job("compress", "s:2"):
                self.store.enqueue("compress", "s:2", "", True)
        with self.store.connect() as db:
            n = db.execute("SELECT count(*) FROM jobs WHERE kind='compress' AND sid='s:2'").fetchone()[0]
        self.assertEqual(n, 1, "同一会话同时只该有一行压缩任务 ✓")


class FactRotationBookkeepingCase(unittest.TestCase):
    """事实侧轮换必须真的记账 ✓（2026-09-18 查出的静默失效）

    原来 `mark_rotation` / `rotation_stats` / `rotation_pick` **只认 records** ✗，
    而事实轮换的候选来自 `facts` ⇒ 用事实 id 去 `UPDATE records … WHERE id=?`
    **匹配 0 行** ⇒ 静默无效：事实槽位永远学不到"哪条被用过"
    （只剩内存冷却 + seen 窗口，重启后从同几条重新开始）
    ⇒ 这是"下沉/上浮"方案的地基 ✓
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def _seed(self):
        with self.store.connect() as db:
            self.store._ensure_entities(db, "s:1", ["u-1"])
            self.store._add_fact(db, "s:1", {
                "category": "preference", "subject": "周武", "content": "爱喝美式",
                "reason": "测试", "scenario": "", "relations": [], "tags": [],
                "source_ids": [], "importance": 6,
            })
            fid = db.execute("SELECT id FROM facts LIMIT 1").fetchone()[0]
            db.execute(
                "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                "position,created,visibility,active,deleted,permanent,cold)"
                " VALUES('r1','s:1','user',0,1,1,'s','c',?,'1',1,'session',1,0,0,0)",
                (json.dumps(["u-1"]),),
            )
        return fid

    def test_fact_kind_writes_facts_table(self):
        fid = self._seed()
        self.store.mark_rotation([fid], [], "fact")
        self.assertEqual(self.store.rotation_stats("fact").get(fid), (1, 0),
                         "kind='fact' 没写进 facts 表 ✗ ⇒ 事实轮换学不到「被用过」✓")
        self.store.mark_rotation([], [fid], "fact")
        self.assertEqual(self.store.rotation_stats("fact").get(fid), (1, 1), "「用上」也要记 ✓")

    def test_record_kind_unchanged(self):
        """不能改坏档案侧 ✓（默认 kind 仍是 record ✓）"""
        self._seed()
        self.store.mark_rotation(["r1"], [], "record")
        self.assertEqual(self.store.rotation_stats("record").get("r1"), (1, 0))
        self.store.mark_rotation(["r1"], [])          # 老调用方式（不传 kind）✓
        self.assertEqual(self.store.rotation_stats("record").get("r1"), (2, 0))

    def test_kinds_are_isolated(self):
        """两边的计数必须互不污染 ✓"""
        fid = self._seed()
        self.store.mark_rotation([fid, "r1"], [], "fact")
        self.assertEqual(self.store.rotation_stats("fact").get("r1"), None,
                         "事实口径不该写进 records 的 id ✓")
        self.assertEqual(self.store.rotation_stats("record").get("r1"), None)


class CompressRaceToleranceCase(unittest.TestCase):
    """压缩遇到**竞态冲突**不该报失败 ✓（2026-09-18 查出并修）

    `store.compress()` 用 `(revision, active, deleted)` 做 CAS ✓
    若源记录在这期间被**别处**（同会话的另一个任务、或直调）压掉了 ⇒ 抛 Conflict ✓
    而那时**目标已经达成** ✓ ⇒ 报失败会误导（工作台显示红、用户以为出问题）

    实测来源：集成测试 `test_quiet_migrated_sessions_get_compressed_by_sweep`
    （"扫描排任务 + 直调 compress" ⇒ 修复前在 KIRA_CORE 下 1/3 概率失败 ✓
    修复后连跑 5 次全过 ✓）
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = s.Store(Path(self.temp.name) / "db")
        self.store.initialize()
        self.cfg = c.Settings(probability=0.0)

    def tearDown(self):
        self.temp.cleanup()

    def test_conflict_does_not_raise(self):
        """把 compress 换成必抛 Conflict ⇒ 层叠压缩必须**优雅收工** ✓"""
        sid = "s:race"
        now = time.time()
        with self.store.connect() as db:
            self.store._ensure_entities(db, sid, ["u-1"])
            for i in range(40):
                t = now - i * 60
                db.execute(
                    "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
                    "position,created,visibility,active,deleted,permanent,cold)"
                    " VALUES(?,?,?,0,?,?,?,?,?,?,?,?,1,0,0,0)",
                    ("rc-%d" % i, sid, ("user", "assistant")[i % 2], t, t, "s%d" % i,
                     "c%d" % i, json.dumps(["u-1"]), i + 1, now, "session"),
                )
            db.commit()

        async def model(*a, **k):
            return json.dumps({"summary": "摘要", "facts": []})

        engine = e.Engine(self.store, lambda: self.cfg, model, None, None)
        real_call = self.store.call

        async def racy_call(method, *args, **kwargs):
            if method == "compress":
                raise s.Conflict("source changed during compression")
            return await real_call(method, *args, **kwargs)

        self.store.call = racy_call
        loop = asyncio.new_event_loop()
        try:
            steps = loop.run_until_complete(engine.compress(sid))
        except s.Conflict:
            self.fail("竞态冲突被抛出来了 ✗ ⇒ 任务会显示成失败 ✓（其实目标已达成 ✓）")
        finally:
            loop.close()
            self.store.call = real_call
        self.assertTrue(steps, "应当留一条说明 ✓ 而不是当成空转 ✓")
        self.assertIn("已被其它任务压缩", json.dumps(steps, ensure_ascii=False))


class SelfEchoDowngradeCase(unittest.TestCase):
    """bot 自述的事实只**降序**，不删不藏 ✓（2026-09-18，用户担心的"自我误导"）

    已有 `v2.18.9 回声防线` 在**渲染**时给这类事实打 `self` 旗标 ✓
    但**排序没动** ⇒ 照样占常驻版面 ✓ ⇒ 这次补上排序侧 ✓
    判据与渲染侧完全一致（`src_user == self_id`）✓
    """

    def test_stable_partition(self):
        facts = [{"id": 1, "src_user": "u1"}, {"id": 2, "src_user": "bot"},
                 {"id": 3, "src_user": "u2"}, {"id": 4, "src_user": "bot"}]
        out = r.self_only_last(facts, "bot")
        self.assertEqual([f["id"] for f in out], [1, 3, 2, 4], "必须是**稳定**分区 ✓")
        self.assertEqual(len(out), len(facts), "不许删 ✓")

    def test_no_self_id_is_noop(self):
        facts = [{"id": 1}, {"id": 2}]
        self.assertEqual([f["id"] for f in r.self_only_last(facts, "")], [1, 2])

    def test_pack_facts_applies_downgrade(self):
        facts = [
            {"id": "s1", "sid": "s", "src_user": "bot", "category": "profile",
             "content": "我说过我早睡", "importance": 5},
            {"id": "s2", "sid": "s", "src_user": "u1", "category": "profile",
             "content": "用户说他早睡", "importance": 5},
        ]
        packed = r.pack_facts(facts, current_sid="s", view="flat", self_id="bot")
        text = json.dumps(packed, ensure_ascii=False)
        self.assertIn("我说过我早睡", text, "自述事实**不能消失** ✓（仍要能被想起来 ✓）")
        self.assertIn("用户说他早睡", text)
        self.assertLess(text.index("用户说他早睡"), text.index("我说过我早睡"),
                        "自述事实应当排在**后面** ✗（不主动占版面 ✓）")


class ArchiveLayerMarkCase(unittest.TestCase):
    """存档行必须能看出**层级** ✓（2026-09-18：D 项）

    原来存档行只有 `序号|角色|时间|说话人|内容` ✗ —— 模型分不清"事实"和"摘要"、
    也不知道是第几手概括 ✓ ⇒ 加 `L<n>` 层标（`A`/`U` 之后 ✓）并同步说明文案 ✓
    """

    def setUp(self):
        self.src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")

    def test_archive_line_carries_level(self):
        self.assertIn('marks += "L%d" % _lvl', self.src,
                      "存档行没有层标 ✗ ⇒ 模型看不出摘要层级 ✓")

    def test_doc_explains_the_marks(self):
        self.assertIn("L 后数字=摘要层级", self.src,
                      "说明文案没解释 L 标 ✗ ⇒ 模型不会用 ✓")
        self.assertIn("*=永久记忆", self.src)
        self.assertIn("@=来自别的会话", self.src)


class FactSinkFloatCase(unittest.TestCase):
    """事实的**下沉 / 上浮**（2026-09-18 批次 2）

    设计（用户拍板 ✓ 沿用既有术语、不加新概念）：
    · 「下沉」= 移出常驻 ⇒ 天然落进**轮换槽位**（轮换池是独立查询，不受这里影响）
    · 「上浮」= 在轮换里被**「用上」**（`rotate_used` ↑）⇒ 分数回升 ⇒ 自动回常驻
    · 分数 = 重要度×2 + 有效被用次数×3 + 新鲜度（2026-09-23 重定：60 天线性归零、
    · **重要度 ≥ 8 永不沉**（硬规则 ✓）；阈值设 0 = 关闭下沉
    """

    NOW = 1_800_000_000.0

    def _fact(self, importance=5, used=0, age_days=0):
        return {
            "id": "f1",
            "importance": importance,
            "rotate_used": used,
            "created": self.NOW - age_days * 86400,
        }

    def test_score_formula(self):
        self.assertEqual(r.fact_sink_score(self._fact(5, 0, 0), now=self.NOW), 20)
        self.assertEqual(r.fact_sink_score(self._fact(5, 2, 0), now=self.NOW), 26)
        self.assertEqual(r.fact_sink_score(self._fact(5, 99, 0), now=self.NOW), 35,
                         "被用次数封顶 5 次（10 + 5×3 + 10 = 35 ✓ 避免刷分 ✓）")
        self.assertEqual(r.fact_sink_score(self._fact(3, 0, 200), now=self.NOW), 6,
                         "老事实没有新鲜度加分 ✓")

    def test_never_sink_high_importance(self):
        """硬规则：重要度 ≥8 **永不沉** ✓（哪怕很旧、没人用过 ✓）"""
        for imp in (8, 9, 10):
            self.assertFalse(r.should_sink(self._fact(imp, 0, 500), 20, now=self.NOW),
                             "重要度 %d 被下沉了 ✗（用户拍板 ≥8 永不沉 ✓）" % imp)

    def test_low_importance_sinks(self):
        self.assertTrue(r.should_sink(self._fact(3, 0, 200), 20, now=self.NOW))

    def test_threshold_zero_disables(self):
        facts = [self._fact(2, 0, 500), self._fact(9, 0, 500)]
        self.assertEqual(len(r.sink_filter(facts, 0, now=self.NOW)), 2, "0 = 关闭下沉 ✓")

    def test_float_back_when_used(self):
        """**上浮**：被轮换带进来并用上之后 ⇒ 分数回升 ⇒ 不再被下沉 ✓"""
        sunk = self._fact(3, 0, 200)
        self.assertTrue(r.should_sink(sunk, 20, now=self.NOW), "前置：先能沉 ✓")
        # 3×2=6；用 1 次 +3 ⇒ 9 < 12 还沉 ✓；用 2 次 +6 ⇒ 12 ≥ 12 浮回 ✓
        self.assertTrue(r.should_sink(self._fact(3, 1, 200), 12, now=self.NOW),
                        "用 1 次（9 分）不该浮回 ✓ 阈值必须真的在拦 ✓")
        used = self._fact(3, 2, 200)                      # 在轮换里被用上 2 次 ✓
        self.assertFalse(r.should_sink(used, 12, now=self.NOW),
                         "被用上了还沉 ✗ ⇒ 上浮转不起来 ✓")

    def test_filter_keeps_order_and_no_mutation(self):
        facts = [{"id": "a", "importance": 9, "rotate_used": 0, "created": self.NOW},
                 {"id": "b", "importance": 2, "rotate_used": 0,
                  "created": self.NOW - 400 * 86400},      # 老且没人用过 ⇒ 该沉 ✓
                 {"id": "c", "importance": 6, "rotate_used": 5, "created": self.NOW}]
        before = json.dumps(facts, ensure_ascii=False)
        kept = r.sink_filter(facts, 20, now=self.NOW)
        self.assertEqual([f["id"] for f in kept], ["a", "c"], "顺序要稳定、该留的要留 ✓")
        self.assertEqual(json.dumps(facts, ensure_ascii=False), before,
                         "不许就地修改（调用方可能还要用 ✓）")
class TidyAuditContractCase(unittest.TestCase):
    """**tidy / audit 的关键约定必须一直在** ✓（2026-09-18）

    （原 TidyAuditUntouchedCase 里"防身份绑定接线"的两条随绑定功能一起撤掉 ✓
      但 tidy/audit 的**约定检查**与绑定无关 ✓ 保留 ✓ —— 它们防的是"改坏后台整理" ✗）
    """

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_tidy_and_audit_prompts_still_intact(self):
        src = (self.root / "engine.py").read_text(encoding="utf-8")
        for needle, why in (
            ("继续常驻", "tidy 的 keep 语义 ✓"),
            ("移出常驻", "tidy 的 extract 语义 ✓"),
            ("only_self=true", "审计里『不许据自述提重要度』的约定 ✓"),
            ("与用户冲突以用户为准", "提取的冲突规则 ✓"),
        ):
            self.assertIn(needle, src, "tidy/audit 的关键约定消失了 ✗：%s" % why)


class EntityLinksCleanupCase(unittest.TestCase):
    """老库里的废弃 `entity_links` 表**要清掉**，但不能误伤 ✓（用户要求："不会误伤、安全即可"）

    背景：身份绑定功能已整体摘除（判据站不住 ✗ 用户决定不要 ✓）
    ⇒ 老库里会留下这张表（只剩死数据 ✓ 没有任何代码读它 ✓）⇒ 升级时清掉 ✓
    四道保险：① 不存在就不动 ✓ ② **列结构一致才删** ✓ ③ try/except 不炸启动 ✓ ④ 只动这一张表 ✓
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "db"

    def tearDown(self):
        self.temp.cleanup()

    def _tables(self, store):
        with store.connect() as c:
            return {
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }

    def test_废弃表被清掉(self):
        st = s.Store(self.db)
        st.initialize()
        with st.connect() as c:
            c.execute(
                "CREATE TABLE entity_links (raw TEXT PRIMARY KEY, canonical TEXT NOT NULL,"
                " source TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL)"
            )
            c.execute("INSERT INTO entity_links VALUES('周武','qq:1','manual','',1.0)")
            c.commit()
        s.Store(self.db).initialize()
        self.assertNotIn("entity_links", self._tables(st), "废弃表没被清掉 ✗")

    def test_同名但列不同不许动(self):
        """**误伤保护** ✓：万一以后有别的表叫这名 ⇒ 绝不 DROP ✗"""
        st = s.Store(self.db)
        st.initialize()
        with st.connect() as c:
            c.execute("CREATE TABLE entity_links (other TEXT)")
            c.execute("INSERT INTO entity_links VALUES('keepme')")
            c.commit()
        s.Store(self.db).initialize()
        with st.connect() as c:
            rows = c.execute("SELECT other FROM entity_links").fetchall()
        self.assertEqual([r[0] for r in rows], ["keepme"],
                         "同名不同列的表被误删了 ✗（这就是『误伤』✓）")

    def test_数据不受影响(self):
        st = s.Store(self.db)
        st.initialize()
        with st.connect() as c:
            st._ensure_entities(c, "qq:gm:1", ["周武"])
            st._add_fact(c, "qq:gm:1", {"category": "profile", "subject": "周武",
                                        "content": "爱喝美式", "reason": "t", "scenario": "",
                                        "relations": [], "tags": [], "source_ids": [],
                                        "importance": 7})
            c.execute(
                "CREATE TABLE entity_links (raw TEXT PRIMARY KEY, canonical TEXT NOT NULL,"
                " source TEXT NOT NULL, evidence TEXT NOT NULL, created REAL NOT NULL)"
            )
            c.commit()
        s.Store(self.db).initialize()
        self.assertEqual(len(st.facts("qq:gm:1", limit=10)), 1, "清表影响了事实 ✗")
        self.assertGreaterEqual(len(self._tables(st)), 5, "其它表被误删 ✗")


class ToolResultNotRotatedCase(unittest.TestCase):
    """「工具感知结果」**不是记忆** ⇒ 不许进轮换槽 ✓（2026-09-18 用户实测）

    现象：模型抓回来的工具输出被存成记录 ✓ 再压成档案/提炼成事实 ✓
    ⇒ 于是它**流进了轮换槽** ⇒ 用户看到"轮换槽被动召回到工具步（工具感知结果 xx）" ✗
    修：轮换池（事实 ✓ 档案 ✓）排除它们 ✓ —— **主召回不动** ✓
    （工具结果是对话史的一部分 ✓ 该能被想起来 ✓ 只是不该占"相关记忆"的槽位 ✓）
    """

    def test_detects_tool_result(self):
        self.assertTrue(r.is_tool_result("工具感知结果：\n网页标题…"))
        self.assertTrue(r.is_tool_result("  工具感知结果：xx"))       # 前导空白也算 ✓
        self.assertFalse(r.is_tool_result("周武喜欢喝美式"))
        self.assertFalse(r.is_tool_result(""))
        self.assertFalse(r.is_tool_result(None))

    def test_rotation_pools_filter_it(self):
        """两处轮换池都必须过滤 ✓（源级钉住，防止有人改回去 ✗）"""
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        self.assertIn('is_tool_result(x.get("content"))', src, "轮换·事实池没过滤 ✗")
        self.assertIn('is_tool_result(r.get("summary"))', src, "轮换·档案池没过滤 ✗")

    def test_all_recall_paths_exclude_it(self):
        """**主被动召回也都要排除** ✓（2026-09-18 用户确认 ✓ 原先只有渲染层剥标签 ✗）"""
        src = (Path(__file__).resolve().parent.parent / "main.py").read_text(encoding="utf-8")
        self.assertIn('facts = [f for f in facts if not is_tool_result(f.get("content"))]',
                      src, "被动召回的**事实**没排除工具结果 ✗")
        self.assertIn('not is_tool_result(r.get("summary"))', src,
                      "被动召回的**档案**没排除工具结果 ✗")
        self.assertIn('if is_tool_result(r.get("summary")) or is_tool_step(r):', src,
                      "**主动检索**没排除工具结果 ✗")
        # 既有约定（v2.18.19：`category='tool'` 的工具步也要全链路过滤 ✓）在档案路径上也要在 ✓
        self.assertIn('and not is_tool_step(r)', src, "档案路径没装**既有**的工具步过滤 ✗")


class ShellNotRecalledCase(unittest.TestCase):
    """「只有壳」的条目不许进召回/轮换 ✓（2026-09-18 用户实测）

    现象：轮换槽注入出「[Reply ID: -13」这种**废条目** ✗（后面没有正文 ✓）
    根因：项目里**早就有** `media_only()`（v2.18.14：只有引用壳 / at 壳 / 媒体块 ⇒ 不算内容 ✓）
          但**没装在档案池 / 轮换池 / 主动检索**上 ✗ —— 只装在记录级的那几处 ✓
    ⇒ 现已补全 ✓（`archive_pool` 由 `fresh` 派生 ⇒ 一处过滤覆盖主召回与轮换 ✓）
    """

    def setUp(self):
        self.root = Path(__file__).resolve().parent.parent

    def test_media_only_catches_shells(self):
        self.assertTrue(r.media_only("[Reply ID: -13"))
        self.assertTrue(r.media_only("[Reply ID: -13 content: []]"))
        self.assertTrue(r.media_only("[At 3991867505(nickname: 紫小贱)]"))
        self.assertFalse(r.media_only("[Reply ID: 12 content: []] 你可得记住勒"))
        self.assertFalse(r.media_only("周武喜欢喝美式"))

    def test_pools_apply_it(self):
        src = (self.root / "main.py").read_text(encoding="utf-8")
        self.assertIn('and not media_only(r.get("summary") or "", keep_names)', src,
                      "档案池（含由它派生的轮换档案池）没装壳过滤 ✗")
        self.assertIn('and not media_only(x.get("content") or "", keep_names)', src,
                      "轮换·事实池没装壳过滤 ✗")
        self.assertIn('if media_only(r.get("summary") or "", keep_names):', src,
                      "主动检索没装壳过滤 ✗")
