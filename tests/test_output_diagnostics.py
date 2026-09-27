import json
import pytest
from test_memory import c, s, e


@pytest.mark.asyncio
async def test_compression_retry_reports_field_without_leaking_output(tmp_path):
    store = s.Store(tmp_path / "db")
    store.initialize()
    store.capture(
        "legacy:unscoped",
        "turn",
        [dict(role="user", content="完整原文", users=[], time=1.0) for _ in range(4)],
    )
    calls = []

    async def model(_, purpose, instruction, schema, payload):
        calls.append(instruction + c.dump(payload))
        if len(calls) == 1:
            return '{"summary":"私人内容不得进入诊断", "facts":"错误类型"}'
        assert "facts" in payload["output_feedback"]
        assert "list_type" in payload["output_feedback"]
        assert "私人内容不得进入诊断" not in payload["output_feedback"]
        return '{"summary":"压缩完成", "facts":[]}'

    cfg = c.Settings(compress_batch_mode="records", threshold=4, batch_size=2, model_retries=1)
    await e.Engine(store, lambda: cfg, model, None, None).compress("legacy:unscoped")
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_audit_invalid_record_id_retries_before_store_write(tmp_path):
    store = s.Store(tmp_path / "db")
    store.initialize()
    rid = store.memorize("a:dm:u", "喜欢猫", ["a:u"], 1.0, 1.0)
    user_src = _user_record(store, "a:dm:u", "我喜欢猫", ["a:u"], 2.0)
    with store.connect() as db:
        fid = store._add_fact(
            db,
            "a:dm:u",
            dict(
                category="preference",
                subject="a:u",
                content="喜欢猫",
                reason="",
                scenario="",
                tags=[],
                relations=[],
                source_ids=[rid, user_src],
            ),
        )
    calls = []

    async def model(_, purpose, instruction, schema, payload):
        calls.append(instruction)
        return c.dump(
            {
                "actions": [
                    dict(
                        action="correct",
                        # 入参里事实 id 已换成 f1..fN 短别名；第一次故意回一个记录 id 触发重试
                        target_id="f1",
                        source_ids=[rid if len(calls) == 1 else "f1"],
                        content="喜欢猫，但不养猫",
                        reason="按原文核对",
                        relations=[],
                    )
                ]
            }
        )

    cfg = c.Settings(model_retries=1)
    await e.Engine(store, lambda: cfg, model, None, None).audit("a:dm:u")
    assert len(calls) == 2
    assert "unknown audit evidence" in calls[1]
    assert store.facts("a:dm:u")[0]["content"] == "喜欢猫，但不养猫"


def test_same_name_manual_reason_is_recorded(tmp_path):
    store = s.Store(tmp_path / "db")
    store.initialize()
    store.observe_name("qq:123", "小夏", observed=1.0)
    rev = store.entities()[0]["revision"]
    store.observe_name(
        "qq:123",
        "小夏",
        revision=rev,
        reason="核对本人确认无误",
        source="manual",
        observed=2.0,
    )
    assert store.entities()[0]["history"][0]["reason"] == "核对本人确认无误"


def test_search_can_exclude_seen_ids_before_pagination(tmp_path):
    store = s.Store(tmp_path / "db")
    store.initialize()
    ids = [
        store.memorize("a:dm:u", f"猫的经历{i}", ["a:u"], float(i), float(i))
        for i in range(5)
    ]
    result = store.search(scope="global", lexical="猫", limit=2, exclude_ids=ids[:2])
    assert result["total"] == 3 and [r["id"] for r in result["items"]] == ids[2:4]


def test_read_archive_projection_avoids_double_encoded_batch_and_pages_ids():
    from alife_test_plugin.retrieval import archive_view

    content = c.dump(
        [dict(id=f"legacy-{i}", role="user", content='原文"不丢失"') for i in range(70)]
    )
    row = dict(
        id="parent",
        sid="legacy:unscoped",
        role="assistant",
        level=1,
        start=1.0,
        end=2.0,
        summary="摘要",
        users=[],
        speaker="小明",
        revision=1,
        permanent=0,
        content=content,
        children=[f"legacy-{i}" for i in range(70)],
    )
    # v2.18.19：紧凑化后**不再回传子条目内容** ✗ 只保留条数与翻页 ✓
    # （去码之后模型也寻不到它们 ✓ 回传纯属浪费 ✓）
    view = archive_view(row)
    assert "content" not in view and view["kids"] == 70
    assert view["next"] == 20
    full = archive_view(row, 60, 20, True)
    # v2.18.19：没有更多时不写 `next` ✓（省掉 `"next":null` ✓）所以用 .get ✓
    assert full.get("next") is None
    assert full["content"] == json.loads(content) and row["content"] == content


