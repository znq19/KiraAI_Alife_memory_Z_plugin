"""冷归档（P7 · v7）：**骨架行 + content 外置** 的真跑验证

关键验收（v6 的整行方案就是死在这里 ✗）：
  ★ 跨边界关系必须活下来：edges(cold→hot) 在搬迁后**依然合法** ✓（行都还在 ✓）
  ★ content 一字不丢：冷库取回与原文**逐字符一致** ✓
  ★ restore 后 records **逐字段**回到迁移前 ✓
  ★ 热行 / 非冷行**一条不动** ✓；迁移后 `PRAGMA foreign_key_check` 必须为空 ✓
"""
import importlib.util
import pytest
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DDL = """
CREATE TABLE records (id TEXT PRIMARY KEY, sid TEXT, content TEXT, summary TEXT,
  cold INTEGER NOT NULL DEFAULT 0, archived_at REAL NOT NULL DEFAULT 0, active INTEGER DEFAULT 1,
  deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE edges (rowid INTEGER PRIMARY KEY, from_id TEXT REFERENCES records(id),
  to_id TEXT REFERENCES records(id), kind TEXT);
CREATE TABLE vectors (rowid INTEGER PRIMARY KEY, record_id TEXT REFERENCES records(id), vec BLOB);
"""


def _cold():
    spec = importlib.util.spec_from_file_location("cold_v7", ROOT / "cold.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["cold_v7"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_hot(path, n_cold=3, n_hot=2):
    db = sqlite3.connect(str(path))
    db.executescript(DDL)
    db.execute("PRAGMA foreign_keys=ON")
    old = time.time() - 400 * 86400
    for i in range(n_hot):
        db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active) "
                   "VALUES(?,?,?,?,0,0,1)", ("hot%d" % i, "S1", "热正文" + str(i), "热摘要%d" % i))
    for i in range(n_cold):
        db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active) "
                   "VALUES(?,?,?,?,1,?,0)", ("cold%d" % i, "S1", "冷正文" * 40 + str(i), "摘要%d" % i, old))
        # ★ 故意造**跨边界**关系：冷行 → 热行（v6 就是被它推翻的 ✓）
        db.execute("INSERT INTO edges(from_id,to_id,kind) VALUES(?,?,?)", ("cold%d" % i, "hot0", "cross"))
        db.execute("INSERT INTO vectors(record_id,vec) VALUES(?,?)", ("cold%d" % i, b"xx"))
    db.commit()
    return db


def test_preview_readonly_and_counts(tmp_path):
    m = _cold()
    db = _make_hot(tmp_path / "hot.db")
    before = db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
    info = m.preview(db, days=180)
    assert info["records"] == 3, info
    assert info["bytes"] > 0
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == before, "preview 不许写 ✗"


def test_spill_keeps_rows_and_clears_content(tmp_path):
    m = _cold()
    hot, cpath = tmp_path / "hot.db", tmp_path / "cold.db"
    db = _make_hot(hot)
    res = m.spill(db, cpath, days=180)
    assert res["ok"] and res["moved"] == 3, res
    assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 5, "★行不许删（外键的根基）✗"
    rows = db.execute("SELECT id, content, summary FROM records WHERE cold=1 ORDER BY id").fetchall()
    assert [r[0] for r in rows] == ["cold0", "cold1", "cold2"]
    assert all(r[1] == "" for r in rows), "冷行 content 应已置空 ✓"
    assert [r[2] for r in rows] == ["摘要0", "摘要1", "摘要2"], "summary 必须原样（打分要用）✓"
    assert db.execute("SELECT id, content FROM records WHERE cold=0 ORDER BY id").fetchall() == \
        [("hot0", "热正文0"), ("hot1", "热正文1")], "热行一条不动 ✓"
    assert db.execute("PRAGMA foreign_key_check").fetchall() == [], "★跨边界外键必须完好 ✓"
    assert db.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 3, "关系行不该被搬 ✓"


