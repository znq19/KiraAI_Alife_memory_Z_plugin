"""前端 JS 逻辑测试：把 web/app.js 里的**真函数**抠出来在 node 里跑（没有 node 就跳过 ✓）

为什么要有它：像"两个模型芯片长得一模一样"这种问题，Python 侧测不到 ✗
只测后端会漏掉**纯前端**的显示/交互逻辑 ✗ —— 用户问题 1（同名模型分不清）正是这一类 ✓
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JS = ROOT / "web" / "app.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None or not APP_JS.exists(),
    reason="需要 node 与 web/app.js",
)

DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");

function extract(name) {
  // 支持 function f(){} / const f = (…) => …; / const f = function…; 三种写法 ✓
  let head = "function " + name + "(";
  let i = src.indexOf(head);
  let arrow = false;
  if (i < 0) { head = "const " + name + " = ("; i = src.indexOf(head); arrow = i >= 0; }
  if (i < 0) { head = "const " + name + " = function"; i = src.indexOf(head); arrow = i >= 0; }
  if (i < 0) throw new Error("抠不到函数 " + name + "（改名了？）");
  if (arrow) {
    let depth = 0;
    for (let j = i + head.length; j < src.length; j++) {
      const c = src[j];
      if ("([{".includes(c)) depth++;
      else if (")]}".includes(c)) depth--;
      else if (c === ";" && depth <= 0) return src.slice(i, j + 1);
    }
    throw new Error("箭头函数 " + name + " 找不到结尾分号");
  }
  let depth = 0, started = false;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === "{") { depth++; started = true; }
    else if (c === "}") { depth--; if (started && depth === 0) return src.slice(i, j + 1); }
  }
  throw new Error("函数 " + name + " 大括号不平衡");
}

const want = ["esc", "modelLabel", "kindsFor", "kindTag", "marqueeName", "markMarquees", "restoreKeysOf", "hostDefaultLabel", "plainModelSelect", "chainCombo", "chainLookup", "chainKeyOf", "chainRefsOf", "chainNameOf",
              "chainRowHtml", "chainListHtml"];
const code = want.map(extract).join("\n");
const sandbox = { Set, Map, Array, String, Object, JSON, console, config: { settings: {} },
  };
require("vm").createContext(sandbox);
require("vm").runInContext(code + "\n;globalThis.__fn={modelLabel,chainLookup,chainListHtml,chainRefsOf,marqueeName,markMarquees,restoreKeysOf,hostDefaultLabel,plainModelSelect,chainCombo};", sandbox);
const F = sandbox.__fn;

const dup = [
  { id: "pid-a:uuid-1", name: "gpt-4o", model: "gpt-4o", provider: "Workbuddy", kind: "llm", legacy: [], dup: true, tag: "pid-a" },
  { id: "pid-b:uuid-2", name: "gpt-4o", model: "gpt-4o", provider: "Workbuddy", kind: "llm", legacy: [], dup: true, tag: "pid-b" },
  { id: "pid-c:uuid-3", name: "gpt-4o", model: "gpt-4o", provider: "Trae", kind: "llm", legacy: [] },
  { id: "pid-d:emb", name: "text-emb", model: "text-emb", provider: "Trae", kind: "embedding", legacy: [] },
];
const labels = dup.map(F.modelLabel);
const uniq = new Set(labels);
const html = F.chainListHtml([], dup, false, F.chainLookup(dup));
const rows = (html.match(/data-ref=/g) || []).length;
// ★ 长名字滚动（用户要求）：结构 + 只有放不下才滚 ✓
const mq = F.marqueeName("gpt-4o · Workbuddy · pid-a");
const toggled = [];
F.markMarquees({ querySelectorAll: () => [
  { scrollWidth: 300, clientWidth: 120, classList: { toggle: (_c, on) => { toggled.push(on); } } },
  { scrollWidth: 100, clientWidth: 120, classList: { toggle: (_c, on) => { toggled.push(on); } } },
]});
// ★ 成对恢复默认：链 ⇄ 旧单模型 必须互相带上 ✓
sandbox.config.schema = { properties: {
  compress_model: {}, compress_models: {}, audit_model: {}, audit_models: {},
  jev_model_name: {}, jev_models: {}, rerank_model: {}, rerank_models: {},
  embedding_model: {} } };
const restore_pairs = {
  compress_models: F.restoreKeysOf("compress_models"),
  audit_models: F.restoreKeysOf("audit_models"),
  rerank_models: F.restoreKeysOf("rerank_models"),
  jev_models: F.restoreKeysOf("jev_models"),
  compress_model: F.restoreKeysOf("compress_model"),
};
// 成对清空后的状态：两个字段都为空 ⇒ 解析出的链必须是空的 ✓
sandbox.config.settings = { compress_models: [], compress_model: "" };
const both_cleared_chain = F.chainRefsOf("compress_model", "").length;
// ★ 2026-10-15（用户实拍严重 bug 回归）：链控件与旧字段的**属性必须各自正确** ✗
//   否则通用渲染器一覆盖 ⇒ 权威的链提交不出去 ⇒ 保存后切页回跳 ✓
const comboHtml = F.chainCombo("compress_model", "prov:X", []);
const selHtml = F.plainModelSelect("compress_model", "prov:X", []);
const markup = {
  combo_chainKey: comboHtml.includes('data-key="compress_models"'),
  combo_arrayKind: comboHtml.includes('data-kind="array"'),
  combo_hasField: comboHtml.includes('data-field="compress_models"'),
  select_legacyKey: selHtml.includes('data-key="compress_model"'),
  select_stringKind: selHtml.includes('data-kind="string"'),
};
// ★ 3.1.1：decision（TypeSafe）/ rerank 必须各进各的下拉，不能混进普通 LLM
const mixed = [
  { id: "p:l1", name: "chat", provider: "P", kind: "llm", legacy: [] },
  { id: "p:d1", name: "jev-latest", provider: "TS", kind: "decision", legacy: [] },
  { id: "p:r1", name: "rerank-x", provider: "P", kind: "rerank", legacy: [] },
  { id: "p:e1", name: "emb", provider: "P", kind: "embedding", legacy: [] },
];
const cnt = (kinds) => (F.chainListHtml([], mixed, kinds, F.chainLookup(mixed)).match(/data-ref=/g) || []).length;
const kind_counts = {
  llm: cnt(["llm"]), decision: cnt(["decision"]), rerank: cnt(["rerank"]), embedding: cnt(["embedding"]),
  sel_decision_has_d: F.plainModelSelect("jev_decision_model", "", mixed).includes('value="p:d1"'),
  sel_decision_has_l: F.plainModelSelect("jev_decision_model", "", mixed).includes('value="p:l1"'),
  sel_llm_has_d: F.plainModelSelect("jev_model", "", mixed).includes('value="p:d1"'),
  sel_rerank_has_r: F.plainModelSelect("rerank_model", "", mixed).includes('value="p:r1"'),
  sel_rerank_has_l: F.plainModelSelect("rerank_model", "", mixed).includes('value="p:l1"'),
};
const empty_labels = {
  jev: F.hostDefaultLabel("jev_model"),
  jev_dec: F.hostDefaultLabel("jev_decision_model"),
  rerank: F.hostDefaultLabel("rerank_model"),
  compress: F.hostDefaultLabel("compress_model"),
};
console.log(JSON.stringify({
  restore_pairs, both_cleared_chain, markup, kind_counts, empty_labels,
  labels, unique: uniq.size, rows,
  legacy_fallback: F.chainRefsOf("compress_model", "workbuddy:gpt-4o").length,
  // ★ 恢复默认后的状态：值清空 ⇒ 链必须为空（= 跟随宿主默认 ✓ 不是"零档" ✗）
  empty_chain: F.chainRefsOf("compress_models", "").length,
  empty_legacy: F.chainRefsOf("compress_model", "").length,
  marquee: {
    copies: (mq.match(/mmq-tx/g) || []).length,
    hasInner: mq.indexOf("mmq-in") >= 0,
    hasTitle: mq.indexOf('title="') >= 0,
    escaped: F.marqueeName("a<b>&c"),
  },
  toggled,
}));
"""