def test_migration_identity_labels_never_infer_platform(tmp_path):
    from alife_test_plugin.retrieval import identity_info

    assert identity_info("legacy:global")["lookup_id"] == ""
    assert identity_info("legacy:unscoped")["lookup_id"] == ""
    assert identity_info("legacy:user:12001")["lookup_id"] == ""
    assert identity_info("legacy:user:qq:12002")["lookup_id"] == "qq:12002"
    store = s.Store(tmp_path / "db")
    store.initialize()
    rid = store.memorize("legacy:user:qq:123", "旧记忆", ["qq:123"], 1.0, 1.0)
    original = store.get(rid)
    store.observe_name("qq:123", "小夏", observed=10.0)
    n = store.entities(ids=["legacy:user:qq:123"])[0]
    assert "小夏" in n["label"] and n["name_source_id"] == "qq:123"
    assert store.get(rid) == original


def test_diagnostics_do_not_echo_extra_keys_or_private_values():
    from alife_test_plugin.output_validation import diagnostic

    with pytest.raises(ValueError) as caught:
        c.parse_output(
            '{"summary":"正常", "facts":[], "PRIVATE_SECRET":"敏感正文"}', c.Compression
        )
    text = diagnostic(caught.value)
    assert (
        "extra_forbidden" in text
        and "PRIVATE_SECRET" not in text
        and "敏感正文" not in text
    )


def test_recall_window_is_bounded_and_has_no_cross_caller_state():
    from alife_test_plugin.retrieval import RecallWindow

    w = RecallWindow()
    w.remember(("group", "user1", "global"), "猫", ["one"])
    assert not w.get(("group", "user2", "global"))["ids"]
    assert not w.get(("group", "user1", "session"))["ids"]
    for n in range(300):
        w.remember(n, "主题", [str(i) for i in range(300)])
    assert len(w.entries) == 256 and len(w.get(299)["ids"]) == 300


@pytest.mark.asyncio
async def test_audit_bounds_complete_evidence_and_keeps_review_reason(tmp_path):
    store = s.Store(tmp_path / "db")
    store.initialize()
    for i in range(3):
        rid = store.memorize("a:dm:u", "完整证据" * 800, ["a:u"], float(i), float(i))
        user_src = _user_record(store, "a:dm:u", "事实%d" % i, ["a:u"], float(i) + 0.5)
        with store.connect() as db:
            store._add_fact(
                db,
                "a:dm:u",
                dict(
                    category="fact",
                    subject="a:u",
                    content=f"独立事实{i}",
                    reason="",
                    scenario="",
                    tags=[],
                    relations=[],
                    source_ids=[rid, user_src],
                ),
            )

    async def model(_, purpose, instruction, schema, payload):
        assert len(payload["facts"]) == 1
        # v2.18.9 回声防线：审计证据必须**带上来源标记** ✗
        # （曾经这里重建字典把 sp/bot 丢了 ✗ 提示词写着 bot 而载荷没有 → 模型无法遵守 ✓）
        # 证据 = 这条事实自己的来源 ✓（"前后各 6 条"的方案按用户决定**已撤掉** ✓）
        assert len(payload["evidence"]) == 2, "证据 = 事实自己的来源（助手 + 用户）✓"
        assert any(e.get("bot") == 1 for e in payload["evidence"]), "助手来源证据必须标 bot ✗"
        assert any(e.get("sp") for e in payload["evidence"]), "用户来源证据必须标 sp ✗"
        assert payload["evidence"][0]["content"] == "完整证据" * 800
        fid = payload["facts"][0]["id"]
        return c.dump(
            {
                "actions": [
                    dict(
                        action="keep",
                        target_id=fid,
                        source_ids=[fid],
                        content=payload["facts"][0]["content"],
                        reason="原文证据充分，保留",
                        relations=None,
                    )
                ]
            }
        )

    cfg = c.Settings(compress_input_chars=4000)
    await e.Engine(store, lambda: cfg, model, None, None).audit("a:dm:u")
    assert len(store.facts("a:dm:u")) == 3
    history = store.edit_history("fact", [f["id"] for f in store.facts("a:dm:u")])
    assert (
        len(history) == 1
        and next(iter(history.values()))[0]["reason"] == "原文证据充分，保留"
    )

