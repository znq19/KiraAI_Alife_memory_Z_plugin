"""时间来源（事件时间 vs 记录时刻）与发言人：能不能定位时间、分清谁说的。"""

import importlib
import json
import os
import sys
import tempfile
import time
import types
import unittest

import pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("alife_time_test")
package.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_time_test", package)
storage = importlib.import_module("alife_time_test.storage")
retrieval = importlib.import_module("alife_time_test.retrieval")

DAY = 86400.0


def _store(tmp_path):
    store = storage.Store(tmp_path / "db.sqlite3")
    store.initialize()
    return store


def _record(store, rid, start, users=None, content="内容", speaker=""):
    with store.connect() as db:
        db.execute(
            "INSERT INTO records(id,sid,role,level,start,end,summary,content,users,"
            "speaker,position,created,search_body) "
            "VALUES (?,?,'user',0,?,?,?,?,?,?,0,?,'')",
            (
                rid,
                "qq:gm:1",
                start,
                start,
                content,
                content,
                json.dumps(users or ["qq:1"]),
                speaker,
                start,
            ),
        )


def _fact(store, content, source_ids):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return store._add_fact(
            db,
            "qq:gm:1",
            {
                "category": "experience",
                "subject": "qq:1",
                "content": content,
                "reason": "",
                "scenario": "",
                "tags": [],
                "relations": [],
                "source_ids": source_ids,
                "importance": 5,
            },
        )


class TimeProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = _store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def row(self, table, rid):
        with self.store.connect() as db:
            return dict(
                db.execute("SELECT * FROM %s WHERE id=?" % table, (rid,)).fetchone()
            )

    def test_new_fact_takes_event_span_from_sources(self):
        """事实的事件时间 = 来源记录的时间跨度（不是入库时刻 ✗）。"""
        _record(self.store, "r1", 1700000000.0, content="周一说想吃蛋糕")
        _record(self.store, "r2", 1700000000.0 + 2 * DAY, content="周三又提了一次")
        fid = _fact(self.store, "想吃蛋糕", ["r1", "r2"])
        row = self.row("facts", fid)
        self.assertEqual(row["event_at"], 1700000000.0)
        self.assertEqual(row["event_end"], 1700000000.0 + 2 * DAY)
        self.assertGreater(row["created"], row["event_end"])

    def test_merge_keeps_the_whole_span(self):
        """合并多个时间点的事实不能"塌成一点" ✗。"""
        early = _fact(self.store, "早上说想吃蛋糕", [])
        late = _fact(self.store, "晚上说不吃巧克力", [])
        with self.store.connect() as db:
            db.execute(
                "UPDATE facts SET event_at=?, event_end=? WHERE id=?",
                (1700000000.0, 1700000000.0, early),
            )
            db.execute(
                "UPDATE facts SET event_at=?, event_end=? WHERE id=?",
                (1700000000.0 + 5 * DAY, 1700000000.0 + 5 * DAY, late),
            )
        self.store.merge_facts(early, [early, late], "想吃蛋糕、不吃巧克力", "合并重复")
        row = self.row("facts", early)
        self.assertEqual(row["event_at"], 1700000000.0)
        self.assertEqual(row["event_end"], 1700000000.0 + 5 * DAY)

    def test_backfill_fills_old_rows(self):
        """存量迁移：老库没有 event_at / speaker，要靠回填补上。"""
        _record(self.store, "old1", 1700000000.0, users=["qq:1"], content="正文")
        _record(
            self.store,
            "old2",
            1700000000.0 + DAY,
            users=["qq:1", "qq:2"],
            content="[星月] 正文里带了名字",
        )
        fid = _fact(self.store, "老事实", ["old1"])
        with self.store.connect() as db:
            db.execute("UPDATE records SET speaker=''")
            db.execute("UPDATE facts SET event_at=0, event_end=0 WHERE id=?", (fid,))
            db.execute(
                "INSERT INTO entities(id,kind,name,updated) VALUES('qq:1','user','星月',0)"
            )
        stats = self.store.backfill_time_provenance()
        self.assertGreaterEqual(stats["records"] + stats["unresolved"], 2)
        self.assertEqual(self.row("facts", fid)["event_at"], 1700000000.0)
        self.assertEqual(self.row("records", "old1")["speaker"], "星月")
        self.assertEqual(self.row("records", "old2")["speaker"], "星月")

    def test_capture_stores_per_message_speaker(self):
        """群聊里同一批次的多条消息，各记各的发言人（不是整批的参与者 ✗）。"""
        self.store.capture(
            "qq:gm:1",
            "ev1",
            [
                {
                    "role": "user",
                    "content": "甲说的话",
                    "time": 1.0,
                    "users": ["qq:1", "qq:2"],
                    "speaker": "甲",
                },
                {
                    "role": "user",
                    "content": "乙说的话",
                    "time": 2.0,
                    "users": ["qq:1", "qq:2"],
                    "speaker": "乙",
                },
            ],
        )
        with self.store.connect() as db:
            rows = {
                r["content"]: r["speaker"]
                for r in db.execute("SELECT content,speaker FROM records")
            }
        self.assertEqual(rows["甲说的话"], "甲")
        self.assertEqual(rows["乙说的话"], "乙")

    def test_archive_view_exposes_speaker(self):
        _record(self.store, "r1", 1.0, speaker="星月")
        with self.store.connect() as db:
            row = dict(db.execute("SELECT * FROM records WHERE id='r1'").fetchone())
        # v2.18.19：读原文改用短键 ✓ speaker → sp ✓
        self.assertEqual(retrieval.archive_view(row)["sp"], "星月")