def _run_driver():
    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / "driver.js"
        driver.write_text(DRIVER, encoding="utf-8")
        out = subprocess.run(["node", str(driver), str(APP_JS)],
                             capture_output=True, text=True, timeout=120)
        assert out.returncode == 0, "node 跑失败：%s" % (out.stderr[-800:] or out.stdout[-800:])
        return json.loads(out.stdout.strip().splitlines()[-1])


def test_model_field_kinds_are_isolated():
    """decision / rerank / llm / embedding 各自只看自己的类型（3.1.1 修复）。"""
    data = _run_driver()
    k = data["kind_counts"]
    assert k == {
        "llm": 1, "decision": 1, "rerank": 1, "embedding": 1,
        "sel_decision_has_d": True, "sel_decision_has_l": False,
        "sel_llm_has_d": False,
        "sel_rerank_has_r": True, "sel_rerank_has_l": False,
    }, k

def test_empty_model_labels_are_honest():
    """JEV/重排空值不能显示成「使用默认主模型」（会误导用户）。"""
    d = _run_driver()["empty_labels"]
    assert "未选择" in d["jev"] and "未选择" in d["jev_dec"], d
    assert "不启用重排" in d["rerank"], d
    assert "使用 KiraAI 默认快速模型" == d["compress"], d