def test_diagnostics_hint_allowed_enum_and_relation_nesting():
    from alife_test_plugin.output_validation import diagnostic

    payload = c.dump(
        {
            "summary": "正常摘要",
            "facts": [
                {
                    "category": "乱写的分类",
                    "subject": "qq:1",
                    "content": "事实",
                    "reason": "",
                    "scenario": "",
                    "tags": [],
                    "relations": [],
                    "predicate": "朋友",
                    "object": "qq:2",
                    "source_ids": ["r1"],
                },
                {
                    "category": "event",
                    "subject": "qq:1",
                    "content": "另一条",
                    "reason": "",
                    "scenario": "",
                    "tags": [],
                    "relations": [
                        {"subject": "qq:1", "predicate": "认为", "object": "qq:2"}
                    ],
                    "source_ids": ["r1"],
                },
            ],
        }
    )
    with pytest.raises(ValueError) as caught:
        c.parse_output(payload, c.Compression)
    text = diagnostic(caught.value)
    assert "event/fact/preference/commitment" in text, text
    assert "relations:[{subject,predicate,object}]" in text, text
    assert "谓词没有表达具体关系" in text, text
    assert "乱写的分类" not in text, "不能回显模型输出的非法值"


def test_category_aliases_are_canonicalised():
    base = dict(
        subject="qq:1",
        content="内容",
        reason="",
        scenario="",
        tags=[],
        relations=[],
        source_ids=["r1"],
    )
    for given, expected in [
        ("偏好", "preference"),
        ("Preference", "preference"),
        ("PREFERENCE", "preference"),
        ("general", "fact"),
        ("关系", "relationship"),
        ("event", "event"),
    ]:
        assert c.Fact(category=given, **base).category == expected
    with pytest.raises(ValueError):
        c.Fact(category="乱写的分类", **base)


def test_compress_instruction_lists_categories_and_relation_shape():
    instruction = e.build_instruction("compress", c.Settings())
    assert "event/fact/preference/commitment/relationship/profile/resource/self" in instruction
    assert "relations:[{subject,predicate,object}]" in instruction or (
        "relations 必须是数组" in instruction and "predicate/object" in instruction
    )


def _user_record(store, sid, content, users, t):
    """造一条**用户**记录。

    v2.18.9 回声防线：审计要改写正文，必须拿得出**用户**的佐证 ✗
    只引用助手自己说过的记录会被服务端拦下 ✓（那正是"用她自己的话改写记忆"）
    """
    store.capture(sid, "u%d" % int(t * 100), [{
        "role": "user", "content": content, "users": list(users),
        "speaker": list(users)[0], "time": float(t),
    }])
    with store.connect() as db:
        return db.execute(
            "SELECT id FROM records WHERE role='user' ORDER BY created DESC LIMIT 1"
        ).fetchone()[0]


# ─────────── v2.18.57：归类报错不再吞真因 + 传输层容错 ───────────


def test_diagnostic_keeps_real_reasons():
    """诊断不许再吞真因 ✓（用户实测：归类失败只看到兜底句 ✗）"""
    import importlib

    v = importlib.import_module("alife_test_plugin.output_validation")
    OutputRejected, diagnostic = v.OutputRejected, v.diagnostic

    # ① 已"被拒"的形态 ⇒ 透传真诊断 ✓
    assert (
        diagnostic(OutputRejected("facts.0.content: string_too_long"))
        == "facts.0.content: string_too_long"
    )
    # ② 我们自己的静态 ValueError ⇒ 直接回显 ✓（这句以前被兜底句盖掉 ✗）
    assert (
        diagnostic(ValueError("unknown classification source"))
        == "unknown classification source"
    )
    # ③ 清单里的照旧原样回显 ✓
    assert diagnostic(ValueError("unknown source id")) == "unknown source id"
    # ④ 非 ValueError / 含换行 / 过长 ⇒ 仍用兜底句 ✓（不给噪音 ✓）
    assert diagnostic(TypeError("x")) == "输出不是契约要求的JSON对象或类型"
    assert diagnostic(ValueError("a\nb")) == "输出不是契约要求的JSON对象或类型"
    assert diagnostic(ValueError("y" * 300)) == "输出不是契约要求的JSON对象或类型"