def test_content_roundtrip_is_lossless(tmp_path):
    m = _cold()
    hot, cpath = tmp_path / "hot.db", tmp_path / "cold.db"
    db = _make_hot(hot)
    before = dict(db.execute("SELECT id, content FROM records WHERE cold=1").fetchall())
    assert m.spill(db, cpath, days=180)["ok"]
    assert m.content_of(cpath, list(before)) == before, "冷库取回必须逐字符一致 ✓"
    r = m.restore(db, cpath, ids=list(before))
    assert r["ok"] and r["restored"] == 3, r
    after = dict(db.execute("SELECT id, content FROM records WHERE cold=1").fetchall())
    assert after == before, "★restore 后必须与迁移前逐字段一致 ✓"
    assert m.content_of(cpath, list(before)) == {}, "restore 后冷库副本应已删 ✓"


def test_idempotent_and_day_gate(tmp_path):
    m = _cold()
    db = _make_hot(tmp_path / "hot.db")
    cpath = tmp_path / "cold.db"
    assert m.spill(db, cpath, days=180)["moved"] == 3
    assert m.spill(db, cpath, days=180)["moved"] == 0, "重跑不应重复搬 ✓"
    db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active) "
               "VALUES(?,?,?,?,1,?,0)", ("fresh", "S1", "刚归档", "s", time.time() - 5 * 86400))
    db.commit()
    assert m.preview(db, days=180)["records"] == 0, "未满 180 天不许搬 ✓"


def test_stats_reports_cold_side(tmp_path):
    m = _cold()
    db = _make_hot(tmp_path / "hot.db")
    cpath = tmp_path / "cold.db"
    st0 = m.stats(cpath)
    assert st0["exists"] is False and st0["records"] == 0
    m.spill(db, cpath, days=180)
    st = m.stats(cpath)
    assert st["exists"] and st["records"] == 3 and st["bytes"] > 0, st


def test_fill_contents_backfills_only_empty(tmp_path):
    """回填：只填空的 ✓ 不覆盖已有正文 ✓ 找不到的保持原样 ✓"""
    m = _cold()
    db = _make_hot(tmp_path / "hot.db")
    cpath = tmp_path / "cold.db"
    m.spill(db, cpath, days=180)
    rows = [{"id": "cold0", "content": ""}, {"id": "hot0", "content": "热正文0"},
            {"id": "nope", "content": ""}]
    out = m.fill_contents(rows, cpath)
    assert out[0]["content"].startswith("冷正文"), "空正文必须被回填 ✓"
    assert out[1]["content"] == "热正文0", "已有正文绝不能被覆盖 ✓"
    assert out[2]["content"] == "", "找不到的原样保留 ✓"
    assert m.default_path(tmp_path / "hot.db").name == "memory_cold.db"


def test_wiring_sites_are_present():
    """接线守卫：三处入口必须在 ⇒ 以后谁删掉一处，立刻红 ✓"""
    main_src = (ROOT / "main.py").read_text(encoding="utf-8")
    stor_src = (ROOT / "storage.py").read_text(encoding="utf-8")
    assert "def _cold_path_of(self" in main_src and "def _cold_fill(self" in main_src
    assert main_src.count("self._cold_fill(") >= 2, "浏览/详情两处都要回填 ✓"
    assert 'result["items"] = await self._cold_fill' in main_src, \
        "面板浏览必须回填（且必须 await ✓ 否则阻塞事件循环 ✗）"
    assert "def undelete(self, kind, target, cold_path=None)" in stor_src, "还原要能取回 ✓"
    # ★ 回收站/冷归档页签（面板卡片拿它的行数据**预填编辑器** ✗）也必须回填 ✓
    i = main_src.index('"trash", kind, category, keyword, offset, 50')
    assert "await self._cold_fill" in main_src[i:i + 420], \
        "/trash 没回填 ⇒ 面板点「查看与编辑」会看到空内容 ✗"
    assert "_cold.restore(db, _p" in stor_src, "还原时必须真的取回正文 ✓"
    assert "cold_archive_enabled" in main_src, "必须受设置开关控制 ✓"


def test_recycle_bin_rows_are_included(tmp_path):
    """回收站（deleted=1）的行也应可外置 ✓ —— 它不参与召回，且 undelete 会取回 ✓"""
    m = _cold()
    db = _make_hot(tmp_path / "hot.db", n_cold=0)
    db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active,deleted) "
               "VALUES(?,?,?,?,0,0,1,1)", ("trash1", "S1", "被删的正文", "被删摘要"))
    db.commit()
    info = m.preview(db, days=180)
    assert info["records"] == 1 and info["ids"] == ["trash1"], info
    assert m.spill(db, tmp_path / "cold.db", days=180)["moved"] == 1
    assert db.execute("SELECT content FROM records WHERE id='trash1'").fetchone()[0] == ""
    assert db.execute("SELECT COUNT(*) FROM records WHERE id='trash1'").fetchone()[0] == 1, "行必须保留 ✓"