def test_same_name_models_render_distinguishable_labels():
    """★ 同名/同名不同型提供商的模型，标签必须各不相同（否则用户分不清、易删错 ✗）"""
    data = _run_driver()
    labels = data["labels"]
    assert len(labels) == 4, "标签数量不对 ✗"
    assert len(set(labels)) == 4, "有名模型的标签重复了 ✗ ⇒ 前端仍然分不清：%s" % labels
    assert any("Workbuddy · pid-a" in x for x in labels), "重名条目没带上区分标识 ✗：%s" % labels
    assert any("向量" in x for x in labels), "向量模型标签应标注类型 ✗：%s" % labels
    assert data["rows"] == 3, "链编辑器应只列出 llm 模型（3 个）✗ 实际 %d" % data["rows"]


def test_legacy_single_model_still_counts_as_first_link():
    """★ 旧配置只有单模型 ⇒ 仍要显示成"第 1 档"（不能因为改造而丢 ✗）"""
    data = _run_driver()
    assert data["legacy_fallback"] == 1, "旧单模型值没有回退成第 1 档 ✗"


def test_long_model_name_marquee_is_ready_for_scrolling():
    """★ 用户要求：名字太长（名+提供商+撞车后缀）⇒ 悬停与选中时**横向滚动** ✓

    结构必须是「两份文本 + 可动画容器」✓（CSS 位移 -50% 正好一份 ⇒ 无缝循环 ✓）
    并且要有 title（悬停能看到完整名 ✓）
    """
    data = _run_driver()
    mq = data["marquee"]
    assert mq["copies"] == 2, "marquee 里必须放**两份**文本，否则滚动不无缝 ✗"
    assert mq["hasInner"], "缺少可动画的容器 .mmq-in ✗"
    assert mq["hasTitle"], "缺少 title ⇒ 鼠标悬停看不到完整名 ✗"
    assert "&lt;" in mq["escaped"] and "&amp;" in mq["escaped"], "名字没转义 ⇒ 有 XSS 风险 ✗"


def test_marquee_only_scrolls_when_text_overflows():
    """★ 只有**真的放不下**才滚 ✓（短名字乱动很难看 ✗）"""
    data = _run_driver()
    assert data["toggled"] == [True, False], (
        "溢出判定不对 ✗（长名字应 True、短名字应 False）实际 %s" % data["toggled"])


def test_marquee_css_exists_in_stylesheet():
    """★ CSS 里必须有滚动动画 + 悬停/选中/悬停显示框三条触发规则 ✓"""
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert "@keyframes mmq-run" in css, "缺少滚动关键帧 ✗"
    assert "translateX(-50%)" in css, "位移动画必须是 -50%（一份宽 ⇒ 无缝 ✓）✗"
    assert ".mmq.is-wide:hover .mmq-in" in css, "悬停触发规则缺失 ✗"
    assert ".chain-item.is-on" in css, "已选中条目常驻滚动规则缺失 ✗"
    assert "prefers-reduced-motion" in css, "应尊重系统「减少动态效果」 ✗"


def test_restore_default_clears_the_chain_so_host_default_applies():
    """★ 用户问的："前端点恢复默认并保存，真的恢复默认并正常使用了吗？"

    恢复默认 ⇒ 值被清空 ⇒ 前端解析出的链必须是**空**（= 跟随宿主默认 ✓）
    而不是残留旧值 ✗（那样保存后还在用旧模型 ⇒ 看起来"恢复了"其实没有 ✗）
    """
    data = _run_driver()
    assert data["empty_chain"] == 0, "清空后链里还有残留 ✗ 实际 %s 档" % data["empty_chain"]
    assert data["empty_legacy"] == 0, "清空后旧单模型字段还有残留 ✗"
    # 而"有值"时仍然照旧解析成 1 档（不能把正常配置也清掉 ✗）
    assert data["legacy_fallback"] == 1, "有旧单模型值时应当解析成第 1 档 ✗"


