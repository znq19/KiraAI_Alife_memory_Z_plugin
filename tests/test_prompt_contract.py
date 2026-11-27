"""提示词 ↔ 契约 一致性守卫（v2.18.58）

守的是：**提示词说清的，契约才敢严** ✓
反过来——契约比提示词严 ⇒ 模型按提示词写也会被拒 ⇒ 白烧一次重试 ✗（本项目实测踩过三次 ✓）
"""

from test_memory import c, e  # noqa: F401


def test_compress_prompt_states_single_record_and_minimum_source_ids():
    """压缩与归类共用这份提示词 ✓ 归类每次只有一条记录 ⇒ 必须写清单记录怎么写 ✓"""
    text = e.build_instruction("compress", c.Settings())
    assert "至少一条" in text, "契约要求 source_ids min_length=1 ⇒ 提示词必须写明 ✓"
    assert "只有一条记录" in text, "归类永远是单条 ⇒ 必须写明这种情形 ✓"
    assert "就是那一条记录的 id" in text, "要给出可照做的写法，不能只说'至少一条' ✓"


def test_compress_prompt_keeps_required_array_keys():
    """tags / relations 是必填键 ✓ 提示词要说明没内容也给空数组 ✓（防缺键⇒白重试 ✗）"""
    text = e.build_instruction("compress", c.Settings())
    assert "没有内容就给空数组" in text


def test_audit_action_content_only_required_when_it_is_used():
    """只有 correct / merge 会改写正文 ⇒ 只有它们需要 content ✓

    以前 content 是必填 ✗ 而提示词没要求每条都给 ✗
    ⇒ keep 不带 content 这种**合乎提示词**的输出会被判失败、白走一次重试 ✗
    """
    base = dict(target_id="f1", source_ids=["f1"], reason="r")
    for action in ("keep", "retract"):
        assert c.AuditAction(action=action, **base).content == ""
    for action in ("correct", "merge"):
        try:
            c.AuditAction(action=action, **base)
        except ValueError:
            pass
        else:
            raise AssertionError("%s 不带 content 必须被拒 ✓" % action)
        assert c.AuditAction(action=action, content="新内容", **base).content


def test_compact_schemas_do_not_contradict_the_instruction():
    """两份文字都会发给模型 ⇒ **互相打架就是 bug** ✗（2026-09-21 全量扫查查出三处）"""
    compress = e.COMPACT_SCHEMAS["compress"]
    assert "importance" in compress.split("必填")[1].split("\n")[0], (
        "compress 的必填清单漏了 importance（指令写着九个键都要有）✗"
    )
    audit = e.COMPACT_SCHEMAS["audit"]
    assert "correct/merge 一般要 content" in audit, (
        "audit 的 content 只在改写正文时才需要 ✗"
    )
    assert "只改 category 时 content 留空" in audit, (
        "audit 应允许仅修正分类，不强迫重写正文 ✗"
    )
    assert "可省" not in audit, (
        "声明里不能写「可省」✗ —— 那等于给模型一个偷懒去选 keep/retract 的台阶"
    )
    dedupe = e.COMPACT_SCHEMAS["dedupe"]
    assert "宁可 keep" not in dedupe, (
        "dedupe 声明不能劝 keep ✗ 默认指令明确只输出 merge"
        "（保守模式该不该 keep 由指令自己说 ✓）"
    )
    assert "reason ≤15 字" in dedupe and "reason ≤60 字" not in dedupe, (
        "dedupe 声明的 reason 上限必须与指令一致（≤15 字）✗"
    )
    assert '"content": str?' in e.COMPACT_SCHEMAS["fact_merge"], (
        "fact_merge 模板里 content 要标成条件必填（只有 merge 需要）✗"
    )