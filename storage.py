"""Archive tree ported from Alife MemoryManager/MemoryStorage (AGPL-3.0).

All raw records survive compression; transactions protect asynchronous edits.
"""

from __future__ import annotations
import asyncio
import hashlib
import json
import re
import logging
import math
import random
import sqlite3
import time
import uuid
from contextlib import contextmanager, nullcontext as _nullcontext, closing
from pathlib import Path
from .contracts import dump, relation_issue
from .output_validation import validate_audit
from .retrieval import clean_text, index_grams, identity_info, strip_reasoning

logger = logging.getLogger(__name__)

# 索引体占位符：摘要里没有任何可检索文字（空串、只有标点/空格）时用它。
# 必须是**非空**的，因为 search_body='' 的语义是「还没进索引」（召回侧靠这个条件
# 兜底，保证绕过本模块写入的行也不会被漏掉）；这类摘要若也写空串，回填每批都会
# 再把它们捞出来 → 后台回填永不结束（界面会一直停在「索引 回填中」）。
# "-" 在 FTS5 默认分词器下不产生任何词元，因此永远不会被查询命中——
# 而这些行的词面得分本来恒为 0，本来就不会被召回，所以没有召回损失。
NO_TOKENS = "-"

# 索引体口径版本：口径一变，旧索引体必须重算，否则会静默漏召回。
#   1 = v2.11.0，只有双字滑窗（单字查询查不到 → 漏）
#   2 = v2.11.1，补上单字 + 查询侧 ≥3 字词元拆对（候选集 = 打分结果的超集）
SEARCH_INDEX_SCHEME = "2"

# 存量净化口径版本：一升级就再跑一次（老库里的空壳记录要移进回收站）
CAPTURE_SCRUB_VERSION = "2"


class Conflict(ValueError):
    pass

def _classify_source_still_valid(row, current):
    """归类守卫：源记录被并发"整理"过时，这次判定还算不算数？

    · 正文一字未改（只是别的任务补了 search_body / bump 了 revision）⇒ 采纳 ✓
    · 记录已被压缩/提炼归档（active=0 或已生成 summary）⇒ 采纳 ✓
      （归类要的正是**原始内容里的长期事实**，被整理不影响它的正确性）
    · 正文被实质改写成别的内容 ⇒ 拒绝 ✗（避免把 A 的判定贴到 B 上）
    """
    if (current["content"] or "") == (row.get("content") or ""):
        return True
    if not current["active"] or str(current["summary"] or "").strip():
        return True
    return False



def uid():
    return uuid.uuid4().hex


FALLBACK_REASON_MARK = "按时间拼接"


def _pick_group(rows, target):
    """从版本行里挑出「确实构成了这次拼接」的那一组。

    判据是**内容本身**，不是 revision 计数器：降级拼接就是
    ``"；".join(按时间倒序去重后的内容)[:上限]``，所以按同样顺序走一遍、
    要求每一步都对得上目标当前内容的当前位置；对不上的（别的簇、或被上限截掉的）
    一律跳过。这样即使审计动过 importance（也会 +1 revision）也不会误判成"被改过"。

    返回 {fact_id: 合并前快照}；理论上对不上就返回空 → 调用方放弃重做。
    """
    wanted = str(target["content"] or "")
    remaining = []
    for row in rows:
        try:
            snapshot = json.loads(row["snapshot"])
        except (TypeError, ValueError):
            continue
        if snapshot.get("id") and str(snapshot.get("content") or ""):
            remaining.append(snapshot)
    # 按内容游标贪心匹配：每一步都要求「这条的内容」正好出现在当前位置。
    # 顺序无关（真实合并按时间倒序，但这里不依赖它），对不上的（别的簇、
    # 或被 fact_merge_max_chars 截掉的尾巴）自然被跳过。
    picked, cursor = [], 0
    while True:
        for snapshot in list(remaining):
            body = str(snapshot["content"])
            if wanted.startswith(body, cursor):
                picked.append(snapshot)
                remaining.remove(snapshot)
                cursor += len(body) + 1  # +1 = 拼接用的分隔符「；」
                break
        else:
            break
    if not picked:
        return {}
    group = {snapshot["id"]: snapshot for snapshot in picked}
    if target["id"] not in group or len(group) < 2:
        return {}
    if not wanted.startswith("；".join(snapshot["content"] for snapshot in picked)):
        return {}
    return group


def scrub_row_text(level, role, text):
    """存量清洗的单行规则（与落库同口径）。

    L0 走落库那套（剥协议外壳 + 思考块）；L1+ 是模型写的散文摘要、本就不该有
    外壳，所以**只剥思考块**——避免把摘要里正常出现的尖括号内容误伤掉。
    """
    text = str(text or "")
    if role == "assistant":
        text = strip_reasoning(text)
    if int(level or 0) == 0:
        text = clean_text(text)
    return text.strip()


def search_body_of(summary):
    """与查询侧同口径的索引体；没有可检索文字时退化为占位符。

    所有写 summary 的地方都必须用它算 search_body：
    漏掉一处，那条记录就会在「编辑/新增后」悄悄离开 FTS 候选集 → 静默漏召回。
    """
    return index_grams(summary) or NO_TOKENS


LEXICAL_MAX_TOKENS = 256   # 词元上限（防病态长查询把召回拖慢；见下）


def _lexical_sql(column, tokens):
    """把「词元命中 × 词长」的求和写成 SQL 表达式（纯 C 层，无 Python 回调）。

    与 relevance() 口径完全一致：score = Σ len(token)（命中即计入）。
    tokens 来自 query_tokens（只含字母数字与汉字），这里转义单引号后安全内联。
    """
    # ⚠️ 词元上限：词元 = 查询里**每一个汉字切片** ⇒ 长文本可达上千 ✗
    #    一是会把 SQL 表达式撑爆（见下），二是每行都要跑一遍全部 instr ✓ 很贵 ✓
    #    取前 N 个（query_tokens 已排序 ⇒ 结果确定 ✓）；>N 的长查询极少见 ✓
    tokens = list(tokens)[:LEXICAL_MAX_TOKENS]
    parts = []
    for token in tokens:
        literal = "'" + str(token).replace("'", "''") + "'"
        parts.append("(instr(%s,%s)>0)*%d" % (column, literal, len(str(token))))
    # ⚠️ 无词元时**绝不能**返回裸 "0" ✗ —— 这个表达式会被拼进 ``ORDER BY``，
    # 而 SQLite 把 ORDER BY 里的**裸整数**当成**列位置** ✓
    #   ORDER BY 0   → "1st ORDER BY term out of range - should be between 1 and 21"
    #   ORDER BY (0) → 同样报错 ✗（实测加括号也不行 ✓）
    #   ORDER BY 0+0 → ✓ 只有写成"表达式"才安全
    # 触发场景很常见：一条纯表情/纯符号消息（如「🤔」）经词面召回传进来 ✓
    # （2026-09-17 用户在群里实测崩过 ✓ 本行即修复 ✓）
    if not parts:
        return "0+0"
    # ★ 平衡合并（两两相加）⇒ 深度 O(log n) ✓，且**求值结果与顺序求和逐值一致** ✓
    #   旧写法 " + ".join(parts) 是左深链 ⇒ 词元上千时 SQLite 直接拒绝解析 ✗
    while len(parts) > 1:
        parts = [
            "(%s + %s)" % (parts[i], parts[i + 1]) if i + 1 < len(parts)
            else parts[i]
            for i in range(0, len(parts), 2)
        ]
    return parts[0]


def proximity_bonus(text, tokens, window=30, bonus=4):
    """同现/邻近加成 ✓ —— **只在 Python 侧重排时用** ✗ 不进 SQL ✓

    为什么：SQL 打分有「与 Python 逐字一致」的判据守着 ✓
      往里加"字符距离"会破坏它 ✗ ⇒ 所以邻近只在**重排**阶段算 ✓

    用户实测（日志）：搜 `doro` 命中 **784** 条 ⇒ 每条都是 4 分**完全并列** ✗
      ⇒ 返回顺序≈随机 ⇒ 有条理的记忆被随机埋掉 ✓
    加邻近后：`doro` 与 `艾莉` **同段(±30字)出现**的那条 ⇒ 8 分 ✓ 直接顶上来 ✓
    """
    from .retrieval import squeeze as _sq   # storage 里 squeeze 是函数内导入的 ✓ 同规矩 ✓

    low = _sq(text or "").casefold()
    hit = [t for t in tokens if t in low]
    if len(hit) < 2:
        return 0
    pos = sorted(low.find(t) for t in hit)
    return bonus if (pos[-1] - pos[0]) <= window else 0


def _lexical_scorer(query):
    """返回一个「词元只算一次」的打分函数，供 SQLite 逐行调用。

    relevance() 每次调用都会重新切查询词元——在全表打分时等于每行重切一遍，
    是 1 秒级的开销来源。这里把词元提到闭包外，逐行只做一次子串计数。
    """
    from .retrieval import score_tokens, squeeze

    # ★ 2026-09-19：打分用**词级**词元 ✓（有 jieba 按词，无则回退按字 ✓）
    #   ⚠️ 只换打分 ✗ 不动 SQL 预筛（storage:2909 的 query_tokens）✓
    #   索引侧超集不变式不受影响 ⇒ **不需要重建索引** ✓
    tokens = score_tokens(query)

    def score(text):
        lowered = squeeze(text).casefold()
        return sum(len(token) * (token in lowered) for token in tokens)

    return score