def test_restore_default_clears_both_chain_and_legacy_field():
    """★ 用户实拍的严重问题：**只清链会让旧字段把旧模型复活** ✗

    现象（2026-10-14 用户报）："前端点恢复默认模型后保存，以压缩/审计为例，切页后模型又回到原本选的，
    从而跟不上最新设置并失败" ✗
    根因：`chainRefsOf` 在**链为空时会回退读 `*_model`**（为兼容老配置 ✓）
      ⇒ 「恢复默认」只清了 `*_models` ✗ ⇒ 旧字段里的旧模型被**复活** ✗
      ⇒ 切页/重载后 UI 又显示回旧模型，**运行时也照旧用它** ✗
    修法：恢复默认必须**成对清空**（`*_models` + `*_model`/`*_model_name` ✓）
    """
    data = _run_driver()
    pairs = data["restore_pairs"]
    # 链字段 ⇒ 必须把配对的旧字段一起带上 ✓
    assert pairs["compress_models"] == ["compress_models", "compress_model"], pairs
    assert pairs["audit_models"] == ["audit_models", "audit_model"], pairs
    assert pairs["rerank_models"] == ["rerank_models", "rerank_model"], pairs
    # JEV 的旧字段叫 jev_model_name（不是 jev_model）⇒ 也必须能配对 ✓
    assert "jev_model_name" in pairs["jev_models"], pairs
    # 反向：从旧字段点恢复默认 ⇒ 也要把链带上 ✓
    assert "compress_models" in pairs["compress_model"], pairs
    # 成对清空后 ⇒ 链必须是空的（= 跟随宿主默认 ✓ 而不是"复活旧值" ✗）
    assert data["both_cleared_chain"] == 0, (
        "成对清空后链里还有残留 ⇒ 会继续用旧模型 ✗ 实际 %s" % data["both_cleared_chain"])


def test_chain_combo_and_legacy_select_carry_correct_attributes():
    """★ 用户实拍严重 bug 的回归：链控件 / 旧字段的属性必须**各自正确** ✗

    背景（2026-10-15，在真浏览器 + 真面板上端到端定位）：
      通用渲染器渲染完字段后会做一次字符串替换：
        `input.replace(/<(input|select|textarea)/, '<$1 data-key="…" data-kind="…"')`
      而模型字段的 HTML 其实是**整条回退链控件** ⇒ 它命中的是控件里的**第一个 input**
      （= 链的权威输入 ✗）⇒ 把 `data-key="compress_models"` / `data-kind="array"`
      **覆盖成** `compress_model` / `string` ✗
      ⇒ 保存时权威的链**从没被提交** ⇒ 后端保留旧链 ⇒ **切页回来显示旧模型** ✓（用户报的）
    修法：链控件自己带对属性且**跳过**那条替换 ✓；隐藏 `<select>` 补上
      `data-key="*_model"` + `data-kind="string"`（它本来就负责收集旧字段 ✓）
    本测试直接断言两个控件的**生成结果**，防止再被覆盖 ✗
    """
    data = _run_driver()
    m = data["markup"]
    assert m["combo_chainKey"], "回退链输入的 data-key 必须是 <*_models> ✗（被覆盖就会切页回跳 ✓）"
    assert m["combo_arrayKind"], "回退链输入的 data-kind 必须是 array ✗（否则会按字符串提交 ⇒ 422 ✓）"
    assert m["combo_hasField"], "回退链输入必须带 data-field=<*_models> ✓"
    assert m["select_legacyKey"], "隐藏 select 必须带 data-key=<*_model>（否则旧字段不被收集 ✗）"
    assert m["select_stringKind"], "隐藏 select 的 data-kind 应为 string ✗"


