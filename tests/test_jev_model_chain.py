"""JEV 模型链（2.22.23）：3.x uuid → 可读名、端点规整、decision 组、链-only 主档。

这些用例全部**不碰网络**：provider_mgr 是桩，_post_sync 用 monkeypatch 截获 URL。
"""

import importlib
import os
import sys
import types
from pathlib import Path

# 有真框架时优先用它（避免 mdecide 回落到 stub core 后污染同进程的其它用例）
if os.environ.get("KIRA_CORE") and os.environ["KIRA_CORE"] not in sys.path:
    sys.path.insert(0, os.environ["KIRA_CORE"])

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("alife_jev_chain")
pkg.__path__ = [str(ROOT)]
sys.modules.setdefault("alife_jev_chain", pkg)
md = importlib.import_module("alife_jev_chain.mdecide")


class Info:
    def __init__(self, mid, name, base, key="k"):
        self.model_id = mid
        self.model_name = name
        self.provider_config = {"base_url": base, "api_key": key}


class Mgr:
    def __init__(self, mapping):
        self.mapping = mapping

    def get_model_info(self, pid, mid):
        return self.mapping.get("%s:%s" % (pid, mid))


class S:
    jev_enabled = True
    jev_model = ""
    jev_decision_model = ""
    jev_models = []
    jev_decision_models = []
    jev_base_url = ""
    jev_api_key = ""
    jev_model_name = ""
    jev_timeout_ms = 5000
    jev_sample = 1.0
    jev_prompt_append = ""


def test_chain_uses_readable_model_name_and_systemone_endpoint():
    """3.x：mid 是 uuid ⇒ 链档必须发 model_name；base_url 必须规整成 /v1/systemone。"""
    mgr = Mgr({"p:u1": Info("u1", "jev-latest", "https://relay/v1")})
    s = S()
    s.jev_models = ["p:u1"]
    c = md.resolve_config(s, mgr)
    assert c.ready
    assert c.model == "jev-latest"          # 不能是 u1
    assert md.endpoint_of(c.base_url) == "https://relay/v1/systemone"


def test_chain_only_promotes_first_link():
    """只配回退链（单模型/手动密钥都空）⇒ 第 1 档当主档，其余作回退。"""
    mgr = Mgr({
        "p:u1": Info("u1", "jev-latest", "https://a/v1"),
        "p:u2": Info("u2", "jev-1.14.0", "https://b"),
    })
    s = S()
    s.jev_models = ["p:u1", "p:u2"]
    c = md.resolve_config(s, mgr)
    assert c.ready
    assert c.model == "jev-latest"
    assert c.base_url == "https://a/v1/systemone"
    assert c.fallbacks == (("https://b/v1/systemone", "k", "jev-1.14.0"),)


def test_decision_group_and_single_decision_model():
    """3.1.1 的 decision 组：单选/多选都能单独工作。"""
    mgr = Mgr({"p:d1": Info("d1", "jev-latest", "https://ts/v1")})

    s = S()
    s.jev_decision_model = "p:d1"
    c = md.resolve_config(s, mgr)
    assert c.ready and c.model == "jev-latest"
    assert md.endpoint_of(c.base_url) == "https://ts/v1/systemone"

    s2 = S()
    s2.jev_decision_models = ["p:d1"]
    c2 = md.resolve_config(s2, mgr)
    assert c2.ready and c2.model == "jev-latest"
    assert c2.base_url == "https://ts/v1/systemone"


def test_primary_is_not_repeated_in_fallbacks():
    mgr = Mgr({"p:u1": Info("u1", "jev-latest", "https://a/v1")})
    s = S()
    s.jev_model = "p:u1"
    s.jev_models = ["p:u1"]
    c = md.resolve_config(s, mgr)
    assert c.fallbacks == ()


def test_unresolvable_entries_are_skipped():
    mgr = Mgr({"p:u1": Info("u1", "jev-latest", "https://a/v1")})
    s = S()
    s.jev_models = ["bad", "p:missing", "p:u1"]
    c = md.resolve_config(s, mgr)
    assert c.ready and c.model == "jev-latest"


def test_post_sync_normalizes_fallback_endpoint(monkeypatch):
    """回退档也必须落到 /v1/systemone（旧实现直接 POST base_url ⇒ 403/404）。"""
    seen = []

    class _Resp:
        def read(self):
            return b'{"answers": {"q": 1}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        if "primary" in req.full_url:
            raise TimeoutError("primary down")
        return _Resp()

    monkeypatch.setattr(md.urllib.request, "urlopen", fake_urlopen)
    client = md.JevClient("https://primary.invalid", "k1", "m1", 0.3)
    client.fallbacks = (("https://backup.invalid/v1", "k2", "m2"),)
    got = client._post_sync({"model": "m1", "state": "s", "questions": {}}, 0.3)
    assert got == {"answers": {"q": 1}}
    assert seen[0] == "https://primary.invalid/v1/systemone"
    assert seen[1] == "https://backup.invalid/v1/systemone"


def test_groups_have_independent_caps():
    """两组各自 10 档：llm 满 10 时 decision 档仍必须保留。"""
    mapping = {}
    for i in range(10):
        mapping["p:l%d" % i] = Info("l%d" % i, "llm-%d" % i, "https://l%d/v1" % i)
    mapping["p:d1"] = Info("d1", "jev-latest", "https://d/v1")
    s = S()
    s.jev_models = ["p:l%d" % i for i in range(10)]
    s.jev_decision_models = ["p:d1"]
    c = md.resolve_config(s, Mgr(mapping))
    assert any(m == "jev-latest" for _b, _k, m in c.fallbacks), c.fallbacks
