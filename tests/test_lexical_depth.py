"""词法打分 SQL 的深层表达式（2026-09-25 用户实测崩溃的回归保护）

现象：`sqlite3.OperationalError: Expression tree is too large (maximum depth 1000)`
根因：`_lexical_sql` 旧写法把「词元命中×词长」拼成**扁平加法链** `a + b + c + …`
     ⇒ SQLite 解析成**左深树** ⇒ 词元数 >1000（长文本的汉字切片可达上千）就超深度 ✗
修法：①**平衡合并**（两两相加）⇒ 深度 O(log n) ✓ 且求值结果与顺序求和逐值一致 ✓
     ②词元上限 256 ✓（防病态长查询每行跑上千次 instr 把召回拖慢 ✓）
"""

import importlib
import itertools
import sqlite3
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_lexd")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_lexd", pkg)
storage = importlib.import_module("alife_lexd.storage")
retrieval = importlib.import_module("alife_lexd.retrieval")

POOL = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥天地玄黄宇宙洪荒日月盈昃辰宿列张"


def _many_tokens(count):
    pairs = ["".join(p) for p in itertools.islice(itertools.product(POOL, repeat=2), count)]
    return retrieval.query_tokens(" ".join(pairs))


def test_sql_runs_on_very_long_token_list():
    """★ 回归：上千词元不能再崩（旧写法必崩 ✓）"""
    tokens = _many_tokens(1300)
    assert len(tokens) > 1000
    sql = storage._lexical_sql("lower(content)", tokens)
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t(content TEXT)")
    db.execute("INSERT INTO t VALUES ('甲乙丙丁')")
    rows = db.execute("SELECT content FROM t WHERE (%s)>=1" % sql).fetchall()
    assert rows == [("甲乙丙丁",)]


def test_token_cap_bounds_expression_size():
    """词元上限生效：表达式里的 instr 个数不超过上限 ✓（不然长查询会拖慢召回）"""
    tokens = _many_tokens(1300)
    sql = storage._lexical_sql("lower(content)", tokens)
    assert sql.count("instr(") <= storage.LEXICAL_MAX_TOKENS


def test_balances_but_value_parity_with_python():
    """平衡合并**不改变取值**：SQL 分 与 Python relevance 逐值一致 ✓"""
    text = "用户对花生过敏，吃了会休克，随身带着肾上腺素笔"
    q = "花生过敏 肾上腺素"
    tokens = retrieval.score_tokens(q)
    assert len(tokens) < storage.LEXICAL_MAX_TOKENS        # 未触发上限 ✓
    sql = storage._lexical_sql("lower(?)", tokens)
    db = sqlite3.connect(":memory:")
    # 表达式里每个词元各有一个占位符 ⇒ 同一个文本要绑 N 次 ✓
    got = db.execute("SELECT (%s)" % sql, tuple([text.lower()] * len(tokens))).fetchone()[0]
    expect = sum(len(t) for t in tokens if t in text.lower())
    assert got == expect, (got, expect)


def test_empty_tokens_still_not_bare_integer():
    """老回归：无词元时必须返回表达式 "0+0"（裸整数会被 ORDER BY 当列位置 ✗）"""
    sql = storage._lexical_sql("lower(content)", [])
    assert sql == "0+0"