def test_source_guards_for_the_three_restore_default_bugs():
    """★ 三个已修 bug 的**源码级守卫**（改回去就报红 ✓）"""
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    # ① 通用渲染器必须**跳过**模型回退链控件 ✗（否则覆盖链的属性 ⇒ 提交不出去）
    assert 'key.endsWith("_model") && key !== "embedding_model"' in src, \
        "通用渲染器没有跳过模型链控件 ✗ ⇒ 链属性会被覆盖 ⇒ 切页回跳 ✓"
    # ② readConfig 必须优先用 data-field ✓
    assert "e.dataset.field || e.dataset.key" in src, "readConfig 没优先用 data-field ✗"
    # ③ 草稿恢复**绝不许**用草稿里的旧 revision ✗（否则一次失败后永远 409 ✓）
    #    注意：注释里引用了原代码 ⇒ 要**去掉注释行**再判 ✗
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("//"))
    assert "config.revision = draft.configRevision" not in code, \
        "草稿又在覆盖 revision ✗ ⇒ 一次保存失败后会永久 409（用户报的\"保存失败\" ✓）"


def test_stylesheet_is_versioned_and_box_scrolls_always():
    """★ 用户反馈"模型名不滚动"的两条真原因（2026-10-15）

    ① `index.html` 引 `style.css` **没有版本号** ✗ ⇒ 浏览器长期用**缓存的旧 CSS**
       ⇒ 之后所有 CSS 改动（含滚动动画）都不会生效 ✗
    ② 显示框里的名字原来**只在鼠标悬停时**才滚 ✗ ⇒ 用户要求"点击上（选中）也要滚" ✓
    """
    idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert 'href="style.css?v=' in idx, "style.css 必须带版本号 ✗（否则旧 CSS 一直缓存 ✓）"
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert ".chain-box .mmq.is-wide .mmq-in," in css, \
        "显示框里的名字必须**常驻滚动**（不只是悬停 ✓）"
    assert "@keyframes mmq-run" in css and "translateX(-50%)" in css


def test_styles_are_injected_and_build_is_never_stale():
    """★ 两条守卫（2026-10-15 用户实拍之后定的）

    ① **滚动样式由 app.js 注入**（`ensureMarqueeStyles` ✓）
       上一版只给 style.css 加 `?v=` ✗ —— 只要框架/浏览器缓存了 CSS，动画就还是加载不上 ✗
    ② **页面 HTML 被缓存时能自愈**（`index.html` 里的版本比对脚本 ✓）
       旧 HTML 指向旧资源 ⇒ 用户"更新了插件却没变化" ✗ ⇒ 发现版本不一致就自动强刷一次 ✓
    ③ 面板里**不允许再塞任何诊断/调试行** ✗（用户明确要求 ✓）
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "function ensureMarqueeStyles()" in src, "滚动样式没有从 JS 注入 ✗"
    assert "ensureMarqueeStyles();" in src, "注入函数没有被调用 ✗"
    for bad in ("modelsDiagText", "APP_BUILD", "chain-src"):
        assert bad not in src, "面板里不该出现诊断/调试物：%s ✗（用户明确不允许 ✓）" % bad
    idx = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    assert "app.js?v=" in idx, "app.js 必须带版本号 ✗"
    assert "alife-asset-bust-" in idx, "缺少『HTML 被缓存就自愈强刷』的脚本 ✗"


def test_marquee_survives_global_reduced_motion_disable():
    """★ 滚动必须**压过面板全局的"减弱动态效果就禁用所有动画"** ✗

    真因（2026-10-15 用户实拍"怎么都不滚"）：
      style.css 里有 `@media (prefers-reduced-motion: reduce){ *{ animation:none !important } }` ✗
      ⇒ 手机开了"减弱动态效果"（很多手机默认开）⇒ 滚动被**全局禁用** ✗
    ⇒ 所以滚动规则必须带 `!important` 且**优先级更高** ✓（并把该设置下的时长改成"放慢" ✓）
    另：菜单**展开后**必须重新测量（隐藏时测量为 0 ✗ ⇒ 永远标不上"需要滚" ✗）
    """
    src = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert "animation:mmq-run 7s linear infinite !important" in src, \
        "滚动动画必须带 !important ✗（否则被全局禁用压过 ⇒ 怎么都不滚 ✓）"
    assert "animation-duration:9s !important" in src, "减弱动态效果下应是\"放慢\"而不是取消 ✗"
    assert "markMarquees(combo);" in src, "菜单展开后没有重新测量 ✗（隐藏时测量为 0 ✓）"
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    assert "mmq-run 7s linear infinite !important" in css, "style.css 里的滚动规则也要带 !important ✗"
