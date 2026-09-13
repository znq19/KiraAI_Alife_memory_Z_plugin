"""对比注入内容的几种编码：省多少 token？"""
import json

FACTS = [
    {"c": "ev", "u": "qq:90001", "x": "昨天和周武一起吃饭", "imp": 6,
     "rel": [{"subject": "qq:769690776", "predicate": "朋友", "object": "qq:90001"}], "t": "09-11"},
    {"c": "pf", "u": "qq:769690776", "x": "周武是用户的大学室友，现在做后端", "imp": 7, "t": "08-20"},
    {"c": "pr", "u": "qq:769690776", "x": "周武不吃香菜，点菜要单独说", "imp": 6, "t": "08-25"},
    {"c": "re", "u": "qq:769690776", "x": "周武养了只猫叫橘子", "imp": 5,
     "rel": [{"subject": "qq:769690776", "predicate": "养的猫", "object": "橘子"}], "t": "09-02"},
]
ARCHIVES = [
    {"a": "m12", "t": "09-02 20:00", "s": "关于周武的画像档案：大学室友、做后端、不吃香菜、猫叫橘子"},
    {"a": "m13", "t": "09-09 20:00", "s": "这段聊了搬家、做饭和周末爬山的琐事"},
    {"a": "m14", "t": "09-10 20:00", "s": "用户提到最近在准备考试，压力大", "mem": 1},
]


def est(text):
    """粗估 token：汉字≈1.5字/token、ASCII≈3.5字/token（Qwen/GLM 量级）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff" or ch in "：、，。（）")
    ascii_n = len(text) - cjk
    return round(cjk / 1.5 + ascii_n / 3.5)


def enc_json(facts):
    return json.dumps({"facts": facts}, ensure_ascii=False, separators=(",", ":"))


def enc_bare_key(facts):
    """去引号：键裸写、值只在必要时裸写（YAML 风格）"""
    out = []
    for f in facts:
        parts = []
        for k, v in f.items():
            if isinstance(v, str):
                need = "" if v.replace(".", "").replace(":", "").replace("-", "").isalnum() and v else '"'
                parts.append("%s:%s%s%s" % (k, need, v, need))
            elif isinstance(v, (int, float)):
                parts.append("%s:%s" % (k, v))
            else:
                parts.append("%s:%s" % (k, json.dumps(v, ensure_ascii=False, separators=(",", ":"))))
        out.append("{" + ",".join(parts) + "}")
    return "facts:[" + ",".join(out) + "]"


def enc_pipe(facts, legend="c|u|x|imp|rel|t"):
    """每行一条、竖线分隔（顺序固定，缺项留空）"""
    rows = ["# " + legend]
    for f in facts:
        rel = f.get("rel") or []
        rel_s = ";".join("%s>%s>%s" % (r["subject"], r["predicate"], r["object"]) for r in rel)
        rows.append("|".join([f["c"], f["u"], f["x"], str(f.get("imp", "")), rel_s, f.get("t", "")]))
    return "\n".join(rows)


def enc_compact_pipe(facts):
    """更狠：去掉主体里重复的前缀（同名主体只写一次）、时间只给一次"""
    return enc_pipe(facts, legend="c|x|imp|rel|t   （u 见 names 表）")


def enc_archives(arch):
    return json.dumps({"archives": arch}, ensure_ascii=False, separators=(",", ":"))


def enc_archives_pipe(arch):
    return "archives:\n" + "\n".join(
        "a|%s|%s|%s%s" % (r["a"], r["t"], r["s"], "|mem" if r.get("mem") else "") for r in arch
    )


base_f = enc_json(FACTS)
base_a = enc_archives(ARCHIVES)
rows = [
    ("① 现在：紧凑 JSON（带引号）", base_f, base_a),
    ("② 去引号（YAML 风，键裸写）", enc_bare_key(FACTS), "archives:" + json.dumps(ARCHIVES, ensure_ascii=False, separators=(",", ":"))[11:]),
    ("③ 竖线行式（位置固定）", enc_pipe(FACTS), enc_archives_pipe(ARCHIVES)),
    ("④ 竖线 + 去掉重复主体/冗余键", enc_compact_pipe(FACTS), enc_archives_pipe(ARCHIVES)),
]
print("%-34s %6s %6s %8s" % ("写法", "字符", "≈token", "相对①"))
b_chars = len(base_f) + len(base_a)
b_tok = est(base_f) + est(base_a)
for name, f, a in rows:
    chars = len(f) + len(a)
    tok = est(f) + est(a)
    print("%-34s %6d %6d %7.0f%%" % (name, chars, tok, 100.0 * tok / b_tok))
print()
print("只算事实区（更常被裁剪的那块）：")
for name, f, _a in rows:
    print("  %-32s %4d 字符 ≈ %3d token" % (name, len(f), est(f)))
print()
print("③ 的样子：")
print(enc_pipe(FACTS))
print()
print("④ 的样子：")
print(enc_compact_pipe(FACTS))