def test_exclude_deleted_option(tmp_path):
    """关掉 include_deleted 时回收站不动 ✓（保留可配置性 ✓）"""
    m = _cold()
    db = _make_hot(tmp_path / "hot.db", n_cold=0)
    db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active,deleted) "
               "VALUES(?,?,?,?,0,0,1,1)", ("trash2", "S1", "x", "y"))
    db.commit()
    assert m._cold_ids(db, 180, time.time(), include_deleted=False) == []
    assert m._cold_ids(db, 180, time.time(), include_deleted=True) == ["trash2"]


def test_panel_cold_buttons_align_with_backend():
    """前后端一致性：面板 4 个按钮 ↔ 后端 4 个 action，缺一即红 ✓"""
    h = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    a = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    for bid, act in (("coldStats", "stats"), ("coldPreview", "preview"),
                     ("coldSpill", "spill"), ("coldRestore", "restore")):
        assert ('id="%s"' % bid) in h, "面板缺按钮 %s ✗" % bid
        assert ('"%s"' % act) in m, "后端缺动作 %s ✗" % act
    assert "async function coldAction(" in a, "面板缺处理函数 ✗"
    assert '"/cold?action="' in a, "面板没调用 /cold 接口 ✗"
    assert a.count('["coldStats", "coldPreview", "coldSpill", "coldRestore"]') == 1, "按钮未绑定 ✗"
    assert 'id="coldMsg"' in h, "面板缺结果提示区 ✗"


def test_cold_auto_is_wired_and_nonblocking():
    """自动冷归档：必须在 ✓ 受两个开关控制 ✓ 且**不阻塞对话**（create_task 而非 await ✓）"""
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "async def _cold_auto_once(self" in m, "缺少自动执行方法 ✗"
    assert "create_task(self._cold_auto_once())" in m, "自动执行没有触发点 ✗"
    seg = m[m.index("async def _cold_auto_once"):]
    seg = seg[:seg.index("def _cold_path_of")]
    for need, why in (("cold_archive_enabled", "总开关"), ("cold_archive_auto", "自动开关"),
                      ("6 * 3600", "节流（6 小时）"), ("asyncio.to_thread", "干活在线程里")):
        pass
    for need, why in (("cold_archive_enabled", "总开关"), ("cold_archive_auto", "自动开关"),
                      ("6 * 3600", "节流（6 小时）"), ("asyncio.to_thread", "干活在线程里")):
        assert need in seg, "自动执行少了 %s（%s）✗" % (need, why)
    c = (ROOT / "contracts.py").read_text(encoding="utf-8")
    cold_src = (ROOT / "cold.py").read_text(encoding="utf-8")
    assert "VACUUM_MIN_BYTES" in cold_src, "VACUUM 阈值（方案 B）缺失 ✗"
    assert "vacuumed" in cold_src, "spill 应报告是否 VACUUM 过 ✓"
    assert (ROOT / "main.py").read_text(encoding="utf-8").count('db.execute("VACUUM")') == 0, \
        "VACUUM 决策应统一在 cold.py（避免两处各说各话 ✓）"
    assert "cold_archive_enabled: bool = True" in c, "冷归档必须默认开 ✓"
    assert "cold_archive_auto: bool = True" in c, "自动执行必须默认开 ✓"


def test_cold_auto_runs_at_startup():
    """按用户要求：**启动后就搬** ⇒ 启动触发必须在；消息到达只作长会话兜底 ✓"""
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "async def _cold_auto_at_start(self" in m, "缺启动自动执行 ✗"
    assert "create_task(self._cold_auto_at_start())" in m, "启动时没有触发 ✗"
    i = m.index("async def build_search_index")
    assert "_cold_auto_at_start" in m[i : i + 1000], "启动任务应挂在 build_search_index（存量升级用）✓"
    seg = m[m.index("async def _cold_auto_at_start"):]
    seg = seg[: seg.index("async def build_search_index")]
    assert "await asyncio.sleep(6)" in seg, "启动触发必须延时（等应用起来 ✓）"
    assert "force=False" in seg, "启动触发要走节流（不要绕过节流 ✓）"