class Store:
    # active(sid) 的短 TTL 缓存 ✓（写入后立即失效 ✓ 见 connect() ✓）
    _ACTIVE_TTL = 1.5
    def __init__(self, path: Path):
        self._fts_state = "unknown"  # unknown/ready/building/unavailable
        self._fts_stats = {"filled": 0, "plain": 0}  # 本次补齐 / 其中无可检索文字
        self._fts_stale = False  # 索引体口径与当前代码不同（需后台重算）
        self._scrub_cursor = 0  # 存量清洗的扫描游标
        self._time_cursor = 0  # 时间/发言人存量回填的扫描游标
        self._scrub_stats = {"changed": 0, "emptied": 0}
        self.path = path
        self._active_cache = {}          # sid -> (取的时刻, 行列表) ✓ 见 active() ✓

    async def call(self, method, *args, **kwargs):
        operation = asyncio.create_task(
            asyncio.to_thread(getattr(self, method), *args, **kwargs)
        )
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Cancelling a coroutine does not stop its SQLite worker thread.
            # Join it before reload may open the same database with a new engine.
            await asyncio.gather(operation, return_exceptions=True)
            raise

    def requeue_running(self):
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state='queued',detail='paused during reload' WHERE state='running'"
            )

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        # FTS 触发器要用到它（连接级注册，成本可忽略）
        from .retrieval import index_grams

        db.create_function("index_grams", 1, index_grams)
        # ★ 2026-09-18：只要这次连接里发生过**写入**，就让 active() 的缓存立刻失效 ✓
        #   用 total_changes 判断 ⇒ **不需要**逐个写方法去调 invalidate ✓
        #   以后新增的任何写路径都不会漏 ✓（漏一个就会读到过期数据 ✗）
        #   读连接 total_changes 恒为 0 ⇒ 不干扰缓存命中 ✓
        before = db.total_changes
        try:
            with db:
                yield db
        finally:
            try:
                if db.total_changes != before:
                    self._active_cache.clear()
            except Exception:                      # 连接已坏 ⇒ 保险起见全清 ✓
                self._active_cache.clear()
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            with closing(sqlite3.connect(self.path)) as source:
                if source.execute("PRAGMA user_version").fetchone()[0] < 3:
                    backup = self.path.with_name(self.path.stem + ".pre-v3.sqlite3")
                    if not backup.exists():
                        temporary = backup.with_suffix(".tmp")
                        with closing(sqlite3.connect(temporary)) as target:
                            source.backup(target)
                        temporary.replace(backup)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
            CREATE TABLE IF NOT EXISTS records (
              id TEXT PRIMARY KEY, sid TEXT NOT NULL, role TEXT NOT NULL,
              level INTEGER NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
              summary TEXT NOT NULL, content TEXT NOT NULL, users TEXT NOT NULL,
              -- speaker：这条是谁说的（显示名）。users 是"可见范围"，别混用 ✗
              speaker TEXT NOT NULL DEFAULT '',
              -- 轮换槽位的记账：展示过几次 / 被真正用到几次（用于排序与冷却）
              rotate_shown INTEGER NOT NULL DEFAULT 0,
              rotate_used INTEGER NOT NULL DEFAULT 0,
              active INTEGER NOT NULL DEFAULT 1, deleted INTEGER NOT NULL DEFAULT 0,
              revision INTEGER NOT NULL DEFAULT 1, permanent INTEGER NOT NULL DEFAULT 0,
              position INTEGER NOT NULL, event_key TEXT UNIQUE, created REAL NOT NULL,
              importance INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS record_context ON records(sid,active,deleted,level DESC,position);
            CREATE INDEX IF NOT EXISTS record_time ON records(level,start,end);
            CREATE TABLE IF NOT EXISTS edges (
              parent TEXT NOT NULL REFERENCES records(id), child TEXT NOT NULL REFERENCES records(id),
              ordinal INTEGER NOT NULL, PRIMARY KEY(parent,child));
            CREATE TABLE IF NOT EXISTS facts (
              id TEXT PRIMARY KEY, sid TEXT NOT NULL, category TEXT NOT NULL, subject TEXT NOT NULL,
              content TEXT NOT NULL, reason TEXT NOT NULL, scenario TEXT NOT NULL, tags TEXT NOT NULL,
              relations TEXT NOT NULL, sources TEXT NOT NULL, fingerprint TEXT NOT NULL,
              deleted INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
              audited REAL NOT NULL DEFAULT 0, importance INTEGER NOT NULL DEFAULT 5,
              merge_pending INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL DEFAULT 0,
              rewrite_pending INTEGER NOT NULL DEFAULT 0,
              rewrite_attempts INTEGER NOT NULL DEFAULT 0,
              -- 事件时间区间（来源记录的时间跨度）。注意 created 是"入库时刻"，
              -- 两者不是一回事 ✗：今天整理出一周前的事，created=今天、event_at=那天。
              event_at REAL NOT NULL DEFAULT 0,
              event_end REAL NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS fact_identity ON facts(sid,subject,fingerprint,deleted);
            CREATE INDEX IF NOT EXISTS fact_subject ON facts(sid,subject,category,deleted);
            -- 注意：依赖新增列的索引必须放在下面的 ALTER 迁移**之后**建，
            -- 否则老库（还没有那一列）会在这一步直接报 no such column ✗
            CREATE TABLE IF NOT EXISTS short_ids (
              short TEXT PRIMARY KEY, real TEXT NOT NULL UNIQUE, created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS short_id_real ON short_ids(real);
            -- 全局词面扫描与永久记忆整理都按这两个条件筛
            CREATE INDEX IF NOT EXISTS fact_scan ON facts(deleted,merge_pending);
            CREATE INDEX IF NOT EXISTS record_permanent ON records(permanent)
              WHERE permanent=1;
            CREATE TABLE IF NOT EXISTS versions (
              id INTEGER PRIMARY KEY, kind TEXT NOT NULL, target TEXT NOT NULL,
              snapshot TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, sid TEXT NOT NULL, state TEXT NOT NULL,
              detail TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS job_unique ON jobs(kind,sid) WHERE state IN ('queued','running');
            CREATE TABLE IF NOT EXISTS job_items (
              id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL,
              target TEXT NOT NULL, action TEXT NOT NULL, note TEXT NOT NULL DEFAULT '',
              before TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS job_item_job ON job_items(job_id);
            -- v2.18.70：回收站每行都要查一次「这条最后什么时候被改/删」
            -- （SELECT max(created) FROM versions WHERE kind=? AND target=?）
            -- 而 versions 表**原先没有任何索引** ⇒ 每行一次全表扫描 ⇒ 页签一切换就卡 ✗
            -- 单表可达数万行（每次编辑/合并/快照都写一条）⇒ 这里补一个覆盖索引 ✓
            CREATE INDEX IF NOT EXISTS version_target ON versions(kind, target, created);
            CREATE TABLE IF NOT EXISTS vectors (
              id TEXT PRIMARY KEY REFERENCES records(id), model TEXT NOT NULL,
              revision INTEGER NOT NULL, vector TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS migration_items (
              source TEXT NOT NULL, source_key TEXT NOT NULL, digest TEXT NOT NULL,
              record_id TEXT REFERENCES records(id), reason TEXT NOT NULL,
              file_hash TEXT NOT NULL, metadata TEXT NOT NULL, created REAL NOT NULL,
              PRIMARY KEY(source,source_key,digest));
            CREATE TABLE IF NOT EXISTS migration_reports (source TEXT PRIMARY KEY, report TEXT NOT NULL);
            INSERT OR IGNORE INTO meta VALUES ('revision',0);
            CREATE TABLE IF NOT EXISTS entities (
              id TEXT PRIMARY KEY, kind TEXT NOT NULL, name TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS entity_names (
              id INTEGER PRIMARY KEY AUTOINCREMENT, entity_id TEXT NOT NULL REFERENCES entities(id),
              name TEXT NOT NULL, source TEXT NOT NULL, context TEXT NOT NULL,
              observed REAL NOT NULL, reason TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS name_entity ON entity_names(entity_id,observed DESC);
            """)

            # ★ 2026-09-18 性能审计：压缩预检 `compress_probe` 的**覆盖索引** ✓
            #   预检只回 (非永久行数, 最早 end, 最新 end) ✓ 但没有覆盖索引时
            #   SQLite 仍要按 3000 行回表 ⇒ 实测 6.7 ms ✗（够用但不够快 ✓）
            #   这个索引正好包含查询用到的全部列 ⇒ **纯索引扫描、零回表** ✓
            db.execute(
                "CREATE INDEX IF NOT EXISTS record_probe ON records("
                "sid, active, deleted, permanent, end)"
            )

            # ★ 2026-09-18：清理「身份绑定」撤除后**留在老库里的废弃表** ✓
            #   该功能已整体摘除（判据站不住 ✗ 用户决定不要 ✓）⇒ 表里只剩死数据 ✓
            #   用户要求："不会误伤、安全即可" ✓ ⇒ **四道保险**：
            #     ① 表不存在 ⇒ 什么都不做 ✓（新库/已清过 ⇒ 幂等 ✓）
            #     ② **列结构必须与当年创建的一模一样**才删 ✓
            #        （万一以后有别的表叫这名 ✗ ⇒ **跳过**并只记一行 warning ✓）
            #     ③ 全程 try/except ⇒ **绝不影响启动** ✓
            #     ④ 只 DROP 这一张表 ✓ 不碰任何其它表/数据 ✓
            try:
                cols = [r[1] for r in db.execute("PRAGMA table_info(entity_links)")]
                if cols:
                    if cols == ["raw", "canonical", "source", "evidence", "created"]:
                        db.execute("DROP TABLE entity_links")
                        logger.info(
                            "[记忆·Z] 已清理废弃的 entity_links 表（身份绑定功能已撤除 ✓）"
                        )
                    else:
                        logger.warning(
                            "[记忆·Z] 同名表 entity_links 列结构不符（%s）⇒ 跳过不动 ✓", cols
                        )
            except Exception:
                logger.debug("[记忆·Z] 清理废弃表失败（已忽略 ✓）", exc_info=True)
            _jcols = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
            if "automatic" not in _jcols:
                # 给旧库补列 ✓（默认 0 = 手动 ⇒ 老任务照旧显示 ✓ 不改变历史行为 ✓）
                db.execute(
                    "ALTER TABLE jobs ADD COLUMN automatic INTEGER NOT NULL DEFAULT 0"
                )
            columns = {r[1] for r in db.execute("PRAGMA table_info(records)")}
            if "visibility" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN visibility TEXT NOT NULL DEFAULT 'session'"
                )
            if "cold" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN cold INTEGER NOT NULL DEFAULT 0"
                )
            if "archived_at" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN archived_at REAL NOT NULL DEFAULT 0"
                )
                # Existing archives start their 180-day clock at upgrade time.
                db.execute(
                    "UPDATE records SET archived_at=? WHERE active=0 AND archived_at=0",
                    (time.time(),),
                )
            if "importance" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN importance INTEGER NOT NULL DEFAULT 0"
                )
            if "category" not in columns:
                # 永久记忆也带类别（沿用事实那套词汇 + rule/note）
                db.execute(
                    "ALTER TABLE records ADD COLUMN category TEXT NOT NULL DEFAULT ''"
                )
            if "access_count" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN access_count INTEGER NOT NULL DEFAULT 0"
                )
            if "last_accessed" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN last_accessed REAL NOT NULL DEFAULT 0"
                )
            if "tidy_at" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN tidy_at REAL NOT NULL DEFAULT 0"
                )
            if "speaker" not in columns:
                # 这条是谁说的（显示名）。users 是"可见范围"，不是同义词 ✗
                db.execute(
                    "ALTER TABLE records ADD COLUMN speaker TEXT NOT NULL DEFAULT ''"
                )
            if "rotate_shown" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN rotate_shown INTEGER NOT NULL DEFAULT 0"
                )
                db.execute(
                    "ALTER TABLE records ADD COLUMN rotate_used INTEGER NOT NULL DEFAULT 0"
                )
            # ★ 冷归档（P7）：此索引必须建在 cold / archived_at **确实存在之后** ✓
            #   （上面 368/372 行已补齐这两列 ✓；try 兜底 ⇒ 万一失败也只是慢一点 ✗ 不影响初始化 ✓）
            try:
                db.execute(
                    "CREATE INDEX IF NOT EXISTS record_cold ON records(cold, archived_at)"
                )
                # 回收站（deleted=1）单条件也要能走索引 ✓（复合索引里 deleted 在第 3 位 ⇒ 用不上 ✗）
                db.execute(
                    "CREATE INDEX IF NOT EXISTS record_deleted ON records(deleted) WHERE deleted=1"
                )
            except sqlite3.Error:
                logger.debug("[cold] record_cold / record_deleted 索引创建失败（忽略 ✓ 只是慢一点）")
            # ★ 2026-09-18：**事实侧也要记账** ✓
            #   原来 `mark_rotation` / `rotation_stats` / `rotation_pick` 只认 records ✗
            #   而事实轮换的候选是 facts 的行 ✓ ⇒ 用事实 id 去 UPDATE records
            #   ⇒ **匹配 0 行、静默无效果** ✗ ⇒ 事实轮换永远学不到"哪条被用过" ✓
            #   （只剩内存里的冷却 + seen 窗口 ✓ 重启即忘 ⇒ 每次从同几条重新开始 ✓）
            fact_columns = {r[1] for r in db.execute("PRAGMA table_info(facts)")}
            if "rotate_shown" not in fact_columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN rotate_shown INTEGER NOT NULL DEFAULT 0"
                )
                db.execute(
                    "ALTER TABLE facts ADD COLUMN rotate_used INTEGER NOT NULL DEFAULT 0"
                )
            # ★ 2026-09-23：**被用次数的时间戳** ✓
            #   没有它 ⇒ `rotate_used` 只增不减 ⇒ "用够几次"变成永久免沉（没有出口）✗
            #   旧库补 0 ⇒ 计分侧按"不衰减"处理 ⇒ 存量数据的命运一点不变 ✓
            if "rotate_used_at" not in fact_columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN rotate_used_at REAL NOT NULL DEFAULT 0"
                )
            if "search_body" not in columns:
                db.execute(
                    "ALTER TABLE records ADD COLUMN search_body TEXT NOT NULL DEFAULT ''"
                )
            job_item_columns = {r[1] for r in db.execute("PRAGMA table_info(job_items)")}
            if "before" not in job_item_columns:
                db.execute(
                    "ALTER TABLE job_items ADD COLUMN before TEXT NOT NULL DEFAULT ''"
                )
            fact_columns = {r[1] for r in db.execute("PRAGMA table_info(facts)")}
            if "importance" not in fact_columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN importance INTEGER NOT NULL DEFAULT 5"
                )
            if "merge_pending" not in fact_columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN merge_pending INTEGER NOT NULL DEFAULT 0"
                )
            if "rewrite_pending" not in fact_columns:
                # 降级拼接（模型输出不可用 → 按时间拼接）出来的事实：等着重做
                db.execute(
                    "ALTER TABLE facts ADD COLUMN rewrite_pending INTEGER NOT NULL DEFAULT 0"
                )
            if "rewrite_attempts" not in fact_columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN rewrite_attempts INTEGER NOT NULL DEFAULT 0"
                )
            if "event_at" not in fact_columns:
                # 事件时间区间（来源记录的时间跨度）；created 是"入库时刻"
                db.execute(
                    "ALTER TABLE facts ADD COLUMN event_at REAL NOT NULL DEFAULT 0"
                )
                db.execute(
                    "ALTER TABLE facts ADD COLUMN event_end REAL NOT NULL DEFAULT 0"
                )
            if "created" not in fact_columns:
                db.execute(
                    "ALTER TABLE facts ADD COLUMN created REAL NOT NULL DEFAULT 0"
                )
                db.execute(
                    "UPDATE facts SET created=coalesce((SELECT max(r.start) FROM records r,"
                    " json_each(facts.sources) s WHERE r.id=s.value),0)"
                )
            # 依赖新增列的索引要放在迁移**之后**建：老库要先补齐列，
            # 否则这一步直接 `no such column`（v2.13.0 的线上事故就出在这）
            db.execute(
                "CREATE INDEX IF NOT EXISTS fact_rewrite ON facts(rewrite_pending) "
                "WHERE rewrite_pending=1"
            )
            db.execute(
                "UPDATE jobs SET state='queued',detail='resumed after restart' WHERE state='running'"
            )
            db.execute("PRAGMA user_version=5")
            db.execute(
                "INSERT OR IGNORE INTO entities(id,kind,name,updated) SELECT DISTINCT value,'user','',0 FROM records,json_each(records.users)"
            )
            db.execute(
                "INSERT OR IGNORE INTO entities(id,kind,name,updated) SELECT DISTINCT sid,'session','',0 FROM records"
            )
            db.execute(
                "INSERT OR IGNORE INTO entities(id,kind,name,updated) SELECT DISTINCT subject,'user','',0 FROM facts WHERE subject LIKE 'legacy:%'"
            )

        # 检索索引：可用就建（派生数据，失败不影响任何功能）
        try:
            self.setup_search_index()
        except sqlite3.Error:
            self._fts_state = "unavailable"
            logger.debug("[记忆·Z] 本机 SQLite 不支持 FTS5，检索走全表路径")

    def setup_search_index(self):
        """建立 FTS5 检索索引（可用才建；不可用就永久走全表路径）。

        索引是**派生数据**：删了、坏了都能从 records 原表重建，所以
        存量用户不需要做任何事，这里失败也不影响任何功能。
        """
        with self.connect() as db:
            # 外部内容表：索引体就是 records.search_body 这一列。
            # contentless 表在"删除/改写"时要求提供与索引时完全一致的原文，
            # 恢复旧快照等场景会因此报错；外部内容表没有这个问题，还自带 rebuild。
            db.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS records_fts USING fts5("
                "body, content='records', content_rowid='rowid')"
            )
            # 索引体存在记录的 search_body 列里（写入时算好），触发器只做搬运——
            # 触发器里调 Python 函数会让「用原生连接写库」直接报 SQL logic error。
            for stale in ("records_fts_ai", "records_fts_au", "records_fts_ad"):
                db.execute("DROP TRIGGER IF EXISTS %s" % stale)
            db.execute(
                """CREATE TRIGGER records_fts_ai AFTER INSERT ON records
                   BEGIN INSERT INTO records_fts(rowid,body)
                   VALUES (new.rowid, new.search_body); END"""
            )
            db.execute(
                """CREATE TRIGGER records_fts_au AFTER UPDATE OF search_body ON records
                   BEGIN INSERT INTO records_fts(records_fts,rowid,body)
                   VALUES('delete', old.rowid, old.search_body);
                   INSERT INTO records_fts(rowid,body)
                   VALUES (new.rowid, new.search_body); END"""
            )
            db.execute(
                """CREATE TRIGGER records_fts_ad AFTER DELETE ON records
                   BEGIN INSERT INTO records_fts(records_fts,rowid,body)
                   VALUES('delete', old.rowid, old.search_body); END"""
            )
            # 召回时每次都要点一遍「还没进索引的行」（正常情况下一条都没有），
            # 给它一个部分索引：索引扫描 0 行，成本可忽略；
            # 没有它的话这个探测就是全表扫描（12k 条 ≈ 18ms，比索引省的还多）。
            # 注意不能写 ON records(rowid)——CREATE INDEX 不认 rowid 这个别名
            # （no such column: rowid）；普通索引本来就隐含 rowid，够用。
            db.execute(
                "CREATE INDEX IF NOT EXISTS record_unindexed ON records(id) "
                "WHERE search_body=''"
            )
        with self.connect() as db:
            missing = db.execute(
                "SELECT 1 FROM records WHERE search_body='' LIMIT 1"
            ).fetchone()
            row = db.execute(
                "SELECT value FROM meta WHERE key='search_index_scheme'"
            ).fetchone()
            stored = row["value"] if row else None
            if stored is None and not db.execute(
                "SELECT 1 FROM records LIMIT 1"
            ).fetchone():
                # 空库：没有旧索引体要作废，直接把口径记下（新装用户不会看到「回填中」）
                db.execute(
                    "INSERT OR REPLACE INTO meta(key,value) "
                    "VALUES ('search_index_scheme',?)",
                    (SEARCH_INDEX_SCHEME,),
                )
                stored = SEARCH_INDEX_SCHEME
        # 口径不同 = 旧索引体不可用（单字查询会漏），后台重算（见 prepare_search_index）
        self._fts_stale = stored != SEARCH_INDEX_SCHEME
        self._fts_state = (
            "building" if (missing or self._fts_stale) else "ready"
        )
        return True

    def scrub_capture_text(self, batch=200):
        """存量清洗：把早期版本混进原文/摘要里的协议外壳与思考块清一遍。

        分批扫（``LIMIT batch``），返回是否扫完；改动条数记在
        :meth:`scrub_stats`。**幂等**：只改真的变了的行。
        扫两类行：含 ``<`` 的（可能是外壳）、以及本来就空的（可能是只有外壳的残骸）。

        **清洗后什么都不剩的行移进回收站**（软删，可还原）：那种记录本来就只是
        一层空外壳（``<msg/>`` 之类），留着只会变成一张张空卡片，还白占压缩额度。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT rowid, id, level, permanent, role, summary, content FROM records "
                "WHERE rowid > ? AND (instr(content,'<')>0 OR instr(summary,'<')>0 "
                "OR (trim(content)='' AND trim(summary)='')) "
                "ORDER BY rowid LIMIT ?",
                (self._scrub_cursor, batch),
            ).fetchall()
            if not rows:
                return True
            self._scrub_cursor = rows[-1]["rowid"]
            updates, emptied = [], []
            for row in rows:
                content = scrub_row_text(row["level"], row["role"], row["content"])
                summary = scrub_row_text(row["level"], row["role"], row["summary"])
                if content == row["content"] and summary == row["summary"]:
                    # 本来就空、且不是可回收的 L0（永久记忆/存档不动）：保持原样
                    if row["level"] == 0 and not row["permanent"] and not summary:
                        emptied.append(row)
                    continue
                if not content and not summary and row["level"] == 0 and not row["permanent"]:
                    emptied.append(row)
                    continue
                updates.append(
                    (
                        content,
                        summary,
                        search_body_of(summary or content),
                        row["rowid"],
                    )
                )
            if updates:
                db.executemany(
                    "UPDATE records SET content=?, summary=?, search_body=? "
                    "WHERE rowid=?",
                    updates,
                )
            for row in emptied:
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) "
                    "VALUES ('record',?,?,?,?)",
                    (
                        row["id"],
                        dump(dict(row)),
                        "清洗后为空：这条原始记录只有协议外壳（如 <msg/>），已移入回收站",
                        time.time(),
                    ),
                )
                db.execute(
                    "UPDATE records SET deleted=1,revision=revision+1 WHERE id=?",
                    (row["id"],),
                )
        self._scrub_stats["changed"] += len(updates)
        self._scrub_stats["emptied"] += len(emptied)
        return False

    def backfill_time_provenance(self, batch=300):
        """存量迁移：给老数据补上「事件时间」与「发言人」。

        - `facts.event_at/event_end`：按 `sources` 里那些存档记录的 start 取最小/最大值
          （纯 SQL，一次搞定）。老库这个字段是空的，不补的话注入里就没有时间 ✗
        - `records.speaker`：显示名。优先用 `users` 里唯一的那个人；否则从正文开头的
          `[名字] ` 前缀里取（宿主渲染常常带这个前缀）。分批做，因为要查一次名字映射。

        幂等：只动 `event_at=0` / `speaker=''` 的行。

        返回 ``{facts, records, scanned, unresolved}``：
        - facts=补上事件时间的事实条数（**必须计**，否则只补事实时调用方会以为啥也没做 ✗）
        - records=补上发言人的记录条数；scanned=本批扫过的候选条数
        - unresolved=扫到但**判定不出来**的（群聊多人 + 正文没带名字）
        """
        facts_filled = 0
        with self.connect() as db:
            facts_filled = db.execute(
                "UPDATE facts SET"
                " event_at=coalesce((SELECT min(r.start) FROM records r,"
                "   json_each(facts.sources) s WHERE r.id=s.value),0),"
                " event_end=coalesce((SELECT max(r.start) FROM records r,"
                "   json_each(facts.sources) s WHERE r.id=s.value),0)"
                " WHERE event_at=0 AND sources<>'[]'"
            ).rowcount or 0
            rows = db.execute(
                "SELECT rowid,id,users,content FROM records"
                " WHERE speaker='' AND role='user' AND rowid > ? LIMIT ?",
                (self._time_cursor, int(batch)),
            ).fetchall()
            if rows:
                self._time_cursor = rows[-1]["rowid"]
        if not rows:
            return {
                "facts": facts_filled,
                "records": 0,
                "scanned": 0,
                "unresolved": 0,
            }
        wanted = set()
        for row in rows:
            try:
                users = json.loads(row["users"] or "[]")
            except (TypeError, ValueError):
                users = []
            if len(users) == 1 and users[0]:
                wanted.add(str(users[0]))
        names = {}
        if wanted:
            with self.connect() as db:
                names = {
                    row["id"]: (row["name"] or "").strip()
                    for row in db.execute(
                        "SELECT id,name FROM entities WHERE id IN (%s)"
                        % ",".join("?" * len(wanted)),
                        sorted(wanted),
                    ).fetchall()
                }
        updates = []
        for row in rows:
            try:
                users = json.loads(row["users"] or "[]")
            except (TypeError, ValueError):
                users = []
            speaker = ""
            if len(users) == 1:
                speaker = names.get(str(users[0]), "") or ""
            if not speaker:
                match = re.match(r"\s*[\[【]([^\]】\n]{1,24})[\]】]", row["content"] or "")
                speaker = (match.group(1).strip() if match else "") or ""
            if speaker:
                updates.append((speaker, row["id"]))
        if updates:
            with self.connect() as db:
                db.executemany(
                    "UPDATE records SET speaker=? WHERE id=?", updates
                )
        return {
            "facts": facts_filled,
            "records": len(updates),
            "scanned": len(rows),
            "unresolved": len(rows) - len(updates),
        }

    def permanents_need_tidy(self, sid, days):
        """这个会话里是否有"太久没被整理"的永久记忆（v2.16.3 周期整理用）。

        `permanent_tidy_days` 原来只是"整理时挑哪些记录"的间隔；
        现在同时当作"至少多久整理一次"：没有任何超重也会定期体检一次 ✓
        """
        cutoff = time.time() - max(0, int(days)) * 86400
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM records WHERE sid=? AND level=100 AND deleted=0"
                " AND active=1 AND cold=0 AND (tidy_at=0 OR tidy_at<?) LIMIT 1",
                (sid, cutoff),
            ).fetchone()
        return bool(row)

    def prepare_capture_scrub(self):
        """是否还需要跑一次存量清洗（口径版本记在 meta 里，升级后会自动再跑一次）。

        注意 meta.value 可能被 SQLite 存成整数（"2" → 2），所以按字符串比。
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='capture_scrub'"
            ).fetchone()
        return str(row["value"] if row else "") != CAPTURE_SCRUB_VERSION

    def finish_capture_scrub(self):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('capture_scrub',?)",
                (CAPTURE_SCRUB_VERSION,),
            )
        return True

    def scrub_stats(self):
        return dict(self._scrub_stats)

    def prepare_search_index(self):
        """索引体口径升级时作废旧索引体；返回是否作了废（后台回填会重算）。

        用 UPDATE 而不是 DROP：AFTER UPDATE OF search_body 触发器会把 FTS 里的
        旧词元删掉，避免外部内容表"与索引时不一致"（那会直接报 SQL logic error）。
        在后台任务里跑，不阻塞插件加载。
        """
        if not getattr(self, "_fts_stale", False):
            return False
        with self.connect() as db:
            db.execute("UPDATE records SET search_body=''")
            db.execute(
                "INSERT OR REPLACE INTO meta(key,value) VALUES ('search_index_scheme',?)",
                (SEARCH_INDEX_SCHEME,),
            )
        self._fts_stale = False
        return True

    def index_backfill(self, batch=500):
        """补齐 search_body 列与索引；返回是否已全部覆盖（供后台分批调用）。

        每批都必须让候选集**真的变小**，否则循环永不结束：没有可检索文字的摘要
        写占位符（见 search_body_of），所以它们处理完就不再是「未进索引」的行。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT rowid, summary FROM records WHERE search_body='' LIMIT ?",
                (batch,),
            ).fetchall()
            if not rows:
                # 索引本机不可用时不要"修好"状态：那会让召回每次白试一遍。
                if self._fts_state != "unavailable":
                    self._fts_state = "ready"
                return True
            bodies = [search_body_of(row["summary"]) for row in rows]
            db.executemany(
                "UPDATE records SET search_body=? WHERE rowid=?",
                [(body, row["rowid"]) for body, row in zip(bodies, rows)],
            )
        self._fts_stats["filled"] += len(bodies)
        self._fts_stats["plain"] += sum(1 for body in bodies if body == NO_TOKENS)
        return False

    def rebuild_search_index(self):
        """整表重建（异常或版本升级时用）。"""
        with self.connect() as db:
            for row in db.execute("SELECT rowid, summary FROM records").fetchall():
                db.execute(
                    "UPDATE records SET search_body=? WHERE rowid=?",
                    (search_body_of(row["summary"]), row["rowid"]),
                )
            db.execute("INSERT INTO records_fts(records_fts) VALUES('rebuild')")
        self._fts_state = "ready"
        return True

    # TODO(v2.19): 按年份分库（方案见 README「L0 容量、分库预留与决策触发线」）
    #   触发线：库 > 500MB / FTS 查询 > 200ms / vacuum > 60s / 单年条目 > 50 万
    #   方案：records_YYYY.db，往年库只读 + 当年库读写，检索时 ATTACH 跨库查询
    #   开关：archive_shard_from_year（0 = 关闭，默认）。现在只保留 id/时间戳稳定这一前提 ✓

    def capacity_stats(self, conn=None):
        """容量仪表：每层条数 / 库大小 / 索引条数 / 逐年增长（P0）。

        供 /status 与界面显示，用来判断何时该分库（见 README 的触发线）。
        防御式：取不到连接或某项查不到就跳过，绝不影响插件运行 ✓

        ⚠️ 连接必须走 self.connect()（本 store 只提供这个上下文管理器）。
        曾经写成 getattr(self, "conn"/"_conn"/"db") —— 那三个属性根本不存在 ✗
        → 每次都在开头 return {} → /status.capacity 恒为空 →
        首页「L0 容量」永远显示「—」（用户实测报告 ✓）
        """
        out = {"levels": {}, "years": {}, "db_bytes": 0, "fts_rows": 0}
        try:
            ctx = self.connect() if conn is None else _nullcontext(conn)
            with ctx as db:
                for key, sql in (
                    ("levels", "SELECT level, count(*) FROM records GROUP BY level"),
                    (
                        "years",
                        "SELECT substr(datetime(created, 'unixepoch'), 1, 4), count(*) "
                        "FROM records GROUP BY 1",
                    ),
                ):
                    try:
                        out[key] = {str(k): v for k, v in db.execute(sql)}
                    except Exception:
                        pass
                try:
                    out["db_bytes"] = (
                        db.execute("PRAGMA page_count").fetchone()[0]
                        * db.execute("PRAGMA page_size").fetchone()[0]
                    )
                except Exception:
                    pass
                try:
                    out["fts_rows"] = db.execute("SELECT count(*) FROM records_fts").fetchone()[0]
                except Exception:
                    pass
        except Exception:
            return {}
        return out

    def expand_query(self, keyword, limit=4):
        """查询词沿**已记录的线索**扩一步：实体别名 + 已核实关系的客体 ✓

        设计原则：**不做自由联想** ✗ —— 只用库里已经写明的东西（别名表 + 关系 JSON）。
        四条闸门：
          ① 只走一步（不沿着新词再扩，防止越扩越远）
          ② 最多 limit 个扩展词
          ③ 词长 ≥2（丢掉单字与标点）
          ④ 去重，且不得包含原查询词
        只读 ✓ 不改任何数据。返回额外词列表（可能为空）。
        """
        words = [w for w in re.split(r"[\s,，、;；/|]+", str(keyword or "")) if len(w) >= 2][:4]
        if not words:
            return []
        extra, seen = [], set(words)
        with self.connect() as db:
            for word in words:
                ids = [r[0] for r in db.execute(
                    "SELECT id FROM entities WHERE name=?"
                    " UNION SELECT entity_id FROM entity_names WHERE name=?",
                    (word, word),
                )]
                if not ids:
                    continue
                marks = ",".join("?" * len(ids))
                rows = db.execute(
                    "SELECT DISTINCT json_extract(rel.value, '$.object')"
                    " FROM facts f, json_each(f.relations) rel"
                    # relations 为空串/脏值时 json_each 会抛 malformed JSON ✗ → 必须 json_valid 过滤（实测确认）
                    " WHERE f.deleted=0 AND json_valid(f.relations)"
                    " AND f.relations IS NOT NULL AND f.relations != ''"
                    " AND f.subject IN (%s)" % marks,
                    ids,
                ).fetchall()
                for (obj,) in rows:
                    # 只接受**非空字符串**：缺 object / object 是 null 或数字时，
                    # json_extract 会给 None 或数字 ✗ —— str(None) 会变成字面量 "None" 被当检索词（实测坑）
                    if not isinstance(obj, str):
                        continue
                    obj = obj.strip()
                    # 只扩"人能读的名字" ✗ —— 实体 id（如 qq:2）当检索词只会带来噪声
                    if len(obj) >= 2 and ":" not in obj and obj not in seen:
                        seen.add(obj)
                        extra.append(obj)
                        if len(extra) >= limit:
                            return extra
        return extra

    def search_index_state(self):
        """索引状态：unavailable / ready / building（供界面显示，不查库）。"""
        return self._fts_state

    def search_index_stats(self):
        """本次补齐的条数（供日志说明「到底好没好」）。"""
        return dict(self._fts_stats)

    def abandon_search_index(self):
        """回填失败时降级：索引不再参与检索（全表路径始终是正确的那个）。

        绝不能把状态留在 building —— 那会让界面永远显示「索引 回填中」，
        却既没有在回填、也不会好。
        """
        self._fts_state = "unavailable"
        return self._fts_state

    def _note_fts(self, path, detail):
        """记录本轮召回的取数路径（**只写日志 + 记一个内部标记，不参与任何逻辑** ✓）

        ★ 2026-09-20：用于"卡回复"排查 —— 让人一眼看出本轮到底走了哪条路、为什么。
          设计要点：
            · 同一 (path, reason) **只在变化时记一条 info** ⇒ 不会每轮刷屏 ✓
            · 每轮都记一条 debug ⇒ 需要细节时把日志级别调到 DEBUG ✓
            · 绝不影响返回结果（不读也不写业务数据 ✓）
        """
        last = getattr(self, "_fts_last_note", None)
        key = (path, detail if path != "fast" else "")
        if key != last:
            self._fts_last_note = key
            if path == "fast":
                logger.info("[recall] 索引快路径生效（%s）", detail)
            else:
                logger.info(
                    "[recall] 本轮走全表参照路径（%s）—— 结果与快路径逐条一致，只是速度较慢；"
                    "索引就绪后会自动回到快路径", detail)
        logger.debug("[recall] path=%s %s", path, detail)

    def _fts_hits(self, tokens, cap=1200):
        """用 FTS 索引取候选 rowid；索引不可用/命中过宽时返回 None（走全表）。

        返回 None 只表示"不优化"，不表示"没有命中"——调用方必须退回全表路径，
        这样两条路径的结果始终一致。

        候选里**一定**要带上「还没进索引的行」（外部工具直接写库的老记录）：
        它们在 SQL 里若用 ``search_body='' OR …`` 表达，SQLite 会因为 OR 放弃
        rowid 索引、退化成全表扫描（实测 0.3ms → 18ms，索引等于白建）。
        所以这里直接把它们的 rowid 并进候选列表。
        """
        if not tokens or self._fts_state != "ready":
            self._note_fts("fallback", "fts_state=%s" % self._fts_state)
            return None
        from .retrieval import fts_match_query

        match = fts_match_query(tokens)
        if not match:
            self._note_fts("fallback", "no-match-expr")
            return None
        t0 = time.perf_counter()
        try:
            with self.connect() as db:
                rows = db.execute(
                    "SELECT rowid FROM records_fts WHERE records_fts MATCH ? LIMIT ?",
                    (match, cap + 1),
                ).fetchall()
                missing = db.execute(
                    "SELECT rowid FROM records WHERE search_body='' LIMIT ?",
                    (cap + 1,),
                ).fetchall()
        except sqlite3.Error:
            self._fts_state = "unavailable"
            self._note_fts("fallback", "sqlite-error -> unavailable")
            return None
        if len(rows) > cap or len(missing) > cap:
            self._note_fts("fallback", "too-broad rows=%d missing=%d cap=%d" % (len(rows), len(missing), cap))
            return None
        _n = len(rows) + len(missing)
        self._note_fts("fast", "hits=%d ms=%.2f" % (_n, (time.perf_counter() - t0) * 1000))
        return [row["rowid"] for row in rows] + [row["rowid"] for row in missing]

    def revision(self):
        """当前库版本号（写入即 +1，供进程内缓存判失效）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='revision'"
            ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def bump(db):
        db.execute("UPDATE meta SET value=value+1 WHERE key='revision'")

    def bootstrap_done(self, sid):
        """本会话是否已经播种过（或已决定跳过）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM meta WHERE key=?", (f"bootstrap:{sid}",)
            ).fetchone()
        return row is not None

    def mark_bootstrap(self, sid, clean=False):
        """记下已处理：清理播种记录后也不会重新播种。

        ``clean=True`` 表示播种当时没有任何会话合并/压缩插件在场，
        这批记录可以确认来源，不需要事后核对。
        """
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES (?,1)", (f"bootstrap:{sid}",)
            )
            if clean:
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?,1)",
                    (f"bootstrap_clean:{sid}",),
                )
            self.bump(db)

    def bootstrap_review(self):
        """有播种记录、但来源无法确认（没有 clean 标记）的会话。"""
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT r.sid FROM records r WHERE r.deleted=0 "
                "AND r.event_key LIKE ? AND NOT EXISTS "
                "(SELECT 1 FROM meta WHERE key='bootstrap_clean:'||r.sid)",
                ("%:bootstrap:%",),
            ).fetchall()
        return [row["sid"] for row in rows]

    def bootstrap_reviewed(self):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM meta WHERE key='bootstrap_review_done'"
                ).fetchone()
                is not None
            )

    def mark_bootstrap_reviewed(self):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES ('bootstrap_review_done',1)")
            self.bump(db)

    def purge_bootstrap(self):
        """软删除历史播种记录；快照留在 versions，且不会重新播种。"""
        report = {"removed": 0, "sessions": 0, "freed_chars": 0}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,sid,summary FROM records WHERE deleted=0 "
                "AND event_key LIKE ?",
                ("%:bootstrap:%",),
            ).fetchall()
            sessions = set()
            for row in rows:
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) "
                    "VALUES ('record',?,?,?,?)",
                    (row["id"], dump(dict(row)), "清理历史播种记录", time.time()),
                )
                db.execute(
                    "UPDATE records SET deleted=1,revision=revision+1 WHERE id=?",
                    (row["id"],),
                )
                self._orphan_facts(db, row["id"])
                sessions.add(row["sid"])
                report["removed"] += 1
                report["freed_chars"] += len(row["summary"] or "")
            for sid in sessions:
                db.execute(
                    "INSERT OR REPLACE INTO meta VALUES (?,1)", (f"bootstrap:{sid}",)
                )
            if report["removed"]:
                self.bump(db)
            report["sessions"] = len(sessions)
        return report

    @staticmethod
    def row(row):
        if row is None:
            return None
        result = dict(row)
        for key in ("users", "tags", "relations", "sources"):
            if key in result:
                result[key] = json.loads(result[key])
        if "relations" in result:
            result["relation_warnings"] = [
                {"relation": r, "reason": relation_issue(r)}
                for r in result["relations"]
                if relation_issue(r)
            ]
            result["verified_relations"] = [
                r for r in result["relations"] if not relation_issue(r)
            ]
        return result

    def attach_evidence(self, facts):
        """为每条事实补上「最新一条来源」：src（记录 ID）与 src_user（说话人）。

        注入与工具返回只带一个指针，模型据此就能 ReadMemoryArchive 追到原文。
        """
        sources = {s for f in facts for s in f.get("sources", [])}
        if not sources:
            return facts
        with self.connect() as db:
            meta = {
                r[0]: (r[1] or 0, json.loads(r[2] or "[]"))
                for r in db.execute(
                    "SELECT id,start,users FROM records WHERE id IN"
                    " (SELECT value FROM json_each(?))",
                    (dump(sorted(sources)),),
                )
            }
        for fact in facts:
            best = None
            for source in fact.get("sources", []):
                start, users = meta.get(source, (0, []))
                if best is None or start >= best[0]:
                    best = (start, source, users)
            if best:
                fact["src"] = best[1]
                fact["src_user"] = best[2][0] if best[2] else ""
        return facts

    def observe_name(
        self,
        entity_id,
        name,
        kind="user",
        source="adapter",
        context="",
        observed=None,
        revision=None,
        reason="",
    ):
        name = str(name or "").strip()
        if not name or len(name) > 500 or any(ord(c) < 32 for c in name):
            return False
        observed = time.time() if observed is None else observed
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                "SELECT * FROM entities WHERE id=?", (entity_id,)
            ).fetchone()
            if revision is not None and (not old or old["revision"] != revision):
                raise Conflict("name changed")
            if old and old["name"] == name and revision is None:
                if observed > old["updated"]:
                    db.execute(
                        "UPDATE entities SET updated=? WHERE id=?",
                        (observed, entity_id),
                    )
                return False
            if old and observed < old["updated"]:
                return False
            db.execute(
                """INSERT INTO entities(id,kind,name,updated) VALUES (?,?,?,?)
              ON CONFLICT(id) DO UPDATE SET name=excluded.name,revision=entities.revision+1,updated=excluded.updated""",
                (entity_id, kind, name, observed),
            )
            db.execute(
                "INSERT INTO entity_names(entity_id,name,source,context,observed,reason) VALUES (?,?,?,?,?,?)",
                (entity_id, name, source, context, observed, reason),
            )
            self.bump(db)
            return True

    def entities(self, query="", ids=None, limit=100, offset=0, summaries=0):
        clauses, args = [], []
        if ids is not None:
            clauses.append("e.id IN (SELECT value FROM json_each(?))")
            args.append(dump(list(ids)))
        if query:
            clauses.append(
                "(instr(lower(e.id),lower(?))>0 OR EXISTS (SELECT 1 FROM entity_names n WHERE n.entity_id=e.id AND instr(lower(n.name),lower(?))>0))"
            )
            args.extend([query, query])
        with self.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT e.* FROM entities e"
                    + (" WHERE " + " AND ".join(clauses) if clauses else "")
                    + " ORDER BY e.updated DESC,e.id LIMIT ? OFFSET ?",
                    [*args, limit, offset],
                )
            ]
            stats = {}
            if summaries and rows:
                ids_ = [r["id"] for r in rows]
                for row in db.execute(
                    "SELECT subject, count(*) AS facts, max(audited) AS seen,"
                    " coalesce((SELECT group_concat(content, char(1)) FROM"
                    " (SELECT content FROM facts x WHERE x.subject=facts.subject"
                    " AND x.deleted=0 ORDER BY x.importance DESC,x.audited DESC"
                    " LIMIT ?)),'') AS top"
                    " FROM facts WHERE deleted=0 AND subject IN"
                    " (SELECT value FROM json_each(?)) GROUP BY subject",
                    (max(1, int(summaries)), dump(ids_)),
                ):
                    stats[row["subject"]] = {
                        "facts": row["facts"],
                        "seen": row["seen"] or 0,
                        "summary": [
                            item[:24] for item in row["top"].split(chr(1)) if item
                        ],
                        "relations": 0,
                    }
                for row in db.execute(
                    "SELECT f.subject, count(*) AS n FROM facts f,"
                    " json_each(f.relations) rel WHERE f.deleted=0 AND"
                    " json_extract(rel.value,'$.subject')=f.subject AND f.subject IN"
                    " (SELECT value FROM json_each(?)) GROUP BY f.subject",
                    (dump(ids_),),
                ):
                    if row["subject"] in stats:
                        stats[row["subject"]]["relations"] = row["n"]
            for r in rows:
                r.update(identity_info(r["id"]))
                if summaries:
                    r["stats"] = stats.get(
                        r["id"], {"facts": 0, "seen": 0, "summary": [], "relations": 0}
                    )
                r["history"] = [
                    dict(n)
                    for n in db.execute(
                        "SELECT name,source,context,observed,reason FROM entity_names WHERE entity_id=? ORDER BY observed DESC,id DESC LIMIT 50",
                        (r["id"],),
                    )
                ]
                if not r["name"] and r["lookup_id"] and r["lookup_id"] != r["id"]:
                    linked = db.execute(
                        "SELECT name FROM entities WHERE id=?", (r["lookup_id"],)
                    ).fetchone()
                    if linked and linked[0]:
                        r["label"] = linked[0] + " · 同号码档案"
                        r["name_source_id"] = r["lookup_id"]
            return rows

    def entity_ids(self, sid, users=(), scope="session"):
        # Resolve identities from the same scope predicate, independently of pagination.
        with self.connect() as db:
            if scope == "global":
                return [r[0] for r in db.execute("SELECT id FROM entities")]
            rows = [
                self.row(r)
                for r in db.execute(
                    """SELECT sid,users FROM records WHERE deleted=0
              AND (sid=? OR visibility='global' OR ((visibility='user' OR ?='linked') AND EXISTS
                (SELECT 1 FROM json_each(records.users) WHERE value IN (SELECT value FROM json_each(?)))))""",
                    (sid, scope, dump(list(users))),
                )
            ]
            ids = {sid, *users}
            for row in rows:
                ids.update(row["users"])
                ids.add(row["sid"])
            return sorted(ids)

    def entity_ids_for_query(self, query, sid="", users=(), scope="session"):
        """Entities whose current or former name (or id) appears in the query text.

        Used to give facts about the people being talked about a ranking boost,
        instead of hoping their names appear in the matched text.
        """
        from .retrieval import squeeze

        query = squeeze((query or "").strip())  # 名字带空格（「星 月」）也能在没空格的句子里认出
        if len(query) < 2 or len(query) > 500:
            return []
        visible = None
        if scope != "global":
            visible = set(self.entity_ids(sid, users, scope))
        with self.connect() as db:
            db.create_function("squeeze", 1, squeeze)
            rows = db.execute(
                "SELECT DISTINCT e.id, e.name FROM entities e "
                "LEFT JOIN entity_names n ON n.entity_id=e.id WHERE "
                "(length(n.name)>=2 AND instr(?,squeeze(n.name))>0) OR "
                "(length(e.name)>=2 AND instr(?,squeeze(e.name))>0) OR "
                "(length(e.id)>=4 AND instr(?,e.id)>0) LIMIT 100",
                (query, query, query),
            ).fetchall()
        ids = []
        for row in rows:
            if visible is not None and row[0] not in visible:
                continue
            ids.append(row[0])
        return ids[:20]

    # ── 身份绑定（2026-09-18 批次 3）────────────────────────────────
    def fact_health(self, threshold=15, now=None, limit=50, offset=0):
        from . import retrieval

        now = float(now or time.time())
        base_cols = [
            "id", "sid", "subject", "content", "category", "importance",
            "rotate_shown", "rotate_used", "rotate_used_at", "created", "event_at",
        ]
        with self.connect() as db:
            cols = {r[1] for r in db.execute("PRAGMA table_info(facts)").fetchall()}
            # ★ 2026-09-18（用户）：体检卡片要和「事实卡片」显示**一样的东西** ✓
            #   只多出本页特有的（分数/被用/年龄/下沉标签）✓
            #   ⇒ 这些列**存在才取** ✓ 免得列名猜错炸整个接口 ✗
            #   ⚠️ 列名表必须**与 SELECT 同序**（用全表顺序会错位取数 ✗ 实测踩到）
            sel_cols = base_cols + [
                c for c in ("tags", "reason", "scenario", "sources", "rewrite_pending")
                if c in cols
            ]
            # ★ 2026-09-18（用户实测"加载太久"）：
            #   原来一次回 **300 行 = 189.5 KB** ✗ 而页面一次只显示 50 条
            #   ⇒ **84% 的流量白传白解析**（手机端传输 + JSON.parse + 内存 ✓）
            #   ⇒ 改成**服务端分页**：每页只回 50 行（≈32 KB ✓ 6 倍小 ✓）
            #   总条数用 `count` 单独回 ✓ 前端翻页器照常工作 ✓
            total = db.execute(
                "SELECT COUNT(*) FROM facts WHERE deleted=0"
            ).fetchone()[0]
            rows = db.execute(
                "SELECT " + ", ".join(sel_cols) + " FROM facts"
                " WHERE deleted=0 ORDER BY importance ASC, created ASC LIMIT ? OFFSET ?",
                (max(1, int(limit or 50)), max(0, int(offset or 0))),
            ).fetchall()
        out = []
        for row in rows:
            fact = dict(zip(sel_cols, row))
            for key in ("tags", "sources"):
                val = fact.get(key)
                if isinstance(val, str):
                    try:
                        fact[key] = json.loads(val)
                    except Exception:
                        fact[key] = []
            fact["rewrite_pending"] = int(fact.get("rewrite_pending") or 0)
            created = float(fact.get("created") or 0)
            fact["age_days"] = max(0, int((now - created) / 86400)) if created else None
            fact["score"] = retrieval.fact_sink_score(fact, now=now)
            fact["sunk"] = retrieval.should_sink(fact, threshold, now=now)
            fact["never_sink"] = int(fact.get("importance") or 5) >= retrieval.NEVER_SINK_IMPORTANCE
            fact["edit_history"] = []  # 体检页读的是"下沉体检"，审校历史沿用事实页 ✓
            fact["relation_warnings"] = []
            out.append(fact)
        out.sort(key=lambda f: (f["score"], f["importance"], f["created"] or 0))
        # `count` = **总数** ✓（前端翻页器要用它）；`rows` = 本页 ✓
        return {"threshold": int(threshold), "count": int(total), "offset": int(offset),
                "now": now, "rows": out}

    def has_active_job(self, kind, sid):
        """该会话是否已有**排队中/在跑**的同类任务 ✓（2026-09-18）

        为什么需要：扫描每轮都会重新判一遍所有会话 ✓ ——
        若某会话已经有任务在排队 ✓ 它仍会被算进"有内容可压"的数量里 ✓
        并被**再入队一次**（`INSERT OR IGNORE` 不报错 ✓ 但计数是假的 ✓）
        实测现象：启动扫描与迁移后扫描相隔 2 秒 ✓ 日志里同两行被数了两次 ✗
        （用户 2026-09-18 的日志就是这个 ✓）
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT 1 FROM jobs WHERE kind=? AND sid=? AND state IN ('queued','running')"
                " LIMIT 1",
                (kind, sid),
            ).fetchone()
        return row is not None

    def can_schedule(self, kind, sid):
        with self.connect() as db:
            row = db.execute(
                "SELECT state,updated FROM jobs WHERE kind=? AND sid=? ORDER BY created DESC LIMIT 1",
                (kind, sid),
            ).fetchone()
            return (
                not row
                or row["state"] != "failed"
                or time.time() - row["updated"] >= 300
            )

    def capture(self, sid, event_key, messages):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_entities(db, sid, {u for m in messages for u in m["users"]})
            pos = db.execute(
                "SELECT coalesce(max(position),0) FROM records WHERE sid=?", (sid,)
            ).fetchone()[0]
            for index, msg in enumerate(messages):
                pos += 1
                # The summary is what reaches the model; content keeps the full text.
                summary = msg.get("summary") or msg["content"]
                db.execute(
                    """INSERT OR IGNORE INTO records
                  (id,sid,role,level,start,end,summary,content,users,speaker,
                   position,event_key,created,search_body,category)
                  VALUES (?,?,?,0,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        uid(),
                        sid,
                        msg["role"],
                        msg["time"],
                        msg["time"],
                        summary,
                        msg["content"],
                        dump(msg["users"]),
                        # 这条是谁说的（显示名）：读原文时模型必须能分清谁说了哪句
                        str(msg.get("speaker") or ""),
                        pos,
                        f"{sid}:{event_key}:{index}",
                        time.time(),
                        # 建索引体：否则新记录要等到下次启动回填才进索引，
                        # 加速会随会话进行而失效（且编辑后可能静默漏召回）。
                        search_body_of(summary),
                        # v2.18.9：category="media" = 只有表情/图片的消息 ✓
                        # 数据照留（不丢 ✓）但默认不进召回 ✗（描述平均 276 字符，纯噪音）
                        str(msg.get("category") or ""),
                    ),
                )
            self.bump(db)

    @staticmethod
    def _ensure_entities(db, sid, users):
        db.execute(
            "INSERT OR IGNORE INTO entities(id,kind,name,updated) VALUES (?,'session','',0)",
            (sid,),
        )
        db.executemany(
            "INSERT OR IGNORE INTO entities(id,kind,name,updated) VALUES (?,'user','',0)",
            [(u,) for u in users],
        )

    def active_by_session(self):
        """一次取回**所有**会话的活跃记录 ✓（按 sid 分组 ✓）

        原来调度器是 `for sid in sessions: active(sid)` ✗ = **N+1 次查询** ✓
        （100 个会话 ⇒ 每 30 秒 100 次查询 ✓ 纯 DB 开销 ✓ 不花钱 ✓ 但没必要 ✓）
        用户 2026-09-17 同意优化 ✓
        ⚠️ 行的形状与 `active(sid)` **完全一致** ✓（同一套 SELECT ✓ 只是去掉 sid 过滤 ✓）
        """
        out = {}
        with self.connect() as db:
            for r in db.execute(
                """SELECT * FROM records WHERE active=1 AND deleted=0
              ORDER BY sid, permanent DESC, level DESC, position, id"""
            ):
                row = self.row(r)
                out.setdefault(row["sid"], []).append(row)
        return out

    def compress_probe(self, sid):
        """压缩闸门的**便宜预检** ✓（2026-09-18 性能审计 ✓）

        为什么需要它：`active(sid)` 要把**整表可用行**搬进 Python 并转成 dict ✓
        实测 3000 条记录的群 ≈ **65 ms** ✓（SQL 32 + 转 dict 32 ✓）；
        而"这条消息到底要不要排压缩任务"只需要**三个数** ✓
        ⇒ 这条查询只回 (非永久行数, 上层摘要行数, 最早 end, 最新 end) ✓ 走覆盖索引 ✓
          ⇒ 毫秒级 ✓（它只**放行**、不否决 ✓：宁可多放行让真判定去否 ✓ 绝不误杀 ✓）

        ⚠️ 口径必须与 `compression_plan` **完全一致** ✗✓：
        · 行数只数**非永久**（计划的候选集 ✓）
        · 时间用**全部可用行**（计划算"陈旧/闲置"用的是 `rows` ✓ **不是**候选集 ✓）
          差这一处就会把"只有永久记忆 + 一条旧记录"的会话误杀 ✓
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT SUM(CASE WHEN permanent=0 THEN 1 ELSE 0 END),"
                " SUM(CASE WHEN permanent=0 AND level>0 THEN 1 ELSE 0 END),"
                " MIN(end), MAX(end) FROM records"
                " WHERE sid=? AND active=1 AND deleted=0",
                (sid,),
            ).fetchone()
        #   返回 (非永久行数, 其中上层摘要行数, 最早 end, 最新 end) ✓
        return (int(row[0] or 0), int(row[1] or 0), row[2], row[3])

    def active(self, sid):
        """该会话全部可用（未删 / 未归档）记录 ✓

        ⚠️ 2026-09-18 性能审计：这是**最热**的查询 ✓
        · 3000 条记录的群实测 **~65 ms**（SQL 物化 32 ms + 转 dict 32 ms）✓
        · 而**同一条消息**里它会被调 2~3 次（`on_request` 开头一次 + 回复后的压缩闸一次）✓
          ⇒ **每轮白付 130 ms** ✗，全在用户等回复的关键路径上 ✓
        ⇒ 加 1.5 秒 TTL 缓存 ✓；**任何写入**（由 `connect()` 用 `total_changes` 判定 ✓
          不需要逐个写方法去失效 ⇒ 以后新增写路径也不会漏 ✓）会让它立刻失效 ✓
        ⇒ 返回**列表副本** ✓（调用方改列表不会污染缓存 ✓；要改里面的 dict 请自己 copy ✓）
        """
        hit = self._active_cache.get(sid)
        if hit is not None and time.time() - hit[0] <= self._ACTIVE_TTL:
            return list(hit[1])
        with self.connect() as db:
            rows = [
                self.row(r)
                for r in db.execute(
                    """SELECT * FROM records WHERE sid=? AND active=1 AND deleted=0
              ORDER BY permanent DESC,level DESC,position,id""",
                    (sid,),
                )
            ]
        self._active_cache[sid] = (time.time(), rows)
        return list(rows)

    def context(self, sid, users=(), scope="session"):
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    """SELECT * FROM records WHERE active=1 AND deleted=0 AND
                (sid=? OR visibility='global' OR (?='global' AND permanent=1) OR ((visibility='user' OR ?='linked') AND EXISTS
                  (SELECT 1 FROM json_each(records.users) WHERE value IN (SELECT value FROM json_each(?)))))
                ORDER BY permanent DESC,level DESC,position,id""",
                    (sid, scope, scope, dump(list(users))),
                )
            ]

    def import_legacy(self, snapshot):
        from .migration import import_snapshot

        return import_snapshot(self, snapshot)

    def scan_legacy(self, root, plugin_id, limit, adapters=(), decay_days=365):
        from .migration import snapshot
        from .identity import Resolver

        with self.connect() as db:
            entities = [
                (r["id"], r["kind"]) for r in db.execute("SELECT id,kind FROM entities")
            ]
        return snapshot(root, plugin_id, limit, Resolver(entities, adapters), decay_days=decay_days)

    def backfill_tool_steps(self):
        """把**存量**的工具步记录补上 `category='tool'` ✓（v2.18.19 ✓ 幂等 ✓）

        背景：工具步的标记是 v2.18.19 才加的 ✗ ⇒ 升级前入库的工具步没标记 ✓
        ⇒ 那些记录**过滤不到** ✗ 存量用户享受不到"bot 看不到工具步"的收益 ✓
        识别方式：工具步的 content 里必定带 `{"tool_calls": …}`（capture 时写入 ✓）
        返回补标的条数 ✓（0 = 无需回填 ✓）
        """
        with self.connect() as db:
            cur = db.execute(
                "UPDATE records SET category='tool'"
                " WHERE category='' AND deleted=0"
                " AND content LIKE '%\"tool_calls\"%'"
            )
            return cur.rowcount or 0

    def legacy_migrated_at(self):
        """上次成功迁移旧记忆的时间戳（0 表示还没成功过）。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT value FROM meta WHERE key='legacy_migrated_at'"
            ).fetchone()
        return float(row["value"]) if row else 0.0

    def set_legacy_migrated_at(self, value):
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO meta VALUES ('legacy_migrated_at',?)",
                (float(value),),
            )
            self.bump(db)

    def synthetic_identity(self):
        """True while any row still carries a synthetic migration identifier."""
        with self.connect() as db:
            for table, column in (
                ("entities", "id"),
                ("records", "sid"),
                ("facts", "sid"),
                ("facts", "subject"),
            ):
                if db.execute(
                    f"SELECT 1 FROM {table} WHERE {column} LIKE 'legacy:%' "
                    f"OR {column} LIKE 'unresolved:%' LIMIT 1"
                ).fetchone():
                    return True
            return False

    def canonicalize_identity(self, adapters=(), bindings=None):
        """Merge synthetic migration identifiers onto the live same-number entity.

        Idempotent: rows that already carry a live identifier are untouched.
        ``bindings`` forces a mapping, e.g. after an adapter lookup proved which
        platform an ``unresolved:user:123`` placeholder belongs to.
        """
        from . import identity

        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            entities = [
                (r["id"], r["kind"])
                for r in db.execute("SELECT id,kind FROM entities")
            ]
            resolver = identity.Resolver(entities, adapters)
            mapping = {}

            def note(value):
                if identity.synthetic(value):
                    mapping[value] = resolver.resolve(value)

            for entity_id, _ in entities:
                note(entity_id)
            records = [dict(r) for r in db.execute("SELECT id,sid,users FROM records")]
            for record in records:
                note(record["sid"])
                for user in json.loads(record["users"]):
                    note(user)
            facts = [
                dict(r)
                for r in db.execute(
                    "SELECT id,sid,subject,category,content,reason,scenario,"
                    "tags,relations,sources,fingerprint FROM facts WHERE deleted=0"
                )
            ]
            for fact in facts:
                note(fact["sid"])
                note(fact["subject"])
                for relation in json.loads(fact["relations"]):
                    note(relation.get("subject"))
                    note(relation.get("object"))
            if bindings:
                for old, value in bindings.items():
                    mapping[old] = value
            record_sid = {record["id"]: record["sid"] for record in records}
            overrides = {}
            for row in db.execute(
                "SELECT record_id,source_key FROM migration_items "
                "WHERE record_id IS NOT NULL"
            ):
                shape = identity.source_shape(row["source_key"])
                if not shape:
                    continue
                kind, adapter, number = shape
                hint = ""
                sid = record_sid.get(row["record_id"], "")
                if sid and not identity.synthetic(sid) and sid != identity.UNSCOPED:
                    name, _number, session = identity.split_adapter(sid)
                    if name and session:
                        hint = name
                if kind == "global":
                    overrides[row["record_id"]] = (
                        identity.GLOBAL,
                        identity.GLOBAL,
                        "global",
                    )
                elif kind == "self":
                    overrides[row["record_id"]] = (
                        identity.SELF,
                        identity.SELF,
                        "self",
                    )
                elif kind == "user":
                    overrides[row["record_id"]] = resolver.user(
                        adapter or hint, number
                    )
                else:
                    overrides[row["record_id"]] = resolver.group(
                        adapter or hint, number
                    )
            report = {
                "records": 0,
                "facts": 0,
                "entities": 0,
                "merged_facts": 0,
                "pending": [],
                "ambiguous": [],
            }
            record_new_sid = {}
            for record in records:
                sid, users = record["sid"], json.loads(record["users"])
                new_sid = mapping.get(sid, (sid, sid, ""))[1] or sid
                new_users = [mapping.get(u, (u, "", ""))[0] for u in users]
                override = overrides.get(record["id"])
                if override:
                    entity, entity_sid, kind = override
                    if kind == "user":
                        if entity_sid and entity_sid != identity.UNSCOPED:
                            new_sid = entity_sid
                        if entity and entity not in new_users:
                            new_users.append(entity)
                    else:
                        new_sid = entity_sid
                new_users = sorted(
                    {
                        user
                        for user in new_users
                        if user and not user.startswith(identity.PENDING)
                    }
                )
                record_new_sid[record["id"]] = new_sid
                if new_sid != sid or new_users != users:
                    db.execute(
                        "UPDATE records SET sid=?,users=? WHERE id=?",
                        (new_sid, dump(new_users), record["id"]),
                    )
                    report["records"] += 1
            for fact in facts:
                sid, subject = fact["sid"], fact["subject"]
                new_sid = mapping.get(sid, (sid, sid, ""))[1] or sid
                if identity.synthetic(sid) or sid == identity.UNSCOPED:
                    for source in json.loads(fact["sources"]):
                        candidate = record_new_sid.get(source)
                        if candidate and candidate != identity.UNSCOPED:
                            new_sid = candidate
                            break
                new_subject = mapping.get(subject, (subject, "", ""))[0]
                relations = json.loads(fact["relations"])
                new_relations = [
                    {
                        **relation,
                        "subject": mapping.get(
                            relation.get("subject"),
                            (relation.get("subject"), "", ""),
                        )[0],
                        "object": mapping.get(
                            relation.get("object"), (relation.get("object"), "", "")
                        )[0],
                    }
                    for relation in relations
                ]
                fingerprint = hashlib.sha256(
                    dump(
                        [
                            fact["category"],
                            new_subject,
                            fact["content"],
                            fact["reason"],
                            fact["scenario"],
                            new_relations,
                        ]
                    ).encode()
                ).hexdigest()
                if (
                    new_sid != sid
                    or new_subject != subject
                    or new_relations != relations
                    or fingerprint != fact["fingerprint"]
                ):
                    db.execute(
                        "UPDATE facts SET sid=?,subject=?,relations=?,fingerprint=?,"
                        "revision=revision+1 WHERE id=?",
                        (
                            new_sid,
                            new_subject,
                            dump(new_relations),
                            fingerprint,
                            fact["id"],
                        ),
                    )
                    report["facts"] += 1
            for old, (entity, _sid, kind) in mapping.items():
                if old == entity:
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO entities(id,kind,name,updated) "
                    "VALUES (?,?, '',0)",
                    (entity, kind or "user"),
                )
                if kind:
                    db.execute(
                        "UPDATE entities SET kind=? WHERE id=?", (kind, entity)
                    )
                db.execute(
                    "UPDATE entity_names SET entity_id=? WHERE entity_id=?",
                    (entity, old),
                )
                db.execute("DELETE FROM entities WHERE id=?", (old,))
                latest = db.execute(
                    "SELECT name FROM entity_names WHERE entity_id=? "
                    "ORDER BY observed DESC,id DESC LIMIT 1",
                    (entity,),
                ).fetchone()
                if latest and latest[0]:
                    db.execute(
                        "UPDATE entities SET name=? WHERE id=? "
                        "AND (name IS NULL OR name='')",
                        (latest[0], entity),
                    )
                report["entities"] += 1
            seen = {}
            for row in db.execute(
                "SELECT id,sid,subject,fingerprint,sources,tags FROM facts "
                "WHERE deleted=0 ORDER BY rowid"
            ):
                key = (row["sid"], row["subject"], row["fingerprint"])
                kept = seen.get(key)
                if kept is None:
                    seen[key] = {
                        "id": row["id"],
                        "sources": row["sources"],
                        "tags": row["tags"],
                    }
                    continue
                sources = sorted(
                    set(json.loads(kept["sources"]) + json.loads(row["sources"]))
                )
                tags = sorted(set(json.loads(kept["tags"]) + json.loads(row["tags"])))
                db.execute(
                    "UPDATE facts SET sources=?,tags=?,revision=revision+1 WHERE id=?",
                    (dump(sources), dump(tags), kept["id"]),
                )
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) "
                    "VALUES ('fact',?,?,?,?)",
                    (
                        row["id"],
                        dump(dict(row)),
                        "identity merge: identical fact",
                        time.time(),
                    ),
                )
                db.execute(
                    "UPDATE facts SET deleted=1,revision=revision+1 WHERE id=?",
                    (row["id"],),
                )
                kept["sources"], kept["tags"] = dump(sources), dump(tags)
                report["merged_facts"] += 1
            pending = sorted(
                {
                    entity
                    for _old, (entity, _sid, _kind) in mapping.items()
                    if entity.startswith(identity.PENDING)
                }
            )
            ambiguous = []
            for old, (entity, _sid, _kind) in mapping.items():
                if not entity.startswith(identity.PENDING):
                    continue
                shape = identity.legacy_shape(old) or identity.pending_shape(old)
                if not shape:
                    continue
                pool = resolver.users if shape[0] == "user" else resolver.groups
                if len(pool.get(shape[2], ())) > 1:
                    ambiguous.append(entity)
            report["pending"] = pending
            report["ambiguous"] = sorted(set(ambiguous))
            if any(
                report[key] for key in ("records", "facts", "entities", "merged_facts")
            ):
                self.bump(db)
            return report

    def refreshable_names(self, limit=200, include_named=False):
        """Entities that have a number and can be looked up, newest first.

        ``include_named=False`` keeps the original behaviour (only entities
        without a current name); the admin batch button can ask for all of
        them, but callers must still skip entities that already have a name
        when writing.
        """
        from . import identity

        result = []
        where = "" if include_named else "WHERE (name IS NULL OR name='')"
        with self.connect() as db:
            rows = db.execute(
                "SELECT id FROM entities %s ORDER BY updated DESC,id LIMIT ?" % where,
                (max(limit * 2, 400),),
            )
            for (entity_id,) in rows:
                if entity_id in (identity.GLOBAL, identity.SELF, identity.UNSCOPED):
                    continue
                _adapter, number, _session = identity.split_adapter(entity_id)
                if not number:
                    shape = identity.pending_shape(entity_id) or identity.legacy_shape(
                        entity_id
                    )
                    number = shape[2] if shape else ""
                if number:
                    result.append(entity_id)
                if len(result) >= limit:
                    break
        return result

    @staticmethod
    def _scope_clause(alias, scope, sid, users):
        """Visibility predicate shared by totals and top-user ranking."""
        if scope == "global":
            return "1=1", []
        if scope == "linked":
            return (
                f"({alias}.sid=? OR {alias}.visibility='global' OR EXISTS "
                f"(SELECT 1 FROM json_each({alias}.users) WHERE value IN "
                "(SELECT value FROM json_each(?))))",
                [sid, dump(list(users))],
            )
        return (
            f"({alias}.sid=? OR {alias}.visibility='global' OR "
            f"({alias}.visibility='user' AND EXISTS "
            f"(SELECT 1 FROM json_each({alias}.users) WHERE value IN "
            "(SELECT value FROM json_each(?)))))",
            [sid, dump(list(users))],
        )

    def totals(self, sid="", users=(), scope="session"):
        clause, args = self._scope_clause("records", scope, sid, users)
        with self.connect() as db:
            row = db.execute(
                "SELECT count(*) AS records, coalesce(sum(permanent),0) AS permanent, "
                f"count(DISTINCT sid) AS sessions FROM records WHERE deleted=0 AND {clause}",
                args,
            ).fetchone()
            people = db.execute(
                "SELECT count(DISTINCT value) FROM records, json_each(records.users) "
                f"WHERE records.deleted=0 AND {clause}",
                args,
            ).fetchone()[0]
            groups = db.execute(
                f"SELECT count(DISTINCT sid) FROM records WHERE deleted=0 AND {clause} "
                "AND instr(sid,':gm:')>0",
                args,
            ).fetchone()[0]
            source_clause, source_args = self._scope_clause("r", scope, sid, users)
            facts = db.execute(
                f"""SELECT count(*) FROM facts WHERE deleted=0 AND (
                  sid IN (SELECT sid FROM records WHERE deleted=0 AND {clause})
                  OR EXISTS(SELECT 1 FROM json_each(facts.sources) v JOIN records r
                            ON r.id=v.value WHERE r.deleted=0 AND {source_clause}))""",
                [*args, *source_args],
            ).fetchone()[0]
        return {
            "records": row["records"],
            "facts": facts,
            "users": people,
            "sessions": row["sessions"],
            "groups": groups,
            "permanent": row["permanent"],
        }

    def needs_tool_cleanup(self):
        with self.connect() as db:
            return not db.execute(
                "SELECT 1 FROM meta WHERE key='tools_cleanup'"
            ).fetchone()

    def cleanup_tool_records(self):
        """One-time repair for legacy tool payloads that flood the context.

        Self-recall echoes are soft-deleted (raw text stays in the database);
        other tool records keep their content but gain a readable summary.
        Idempotent: after the first pass nothing matches any more.
        """
        from .retrieval import (
            TOOL_RESULT_PREFIX,
            looks_like_memory_payload,
            tool_call_summary,
            tool_preview,
        )

        report = {"scanned": 0, "removed": 0, "rewritten": 0, "freed_chars": 0}
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,sid,role,summary,content FROM records WHERE deleted=0 AND "
                "(summary LIKE ? OR summary LIKE ?)",
                (TOOL_RESULT_PREFIX + "%", '%{"tool_calls"%'),
            ).fetchall()
            for row in rows:
                report["scanned"] += 1
                summary, content = row["summary"], row["content"]
                if looks_like_memory_payload(
                    summary
                ) or looks_like_memory_payload(content):
                    db.execute(
                        "INSERT INTO versions(kind,target,snapshot,reason,created) "
                        "VALUES ('record',?,?,?,?)",
                        (
                            row["id"],
                            dump(dict(row)),
                            "清理自身记忆读取回声",
                            time.time(),
                        ),
                    )
                    db.execute(
                        "UPDATE records SET deleted=1,revision=revision+1 WHERE id=?",
                        (row["id"],),
                    )
                    self._orphan_facts(db, row["id"])
                    report["removed"] += 1
                    report["freed_chars"] += len(summary)
                    continue
                if summary.startswith(TOOL_RESULT_PREFIX):
                    if len(summary) <= 400:
                        continue
                    new_summary = TOOL_RESULT_PREFIX + tool_preview(
                        summary[len(TOOL_RESULT_PREFIX) :]
                    )
                else:
                    text_part, _, blob = summary.partition("\n{")
                    label = ""
                    try:
                        label = tool_call_summary(
                            json.loads("{" + blob).get("tool_calls", [])
                        )
                    except (ValueError, TypeError, AttributeError):
                        label = ""
                    new_summary = (
                        (text_part.strip() + " ") if text_part.strip() else ""
                    ) + (label or "[调用工具]")
                if new_summary == summary:
                    continue
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) "
                    "VALUES ('record',?,?,?,?)",
                    (row["id"], dump(dict(row)), "工具记录摘要压缩", time.time()),
                )
                db.execute(
                    "UPDATE records SET summary=?,search_body=?,"
                    "revision=revision+1 WHERE id=?",
                    (new_summary, search_body_of(new_summary), row["id"]),
                )
                report["rewritten"] += 1
                report["freed_chars"] += max(0, len(summary) - len(new_summary))
            db.execute("INSERT OR REPLACE INTO meta VALUES ('tools_cleanup',1)")
            self.bump(db)
        return report

    def migration_status(self):
        with self.connect() as db:
            return {
                "reports": [
                    json.loads(r[0])
                    for r in db.execute(
                        "SELECT report FROM migration_reports ORDER BY source"
                    )
                ],
                "skips": [
                    dict(r)
                    for r in db.execute(
                        "SELECT source,reason,count(*) AS count FROM migration_items WHERE reason<>'' GROUP BY source,reason"
                    )
                ],
                "total_imported": db.execute(
                    "SELECT count(DISTINCT record_id) FROM migration_items"
                ).fetchone()[0],
            }

    def sessions(self):
        with self.connect() as db:
            return [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT sid FROM records WHERE active=1 AND deleted=0"
                )
            ]

    def get(self, record_id, include_deleted=False):
        clause = "" if include_deleted else " AND deleted=0"
        with self.connect() as db:
            result = self.row(
                db.execute(
                    "SELECT * FROM records WHERE id=?" + clause, (record_id,)
                ).fetchone()
            )
            if result:
                result["legacy_sources"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT source,source_key,digest,file_hash,metadata FROM migration_items WHERE record_id=?",
                        (record_id,),
                    )
                ]
                result["children"] = [
                    r[0]
                    for r in db.execute(
                        "SELECT child FROM edges WHERE parent=? ORDER BY ordinal",
                        (record_id,),
                    )
                ]
                result["parents"] = [
                    r[0]
                    for r in db.execute(
                        "SELECT parent FROM edges WHERE child=?", (record_id,)
                    )
                ]
                result["versions"] = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM versions WHERE kind='record' AND target=? ORDER BY id DESC",
                        (record_id,),
                    )
                ]
            return result

    def _add_fact(self, db, sid, fact):
        fingerprint = hashlib.sha256(
            dump(
                [
                    fact[k]
                    for k in (
                        "category",
                        "subject",
                        "content",
                        "reason",
                        "scenario",
                        "relations",
                    )
                ]
            ).encode()
        ).hexdigest()
        old = db.execute(
            "SELECT * FROM facts WHERE sid=? AND subject=? AND fingerprint=? AND deleted=0",
            (sid, fact["subject"], fingerprint),
        ).fetchone()
        if old:
            sources = sorted(set(json.loads(old["sources"]) + fact["source_ids"]))
            tags = sorted(set(json.loads(old["tags"]) + fact["tags"]))
            db.execute(
                "UPDATE facts SET sources=?,tags=?,importance=MAX(importance,?),"
                "revision=revision+1 WHERE id=?",
                (dump(sources), dump(tags), int(fact.get("importance", 5)), old["id"]),
            )
            return old["id"]
        new_id = uid()
        db.execute(
            """INSERT INTO facts(id,sid,category,subject,content,reason,scenario,tags,
           relations,sources,fingerprint,importance,created)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                new_id,
                sid,
                fact["category"],
                fact["subject"],
                fact["content"],
                fact["reason"],
                fact["scenario"],
                dump(fact["tags"]),
                dump(fact["relations"]),
                dump(fact["source_ids"]),
                fingerprint,
                int(fact.get("importance", 5)),
                time.time(),  # created = 入库时刻（不是事件时间 ✗）
            ),
        )
        # 事件时间区间：取来源记录的时间跨度（没有来源就留 0）
        ids = [str(x) for x in (fact.get("source_ids") or []) if x]
        if ids:
            span = db.execute(
                "SELECT min(start) AS a, max(start) AS b FROM records WHERE id IN (%s)"
                % ",".join("?" * len(ids)),
                ids,
            ).fetchone()
            if span and span["a"]:
                db.execute(
                    "UPDATE facts SET event_at=?, event_end=? WHERE id=?",
                    (float(span["a"]), float(span["b"] or span["a"]), new_id),
                )
        return new_id

    # ---- 短码：给模型看的证据编码 ----------------------------------------
    # 真实 id 形如 legacy-<64位十六进制>（71 字符）或 1-1772...-d8feeb77ac6e。
    # 又长又容易抄错，所以对外只给 6 位 base36 短码，两个方向都能查。
    SHORT_LEN = 6
    _SHORT_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"

    def short_id(self, real):
        """真实 id → 短码（幂等，落库持久化，重启不变）。"""
        real = str(real or "")
        if not real:
            return ""
        with self.connect() as db:
            row = db.execute(
                "SELECT short FROM short_ids WHERE real=?", (real,)
            ).fetchone()
            if row:
                return row["short"]
            for length in range(self.SHORT_LEN, self.SHORT_LEN + 4):
                for _ in range(8):
                    short = "".join(
                        random.choice(self._SHORT_ALPHABET) for _ in range(length)
                    )
                    taken = db.execute(
                        "SELECT 1 FROM short_ids WHERE short=?", (short,)
                    ).fetchone()
                    if taken:
                        continue
                    db.execute(
                        "INSERT INTO short_ids(short,real,created) VALUES (?,?,?)",
                        (short, real, time.time()),
                    )
                    return short
            return real  # 极端情况：回退到真实 id，功能不受影响

    def spaced_names(self, limit=200):
        """已登记、且名字里真的带空白的名（昵称可以是「星 月」）。

        渲染给模型看之前把这类名字保护起来，空白归一就不会误合并它们。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT name FROM entity_names "
                "WHERE length(name)>=2 AND instr(name,' ')>0 LIMIT ?",
                (limit,),
            ).fetchall()
            rows += db.execute(
                "SELECT name FROM entities WHERE length(name)>=2 AND instr(name,' ')>0 LIMIT ?",
                (limit,),
            ).fetchall()
        return sorted({r[0] for r in rows if r[0]}, key=len, reverse=True)

    def real_id(self, value):
        """短码或真实 id → 真实 id；两种都认，找不到就原样返回。"""
        value = str(value or "")
        if not value:
            return ""
        with self.connect() as db:
            row = db.execute(
                "SELECT real FROM short_ids WHERE short=?", (value,)
            ).fetchone()
            if row:
                return row["real"]
            row = db.execute(
                "SELECT real FROM short_ids WHERE real=?", (value,)
            ).fetchone()
            return row["real"] if row else value

    def distilled_only(self, sid, ids):
        """这批记录是否**全部**属于「迁移导入 + 已提炼过知识」✓（方案 B 的判据 ✓）

        判据（对**存量用户**同样有效 ✗✓ 不依赖任何新列 ✓）：
          · 记录在 `migration_items` 里（= 迁移来的 ✓）
          · **同一个来源 key** 已经产生过一条非 event 事实
            （按 key 而不是 record_id ✗✓：重复导入时**首次那条**才写了事实 ✓
             而重复项**记录照写、事实不写** ✗ 按 record_id 判会漏掉它们 ⇒ 白走压缩 ✓）
          · 那条事实的 category **不是 event** ✗（经历类仍需模型做叙事摘要 ✓）
        三条都满足 ⇒ 知识已经在事实层 ✓ 再压一遍纯属重复花钱 ✗
        """
        if not ids:
            return False
        marks = ",".join("?" * len(ids))
        with self.connect() as db:
            hit = db.execute(
                "SELECT count(DISTINCT r.id) FROM records r "
                " JOIN migration_items mi ON mi.record_id = r.id "
                " WHERE r.sid = ? AND r.id IN (%s) "
                "   AND EXISTS ("
                "     SELECT 1 FROM migration_items mi2 "
                "       JOIN facts f ON f.deleted = 0 AND f.category != 'event' "
                "       JOIN json_each(f.sources) s ON s.value = mi2.record_id "
                "      WHERE mi2.source_key = mi.source_key)" % marks,
                [sid, *ids],
            ).fetchone()[0]
        return int(hit) == len(set(ids))

    def archive_distilled(self, sid, candidates):
        """只归档（active=0）**不调模型** ✗✓ —— 知识已在事实层 ✓ 原文仍可按 ID 检索 ✓

        ⚠️ 不创建上层摘要 ✓（这正是省掉的那次模型调用 ✓）
        合并 / 审计**不受影响** ✗：合并由 job 包装层（`queue_fact_merges` ✓）
        与召回路径触发 ✓ 审计由 scheduler 按会话跑 ✓ 都与本路径无关 ✓✓
        """
        if not candidates:
            return 0
        changed = 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in candidates:
                cur = db.execute(
                    "UPDATE records SET active=0, revision=revision+1 "
                    " WHERE id=? AND sid=? AND active=1 AND deleted=0",
                    (row["id"], sid),
                )
                changed += cur.rowcount or 0
            db.commit()
        return changed

    def compress(self, sid, candidates, level, output):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for row in candidates:
                current = db.execute(
                    "SELECT revision,active,deleted FROM records WHERE id=? AND sid=?",
                    (row["id"], sid),
                ).fetchone()
                if not current or current["deleted"] or not current["active"]:
                    raise Conflict("source changed during compression")
                if current["revision"] != row["revision"]:
                    # v2.18.74：语义化守卫（同 classify 的修法）——
                    # 抽事实（_add_fact）/补索引都会 bump revision 但**不改正文**，
                    # 以前一律判死 ⇒ 归类一成功、压缩计划就失效 ✗（互相打架）
                    # 比较「压缩会覆盖的字段」（正文 + 摘要）：
                    #   抽事实/补索引只 bump revision ⇒ 容忍
                    #   用户手改过 summary/content ⇒ 仍然拒绝（不许被模型输出覆盖）
                    fresh = db.execute(
                        "SELECT content,summary FROM records WHERE id=?", (row["id"],)
                    ).fetchone()
                    if fresh and (
                        (fresh["content"] or "") != (row.get("content") or "")
                        or (fresh["summary"] or "") != (row.get("summary") or "")
                    ):
                        raise Conflict("source changed during compression")
            ids = {r["id"] for r in candidates}
            widen_source_ids(output["facts"], candidates)  # 补齐漏列来源（见模块级函数）✓
            if any(not set(f["source_ids"]) <= ids for f in output["facts"]):
                raise ValueError("unknown source id")
            start, end = (
                min(r["start"] for r in candidates),
                max(r["end"] for r in candidates),
            )
            archive_id = f"{level}-{int(start * 1000)}-{int(end * 1000)}-{uid()[:12]}"
            content = dump(
                [
                    {
                        "id": r["id"],
                        "role": r["role"],
                        "level": r["level"],
                        "content": r["summary"],
                    }
                    for r in candidates
                ]
            )
            users = sorted({u for r in candidates for u in r["users"]})
            db.execute(
                """INSERT INTO records(id,sid,role,level,start,end,summary,content,users,position,created,search_body)
               VALUES (?,?,'assistant',?,?,?,?,?,?,?,?,?)""",
                (
                    archive_id,
                    sid,
                    level,
                    start,
                    end,
                    # v2.18.19：这一批若被**字符上限截断** ✗（可能切在轮中间 ✓）
                    # ⇒ 摘要尾部加 `…` ✓ 表示"后面还有" ✓
                    #    用省略号而不是「（续）」✓ —— 更短 ✓ 且沿用仓库既有的截断约定 ✓
                    #    已以 `…` 结尾就不重复加 ✗（模型自己可能写了 ✓）
                    # ⚠️ 内联表达式 ✗ 不要另起变量 ✓（定义与使用曾在不同作用域炸过 ✓）
                    str(output["summary"])
                    + (
                        "…"
                        if any(r.get("_partial") for r in candidates)
                        and not str(output["summary"]).rstrip().endswith("…")
                        else ""
                    ),
                    content,
                    dump(users),
                    min(r["position"] for r in candidates),
                    time.time(),
                    search_body_of(output["summary"]),
                ),
            )
            visibility = {r.get("visibility", "session") for r in candidates}
            if len(visibility) != 1:
                raise ValueError("mixed visibility cannot be compressed")
            db.execute(
                "UPDATE records SET visibility=? WHERE id=?",
                (visibility.pop(), archive_id),
            )
            archived_at = time.time()
            for index, row in enumerate(candidates):
                db.execute(
                    "INSERT INTO edges VALUES (?,?,?)", (archive_id, row["id"], index)
                )
                db.execute(
                    "UPDATE records SET active=0,revision=revision+1,"
                    "archived_at=CASE WHEN archived_at>0 THEN archived_at ELSE ? END "
                    "WHERE id=?",
                    (archived_at, row["id"]),
                )
                db.execute(
                    "UPDATE vectors SET revision=revision+1 WHERE id=?", (row["id"],)
                )
            for fact in output["facts"]:
                self._add_fact(db, sid, fact)
            self.bump(db)
            return archive_id

    def memorize(self, sid, content, users, start, end, importance=8, category=""):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._ensure_entities(db, sid, users)
            record_id = f"100-{int(start * 1000)}-{int(end * 1000)}-{uid()[:12]}"
            db.execute(
                """INSERT INTO records(id,sid,role,level,start,end,summary,content,users,
              permanent,position,created,importance,category,search_body)
              VALUES (?,?,'assistant',100,?,?,?,?,?,1,0,?,?,?,?)""",
                (
                    record_id,
                    sid,
                    start,
                    end,
                    content,
                    content,
                    dump(users),
                    time.time(),
                    int(importance),
                    str(category or ""),
                    search_body_of(content),
                ),
            )
            self.bump(db)
            return record_id

    def find_permanent(self, sid, content):
        """Exact (normalised) duplicate among this session's permanent memories."""
        from .retrieval import normalize_text

        target = normalize_text(content)
        with self.connect() as db:
            for row in db.execute(
                "SELECT * FROM records WHERE sid=? AND permanent=1 AND deleted=0 "
                "ORDER BY end DESC,id",
                (sid,),
            ):
                if normalize_text(row["content"]) == target:
                    return self.row(row)
        return None

    def permanent_records(self, sid="", all_sessions=False):
        """Active permanent memories, newest first.

        ``all_sessions`` 用于 recall_scope=global：注入本来就是全局的
        （任何会话都在付所有会话的永久记忆），去重与整理也应看到同一个池子。
        """
        clause = "permanent=1 AND deleted=0 AND active=1"
        args = ()
        if not all_sessions:
            clause += " AND sid=?"
            args = (sid,)
        with self.connect() as db:
            return [
                self.row(row)
                for row in db.execute(
                    "SELECT * FROM records WHERE " + clause + " ORDER BY end DESC,id",
                    args,
                )
            ]

    def untidyable_ids(self, ids):
        """指定的这几条里，**不参与整理**的（冷归档 / 已停用 / 已删除 / 不存在）✓

        ⚠️ 2026-09-19（用户实测）：单条「重新提取事实」点到**冷归档**的条 ⇒
        引擎只会回"指定的永久记忆不在可整理集合里，本次跳过" ✗
        （前端已隐藏按钮 ✓ 这里再兜一层 ⇒ bot / 手搓请求得到明确 400 ✓）
        可整理集合 = ``tidy_candidates`` 的 WHERE：permanent=1 AND deleted=0 AND active=1 AND cold=0 ✓
        """
        ids = [str(i) for i in (ids or []) if i]
        if not ids:
            return []
        ph = ",".join("?" * len(ids))
        with self.connect() as db:
            ok = {
                str(r[0])
                for r in db.execute(
                    "SELECT id FROM records WHERE id IN (" + ph + ") "
                    "AND permanent=1 AND deleted=0 AND active=1 AND cold=0",
                    tuple(ids),
                )
            }
        return [i for i in ids if i not in ok]

    def sessions_with_any_permanent(self):
        """有意久记忆的会话（哪怕只有一条）——跨会话去重需要它们都能被扫到。"""
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT sid FROM records WHERE permanent=1 AND deleted=0 "
                    "AND active=1"
                )
            ]

    def repair_synthetic_names(self):
        """Undo nicknames written by third-party synthetic messages.

        A reminder plugin publishes messages whose sender nickname is
        "提醒任务所有者"; observing those overwrote the real nickname. Restore
        the most recent non-synthetic name from history and record the repair.
        """
        from .retrieval import SYNTHETIC_NAMES

        synthetic = dump(sorted(SYNTHETIC_NAMES))
        repaired = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id,name FROM entities WHERE name IN "
                "(SELECT value FROM json_each(?))",
                (synthetic,),
            ).fetchall()
            for row in rows:
                previous = db.execute(
                    "SELECT name FROM entity_names WHERE entity_id=? AND name<>'' "
                    "AND name NOT IN (SELECT value FROM json_each(?)) "
                    "ORDER BY observed DESC,id DESC LIMIT 1",
                    (row["id"], synthetic),
                ).fetchone()
                if not previous or not previous["name"]:
                    continue
                db.execute(
                    "UPDATE entities SET name=?,updated=? WHERE id=?",
                    (previous["name"], time.time(), row["id"]),
                )
                db.execute(
                    "INSERT INTO entity_names(entity_id,name,source,context,"
                    "observed,reason) VALUES (?,?,?,?,?,?)",
                    (
                        row["id"],
                        previous["name"],
                        "repair",
                        "",
                        time.time(),
                        "忽略第三方插件的合成昵称",
                    ),
                )
                repaired.append({"id": row["id"], "name": previous["name"]})
            if repaired:
                self.bump(db)
        return repaired

    def permanent_stats(self, sid):
        """Active vs archived permanent memories, for diagnostics."""
        with self.connect() as db:
            row = db.execute(
                "SELECT sum(CASE WHEN active=1 AND cold=0 THEN 1 ELSE 0 END) AS live, "
                "sum(CASE WHEN active=0 OR cold=1 THEN 1 ELSE 0 END) AS archived "
                "FROM records WHERE sid=? AND permanent=1 AND deleted=0",
                (sid,),
            ).fetchone()
        return {"live": row["live"] or 0, "archived": row["archived"] or 0}

    def sessions_by_age(self):
        """按「**最老活跃记录**的 start」升序返回会话 ✓（越老越先处理 ✓）

        2026-09-17 用户要求（方案 C ✓）：原来 `queue_compress_all` 用
        `sorted(sessions)` ✗ = **字母序** ⇒ 挑出来的 8 个跟"谁更需要压"无关 ✓
        换成这个 ⇒ 每次扫描都从**真正最陈旧**的会话开始 ✓
        """
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT sid FROM records WHERE active=1 AND deleted=0 "
                    "GROUP BY sid ORDER BY min(COALESCE(start, created)) ASC, sid"
                )
            ]

    def prune_jobs(self, keep_days=7, keep_min=50):
        """清掉过期的历史任务 ✓（工作台"工作明细"不再**无限增长** ✓ 方案 A ✓）

        只删**历史记录** ✗ 不动任何业务数据 ✓✓
        · `queued` / `running` 的**一律不碰** ✓（正在跑的绝不能删 ✓）
        · 至少保留最近 `keep_min` 条 ✓（免得列表被清空 ✓）
        · 顺带清掉孤儿 job_items ✓
        """
        cutoff = time.time() - max(1, int(keep_days)) * 86400
        keep = max(1, int(keep_min))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM jobs WHERE state NOT IN ('queued','running') "
                "AND id NOT IN (SELECT id FROM jobs ORDER BY updated DESC LIMIT ?)",
                (keep,),
            )
            n = db.execute(
                "DELETE FROM jobs WHERE state NOT IN ('queued','running') AND updated < ?",
                (cutoff,),
            ).rowcount
            db.execute(
                "DELETE FROM job_items WHERE job_id NOT IN (SELECT id FROM jobs)"
            )
            db.commit()
        return int(n or 0)

    def sessions_by_audit_age(self, limit, recheck_seconds=0, reserve_buckets=1, bucket_sids=()):
        """Sessions with audit-eligible facts, most stale first.

        `reserve_buckets` ✓：给「**无归属的桶**」（`legacy:global` / `legacy:unscoped`）
        **预留**至多这么多名额 ✓ 让它们有机会被审计归类 ✓（方案 A ✓ 2026-09-17 用户要求 ✓）

        ⚠️ **安全约束（关键 ✓）**：迁移导入的桶可能有**几千条**事实 ✗
        如果简单地"永远排最前" ⇒ **普通会话会被饿死** ✗✓
        （引擎里本来就写着 "a big imported backlog cannot keep the queue permanently busy" ✓）
        所以这里只**限量预留** ✓ 其余名额**照旧**按陈旧度给普通会话 ✓
        桶一条都没有时 ⇒ 返回值与改动前**完全一致** ✓（零行为变化 ✓）
        """
        where = ["deleted=0", "merge_pending=0"]
        args = []
        if recheck_seconds > 0:
            where.append("(audited=0 OR audited < ?)")
            args.append(time.time() - recheck_seconds)
        clause = " AND ".join(where)
        limit = max(1, int(limit))
        with self.connect() as db:
            normal = [
                row[0]
                for row in db.execute(
                    "SELECT sid FROM facts WHERE " + clause + " GROUP BY sid "
                    "ORDER BY min(audited) ASC, sid LIMIT ?",
                    [*args, limit],
                )
            ]
            reserved = []
            if reserve_buckets and bucket_sids:
                marks = ",".join("?" * len(bucket_sids))
                reserved = [
                    row[0]
                    for row in db.execute(
                        "SELECT sid FROM facts WHERE " + clause + " AND sid IN (%s) "
                        "GROUP BY sid ORDER BY min(audited) ASC, sid LIMIT ?" % marks,
                        [*args, *bucket_sids, max(1, int(reserve_buckets))],
                    )
                ]
        out = []
        for sid in [*reserved, *normal]:
            if sid not in out:
                out.append(sid)
        return out[:limit]

    def sessions_with_permanents(self):
        with self.connect() as db:
            return [
                row[0]
                for row in db.execute(
                    "SELECT sid FROM records WHERE permanent=1 AND deleted=0 "
                    "AND active=1 GROUP BY sid HAVING count(*)>1"
                )
            ]

    def merge_records(self, target_id, source_ids, content, reason):
        """Fold similar permanent memories into the newest one; originals stay."""
        ids = list(dict.fromkeys(source_ids))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {}
            for record_id in ids:
                row = db.execute(
                    "SELECT * FROM records WHERE id=? AND deleted=0", (record_id,)
                ).fetchone()
                if not row or not row["permanent"]:
                    raise ValueError("invalid merge source")
                rows[record_id] = row
            if target_id not in rows:
                raise ValueError("invalid merge target")
            sid = rows[target_id]["sid"]
            if any(row["sid"] != sid for row in rows.values()):
                raise ValueError("cross-session merge is forbidden")
            for record_id, row in rows.items():
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) "
                    "VALUES ('record',?,?,?,?)",
                    (record_id, dump(dict(row)), reason, time.time()),
                )
            # Summary is what the model sees; content keeps the archive text.
            db.execute(
                "UPDATE records SET summary=?,search_body=?,"
                "revision=revision+1 WHERE id=?",
                (content, search_body_of(content), target_id),
            )
            folded_at = time.time()
            for record_id in ids:
                if record_id == target_id:
                    continue
                db.execute(
                    "UPDATE records SET active=0,cold=1,archived_at=?,"
                    "revision=revision+1 WHERE id=?",
                    (folded_at, record_id),
                )
                db.execute("DELETE FROM vectors WHERE id=?", (record_id,))
            self.bump(db)
        return {"target": target_id, "folded": len(ids)}

    def edit(self, kind, target, revision, patch, reason):
        table = "records" if kind == "record" else "facts"
        allowed = (
            {"summary", "active", "deleted", "category", "importance"}
            if kind == "record"
            else {
                "content",
                "category",
                "subject",
                "reason",
                "scenario",
                "tags",
                "relations",
                "deleted",
            }
        )
        if not patch or not set(patch) <= allowed:
            raise ValueError("invalid editable fields")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute(
                f"SELECT * FROM {table} WHERE id=?", (target,)
            ).fetchone()
            if not old or old["revision"] != revision:
                raise Conflict("record changed; reload before saving")
            # v2.18.66：撤回后**还原**必须可行 ✗ —— 以前这里写死 `AND deleted=0` ✗
            # ⇒ 已删除/已撤回的那条永远查不到 ⇒ 走「还原」必然 Conflict ✗（真跑踩到 ✓）
            # 安全边界保留：patch 里没有明确的 `deleted: False` 时，仍不许改已删除的条目 ✓
            if old["deleted"] and patch.get("deleted") is not False:
                raise Conflict("already deleted; restore it first")
            if kind == "record" and "active" in patch and not old["permanent"]:
                raise ValueError(
                    "only permanent memories can leave/rejoin context manually"
                )
            db.execute(
                "INSERT INTO versions(kind,target,snapshot,reason,created) VALUES (?,?,?,?,?)",
                (kind, target, dump(dict(old)), reason, time.time()),
            )
            updates = dict(patch)
            if kind == "record" and "summary" in updates:
                # 摘要改了，索引体必须跟着改：否则 FTS 里留的还是旧文字，
                # 这条记录既不会出现在命中集里、又不满足 search_body='' 的兜底条件
                # → 之后词面检索会静默漏掉它（直到下次启动回填）。
                updates["search_body"] = search_body_of(updates["summary"])
            if kind == "record" and "active" in patch:
                # Forget means "keep the archive but stop surfacing it"; restore revives it.
                if patch["active"]:
                    updates.update(cold=0, archived_at=0.0)
                else:
                    updates.update(cold=1, archived_at=time.time())
            values = [
                dump(v) if isinstance(v, list) else v for v in updates.values()
            ]
            db.execute(
                f"UPDATE {table} SET {','.join(k + '=?' for k in updates)},revision=revision+1 WHERE id=?",
                (*values, target),
            )
            if kind == "fact":
                db.execute("UPDATE facts SET fingerprint=? WHERE id=?", (uid(), target))
            else:
                db.execute("DELETE FROM vectors WHERE id=?", (target,))
                if patch.get("deleted"):
                    self._orphan_facts(db, target)
            self.bump(db)

    def restore(self, kind, target, version_id, revision):
        """Roll a record/fact back to a stored snapshot (itself versioned)."""
        table = "records" if kind == "record" else "facts"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                f"SELECT * FROM {table} WHERE id=?", (target,)
            ).fetchone()
            if not current or current["revision"] != revision:
                raise Conflict("target changed; reload before restoring")
            snapshot = db.execute(
                "SELECT snapshot FROM versions WHERE id=? AND kind=? AND target=?",
                (version_id, kind, target),
            ).fetchone()
            if not snapshot:
                raise ValueError("version not found")
            data = json.loads(snapshot["snapshot"])
            columns = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            updates = {
                k: v
                for k, v in data.items()
                if k in columns and k not in ("id", "revision")
            }
            if not updates:
                raise ValueError("nothing to restore")
            db.execute(
                "INSERT INTO versions(kind,target,snapshot,reason,created) VALUES (?,?,?,?,?)",
                (kind, target, dump(dict(current)), "恢复前存档", time.time()),
            )
            if kind == "record" and "summary" in updates:
                # 恢复的是旧快照，索引体必须按恢复后的摘要重算，
                # 否则 FTS 外部内容表会因"与索引时不一致"报错
                updates["search_body"] = search_body_of(updates["summary"])
            values = [dump(v) if isinstance(v, (list, dict)) else v for v in updates.values()]
            db.execute(
                f"UPDATE {table} SET {','.join(k + '=?' for k in updates)},"
                "revision=revision+1 WHERE id=?",
                (*values, target),
            )
            if kind == "fact":
                db.execute(
                    "UPDATE facts SET fingerprint=?,merge_pending=0 WHERE id=?",
                    (uid(), target),
                )
            else:
                db.execute("DELETE FROM vectors WHERE id=?", (target,))
            self.bump(db)
            return {"kind": kind, "target": target, "restored_from": version_id}

    def profile(self, entity_id, summary_count=3, sid="", global_scope=True, hide_pending=False):
        """Aggregated view of one entity: names, facts by category, relations, stats."""
        rows = self.entities(ids=[entity_id], limit=1)
        if not rows:
            return None
        entity = rows[0]
        where, args = ["deleted=0", "subject=?"], [entity_id]
        if hide_pending:
            where.append("merge_pending=0")
        if not global_scope and sid:
            where.append("sid=?")
            args.append(sid)
        with self.connect() as db:
            facts = [
                self.row(r)
                for r in db.execute(
                    "SELECT * FROM facts WHERE " + " AND ".join(where)
                    + " ORDER BY importance DESC,audited DESC,id",
                    args,
                )
            ]
            relations = []
            for fact in facts:
                for rel in fact["relations"]:
                    if rel.get("subject") == entity_id or rel.get("object") == entity_id:
                        relations.append({**rel, "fact_id": fact["id"]})
            sessions = {
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT sid FROM facts WHERE "
                    + " AND ".join(where),
                    args,
                )
            }
            last_active = db.execute(
                "SELECT max(start) FROM records WHERE deleted=0 AND users LIKE ?",
                ("%" + entity_id + "%",),
            ).fetchone()[0]
        groups = {}
        for fact in facts:
            groups.setdefault(fact["category"], []).append(fact)
        summary = [f["content"] for f in facts[:summary_count]]
        return {
            "entity": {
                "id": entity["id"],
                "kind": entity["kind"],
                "name": entity["name"],
                "revision": entity["revision"],
                "lookup_id": entity.get("lookup_id", ""),
                "label": entity.get("label", ""),
                "aliases": list(
                    dict.fromkeys(
                        h["name"] for h in entity["history"] if h["name"] != entity["name"]
                    )
                )[:10],
                "history": entity["history"][:20],
            },
            "summary": summary,
            "categories": groups,
            "relations": relations,
            "stats": {
                "facts": len(facts),
                "relations": len(relations),
                "sessions": len(sessions),
                "last_active": last_active or 0,
            },
        }

    @staticmethod
    def _orphan_facts(db, record_id):
        """Derived facts with no remaining live evidence must not be recalled."""
        for fact in db.execute(
            """SELECT * FROM facts WHERE deleted=0
              AND EXISTS(SELECT 1 FROM json_each(facts.sources) WHERE value=?)
              AND NOT EXISTS(SELECT 1 FROM records,json_each(facts.sources) src
                             WHERE records.id=src.value AND records.deleted=0)""",
            (record_id,),
        ).fetchall():
            db.execute(
                "INSERT INTO versions(kind,target,snapshot,reason,created) "
                "VALUES ('fact',?,?,?,?)",
                (fact["id"], dump(dict(fact)), "source deleted", time.time()),
            )
            db.execute(
                "UPDATE facts SET deleted=1,revision=revision+1 WHERE id=?",
                (fact["id"],),
            )

    def _drop_media_only(self, items, include_tools=False):
        """v2.18.12：把"只有引用壳/媒体、没有实质文字"的记录也滤掉 ✓

        `category="media"` 只管**新写入**的记录 ✗ 这里兜住两件事：
        ① **存量**记录（升级前就存进去的贴纸/图片描述 ✓ 日志实测过 ✓）
        ② 套着 `[Reply …]` / `↩N` 壳的媒体 ✓（旧判定漏掉的正是这种 ✓）

        v2.18.19 追加：**工具步**默认也滤掉 ✓
        工具步对**主模型**是过程噪声 ✗（bot 主被动召回 + 查档案都搜不到 ✓）
        但**压缩侧不排除** ✓（转 `[工具调用]` 占位 ✓）⇒ 所以只在这条**召回**路上过滤 ✓
        前端的"显示工具步"开关会以 `include_tools=True` 调进来 ✓
        """
        from .retrieval import is_tool_step, media_only

        names = self.known_names()
        return [
            r
            for r in items
            if (include_tools or not is_tool_step(r))
            and not media_only(str(r.get("content") or ""), names)
        ]

    def known_names(self, ttl=60):
        """已知成员名（**缓存 60 秒** ✗ 不加缓存的话每次检索都要查库 ✓ 那才是真拖慢 ✓）

        v2.18.14：`@X` 里 X 是不是"已知的 at"靠它判 ✓
        缓存过期最多让**新成员**的 at 被当成正文 ✓（安全方向 ✓ 无害 ✓）
        """
        now = time.time()
        cache = getattr(self, "_names_cache", None)
        if cache and now - cache[0] < ttl:
            return cache[1]
        with self.connect() as db:
            names = {
                str(r[0]) for r in db.execute("SELECT name FROM entities WHERE name<>''")
            }
        self._names_cache = (now, names)
        return names

    def _rerank(self, out, lexical, want, offset):
        """把"查询词同段出现"的行顶上来 ✓（**只重排，不增删** ✓ 结果条数不变 ✓）

        · 只处理第一页（offset=0 ✓）—— 带 offset 时重排会让翻页语义错乱 ✗
        · 同分时用**确定性**决胜：邻近 → 重要度 → 新近 → id（不再"随机" ✓）
        · 绝不过滤任何行 ✗ ⇒ 不会让召回变少 ✓
        """
        try:
            if offset or not lexical:
                return out
            items = out.get("items") or []
            if len(items) < 2:
                return out
            from .retrieval import score_tokens as _st

            toks = _st(lexical)
            if len(toks) < 2:
                return out          # 单词查询没有"同现"可言 ⇒ 原样返回 ✓

            # ★ 关键：**没有任何一条"同现" ⇒ 原样返回** ✓✓
            #   因为既有排序里还带着 prefer_sid（同分让常驻优先 ✓）、
            #   tier、翻页等语义 ✗ —— 那些都是**判据守着**的契约 ✓
            #   一动手就可能把它们弄坏 ✗（实测：prefer_sid 与翻页两条判据当场报红 ✓）
            bonus = []
            for it in items:
                txt = "%s %s" % (it.get("summary") or "", it.get("content") or "")
                bonus.append(proximity_bonus(txt, toks))
            if not any(bonus):
                return out

            # 有同现 ⇒ 只把"同现"的行上提 ✓
            #   用**稳定排序** ✓（sorted 天然稳定 ✓）⇒ 其余行的相对顺序**分毫不动** ✓
            order = sorted(range(len(items)), key=lambda i: (-bonus[i], i))
            out["items"] = [items[i] for i in order][:want]
            out["reranked"] = True
        except Exception:
            pass            # 重排只是优化 ✓ 失败就退回原顺序 ✓ 绝不影响检索 ✓
        return out

    def search(
        self,
        sid="",
        keyword="",
        include_tools=False,
        level=None,
        start=None,
        end=None,
        offset=0,
        limit=30,
        subject="",
        scope="session",
        users=(),
        active=False,
        vector=None,
        model="",
        lexical="",
        expand=None,
        exclude_sid="",
        exclude_ids=(),
        prefer_sid="",
        prefer_users=(),
        cold_after_days=0,
        include_cold=False,
        strict_session=False,
        skip_media=True,
    ):
        clauses, args = ["deleted=0"], []
        if skip_media:
            # v2.18.9：只有表情/图片的消息默认不进召回 ✗（纯占上下文 ✓）
            # 数据仍在库里 ✓ 关掉开关即可召回 ✓
            clauses.append("category != 'media'")
        if not include_cold:
            clauses.append("cold=0")
            if cold_after_days:
                # Archives fade out of search after the configured storage age.
                clauses.append(
                    "NOT (active=0 AND archived_at>0 AND archived_at<=?)"
                )
                args.append(time.time() - cold_after_days * 86400)
        if exclude_ids:
            clauses.append("id NOT IN (SELECT value FROM json_each(?))")
            args.append(dump(list(exclude_ids)))
        if exclude_sid:
            clauses.append("sid<>?")
            args.append(exclude_sid)
        if strict_session and sid:
            # Admin browsing: show exactly this session, not the global recall view.
            clauses.append("sid=?")
            args.append(sid)
        elif scope == "session":
            clauses.append(
                "(sid=? OR visibility='global' OR (visibility='user' AND EXISTS (SELECT 1 FROM json_each(records.users) WHERE value IN (SELECT value FROM json_each(?)))) )"
            )
            args.extend([sid, dump(list(users))])
        elif scope == "linked":
            clauses.append(
                "(sid=? OR visibility='global' OR EXISTS (SELECT 1 FROM json_each(records.users) WHERE value IN (SELECT value FROM json_each(?))))"
            )
            args.extend([sid, dump(list(users))])
        if keyword:
            # 两侧都做空白归一：库里存着「翅 膀」时，用「翅膀」也要搜得到（反之亦然）
            clauses.append("""(instr(lower(squeeze(summary)),lower(squeeze(?)))>0 OR EXISTS
              (SELECT 1 FROM entity_names n WHERE
              instr(lower(squeeze(n.name)),lower(squeeze(?)))>0
              AND (n.entity_id=records.sid OR n.entity_id IN (SELECT value FROM json_each(records.users)))))""")
            args.extend([keyword, keyword])
        if level is not None:
            clauses.append("level=?")
            args.append(level)
        if start is not None:
            clauses.append("end>=?")
            args.append(start)
        if end is not None:
            clauses.append("start<=?")
            args.append(end)
        if active:
            clauses.append("active=1")
        if subject:
            clauses.append(
                "EXISTS (SELECT 1 FROM json_each(records.users) WHERE value=?)"
            )
            args.append(subject)
        tier_sql, tier_args = "", []
        if prefer_sid or prefer_users:
            # Session affinity only reorders; the recall scope stays untouched.
            tier_sql = (
                "CASE WHEN sid=? THEN 0 WHEN EXISTS(SELECT 1 FROM json_each(records.users) "
                "WHERE value IN (SELECT value FROM json_each(?))) THEN 1 "
                "WHEN visibility='global' THEN 2 ELSE 3 END, "
            )
            tier_args = [prefer_sid, dump(list(prefer_users))]
        with self.connect() as db:
            # ★ 2026-09-20 观测：记录本轮**走哪条取数分支**（纯日志 ✓ 不影响逻辑）
            logger.debug("[recall] 分支=%s lexical=%r vector=%s fts_state=%s",
                         "lexical" if (lexical and not vector) else ("vector" if vector else "none"),
                         (lexical or "")[:40], bool(vector), self._fts_state)
            from .retrieval import squeeze

            db.create_function("squeeze", 1, squeeze)
            if lexical and not vector:
                from .retrieval import query_tokens

                # 打分整段下推到 SQL：与逐行 Python 打分口径完全一致
                # （score = Σ 命中词元的长度），但全程在 C 层跑——
                # 此前 relevance() 每行都要重切一次查询词元，是秒级开销的来源。
                # 词元不再截断：原来 [:24] 会让长消息静默少召回。
                # v2.18 第5项：查询词过一层"库里已有线索"的扩展（实体别名 + 已核实关系客体）
                #   门控：**只在该查询词元很少时**启动（这正是召回变窄的场景）✗
                #   扩展词全部有库内出处 ✓ 只**补**词元、不改打分口径 ✓
                _expand_on = getattr(self, "_expand_enabled", True) if expand is None else bool(expand)
                if _expand_on:
                    base_tokens = query_tokens(lexical)
                    if 0 < len(base_tokens) <= 3:
                        # 扩展是**增益**，绝不能拖垮召回 ✗ —— 任何异常都退回不扩（实测过空串 relations 会抛错）
                        try:
                            extra = self.expand_query(lexical)
                        except Exception:
                            extra = []
                        if extra:
                            lexical = lexical + " " + " ".join(extra)
                # ★ 2026-09-19（判据抓到的真 bug）：原来**同一个 tokens 喂三处** ✗
                #   → 过滤（要"能查到"= 索引超集 ✓）、打分（要"排序准"= 词级 ✓）、FTS 预筛
                #   ⇒ 拆开 ✓：**过滤/FTS 继续按字** ✓（保住不漏召回 ✓）、
                #     **打分改用词级** ✓（治「761 命中挑不出答案」✓）
                from .retrieval import score_tokens as _score_tokens

                filt_tokens = query_tokens(lexical)          # 过滤：按字（超集 ✓ 不动）
                score_toks = _score_tokens(lexical)          # 打分：词级 ✓
                clauses.append("(%s)>0" % _lexical_sql("lower(summary)", filt_tokens))
                # 排序用词级表达式（下面 ORDER BY 引用 lex_sql 时用这个 ✓）
                lexical_sql = _lexical_sql("lower(summary)", score_toks)
                hits = self._fts_hits(filt_tokens)
                if hits is not None:
                    # 先用 FTS 索引取候选（命中太宽返回 None → 退回全表）。
                    # 这里**只能**是纯 rowid 约束：一旦写成
                    # ``(search_body='' OR rowid IN …)``，SQLite 就会放弃 rowid
                    # 索引、退化成全表扫描 → 索引白建（实测 0.3ms → 18ms）。
                    # 「还没进索引的行」由 _fts_hits 直接并进候选列表（见那里）。
                    clauses.append(
                        "rowid IN (SELECT value FROM json_each(?))"
                    )
                    args.append(dump(hits))
            # 归档也参与召回时，同分让常驻的排在前面：
            # 旧原文和新摘要词面打平时，先给模型看「还在上下文里」的那条。
            active_tier = "" if active else "CASE WHEN active=1 THEN 0 ELSE 1 END,"
            where = " AND ".join(clauses)
            total = db.execute(
                "SELECT count(*) FROM records WHERE " + where, args
            ).fetchone()[0]
            # ★ 2026-09-19：**取宽一点**，好让重排有挑选余地 ✓
            #   只在第一页做（offset=0 ✓）—— 带 offset 时重排会让"翻页"语义错乱 ✗
            _want = max(1, int(limit))
            # ⚠️ 取宽会改变"先排除已见、再翻页"的既有语义 ✗（判据当场报红 ✓）
            #   ⇒ **只在纯检索（无 offset / 无 exclude_ids）时取宽** ✓
            #     那正是"第一页、按相关性"的典型场景 ✓ 重排最有意义 ✓
            _pure = not (offset or exclude_ids or exclude_sid)
            _wide = max(_want, min(_want * 3, 200)) if (lexical and _pure) else _want
            limit = _wide
            if vector and lexical:
                return {
                    "total": total,
                    "items": (
                        self._drop_media_only(self._fused_rows(
                        db,
                        where,
                        args,
                        tier_sql,
                        tier_args,
                        vector,
                        model,
                        lexical,
                        limit,
                        offset,
                        prefer_sid,
                        prefer_users,
                    ))
                        if skip_media
                        else self._fused_rows(
                        db,
                        where,
                        args,
                        tier_sql,
                        tier_args,
                        vector,
                        model,
                        lexical,
                        limit,
                        offset,
                        prefer_sid,
                        prefer_users,
                    )
                    ),
                }
            if vector:
                norm = math.sqrt(sum(x * x for x in vector))

                def cosine(raw):
                    other = json.loads(raw)
                    if len(other) != len(vector):
                        return -1.0
                    denom = norm * math.sqrt(sum(x * x for x in other))
                    return (
                        sum(x * y for x, y in zip(vector, other)) / denom
                        if denom
                        else -1.0
                    )

                db.create_function("similarity", 1, cosine)
                sql = f"""SELECT records.*,coalesce((SELECT similarity(vector) FROM vectors WHERE vectors.id=records.id
                  AND vectors.model=? AND vectors.revision=records.revision),-1) AS score FROM records WHERE {where}
                  ORDER BY {tier_sql}score DESC,end,id LIMIT ? OFFSET ?"""
                rows = db.execute(sql, [model, *args, *tier_args, limit, offset])
            else:
                rows = db.execute(
                    "SELECT * FROM records WHERE "
                    + where
                    + (
                        f" ORDER BY {tier_sql}{lexical_sql} DESC,{active_tier}end,id LIMIT ? OFFSET ?"
                        if lexical
                        else f" ORDER BY {tier_sql}end,id LIMIT ? OFFSET ?"
                    ),
                    [*args, *tier_args, limit, offset],
                )
            items = [self.row(r) for r in rows]
            if skip_media:
                # 兜住存量与"套了引用壳的媒体" ✓（开关关掉则照常返回 ✓）
                items = self._drop_media_only(items, include_tools)
            out = {"total": total, "items": items}
            return self._rerank(out, lexical, _want, offset)

    def _fused_rows(
        self, db, where, args, tier_sql, tier_args, vector, model,
        lexical, limit, offset, prefer_sid="", prefer_users=(),
    ):
        """词面 + 向量融合取页（只在两者同时可用时启用）。

        各取一路候选池 → 按本池最大值归一 → 0.3*词面 + 0.7*向量 → 重排取页。
        没有向量时不走这里，行为与以前完全一致。
        """
        import math

        db.create_function("lexical_score", 1, _lexical_scorer(lexical))
        norm = math.sqrt(sum(x * x for x in vector))

        def cosine(raw):
            other = json.loads(raw)
            if len(other) != len(vector):
                return -1.0
            denom = norm * math.sqrt(sum(x * x for x in other))
            return (
                sum(x * y for x, y in zip(vector, other)) / denom if denom else -1.0
            )

        db.create_function("similarity", 1, cosine)
        pool = max(30, (offset + limit) * 3)
        vec_sql = f"""SELECT records.*,
              coalesce((SELECT similarity(vector) FROM vectors WHERE vectors.id=records.id
                AND vectors.model=? AND vectors.revision=records.revision),-1) AS vscore,
              lexical_score(summary) AS lscore
            FROM records WHERE {where}
            ORDER BY {tier_sql}vscore DESC LIMIT ?"""
        lex_sql = f"""SELECT records.*,0 AS vscore, lexical_score(summary) AS lscore
            FROM records WHERE {where} AND lexical_score(summary)>0
            ORDER BY {tier_sql}lscore DESC LIMIT ?"""
        pool_rows = {}
        for row in db.execute(vec_sql, [model, *args, *tier_args, pool]):
            pool_rows[row["id"]] = self.row(row)
        for row in db.execute(lex_sql, [*args, *tier_args, pool]):
            item = self.row(row)
            if row["id"] in pool_rows:
                pool_rows[row["id"]]["lscore"] = max(
                    pool_rows[row["id"]]["lscore"], item["lscore"]
                )
            else:
                pool_rows[row["id"]] = item
        if not pool_rows:
            return []
        users = set(prefer_users or ())
        max_l = max((r["lscore"] for r in pool_rows.values()), default=0) or 1.0
        max_v = max((max(r["vscore"], 0.0) for r in pool_rows.values()), default=0) or 1.0
        for item in pool_rows.values():
            item["_fused"] = 0.3 * (item["lscore"] / max_l) + 0.7 * (
                max(item["vscore"], 0.0) / max_v
            )
            if tier_sql:
                # 会话亲和仍然优先，与旧路径保持一致
                if item["sid"] == prefer_sid:
                    item["_tier"] = 0
                elif users & set(item["users"]):
                    item["_tier"] = 1
                else:
                    item["_tier"] = 2 if item.get("visibility") == "global" else 3
        ordered = sorted(
            pool_rows.values(),
            key=lambda r: (r.get("_tier", 0), -r["_fused"], -r["end"], r["id"]),
        )
        page = ordered[offset : offset + limit]
        for item in page:
            item.pop("lscore", None)
            item.pop("vscore", None)
            item.pop("_fused", None)
            item.pop("_tier", None)
        return page

    def facts(
        self,
        sid="",
        subject="",
        category="",
        limit=100,
        offset=0,
        global_scope=False,
        users=(),
        include_shared=False,
        lexical="",
        exclude_ids=(),
        prefer_sid="",
        prefer_users=(),
        prefer_subjects=(),
        hide_pending=False,
        importance_first=False,
        min_score=0,
    ):
        where, args = ["deleted=0"], []
        if hide_pending:
            where.append("merge_pending=0")
        if exclude_ids:
            where.append("id NOT IN (SELECT value FROM json_each(?))")
            args.append(dump(list(exclude_ids)))
        if not global_scope:
            if include_shared:
                # Facts explicitly parked in the global bucket (cross-session
                # merges) are shared knowledge, so they are visible everywhere.
                where.append("""(sid=? OR sid='global' OR EXISTS (SELECT 1 FROM records,json_each(facts.sources) AS src WHERE records.id=src.value
                  AND records.deleted=0 AND (records.visibility='global' OR (records.visibility='user' AND EXISTS
                  (SELECT 1 FROM json_each(records.users) WHERE value IN (SELECT value FROM json_each(?)))))))""")
                args.extend([sid, dump(list(users))])
            else:
                where.append("sid=?")
                args.append(sid)
        for key, value in [("subject", subject), ("category", category)]:
            if value:
                where.append(key + "=?")
                args.append(value)
        tier_parts, tier_args = [], []
        if prefer_sid or prefer_users:
            # Same session first, then facts about the current participants.
            tier_parts.append(
                "CASE WHEN sid=? THEN 0 WHEN subject IN "
                "(SELECT value FROM json_each(?)) THEN 1 ELSE 2 END"
            )
            tier_args += [prefer_sid, dump(list(prefer_users))]
        if prefer_subjects:
            # Then facts about entities named in the current message.
            tier_parts.append(
                "CASE WHEN subject IN (SELECT value FROM json_each(?)) THEN 0 ELSE 1 END"
            )
            tier_args.append(dump(list(prefer_subjects)))
        tier_sql = (", ".join(tier_parts) + ", ") if tier_parts else ""
        with self.connect() as db:
            if lexical:
                from .retrieval import query_tokens

                # 同样下推到 SQL（含 min_score 门槛），全程 C 层
                tokens = query_tokens(lexical)
                fact_sql = _lexical_sql("lower(content)", tokens)
                # min_score 是「内容匹配」门槛：中文按 2 字切分，任一双字片段命中 = 2 分。
                # 默认只要求 >0；调用方（被动召回）用更高门槛挡掉「今天/喜欢」这类常见词。
                threshold = max(1, int(min_score or 0))
                where.append("(%s)>=%d" % (fact_sql, threshold))
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT * FROM facts WHERE "
                    + " AND ".join(where)
                    + (
                        f" ORDER BY {tier_sql}{fact_sql} DESC,"
                        f"{'importance DESC,created DESC,' if importance_first else 'audited,'}id LIMIT ? OFFSET ?"
                        if lexical
                        else f" ORDER BY {tier_sql}"
                        f"{'importance DESC,created DESC,' if importance_first else 'audited,'}id LIMIT ? OFFSET ?"
                    ),
                    [*args, *tier_args, limit, offset],
                )
            ]

    def audit_candidates(self, sid, limit=20, recheck_seconds=0):
        """Facts eligible for audit: never audited, or stale beyond the cooldown."""
        where = ["deleted=0", "merge_pending=0", "sid=?"]
        args = [sid]
        if recheck_seconds > 0:
            where.append("(audited=0 OR audited < ?)")
            args.append(time.time() - recheck_seconds)
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT * FROM facts WHERE "
                    + " AND ".join(where)
                    + " ORDER BY audited,id LIMIT ?",
                    [*args, limit],
                )
            ]

    def covering_fact(self, sid, content, threshold=0.4):
        """该内容是否已被某条事实覆盖（零模型）。

        用于永久记忆的「入闸」：能被事实覆盖的信息没必要再占一个每轮常驻的席位。
        """
        from .retrieval import similarity

        text = str(content or "").strip()
        if len(text) < 4:
            return None
        with self.connect() as db:
            rows = [
                self.row(r)
                for r in db.execute(
                    "SELECT * FROM facts WHERE deleted=0 AND merge_pending=0"
                    " AND (sid=? OR sid='global') LIMIT 400",
                    (sid,),
                )
            ]
        best, score = None, 0.0
        for row in rows:
            current = similarity(text, row["content"], min_overlap=3)
            if current > score:
                best, score = row, current
        return {"fact": best, "score": round(score, 3)} if best and score >= threshold else None

    def tidy_candidates(self, sid, days=14, limit=50, ids=None):
        """按「保留度」从低到高挑永久记忆整理候选（零模型）。

        保留度 = 重要度 0.35 + 访问衰减 0.25 + 创建衰减 0.1 + 访问加成 ≤0.3，
        再给 rule 类一个保底加成——它不是不能被整理，只是不会被优先挑中。
        """
        now = time.time()
        cutoff = now - max(0, int(days)) * 86400
        with self.connect() as db:
            rows = [
                self.row(r)
                for r in db.execute(
                    "SELECT * FROM records WHERE sid=? AND permanent=1 AND deleted=0"
                    " AND active=1 AND cold=0",
                    (sid,),
                )
            ]
        if ids:
            # v2.18.19：只整理**指定的这几条** ✓（前端"重新提取事实"按钮 / bot 指定 ✓）
            keep = {str(i) for i in ids if i}
            rows = [r for r in rows if str(r.get("id")) in keep]
        fresh = []
        for row in rows:
            if row.get("tidy_at") and row["tidy_at"] > cutoff:
                continue
            score = (
                (row.get("importance") or 5) / 10 * 0.35
                + 0.5 ** ((now - (row.get("last_accessed") or row.get("start") or now)) / 86400 / 30) * 0.25
                + 0.5 ** ((now - (row.get("start") or now)) / 86400 / 90) * 0.1
                + min((row.get("access_count") or 0) * 0.05, 0.3)
            )
            if str(row.get("category") or "") == "rule":
                score += 0.5
            row["_score"] = round(score, 4)
            fresh.append(row)
        fresh.sort(key=lambda r: (r["_score"], r["id"]))
        return fresh[:limit]

    def touch_accessed(self, ids):
        """记一次「被召回/被注入了」，供保留度评分使用。"""
        ids = [value for value in dict.fromkeys(ids or []) if value]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        with self.connect() as db:
            db.execute(
                f"UPDATE records SET access_count=access_count+1, last_accessed=?"
                f" WHERE id IN ({marks})",
                [time.time(), *ids],
            )

    def touch_tidy(self, ids, at=None):
        """记一次「刚整理过」，配合 tidy_days 做幂等限流。

        v2.18.66：`at` 可选 —— 传 0 表示**把限流清零**（让这几条立刻可被整理 ✓）
        「指定条目重新整理」那条路本来调的是 `touch_tidy_at(ids, 0)` ✗ 但这个方法**根本不存在** ✗
        ⇒ 真跑起来是 AttributeError（好在没人踩到过）⇒ 现在由本方法承担这个语义 ✓
        """
        ids = [value for value in dict.fromkeys(ids or []) if value]
        if not ids:
            return
        marks = ",".join("?" * len(ids))
        with self.connect() as db:
            db.execute(
                f"UPDATE records SET tidy_at=? WHERE id IN ({marks})",
                [time.time() if at is None else at, *ids],
            )

    def add_facts(self, sid, facts):
        """批量写入事实（整理提炼用）：走与压缩/归类同一条 _add_fact 链路。"""
        ids = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for fact in facts:
                ids.append(self._add_fact(db, sid, fact))
            self.bump(db)
        return ids

    def flag_similar_pairs(self, ids, threshold, cross_threshold=0.0):
        """在给定事实集合内找出「同主体且相似」的重复项（零模型）。

        召回时调用：那一瞬间候选正好都在手里，判定几乎是白送的。
        同主体、同类别用写入侧阈值；同主体、跨类别用更保守的 cross_threshold
        （跨类型常常是「同一件事被记成了不同类别」，但也可能是两件不同性质的事，
        所以门槛更高，且最终处置交给带原文证据的模型）。跨主体一律不参与。
        """
        from .retrieval import similarity

        ids = [value for value in dict.fromkeys(ids or []) if value]
        if len(ids) < 2:
            return []
        marks = ",".join("?" * len(ids))
        with self.connect() as db:
            rows = [
                self.row(r)
                for r in db.execute(
                    f"SELECT * FROM facts WHERE id IN ({marks})"
                    " AND deleted=0 AND merge_pending=0",
                    ids,
                )
            ]
        flagged = set()
        for index, left in enumerate(rows):
            for right in rows[index + 1 :]:
                if left["subject"] != right["subject"]:
                    continue  # 跨主体：合并后归谁是个新问题，不在这里处理
                same_scope = left["category"] == right["category"]
                limit = threshold if same_scope else cross_threshold
                if limit <= 0:
                    continue
                if similarity(left["content"], right["content"], min_overlap=2) >= limit:
                    flagged.add(left["id"])
                    flagged.add(right["id"])
        return sorted(flagged)

    def similar_facts(
        self, sid, subject, category, content, limit=3, min_score=0.25,
        cross_session=False, exclude_ids=(),
    ):
        """Local near-duplicate candidates for a fact about to be stored."""
        from .retrieval import similarity

        where = ["f.deleted=0", "f.merge_pending=0", "f.subject=?", "f.category=?"]
        args = [subject, category]
        if not cross_session:
            where.append("f.sid=?")
            args.append(sid)
        if exclude_ids:
            where.append("f.id NOT IN (SELECT value FROM json_each(?))")
            args.append(dump(list(exclude_ids)))
        with self.connect() as db:
            rows = db.execute(
                "SELECT f.*, coalesce((SELECT max(r.start) FROM records r,"
                " json_each(f.sources) s WHERE r.id=s.value),0) AS time"
                " FROM facts f WHERE " + " AND ".join(where),
                args,
            ).fetchall()
        scored = []
        for row in rows:
            score = similarity(content, row["content"], min_overlap=2)
            if score >= min_score:
                scored.append((score, self.row(row)))
        scored.sort(key=lambda item: -item[0])
        return scored[:limit]

    def facts_for_merge(self, sid="", ids=None, pending_only=False):
        """Fact rows with their newest evidence time, for merge decisions."""
        where, args = ["f.deleted=0"], []
        if pending_only:
            where.append("f.merge_pending=1")
        if sid:
            where.append("f.sid=?")
            args.append(sid)
        if ids:
            where.append("f.id IN (SELECT value FROM json_each(?))")
            args.append(dump(list(ids)))
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT f.*, coalesce((SELECT max(r.start) FROM records r,"
                    " json_each(f.sources) s WHERE r.id=s.value),0) AS time"
                    " FROM facts f WHERE " + " AND ".join(where) + " ORDER BY time,id",
                    args,
                )
            ]

    def trash(self, kind="facts", category="", keyword="", offset=0, limit=50):
        """回收站：已删除的事实/存档，以及未删但已移出上下文的冷归档。"""
        kind = kind if kind in ("facts", "records", "cold") else "facts"
        word = (keyword or "").strip()
        with self.connect() as db:
            if kind == "facts":
                where, args = ["f.deleted=1"], []
                if category:
                    where.append("f.category=?")
                    args.append(category)
                if word:
                    where.append("instr(lower(f.content),lower(?))>0")
                    args.append(word)
                clause = " AND ".join(where)
                total = db.execute(
                    "SELECT count(*) FROM facts f WHERE " + clause, args
                ).fetchone()[0]
                rows = [
                    self.row(r)
                    for r in db.execute(
                        "SELECT f.id, f.sid, f.category, f.subject, f.content, f.reason,"
                        " f.importance, f.deleted, f.created,"
                        " coalesce((SELECT max(v.created) FROM versions v"
                        " WHERE v.kind='fact' AND v.target=f.id),0) AS removed_at"
                        " FROM facts f WHERE " + clause + " ORDER BY removed_at DESC, f.id"
                        " LIMIT ? OFFSET ?",
                        [*args, max(1, min(200, limit)), max(0, offset)],
                    )
                ]
            else:
                clause = "deleted=1" if kind == "records" else "deleted=0 AND cold=1"
                args = []
                if word:
                    clause += " AND instr(lower(summary),lower(?))>0"
                    args.append(word)
                total = db.execute(
                    "SELECT count(*) FROM records WHERE " + clause, args
                ).fetchone()[0]
                # v2.18.65：卡片正文改成「摘要优先，没摘要就截原文」
                # （以前只带 summary ⇒ summary 为空的记录卡片一片空白 ✗）
                rows = [
                    self.row(r)
                    for r in db.execute(
                        "SELECT id,sid,level,summary,start,end,users,permanent,cold,"
                        "archived_at,deleted,"
                        " coalesce(nullif(summary,''), substr(coalesce(content,''),1,300))"
                        " AS preview,"
                        " coalesce((SELECT max(v.created) FROM"
                        " versions v WHERE v.kind='record' AND v.target=records.id),0)"
                        " AS removed_at FROM records WHERE " + clause
                        + " ORDER BY coalesce(nullif(archived_at,0), removed_at) DESC, id"
                        " LIMIT ? OFFSET ?",
                        [*args, max(1, min(200, limit)), max(0, offset)],
                    )
                ]
        return {"kind": kind, "items": rows, "total": total}

    def trash_stats(self):
        """回收站三个页签各有多少条（v2.18.65 一次查完，供面板显示数量）"""
        with self.connect() as db:
            facts = db.execute("SELECT count(*) FROM facts WHERE deleted=1").fetchone()[0]
            records = db.execute(
                "SELECT count(*) FROM records WHERE deleted=1"
            ).fetchone()[0]
            cold = db.execute(
                "SELECT count(*) FROM records WHERE deleted=0 AND cold=1"
            ).fetchone()[0]
        return {"facts": facts, "records": records, "cold": cold}

    def undelete(self, kind, target, cold_path=None):
        """从回收站还原：软删的条目重新可见，动作本身也写一条版本。"""
        table = "facts" if kind == "fact" else "records"
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM " + table + " WHERE id=?", (target,)
            ).fetchone()
            if row is None:
                raise ValueError("target not found")
            if not row["deleted"]:
                return False
            db.execute(
                "INSERT INTO versions(kind,target,snapshot,reason,created)"
                " VALUES (?,?,?,?,?)",
                (
                    "fact" if kind == "fact" else "record",
                    target,
                    dump(dict(row)),
                    "从回收站还原",
                    time.time(),
                ),
            )
            db.execute(
                "UPDATE " + table + " SET deleted=0,revision=revision+1 WHERE id=?",
                (target,),
            )
            self.bump(db)
            # ★ 冷归档（P7）：若正文已被外置，还原时**一并取回**（保持还原后内容完整 ✓）
            if table == "records":
                try:
                    from . import cold as _cold
                    _p = cold_path or _cold.default_path(self.path)
                    if Path(_p).exists():
                        _cold.restore(db, _p, ids=[target])
                except Exception:
                    logger.debug("[cold] 还原时取回正文失败（不影响还原本身 ✓）", exc_info=True)
        return True

    def reactivate(self, record_id):
        """把冷归档取回上下文：重新参与检索与注入。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise ValueError("target not found")
            if row["deleted"]:
                raise ValueError("target removed")
            if not row["cold"] and row["active"]:
                return False
            db.execute(
                "INSERT INTO versions(kind,target,snapshot,reason,created)"
                " VALUES ('record',?,?,?,?)",
                (record_id, dump(dict(row)), "从冷归档取回", time.time()),
            )
            db.execute(
                "UPDATE records SET cold=0,active=1,archived_at=0,"
                "revision=revision+1 WHERE id=?",
                (record_id,),
            )
            self.bump(db)
        return True

    def purge(self, kind, target):
        """彻底删除：连版本与关联一起移除，不可恢复（高级操作）。"""
        table = "facts" if kind == "fact" else "records"
        version_kind = "fact" if kind == "fact" else "record"
        with self.connect() as db:
            row = db.execute(
                "SELECT id FROM " + table + " WHERE id=?", (target,)
            ).fetchone()
            versions = db.execute(
                "SELECT count(*) FROM versions WHERE kind=? AND target=?",
                (version_kind, target),
            ).fetchone()[0]
            if row is None and not versions:
                raise ValueError("target not found")
            if table == "records":
                # records 被 migration_items 外键引用，先断开再删
                db.execute(
                    "UPDATE migration_items SET record_id=NULL WHERE record_id=?",
                    (target,),
                )
                db.execute(
                    "DELETE FROM edges WHERE parent=? OR child=?", (target, target)
                )
                db.execute("DELETE FROM vectors WHERE id=?", (target,))
            db.execute("DELETE FROM " + table + " WHERE id=?", (target,))
            db.execute(
                "DELETE FROM versions WHERE kind=? AND target=?",
                (version_kind, target),
            )
            db.execute("DELETE FROM job_items WHERE target=?", (target,))
            self.bump(db)
        return True

    def records_by_ids(self, ids):
        """批量取记录概要，供后台任务明细使用。"""
        listing = list(dict.fromkeys(ids))
        if not listing:
            return []
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT id,sid,role,level,start,end,summary,users,permanent,"
                    "deleted,active FROM records WHERE id IN"
                    " (SELECT value FROM json_each(?))",
                    (dump(listing),),
                )
            ]

    def facts_by_ids(self, ids, include_deleted=False):
        """按 ID 取事实。include_deleted 供审计明细/编辑器查看已撤回的事实。"""
        if not ids:
            return []
        clause = "" if include_deleted else " AND f.deleted=0"
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT f.*, coalesce((SELECT max(r.start) FROM records r,"
                    " json_each(f.sources) s WHERE r.id=s.value),0) AS time"
                    " FROM facts f WHERE f.id IN (SELECT value FROM json_each(?))"
                    + clause,
                    (dump(list(ids)),),
                )
            ]

    def versions_of(self, kind, target, limit=20):
        with self.connect() as db:
            return [
                {"id": r["id"], "reason": r["reason"], "created": r["created"]}
                for r in db.execute(
                    "SELECT id,reason,created FROM versions WHERE kind=? AND target=?"
                    " ORDER BY id DESC LIMIT ?",
                    (kind, target, limit),
                )
            ]

    def facts_since(self, sid, since):
        """Facts written (or first seen) after a timestamp, newest first.

        An empty ``sid`` scans every session (used once after legacy import).
        """
        where, args = ["f.deleted=0", "f.created>=?"], [since]
        if sid:
            where.append("f.sid=?")
            args.append(sid)
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT f.*, coalesce((SELECT max(r.start) FROM records r,"
                    " json_each(f.sources) s WHERE r.id=s.value),0) AS time"
                    " FROM facts f WHERE " + " AND ".join(where)
                    + " ORDER BY f.created DESC, f.id",
                    args,
                )
            ]

    def mark_merge_pending(self, ids, pending=1):
        if not ids:
            return 0
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = 0
            for fact_id in ids:
                changed += db.execute(
                    "UPDATE facts SET merge_pending=? WHERE id=? AND deleted=0",
                    (1 if pending else 0, fact_id),
                ).rowcount
            self.bump(db)
            return changed

    def pending_facts(self, limit=500):
        with self.connect() as db:
            return [
                {"id": r["id"], "sid": r["sid"]}
                for r in db.execute(
                    "SELECT id,sid FROM facts WHERE merge_pending=1 AND deleted=0 LIMIT ?",
                    (limit,),
                )
            ]

    def backfill_rewrite_pending(self):
        """给**历史**的降级拼接事实补上待重做标记。

        v2.13.0 才引入这个标记，所以升级前拼接出来的事实没有标记、
        手动/自动重做都挑不到它们（用户实测报过）。

        判据取「这条事实**最新一条版本记录**的 reason」（界面那行「最近审校」显示的
        就是它）——降级拼接的 reason 只写在版本里，**不会**写到 facts.reason 上。
        幂等：重做成功后目标事实会多一条「重做合并…」的版本，于是不再匹配。
        """
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE facts SET rewrite_pending=1 WHERE deleted=0 "
                "AND rewrite_pending=0 AND id IN ("
                "  SELECT v.target FROM versions v WHERE v.kind='fact'"
                "   AND instr(v.reason, ?)>0"
                "   AND v.id=(SELECT max(x.id) FROM versions x"
                "             WHERE x.kind='fact' AND x.target=v.target)"
                ")",
                (FALLBACK_REASON_MARK,),
            )
            return int(cursor.rowcount or 0)

    def mark_rotation(self, shown_ids=(), used_ids=(), kind="record"):
        """轮换槽位记账：展示过 +N、被用过 +M（用于排序、冷却与观测）✓

        ⚠️ 2026-09-18 修：原来**只写 records** ✗，而事实轮换的候选来自 `facts` ✓
        ⇒ 用事实 id 调用时 `UPDATE records … WHERE id=?` **匹配 0 行** ✓
        ⇒ 静默失效：事实槽位学不到"被用过" ✓（重启后从同几条重新开始 ✓）
        ⇒ 现在按 ``kind`` 分派（``"fact"`` ⇒ facts 表 ✓ 其余 ⇒ records ✓）
        """
        table = "facts" if str(kind) == "fact" else "records"
        shown = [str(i) for i in (shown_ids or []) if i]
        used = [str(i) for i in (used_ids or []) if i]
        with self.connect() as db:
            if shown:
                db.executemany(
                    "UPDATE " + table + " SET rotate_shown=rotate_shown+1 WHERE id=?",
                    [(i,) for i in shown],
                )
            if used:
                if table == "facts":
                    # ★ 2026-09-23：事实侧同时记**最后一次被用上**的时间 ✓
                    #   计分时据此做 60 天半衰期（见 retrieval._effective_used）✓
                    stamp = time.time()
                    db.executemany(
                        "UPDATE facts SET rotate_used=rotate_used+1, rotate_used_at=?"
                        " WHERE id=?",
                        [(stamp, i) for i in used],
                    )
                else:
                    db.executemany(
                        "UPDATE records SET rotate_used=rotate_used+1 WHERE id=?",
                        [(i,) for i in used],
                    )
        return len(shown) + len(used)

    def rotation_stats(self, kind="record"):
        """一次性取回"有轮换记录"的计数（量很小）。

        热路径（每轮注入）再用它做**纯内存排序**，省掉每轮的两次 DB 往返 ✓
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,rotate_shown,rotate_used FROM "
                + ("facts" if str(kind) == "fact" else "records")
                + " WHERE rotate_shown > 0"
            ).fetchall()
        return {
            row["id"]: (int(row["rotate_shown"] or 0), int(row["rotate_used"] or 0))
            for row in rows
        }

    def rotation_pick(self, ids, limit, kind="record"):
        """从给定的候选 id 里挑下一批轮换条目（排序即"学习"）。

        优先级：
        ① 从没展示过的优先
        ② 展示过但 **用过/展示** 比例高的优先（历史证明有用）
        ③ 展示次数少的优先，同次数按 id 稳定排序（可复现）
        """
        wanted = [str(i) for i in (ids or []) if i]
        if not wanted or limit <= 0:
            return []
        with self.connect() as db:
            rows = db.execute(
                "SELECT id,rotate_shown,rotate_used FROM "
                + ("facts" if str(kind) == "fact" else "records")
                + " WHERE id IN (%s)"
                % ",".join("?" * len(wanted)),
                wanted,
            ).fetchall()
        stats = {
            row["id"]: (int(row["rotate_shown"] or 0), int(row["rotate_used"] or 0))
            for row in rows
        }
        # 保持调用方给的候选顺序（那本身就是相关性排序）作为最后的稳定键
        order = {rid: index for index, rid in enumerate(wanted)}

        def key(rid):
            shown, used = stats.get(rid, (0, 0))
            return (
                0 if shown == 0 else 1,          # ① 没展示过的优先
                -(used / shown) if shown else 0,  # ② 用过比例高的优先
                shown,                            # ③ 展示次数少的优先
                order.get(rid, 0),
            )

        return sorted(wanted, key=key)[: int(limit)]

    def mark_rewrite_pending(self, fact_ids, pending=1):
        """给「降级拼接」出来的事实打/清待重做标记（不动其它任何字段）。"""
        ids = [str(fid) for fid in (fact_ids or []) if fid]
        if not ids:
            return 0
        with self.connect() as db:
            db.executemany(
                "UPDATE facts SET rewrite_pending=? WHERE id=?",
                [(1 if pending else 0, fid) for fid in ids],
            )
        return len(ids)

    def bump_rewrite_attempts(self, fact_ids):
        """给重试计数 +1（重做被安全校验拒绝时也要计，否则每轮审计都白试一遍）。"""
        ids = [str(fid) for fid in (fact_ids or []) if fid]
        if not ids:
            return 0
        with self.connect() as db:
            db.executemany(
                "UPDATE facts SET rewrite_attempts=rewrite_attempts+1 WHERE id=?",
                [(fid,) for fid in ids],
            )
        return len(ids)

    def pending_rewrites(self, limit=3, max_attempts=3):
        """等着重做的降级拼接事实。

        **判据现算**，不依赖 `rewrite_pending` 是否被回填过：一条事实的最新版本记录
        reason 里出现降级拼接的特征串就算（界面那行「最近审校」显示的就是它）。
        这样即使插件只是热重载、`initialize` 没跑到回填那一步，也照样挑得到。
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM facts WHERE deleted=0 AND rewrite_attempts < ? "
                "AND (rewrite_pending=1 OR id IN ("
                "  SELECT v.target FROM versions v WHERE v.kind='fact'"
                "   AND instr(v.reason, ?)>0"
                "   AND v.id=(SELECT max(x.id) FROM versions x"
                "             WHERE x.kind='fact' AND x.target=v.target)"
                ")) ORDER BY audited ASC, created ASC LIMIT ?",
                (int(max_attempts), FALLBACK_REASON_MARK, int(limit)),
            ).fetchall()
        return [dict(row) for row in rows]

    def rewrite_backlog(self):
        """还有多少条待重做（含已经放弃自动重试的），给界面用。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT count(*) AS n FROM facts WHERE rewrite_pending=1 AND deleted=0"
            ).fetchone()
        return int(row["n"] if row else 0)

    def unmerge_fact(self, fact_id):
        """把一次「降级拼接」的合并**还原回去**，并把这一簇重新排进合并队列。

        只用版本快照，不发任何模型请求；任何一步对不上就整体放弃（绝不半还原）。
        返回是否真的还原了。

        为什么是「还原重做」而不是「让审计改写那句话」：审计契约里没有"新增事实"
        的动作（只有 keep/correct/merge/retract），改写只能把 N 句压成 1 句、会丢信息；
        而 ``merge_facts`` 当时给组里**每一条**都写了合并前的完整快照，所以可以原样退回。
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            target = self.row(
                db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
            )
            if target is None or target["deleted"] or not target["rewrite_pending"]:
                return False
            latest = self.row(
                db.execute(
                    "SELECT * FROM versions WHERE kind='fact' AND target=? "
                    "ORDER BY id DESC LIMIT 1",
                    (fact_id,),
                ).fetchone()
            )
            if latest is None:
                return False
            # 那次合并给组里每条都写了同一 reason + 同一时间戳的版本。
            # v2.13.0 之前是逐条 time.time()（差几微秒），所以下面还有一层时间窗兜底。
            group = db.execute(
                "SELECT snapshot FROM versions WHERE kind='fact' AND reason=? AND created=?",
                (latest["reason"], latest["created"]),
            ).fetchall()
            if len(group) < 2:
                # 老库兜底：同一次合并的版本只差几微秒 → 用时间窗把候选放宽，
                # 后面再用「内容确实是这次拼接的前缀」逐步验证（下面 _pick_group）。
                group = db.execute(
                    "SELECT snapshot FROM versions WHERE kind='fact' AND reason=? "
                    "AND abs(created-?) <= 5",
                    (latest["reason"], latest["created"]),
                ).fetchall()
            snapshots = _pick_group(group, target)
            base = snapshots.get(fact_id)
            if base is None or len(snapshots) < 2:
                return False
            sources = [snap for fid, snap in snapshots.items() if fid != fact_id]
            alive = {
                row["id"]: row["deleted"]
                for row in db.execute(
                    "SELECT id,deleted FROM facts WHERE id IN (%s)"
                    % ",".join("?" * len(sources)),
                    [snap["id"] for snap in sources],
                ).fetchall()
            }
            if len(alive) != len(sources):
                return False  # 有条来源被彻底删了：不还原，保持现状
            for snap in sources:  # 1) 来源从回收站取回
                db.execute(
                    "UPDATE facts SET deleted=0, revision=revision+1 WHERE id=?",
                    (snap["id"],),
                )
            # 2) 目标回退到合并前，并重新排队（merge_pending=1 期间不会被注入）
            db.execute(
                "UPDATE facts SET sid=?, content=?, reason=?, scenario=?, tags=?,"
                " relations=?, sources=?, importance=?, fingerprint=?,"
                " event_at=?, event_end=?,"
                " merge_pending=1, rewrite_pending=0,"
                " rewrite_attempts=rewrite_attempts+1, revision=revision+1"
                " WHERE id=?",
                (
                    base.get("sid") or target["sid"],
                    base["content"],
                    base.get("reason") or "",
                    base.get("scenario") or "",
                    base.get("tags") or "[]",
                    base.get("relations") or "[]",
                    base.get("sources") or "[]",
                    int(base.get("importance") or 5),
                    base.get("fingerprint") or "",
                    float(base.get("event_at") or 0),
                    float(base.get("event_end") or 0),
                    fact_id,
                ),
            )
            # 3) 留个痕迹：界面「最近审校」会显示这行
            db.execute(
                "INSERT INTO versions(kind,target,snapshot,reason,created) "
                "VALUES ('fact',?,?,?,?)",
                (
                    fact_id,
                    dump(dict(target)),
                    "重做合并：上次模型输出不可用，已还原来源重试",
                    time.time(),
                ),
            )
        return True

    def merge_facts(self, target_id, source_ids, content, reason, new_sid="", tags=None):
        tags_override = tags
        """Fold near-duplicate facts into ``target_id``; the rest are soft-deleted.

        The target keeps its identity (and usually its session); tags, relations
        and sources are unioned and importance takes the maximum.
        """
        group = list(dict.fromkeys([target_id, *source_ids]))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = {}
            for fact_id in group:
                row = db.execute(
                    "SELECT * FROM facts WHERE id=?", (fact_id,)
                ).fetchone()
                if not row or row["deleted"]:
                    raise Conflict("merge source changed")
                rows[fact_id] = row
            target = rows[target_id]
            if any(
                (rows[k]["subject"], rows[k]["category"])
                != (target["subject"], target["category"])
                for k in group
            ):
                raise ValueError("cross-scope merge is forbidden")
            if new_sid and len({rows[k]["sid"] for k in group}) > 1:
                target_sid = new_sid
            else:
                target_sid = target["sid"]
            sources = sorted({s for k in group for s in json.loads(rows[k]["sources"])})
            tags = sorted({t for k in group for t in json.loads(rows[k]["tags"])})
            # v2.18.9：合并后标签不贴切了就该能改 ✗ 没给就沿用并集 ✓
            if tags_override is not None:
                tags = sorted({str(t).strip() for t in tags_override if str(t).strip()})
            relations = {
                dump(rel): rel for k in group for rel in json.loads(rows[k]["relations"])
            }
            importance = max(rows[k]["importance"] for k in group)
            # 同一次合并的版本必须共用同一个时间戳：这样「重做合并」才能靠
            # (reason, created) 把整组快照找回来（见 unmerge_fact）。
            merged_at = time.time()
            for k in group:
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) "
                    "VALUES ('fact',?,?,?,?)",
                    (k, dump(dict(rows[k])), reason, merged_at),
                )
            # 事件时间区间取并集：合并多个时间点的事实不该"塌成一点" ✗
            spans = [
                (float(rows[k]["event_at"] or 0), float(rows[k]["event_end"] or 0))
                for k in group
            ]
            starts = [a for a, _ in spans if a] or [float(rows[target_id]["event_at"] or 0)]
            ends = [b for _, b in spans if b] or starts
            db.execute(
                "UPDATE facts SET sid=?,content=?,sources=?,tags=?,relations=?,"
                "importance=?,merge_pending=0,fingerprint=?,event_at=?,event_end=?,"
                "revision=revision+1 WHERE id=?",
                (
                    target_sid,
                    content,
                    dump(sources),
                    dump(tags),
                    dump(list(relations.values())),
                    importance,
                    uid(),
                    min(starts),
                    max(ends),
                    target_id,
                ),
            )
            for k in group:
                if k != target_id:
                    db.execute(
                        "UPDATE facts SET deleted=1,merge_pending=0,"
                        "revision=revision+1 WHERE id=?",
                        (k,),
                    )
            self.bump(db)
            return target_id

    def edit_history(self, kind, targets):
        with self.connect() as db:
            rows = db.execute(
                """SELECT target,reason,created FROM
              (SELECT target,reason,created,id,row_number() OVER (PARTITION BY target ORDER BY id DESC) AS n
               FROM versions WHERE kind=? AND target IN (SELECT value FROM json_each(?)))
              WHERE n<=5 ORDER BY id DESC""",
                (kind, dump(list(targets))),
            )
            result = {}
            for row in rows:
                result.setdefault(row["target"], []).append(dict(row))
            return result

    def classify(self, row, output):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute(
                "SELECT revision,deleted,active,content,summary FROM records WHERE id=?",
                (row["id"],),
            ).fetchone()
            # v2.18.74：★ 守卫语义化 —— 以前死抠 revision 相等 ✗，
            # 而模型调用要好几秒，期间「分层压缩/提炼归档/清理」都会 bump revision
            # ⇒ 归类几乎每次都 Conflict 判死 ✗（用户实测：记忆归类总是失败 ✗）
            # 现在：真的没了/被删才拒；只是被"整理"过（正文没被换成别的内容）就采纳 ✓
            if not current or current["deleted"]:
                raise Conflict("classification source changed")
            stale = current["revision"] != row["revision"]
            if stale and not _classify_source_still_valid(row, current):
                raise Conflict("classification source changed")
            for fact in output["facts"]:
                if set(fact["source_ids"]) != {row["id"]}:
                    raise ValueError("unknown classification source")
                self._add_fact(db, row["sid"], fact)
            self.bump(db)

    def audit(self, candidates, output, job_id=""):
        by_id = {r["id"]: r for r in candidates}
        validate_audit(candidates, output)
        counts = {"keep": 0, "correct": 0, "merge": 0, "retract": 0, "merged_facts": 0}
        clamped = 0   # v2.18.9：自我来源想提权、被压回原值的次数 ✓
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # v2.18.9 回声防线（**服务端自己判定** ✗ 不依赖模型自觉 ✓）：
            # 若一条事实的**全部来源都是助手自己的发言** → 一律不许提 importance ✓
            # （模型可能没看懂 bot 标记、或干脆不填 only_self ✓ 那就拦不住了 ✓）
            source_ids = {
                sid for old in candidates for sid in (old.get("sources") or [])
            }
            # v2.18.9：周围上下文也算证据 ✓ 用来判断"审计是不是看得到用户那一侧" ✓
            bot_only = set()
            if source_ids:
                # ⚠️ 分批查：老 SQLite 的变量上限是 999 ✗ 一批里来源可能上千 ✓
                # 超限会直接 OperationalError → 审计任务整体失败 ✗（防御性分批 ✓）
                ordered = list(source_ids)
                for start in range(0, len(ordered), 500):
                    chunk = ordered[start : start + 500]
                    rows = db.execute(
                        "SELECT id,role FROM records WHERE id IN (%s)"
                        % ",".join("?" * len(chunk)),
                        tuple(chunk),
                    ).fetchall()
                    bot_only |= {
                        r[0] for r in rows if str(r[1] or "") == "assistant"
                    }
            for old in candidates:
                cur = db.execute(
                    "SELECT revision,deleted FROM facts WHERE id=?", (old["id"],)
                ).fetchone()
                if not cur or cur["deleted"]:
                    raise Conflict("audit evidence changed")
                # v2.18.74：revision 变化不再判死（合并/编辑等并发写很常见）；
                # 事实仍在、未被删 ⇒ 这次审计判定仍然成立 ✓
            for a in output["actions"]:
                old = by_id[a["target_id"]]
                group = {a["target_id"], *a["source_ids"]}
                # v2.18.9 回声防线 —— **服务端只留这一条** ✗
                # 一条事实的来源**全是助手自己**（或压根没有来源 ✓）时：
                # 不许靠"她自己说过"来**提升重要度** ✗（那是自我强化的燃料 ✓）
                # 其余一切（改正文 ✓ 合并 ✓ 软删 ✓）**完全交还给模型判断** ✓
                # —— "不让改"没有意义 ✓ 提示词已经讲清"以用户为准" ✓
                #    （而且原文与版本都留着 ✓ 被改错了也能恢复 ✓）
                sources = old.get("sources") or []
                self_only = not sources or all(s in bot_only for s in sources)
                if self_only:
                    a = dict(a)
                    a["only_self"] = True
                counts[a["action"]] += 1
                if a["action"] == "merge":
                    counts["merged_facts"] += len(group) - 1
                db.execute(
                    "INSERT INTO versions(kind,target,snapshot,reason,created) VALUES ('fact',?,?,?,?)",
                    (old["id"], dump(old), a["reason"], time.time()),
                )
                if a.get("importance") is not None:
                    new_imp = a["importance"]
                    # 模型自报 or **服务端判定**（后者才是真正的防线 ✓）
                    sources = old.get("sources") or []
                    server_self_only = bool(sources) and all(
                        s in bot_only for s in sources
                    )
                    if a.get("only_self") or server_self_only:
                        # v2.18.9 回声防线：来源全是助手自己 → **只许降不许升** ✗
                        # 提升 = "我自己说过 → 越来越可信" ✓ 正是自我强化的燃料 ✓
                        # 下调是安全的（越来越不重要），仍予保留 ✓
                        old_imp = old.get("importance")
                        if isinstance(old_imp, int) and new_imp > old_imp:
                            new_imp = old_imp
                            clamped += 1
                    db.execute(
                        "UPDATE facts SET importance=?,revision=revision+1 WHERE id=?",
                        (new_imp, a["target_id"]),
                    )
                if a["action"] == "keep":
                    continue
                if a["action"] == "retract":
                    # 软删：退出注入与检索，原文与版本都还在，可恢复。
                    db.execute(
                        "UPDATE facts SET deleted=1,revision=revision+1 WHERE id=?",
                        (a["target_id"],),
                    )
                    continue
                sources = sorted({s for k in group for s in by_id[k]["sources"]})
                relations = {
                    dump(rel): rel for k in group for rel in by_id[k]["relations"]
                }
                if a.get("relations") is not None:
                    relations = {dump(rel): rel for rel in a["relations"]}
                tags = sorted({tag for k in group for tag in by_id[k]["tags"]})
                # v2.18.9：**内容改了，标签也该能跟着改** ✗
                # 此前只能取"组内并集" → 模型改了正文，标签却永远停在旧的 ✓
                # 模型给了用模型的 ✓ 没给就沿用并集 ✓（老格式回答照样能过 ✓）
                if a.get("tags") is not None:
                    tags = sorted({str(t).strip() for t in a["tags"] if str(t).strip()})
                db.execute(
                    "UPDATE facts SET content=?,sources=?,relations=?,tags=?,fingerprint=?,revision=revision+1 WHERE id=?",
                    (
                        a["content"],
                        dump(sources),
                        dump(list(relations.values())),
                        dump(tags),
                        uid(),
                        old["id"],
                    ),
                )
                # v2.17.6：审计可以纠正**归属**（主体记错是线上事故 ✗）
                #   只对 correct 生效；只接受"已知实体"（id 或唯一名字）；拿不准就拒绝，绝不写垃圾 ✓
                wanted = (a.get("subject") or "").strip() if a["action"] == "correct" else ""
                if wanted and wanted != old["subject"]:
                    if "@" in wanted:
                        # v2.18：消歧写法「名字@短码」—— 重名时审计也能改对归属 ✓
                        # 短码是库里持久稳定的 ✓；解析不到就按"拒绝"处理（不猜 ✗）
                        _base, _sep, code = wanted.rpartition("@")
                        _row = db.execute(
                            "SELECT real FROM short_ids WHERE short=?", (code.strip(),)
                        ).fetchone()
                        hits = {_row[0]} if _row else set()
                    else:
                        hits = {row[0] for row in db.execute(
                            "SELECT id FROM entities WHERE id=? OR name=?", (wanted, wanted))}
                    # 重名撞车时**必须拒绝** ✗（任取一个 = 制造新的错记，正是本次事故那一类）
                    hit = (hits.pop(),) if len(hits) == 1 else None
                    if hit and hit[0] != old["subject"]:
                        db.execute(
                            "UPDATE facts SET subject=?,revision=revision+1 WHERE id=?",
                            (hit[0], old["id"]),
                        )
                        counts["subject_fixed"] = counts.get("subject_fixed", 0) + 1
                    else:
                        counts["subject_rejected"] = counts.get("subject_rejected", 0) + 1
                if a["action"] == "merge":
                    for k in group - {old["id"]}:
                        db.execute(
                            "INSERT INTO versions(kind,target,snapshot,reason,created) VALUES ('fact',?,?,?,?)",
                            (k, dump(by_id[k]), a["reason"], time.time()),
                        )
                        db.execute(
                            "UPDATE facts SET deleted=1,revision=revision+1 WHERE id=?",
                            (k,),
                        )
            for old in candidates:
                db.execute(
                    "UPDATE facts SET audited=? WHERE id=?", (time.time(), old["id"])
                )
            self.bump(db)
        if job_id:
            items = []
            for action in output["actions"]:
                target = action["target_id"]
                items.append(
                    {
                        "kind": "fact",
                        "target": target,
                        "action": action["action"],
                        "note": action.get("reason", ""),
                        "before": by_id[target]["content"] if target in by_id else "",
                    }
                )
                if action["action"] == "merge":
                    # 被并入的事实也列出来，前端才能显示「A + B → C」的方向
                    for other in action["source_ids"]:
                        if other == target or other not in by_id:
                            continue
                        items.append(
                            {
                                "kind": "fact",
                                "target": other,
                                "action": "merged",
                                "note": target,
                                "before": by_id[other]["content"],
                            }
                        )
            self.add_job_items(job_id, items)
        if clamped:
            # 提权被压回也要记账 ✓ 否则报告会少报"她想给自己加权重"的次数 ✗
            counts["only_self_clamped"] = clamped
        return counts

    def set_vector(self, record_id, model, revision, vector):
        if not vector or any(
            not isinstance(x, (float, int)) or not math.isfinite(x) for x in vector
        ):
            raise ValueError("invalid embedding")
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO vectors VALUES (?,?,?,?)",
                (record_id, model, revision, dump(vector)),
            )

    def enqueue(self, kind, sid, detail="", automatic=False):
        """把任务排进队列 ✓（同一 (kind,sid) 只占一行 ✓）

        ⚠️ 2026-09-18 修两处真问题 ✗✓（同 (kind,sid) 有 UNIQUE ✓）：
        ① 原来是 `INSERT OR IGNORE` ✗ ⇒ 只要已有一行（**哪怕是 completed** ✓）
           插入就被**静默忽略** ⇒ **这个会话再也排不上同类任务** ✓
           （要等清理：50 条以外或 7 天前 ✓ —— 用户会看到"扫描说有内容、任务却没跑" ✓）
        ② 紧接着 `SELECT ... state IN ('queued','running')` 可能返回 None ⇒ `[0]` ⇒
           **TypeError** ✗（被扫描的 per-session try 吞成一行日志 ✓ 更难发现 ✓）

        ⇒ 改成：**排队中就复用** ✓ / **已完结就重开** ✓ / 没有就新建 ✓ 三种都返回有效 id ✓
        ⇒ 手动排的任务优先标 manual ✓（不被自动任务的静默规则吃掉 ✓）
        """
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT id, state FROM jobs WHERE kind=? AND sid=?", (kind, sid)
            ).fetchone()
            if row:
                jid, state = row
                if state in ("queued", "running"):
                    # 已在队列/执行中 ⇒ 只补充 detail 与 manual 标记 ✓
                    db.execute(
                        "UPDATE jobs SET detail=COALESCE(NULLIF(?, ''), detail),"
                        " automatic=CASE WHEN ? THEN automatic ELSE 0 END, updated=?"
                        " WHERE id=?",
                        (detail, 1 if automatic else 0, now, jid),
                    )
                else:
                    # 已完结（completed/failed 等）⇒ **重开** ✓ 否则这个会话永远排不上 ✗
                    # ⚠️ 重开必须**连上一轮的 items 一起清** ✗✓（2026-09-18 用户反馈：
                    #    「明细」里标题写着"该会话还没有永久记忆" ✗ 正文却列出两条「保留」✓）
                    #    —— 那两条是**上一轮**的条目 ✓ 被**这一轮**的结论配着显示 ⇒ 张冠李戴 ✓
                    db.execute("DELETE FROM job_items WHERE job_id=?", (jid,))
                    db.execute(
                        "UPDATE jobs SET state='queued', detail=?, automatic=?,"
                        " created=?, updated=? WHERE id=?",
                        (detail, 1 if automatic else 0, now, now, jid),
                    )
                db.commit()
                return jid
            jid = uid()
            db.execute(
                "INSERT INTO jobs(id,kind,sid,state,detail,created,updated,automatic)"
                " VALUES (?,?,?,'queued',?,?,?,?)",
                (jid, kind, sid, detail, now, now, 1 if automatic else 0),
            )
            db.commit()
            return jid

    def claim(self, kind="", exclude=()):
        """Claim the oldest queued job; dedupe has its own worker lane."""
        clauses, args = ["state='queued'"], []
        if kind:
            clauses.append("kind=?")
            args.append(kind)
        if exclude:
            clauses.append("kind NOT IN (SELECT value FROM json_each(?))")
            args.append(dump(list(exclude)))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM jobs WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created LIMIT 1",
                args,
            ).fetchone()
            if row:
                db.execute(
                    "UPDATE jobs SET state='running',updated=? WHERE id=?",
                    (time.time(), row["id"]),
                )
            return dict(row) if row else None

    def finish(self, job, state, detail="", only_running=False):
        # only_running：取消路径专用。worker 可能在 finish(completed) 的 await
        # 里被取消，若再无条件改写就会把已完成的任务退回 queued、重复执行。
        where = " AND state='running'" if only_running else ""
        with self.connect() as db:
            db.execute(
                "UPDATE jobs SET state=?,detail=?,updated=? WHERE id=?" + where,
                (state, detail, time.time(), job),
            )
            self.bump(db)

    def drop_job(self, job_id):
        """删掉一个任务及其明细 ✗✓（只用于"自动 + 空转"的任务 ✓）

        为什么：这类任务**不调模型**、什么也没做 ✓ 留在工作台上只是噪音 ✓
        用户 2026-09-17 反馈："像这种的后台模型非手动的日志和工作台记录不要显示"
        ⚠️ 手动任务**永远不删** ✓ 真干活的也**永远不删** ✓（见 engine 的判定 ✓）
        """
        if not job_id:
            return 0
        with self.connect() as db:
            n = db.execute("DELETE FROM jobs WHERE id=?", (job_id,)).rowcount
            db.execute("DELETE FROM job_items WHERE job_id=?", (job_id,))
            db.commit()
        return n or 0

    def add_job_items(self, job_id, items):
        """记录后台任务处理了哪些条目，供前端「明细」查看与快速编辑。"""
        rows = [
            (
                job_id,
                item.get("kind", "record"),
                item["target"],
                item.get("action", ""),
                item.get("note", ""),
                item.get("before", ""),
                time.time(),
            )
            for item in items
            if item.get("target")
        ]
        if not job_id or not rows:
            return 0
        with self.connect() as db:
            db.executemany(
                "INSERT INTO job_items(job_id,kind,target,action,note,before,created)"
                " VALUES (?,?,?,?,?,?,?)",
                rows,
            )
            # 明细只做近期追溯，顺手清掉过期记录
            if random.random() < 0.05:
                db.execute(
                    "DELETE FROM job_items WHERE created < ?", (time.time() - 30 * 86400,)
                )
            self.bump(db)
        return len(rows)

    def job_items(self, job_id):
        with self.connect() as db:
            return [
                self.row(r)
                for r in db.execute(
                    "SELECT kind,target,action,note,before FROM job_items"
                    " WHERE job_id=? ORDER BY id",
                    (job_id,),
                )
            ]

    def jobs_by_id(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            return dict(row) if row else None

    def status(self):
        with self.connect() as db:
            return {
                "revision": db.execute(
                    "SELECT value FROM meta WHERE key='revision'"
                ).fetchone()[0],
                "levels": [
                    dict(r)
                    for r in db.execute(
                        "SELECT level,count(*) AS count,sum(active) AS active FROM records WHERE deleted=0 GROUP BY level ORDER BY level"
                    )
                ],
                "records": db.execute(
                    "SELECT count(*) FROM records WHERE deleted=0"
                ).fetchone()[0],
                "facts": db.execute(
                    "SELECT count(*) FROM facts WHERE deleted=0"
                ).fetchone()[0],
                "users": db.execute(
                    "SELECT count(DISTINCT subject) FROM facts WHERE deleted=0"
                ).fetchone()[0],
                "jobs": [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM jobs ORDER BY created DESC LIMIT 50"
                    )
                ],
            }

    def export(self):
        with self.connect() as db:
            return {
                t: [dict(r) for r in db.execute("SELECT * FROM " + t)]
                for t in (
                    "records",
                    "edges",
                    "facts",
                    "versions",
                    "migration_items",
                    "migration_reports",
                    "entities",
                    "entity_names",
                )
            }


def widen_source_ids(facts, candidates):
    """补齐事实**漏列**的来源（同批次内；只增不减；最多补一条；没有关键词就跳过）。

    为什么需要它：压缩模型常只挑一条记录当来源，而事实可能来自**同批次**的另一条
    （真实事故：内容在 B，来源只挂了 A → 审计报「证据无 X」→ 把**对的**事实改坏 ✗）。
    返回被补齐的事实条数（0 = 没有动过任何事实）。
    """
    if not facts or not candidates:
        return 0

    def _text(row):
        try:
            return str(row["summary"] or "") + str(row["content"] or "")
        except Exception:
            return ""

    batch = {str(r["id"]): _text(r) for r in candidates}
    fixed = 0
    for fact in facts:
        words = re.findall(r"[\u4e00-\u9fa5]{2,6}|[A-Za-z0-9]{3,}", str(fact.get("content") or ""))
        if not words:
            continue  # 没有可用关键词 → 不乱补 ✗
        seen = {str(x) for x in (fact.get("source_ids") or [])}
        if any(w in batch.get(rid, "") for rid in seen for w in words):
            continue  # 已声明的来源里就有该内容 → 不动 ✓
        for rid, text in batch.items():
            if rid not in seen and any(w in text for w in words):
                seen.add(rid)
                fixed += 1
                break  # 最多补一条 ✓ 防止来源膨胀
        fact["source_ids"] = sorted(seen)  # 只增不减 ✓
    return fixed