def test_prompt_tells_model_to_absolutize_relative_dates():
    """「昨天」必须在抽取时换算成绝对日期，否则写进事实就永久失真 ✗。"""
    engine = (ROOT / "engine.py").read_text(encoding="utf-8")
    assert "换算成绝对日期" in engine
    assert "records[].t" in engine


def test_rules_document_time_and_speaker_fields():
    """规则块要说明时间与发言人短键，模型才会用。"""
    main = (ROOT / "main.py").read_text(encoding="utf-8")
    # v2.18.19：改用**下一个顶层定义**做边界 ✗ 不再靠脆弱的首个括号行 ✓
    # （旧写法会在 _GROUPED_FACT_DOC 里出现括号时提前截断 ✓）
    # v2.18.19：格式说明在 `_GROUPED_FACT_DOC` 里 ✗（它拼进 MEMORY_RULES ✓）
    # 所以切片要从它的定义处开始 ✓ 否则切不到格式段 ✓
    start = main.find("_GROUPED_FACT_DOC = (")
    _end = main.find("\ndef memory_rules", start)
    block = main[start : _end if _end > start else len(main)]
    # v2.17.0：事实改为按主体分组、组内位置化 —— 时间/发言人不再是键名，而是位置槽
    for field in ("时间", "说话人", "SearchMemoryArchive", "主体码"):
        assert field in block, field


if __name__ == "__main__":
    unittest.main()


def test_short_time_keeps_the_year_when_it_is_not_this_year():
    """存档给模型看的时间：同年省年份，跨年必须带——否则模型会当成今年 ✗。

    （模型知道"现在"是几号，所以同年省略是安全的；跨年省略就是错的。）
    """
    now = time.time()
    same_year = retrieval.short_time(now)
    assert len(same_year.split(" ")[0].split("-")) == 2, same_year  # MM-DD
    older = retrieval.short_time(now - 400 * 86400)  # 任何日期往前 400 天必然跨年（300 天在 1–10 月不够）
    assert len(older.split(" ")[0].split("-")) == 3, older  # YYYY-MM-DD
    assert retrieval.short_day(now - 400 * 86400).count("-") == 2


def test_compression_input_always_has_the_year():
    """压缩输入的时间必须带年份：否则"昨天"换算成绝对日期时会算错 ✗。"""
    assert "YYYY-MM-DD HH:MM" in (ROOT / "retrieval.py").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_startup_background_task_actually_backfills(tmp_path):
    """线上"没有那行日志"的真因：启动任务有没有真的跑到回填 ✗

    这条测的是**接线**（不只是方法本身）：plugin.build_search_index() 是启动时创建的
    那个后台任务，它必须先跑存量回填、再管索引。
    """
    if not os.environ.get("KIRA_CORE"):
        pytest.skip("set KIRA_CORE for host integration")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_helpers_plugin import build_plugin

    plugin, store = await build_plugin(tmp_path)
    try:
        store.capture(
            "qq:gm:1",
            "ev0",
            [
                {
                    "role": "user",
                    "content": "四十天前说想吃蛋糕",
                    "time": 1700000000.0,
                    "users": ["qq:1"],
                    "speaker": "",
                }
            ],
        )
        with store.connect() as db:  # 造一条事实 + 抹掉事件时间（模拟存量）
            db.execute("BEGIN IMMEDIATE")
            fid = store._add_fact(
                db,
                "qq:gm:1",
                {
                    "category": "experience",
                    "subject": "qq:1",
                    "content": "想吃蛋糕",
                    "reason": "",
                    "scenario": "",
                    "tags": [],
                    "relations": [],
                    "source_ids": [],
                    "importance": 5,
                },
            )
            db.execute("UPDATE facts SET event_at=0, event_end=0 WHERE id=?", (fid,))
            db.execute("UPDATE records SET speaker=''")
            db.execute(
                "UPDATE facts SET sources=(SELECT json_group_array(id) FROM records) WHERE id=?",
                (fid,),
            )
        await plugin.build_search_index()  # 启动时那个后台任务
        with store.connect() as db:
            row = dict(db.execute("SELECT * FROM facts WHERE id=?", (fid,)).fetchone())
        assert row["event_at"] == 1700000000.0, "后台任务没把存量事件时间补上 ✗"
        assert row["event_end"] == 1700000000.0
    finally:
        await plugin.terminate()