def _storage_mod():
    """storage.py 里有相对导入 ⇒ 必须先造出"包"上下文 ✓"""
    pkg = "cold_test_pkg"
    if pkg not in sys.modules:
        m = type(sys)(pkg)
        m.__path__ = [str(ROOT)]
        sys.modules[pkg] = m
    spec = importlib.util.spec_from_file_location(pkg + ".storage", ROOT / "storage.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_cold_index_really_created(tmp_path):
    """冷行索引必须**真的**建出来 ✓ —— 它决定"检查有没有可搬的"走索引还是全表扫"""
    st = _storage_mod().Store(tmp_path / "x.db")
    st.initialize()
    with st.connect() as db:
        names = [r[0] for r in db.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")]
    assert "record_cold" in names, "缺 record_cold 索引 ⇒ 检查会退化成全表扫 ✗"
    assert "record_deleted" in names, "缺 record_deleted 索引 ⇒ 回收站那条会全表扫 ✗"


def test_vacuum_only_for_large_moves(tmp_path):
    """方案 B：小批量**不** VACUUM（空闲页留着复用 ✓）；大批量才真的缩文件 ✓"""
    m = _cold()
    db = _make_hot(tmp_path / "small.db")
    small = m.spill(db, tmp_path / "small_cold.db", days=180)
    assert small["moved"] == 3, small
    assert small.get("vacuumed") is False, "小批量不该 VACUUM（VACUUM 会独占锁几秒 ✗）"
    db2 = _make_hot(tmp_path / "big.db", n_cold=0)
    big = "正" * 60000
    for i in range(100):
        db2.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active) "
                    "VALUES(?,?,?,?,1,?,0)",
                    ("big%d" % i, "S1", big + str(i), "s", time.time() - 400 * 86400))
    db2.commit()
    res = m.spill(db2, tmp_path / "big_cold.db", days=180)
    assert res["moved"] == 100, res
    assert res.get("vacuumed") is True, "≥5MB 应触发 VACUUM ✓"
    assert db2.execute("PRAGMA freelist_count").fetchone()[0] == 0, "VACUUM 后不应有空闲页 ✓"


def test_cold_fill_never_blocks_event_loop():
    """★ 冷库读是阻塞 IO ⇒ 必须在 to_thread 里（绝不能在事件循环上直接做 ✗）"""
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    assert "async def _cold_fill(self" in m, "_cold_fill 必须是 async ✗"
    seg = m[m.index("async def _cold_fill"):][:1200]
    assert "await asyncio.to_thread(_work)" in seg, "必须走 to_thread（否则阻塞事件循环 ✗）"
    assert m.count("await self._cold_fill") >= 2, "调用点必须 await ✗"
    # 反向自检：把 await 去掉后，上面的判据必须不成立 ✓
    bad = m.replace("await self._cold_fill", "self._cold_fill")
    assert bad.count("await self._cold_fill") != 2, "守卫发现不了【漏 await】✗"


def test_split_queries_equal_or_version(tmp_path):
    """安全优化验证：拆成两条查询再取并集 ≡ 原来的 `(冷条件) OR deleted=1` ✓
    （含"又冷又删"的重叠行 ⇒ 去重必须正确 ✓）"""
    m = _cold()
    db = _make_hot(tmp_path / "hot.db", n_cold=2)
    old = time.time() - 400 * 86400
    db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active,deleted) "
               "VALUES(?,?,?,?,0,0,1,1)", ("trashA", "S1", "删的正文", "s"))
    db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active,deleted) "
               "VALUES(?,?,?,?,1,?,0,1)", ("both", "S1", "又冷又删", "s", old))
    db.execute("INSERT INTO records(id,sid,content,summary,cold,archived_at,active,deleted) "
               "VALUES(?,?,?,?,1,?,0,0)", ("oldcold", "S1", "老冷行", "s", old))
    db.commit()
    cut = time.time() - 180 * 86400
    got = set(m._cold_ids(db, 180, time.time(), include_deleted=True))
    ref = {r[0] for r in db.execute(
        "SELECT id FROM records WHERE (cold=1 AND (archived_at=0 OR archived_at<=?) OR deleted=1) "
        "AND content IS NOT NULL AND content <> ''", (cut,))}
    assert got == ref, "拆分版必须与 OR 版完全等价 ✓（重叠行要去重 ✓）"
    only_cold = set(m._cold_ids(db, 180, time.time(), include_deleted=False))
    assert only_cold == {r[0] for r in db.execute(
        "SELECT id FROM records WHERE cold=1 AND (archived_at=0 OR archived_at<=?) "
        "AND content IS NOT NULL AND content <> ''", (cut,))}, "关回收站时只取冷行 ✓"
    assert "both" in got and "trashA" in got and "oldcold" in got, got


def test_cold_panel_api_never_blocks_event_loop():
    """★ 面板四个按钮（stats/preview/spill/restore）都要读库 ⇒ **必须走线程** ✓
    否则点一下，事件循环被占住 ⇒ 整个应用（含对话）都会停 ✗ —— 实测过这种卡顿 ✓"""
    m = (ROOT / "main.py").read_text(encoding="utf-8")
    i = m.index("async def api_cold(")
    seg = m[i : i + 2600]
    assert seg.count("await asyncio.to_thread(") >= 3, "api_cold 的动作必须走 to_thread ✗"
    offenders = [
        ln.strip()[:60]
        for ln in seg.split("\n")
        if "with self.store.connect()" in ln and len(ln) - len(ln.lstrip()) == 8
    ]
    assert not offenders, "api_cold 顶层还有同步读库 ✗：%s" % offenders
    assert "_cold.stats" in seg and "await asyncio.to_thread(_cold.stats" in seg, "stats 也要走线程 ✓"


def test_no_blocking_io_directly_in_async_handlers():
    """★ 通用守卫（同类 bug 的根治）：async 处理器里**直接**做阻塞 IO ⇒ 卡住整个应用 ✗

    只查「直接体」✓ —— 嵌套 def 里的事不算（只要外面用 to_thread 调用它就没问题 ✓）
    这条覆盖了之前两次真实事故：_cold_fill 与 api_cold ✗
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # ast.get_source_segment 每个节点都会重新切源码；main.py 很大 ⇒ 实测 ~9s。
    # 预计算行偏移，语义一致但线性扫描。
    _line_starts = [0]
    for _line in src.splitlines(keepends=True):
        _line_starts.append(_line_starts[-1] + len(_line))

    def _segment(node):
        return src[
            _line_starts[node.lineno - 1] + node.col_offset:
            _line_starts[node.end_lineno - 1] + node.end_col_offset
        ]

    BLOCK = (
        "self.store.connect(", "sqlite3.connect(", "_cold.spill(", "_cold.restore(",
        "_cold.preview(", "_cold.stats(", "_cold.content_of(", "_cold.fill_contents(",
    )
    bad = []

    def direct_calls(fn):
        def walk(stmt):
            for child in ast.iter_child_nodes(stmt):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue          # 嵌套定义先跳过 ✓（它们可能在线程里被调用 ✓）
                if isinstance(child, ast.Call):
                    seg = _segment(child)
                    if any(b in seg for b in BLOCK):
                        bad.append("%s() 行 %d：%s" % (fn.name, child.lineno, seg[:56]))
                walk(child)

        for stmt in fn.body:
            # ★ 语句**本身就是**嵌套定义时也要跳过（否则会把 to_thread 里的活儿误判 ✗）
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            walk(stmt)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            direct_calls(node)
    assert not bad, "async 里直接做阻塞 IO ⇒ 会卡住整个应用 ✗：%s" % bad[:3]


def test_trash_returns_preview_even_when_summary_is_empty(tmp_path):
    """v2.18.65：卡片正文不能只靠 summary ✗ 没摘要的记录必须拿得到 preview ✓"""
    import time as _time

    store = _storage_mod().Store(tmp_path / "db")
    store.initialize()
    sid = "a:dm:preview"
    now = _time.time()
    store.capture(
        sid,
        "e1",
        [dict(role="user", content="正文内容一段话", summary="", users=["a:u"], time=now)],
    )
    with store.connect() as db:
        rid = db.execute("SELECT id FROM records WHERE sid=?", (sid,)).fetchone()[0]
        db.execute("UPDATE records SET summary='' WHERE id=?", (rid,))
        db.execute("UPDATE records SET deleted=1 WHERE id=?", (rid,))
        db.commit()
    out = store.trash("records", "", "", 0, 50)
    row = out["items"][0]
    assert row.get("preview"), "summary 为空时 preview 必须带正文 ✗"
    assert "正文内容" in row["preview"], "preview 应该是原文 ✗"
    stats = store.trash_stats()
    assert stats["records"] >= 1 and set(stats) == {"facts", "records", "cold"}


def test_cold_fill_callers_match_its_signature():
    """v2.18.65：`_cold_fill` 的**参数**必须和调用方一致（专治"编辑只落一半"）

    实测教训：一次编辑只应用了一半（签名没加 field，调用处却传了 field="preview"）
    ⇒ 运行时会 TypeError ⇒ 回收站接口直接 500 ✗ ⇒ 这条守卫专门盯这类问题
    """
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'async def _cold_fill(self, items, cfg=None, field="content")' in src, (
        "签名必须带 field（默认 content ⇒ 浏览/详情两条老路语义不变）"
    )
    assert "fill_contents(items, p, field=field)" in src, "field 要透传给 cold.fill_contents"
    assert 'field="preview"' in src, "回收站列表要按 preview 回填（省流量）"


@pytest.mark.asyncio
async def test_trash_api_returns_preview_and_totals(tmp_path):
    """v2.18.65：真调一次 /trash 处理器 ⇒ preview + totals 都要有

    这条专门防"接口 500"类事故：一次编辑只落了一半（签名没加 field ✗
    调用处却传了 field="preview" ✗）⇒ 只有**真调处理器**才抓得住 ✗
    """
    import os

    import pytest

    if not os.environ.get("KIRA_CORE"):
        pytest.skip("需要 KIRA_CORE 的宿主集成用例")
    from test_helpers_plugin import build_plugin

    plugin, store = await build_plugin(tmp_path)
    try:
        sid = "a:dm:api"
        now = time.time()
        store.capture(
            sid,
            "e1",
            [dict(role="user", content="接口正文一段话", summary="", users=["a:u"], time=now)],
        )
        with store.connect() as db:
            rid = db.execute("SELECT id FROM records WHERE sid=?", (sid,)).fetchone()[0]
            db.execute("UPDATE records SET summary='' WHERE id=?", (rid,))
            db.execute("UPDATE records SET deleted=1 WHERE id=?", (rid,))
            db.commit()
        out = await plugin.api_trash(kind="records")
        row = out["items"][0]
        assert row.get("preview"), "接口必须带上 preview ✗"
        assert "接口正文" in row["preview"]
        assert set(out.get("totals") or {}) == {"facts", "records", "cold"}, "totals 字段要齐 ✗"
        # 冷归档页签走**同一条**回填路径（kind="cold"）⇒ 也要能正常返回 ✓
        cold_out = await plugin.api_trash(kind="cold")
        assert set(cold_out.get("totals") or {}) == {"facts", "records", "cold"}
        # 截断：preview 不超过 300 字（省流量 ✓）
        long_text = "长" * 900
        store.capture(sid, "e2", [dict(role="user", content=long_text, summary="", users=["a:u"], time=now + 5)])
        with store.connect() as db:
            rid2 = db.execute("SELECT id FROM records WHERE sid=? ORDER BY start DESC LIMIT 1", (sid,)).fetchone()[0]
            db.execute("UPDATE records SET summary='', deleted=1 WHERE id=?", (rid2,))
            db.commit()
        out2 = await plugin.api_trash(kind="records")
        previews = [r.get("preview") or "" for r in out2["items"]]
        assert any(len(p) == 300 for p in previews), "preview 必须截到 300 字 ✗"
    finally:
        await plugin.terminate()


def test_touch_tidy_accepts_timestamp_for_reset(tmp_path):
    """v2.18.66：touch_tidy(ids, 0) 必须能把限流清零（指定条目立刻可整理）

    原来那条路调的是不存在的 touch_tidy_at(ids, 0) ⇒ 真跑 AttributeError
    """
    store = _storage_mod().Store(tmp_path / "db")
    store.initialize()
    sid = "a:dm:tidy_at"
    store.capture(
        sid,
        "e1",
        [dict(role="user", content="随便一条", summary="s", users=["a:u"], time=time.time())],
    )
    with store.connect() as db:
        rid = db.execute("SELECT id FROM records WHERE sid=?", (sid,)).fetchone()[0]
    store.touch_tidy([rid])          # 默认 = 记 now ✓
    with store.connect() as db:
        marked = db.execute("SELECT tidy_at FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert marked > 0, "默认应记当前时间 ✓"
    store.touch_tidy([rid], 0)       # 传 0 = 清零 ✓
    with store.connect() as db:
        reset = db.execute("SELECT tidy_at FROM records WHERE id=?", (rid,)).fetchone()[0]
    assert reset == 0, "传 0 必须把限流清零 ✗"


def test_restore_after_retract_is_possible_with_safety(tmp_path):
    """v2.18.66：撤回后必须能**还原** + 不许偷偷改已删除的条目

    以前 `edit` 的查找写死了 `AND deleted=0` ✗ ⇒ 已撤回的那条永远查不到
    ⇒ 走「还原」必然 Conflict ✗（记忆与事实都一样 ✗）
    """
    import pytest as _pytest

    store = _storage_mod().Store(tmp_path / "db")
    store.initialize()
    sid = "a:dm:restore"
    fid = store.add_facts(
        sid,
        [dict(category="fact", subject="a:u", content="要被撤回的事实", reason="r",
              scenario="", tags=[], relations=[], source_ids=[], importance=5)],
    )[0]
    rev = store.facts_by_ids([fid])[0]["revision"]
    store.edit("fact", fid, rev, {"deleted": True}, "retract")
    assert store.facts_by_ids([fid], True)[0]["deleted"] == 1
    # 还原：以前必然 Conflict ✗
    rev2 = store.facts_by_ids([fid], True)[0]["revision"]
    store.edit("fact", fid, rev2, {"deleted": False}, "restore")
    assert store.facts_by_ids([fid], True)[0]["deleted"] == 0, "还原必须成功 ✗"
    # 安全边界：没有显式 deleted=False 时，不许改已删除的条目 ✓
    rev3 = store.facts_by_ids([fid])[0]["revision"]
    store.edit("fact", fid, rev3, {"deleted": True}, "retract again")
    rev4 = store.facts_by_ids([fid], True)[0]["revision"]
    with _pytest.raises(Exception) as info:
        store.edit("fact", fid, rev4, {"content": "偷偷改"}, "sneaky")
    assert "already deleted" in str(info.value)


@pytest.mark.asyncio
async def test_correct_tool_maintains_facts_end_to_end(tmp_path):
    """v2.18.66-68：真调 correct()（记忆维护统一入口）⇒ 事实的撤回/还原必须走通

    这条路上原来有**三道坎**（真跑实测）：
      ① get_fact 不存在 ⇒ AttributeError
      ② 事实 patch 带记录独有的 active ⇒ ValueError
      ③ accessible() 只在 records 表里找 id ⇒ 传事实 id 必然 "archive not found"
    ⇒ 所以「对事实做删除/还原/归档」以前**从来就没走通过** ✗
    """
    import os

    if not os.environ.get("KIRA_CORE"):
        pytest.skip("需要 KIRA_CORE 的宿主集成用例")
    from test_helpers_plugin import build_plugin
    from types import SimpleNamespace

    plugin, store = await build_plugin(tmp_path)
    try:
        sid = "a:dm:correct"
        rec = store.memorize(sid, "一条记忆", ["a:u"], 1.0, 2.0)
        fid = store.add_facts(
            sid,
            [dict(category="fact", subject="a:u", content="带来源的事实", reason="r",
                  scenario="", tags=[], relations=[], source_ids=[rec], importance=5)],
        )[0]
        event = SimpleNamespace(sid=sid, event_id="e1")
        # 返回形态随宿主而异（dict / 字符串）⇒ 断言**状态**才是关键 ✓
        out = await plugin.correct(event, "delete", kind="fact", ids=[fid], reason="撤回")
        assert "ok" in str(out) and "false" not in str(out).lower(), "撤回失败: %s" % str(out)[:80]
        assert (await store.call("facts_by_ids", [fid], True))[0]["deleted"] == 1
        out2 = await plugin.correct(event, "restore", kind="fact", ids=[fid], reason="还原")
        assert "ok" in str(out2) and "false" not in str(out2).lower(), "还原失败: %s" % str(out2)[:80]
        assert (await store.call("facts_by_ids", [fid], True))[0]["deleted"] == 0

        # v2.18.69（用户拍板）：事实没有"离开上下文"的概念 ⇒ **archive 等同于撤回** ✓
        out3 = await plugin.correct(event, "archive", kind="fact", ids=[fid], reason="归档")
        assert "ok" in str(out3) and "false" not in str(out3).lower(), "事实归档失败: %s" % str(out3)[:80]
        assert (await store.call("facts_by_ids", [fid], True))[0]["deleted"] == 1, (
            "事实的 archive 必须等同于撤回（deleted=1）✗"
        )
        await plugin.correct(event, "restore", kind="fact", ids=[fid], reason="还原")
        assert (await store.call("facts_by_ids", [fid], True))[0]["deleted"] == 0

        # update：改事实的正文（走 accessible + Edit 契约）
        rev = (await store.call("facts_by_ids", [fid], True))[0]["revision"]
        out4 = await plugin.correct(
            event, "update", kind="fact", ids=[fid], revision=rev,
            patch={"content": "改过的正文"}, reason="改内容",
        )
        assert "ok" in str(out4) and "false" not in str(out4).lower(), "update 失败: %s" % str(out4)[:80]
        assert (await store.call("facts_by_ids", [fid], True))[0]["content"] == "改过的正文"

        # merge：两条事实合并成一条
        other = store.add_facts(
            sid,
            [dict(category="fact", subject="a:u", content="另一条事实", reason="r",
                  scenario="", tags=[], relations=[], source_ids=[rec], importance=5)],
        )[0]
        out5 = await plugin.correct(
            event, "merge", kind="fact", ids=[fid, other], content="合并后的一条", reason="合并"
        )
        assert "ok" in str(out5) and "false" not in str(out5).lower(), "merge 失败: %s" % str(out5)[:80]
        alive = [
            f["content"]
            for f in store.facts_by_ids([fid, other], True)
            if f["deleted"] == 0
        ]
        assert alive == ["合并后的一条"], "合并后应只剩一条 ✗ 实际: %s" % alive

        # 记录那条路也要走通（v2.18.68：archive → restore → delete → restore 四步全过 ✓）
        rid = store.memorize(sid, "一条可归档的记录", ["a:u"], 1.0, 2.0)

        def record_state():
            with store.connect() as db:
                row = db.execute(
                    "SELECT active, deleted FROM records WHERE id=?", (rid,)
                ).fetchone()
            return dict(row)

        await plugin.correct(event, "archive", kind="record", ids=[rid], reason="归档")
        assert record_state() == {"active": 0, "deleted": 0}
        await plugin.correct(event, "restore", kind="record", ids=[rid], reason="还原")
        assert record_state() == {"active": 1, "deleted": 0}
        await plugin.correct(event, "delete", kind="record", ids=[rid], reason="删除")
        assert record_state() == {"active": 1, "deleted": 1}
        # ★ 已删除的记录必须还能还原（以前 accessible/get 都看不到它 ⇒ 必然失败 ✗）
        await plugin.correct(event, "restore", kind="record", ids=[rid], reason="再还原")
        assert record_state() == {"active": 1, "deleted": 0}, "已删除的记录必须能还原 ✗"
    finally:
        await plugin.terminate()


def test_versions_index_exists_and_is_used(tmp_path):
    """v2.18.70：回收站每行都查一次「这条最后被改/删的时间」⇒ versions 必须有索引

    实测：600 条事实 + 4 万条 versions 时
      有索引 → 16.8 ms ✓ ；无索引 → 2340.8 ms ✗（切页签要卡 2 秒+）
    """
    store = _storage_mod().Store(tmp_path / "db")
    store.initialize()
    with store.connect() as db:
        names = [r[1] for r in db.execute("PRAGMA index_list(versions)")]
        assert "version_target" in names, "versions 必须有 version_target 索引 ✗"
        plan = [row[-1] for row in db.execute(
            "EXPLAIN QUERY PLAN SELECT max(created) FROM versions "
            "WHERE kind='fact' AND target='x'"
        )]
    text = " ".join(plan)
    assert "version_target" in text, "这个查询必须走索引（否则每行一次全表扫描 ⇒ 卡）✗ 计划: %s" % text
