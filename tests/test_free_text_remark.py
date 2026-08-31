"""备注字段 LLM 解析（问题六修复，8.30）回归测试。

覆盖 ``api.views._parse_free_text_requirement`` 的行为契约：
- 备注非空 → 调 A 的 ``parse_requirement_input``（LLM），成功时用解析结果整体替换；
- 备注为空 / 缺失 → 原样返回（零 LLM 调用）；
- LLM 抛错 / 返回形状异常 → 原样返回（按无备注规划，不阻断）。

LLM 一律 Mock（禁联网纪律）；views 函数内延迟导入
``from call_llm.parse_input import parse_requirement_input``，monkeypatch
该模块属性即可拦截。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_B_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_B_ROOT), str(_B_ROOT / "django_server")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# a_side（A 侧镜像，含 call_llm）：与 travelagent/settings.py 同款布局，
# append 不 insert——B 的模块永远优先（AB 合码方案 §二铁律 2）。
_A_SIDE = str(_B_ROOT / "a_side")
if _A_SIDE not in sys.path:
    sys.path.append(_A_SIDE)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=True,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        ROOT_URLCONF=None,
    )
import django  # noqa: E402

django.setup()

from api import views  # noqa: E402


def _body(remark: object = None, with_remark_key: bool = True) -> dict:
    content = {
        "destination": "北京",
        "start_date": "2026-09-05",
        "days": 1,
        "visitor_number": 2,
        "constraints": {
            "budget": 3000,
            "must_visit": [],
            "required_tags": [],
            "dismissed_tags": [],
            "days": 1,
            "daily_travel_time": 480,
        },
        "preferences": {
            "preferred_tags": [],
            "avoid_tags": [],
            "required_tags": [],
            "dismissed_tags": [],
            "must_visit": [],
        },
    }
    if with_remark_key:
        content["free_text_requirement"] = remark
    return {"days": 1, "content": content}


def _patch_parse(monkeypatch, fn):
    """把 ``call_llm.parse_input.parse_requirement_input`` 换成 ``fn``；返回调用记录。"""
    import call_llm.parse_input as parse_input_mod

    calls: list = []

    def wrapper(raw_input, **kwargs):
        calls.append(raw_input)
        return fn(raw_input, **kwargs)

    monkeypatch.setattr(parse_input_mod, "parse_requirement_input", wrapper)
    return calls


def test_empty_remark_skips_llm(monkeypatch):
    """备注为空串 / None / 缺失 → 原样返回且零 LLM 调用（不产生延迟）。"""
    for payload in (_body(""), _body(None), _body(with_remark_key=False)):
        calls = _patch_parse(monkeypatch, lambda raw, **kw: raw)
        result = views._parse_free_text_requirement(payload)
        assert result is payload
        assert calls == []


def test_llm_success_replaces_payload(monkeypatch):
    """备注非空 + LLM 成功 → 解析结果整体替换（标签/忌口已归并进结构化字段）。"""
    parsed = _body("（已解析）")
    parsed["content"]["preferences"]["preferred_tags"] = ["历史文化"]
    parsed["content"]["preferences"]["food_preferences"] = ["不吃辣"]
    calls = _patch_parse(monkeypatch, lambda raw, **kw: parsed)

    payload = _body("带老人出行，慢节奏，不吃辣")
    result = views._parse_free_text_requirement(payload)

    assert result is parsed
    assert parsed["content"]["preferences"]["preferred_tags"] == ["历史文化"]
    assert calls == [payload], "原始 body（含备注原文）应整体交给 LLM"


def test_llm_failure_falls_back_to_original(monkeypatch):
    """LLM 抛错（超时/无 key/网络）→ 原样返回（按无备注规划，不阻断主链路）。"""
    calls = _patch_parse(
        monkeypatch,
        lambda raw, **kw: (_ for _ in ()).throw(RuntimeError("LLM 超时（测试注入）")),
    )
    payload = _body("备注内容")
    result = views._parse_free_text_requirement(payload)
    assert result is payload
    assert len(calls) == 1


def test_llm_bad_shape_falls_back_to_original(monkeypatch):
    """LLM 返回形状异常（None / 非 dict / content 非 dict）→ 原样返回。"""
    for bad in (None, "not a dict", {"content": "not a dict"}, {}):
        _patch_parse(monkeypatch, lambda raw, **kw: bad)
        payload = _body("备注内容")
        result = views._parse_free_text_requirement(payload)
        assert result is payload


def test_plan_view_feeds_parsed_payload_to_runtime(monkeypatch):
    """集成：``plan()`` 视图先解析备注再把结果交给 ``runtime.init_from_requirement``。"""
    from django.http import HttpRequest

    parsed = _body("（已解析）")
    parsed["content"]["preferences"]["preferred_tags"] = ["历史文化"]

    captured: list = []

    class _FakeTimeline:
        days = [{"day": 1}]

    class _FakeRuntime:
        def init_from_requirement(self, payload):
            captured.append(payload)
            return _FakeTimeline()

        def enrich_transport_details(self, timeline):  # 2026-09-01：no-op
            return None

    fake_runtime = _FakeRuntime()
    # views.py 顶层 `from runtime.agent_runtime import runtime` 持有引用，
    # 直接换 views 命名空间里的名字。
    monkeypatch.setattr(views, "runtime", fake_runtime)
    # to_dict 无法序列化假 timeline → Mock 成普通 dict（本测试只关心 payload 传递）。
    monkeypatch.setattr(views, "to_dict", lambda tl: {"days": tl.days})
    _patch_parse(monkeypatch, lambda raw, **kw: parsed)

    req = HttpRequest()
    req.method = "POST"
    import json as _json

    req._body = _json.dumps(_body("想看历史文化景点"), ensure_ascii=False).encode("utf-8")

    resp = views.plan(req)
    assert resp.status_code == 200
    assert captured == [parsed], "runtime 应收到 LLM 解析后的需求"


def test_llm_null_include_meal_time_downgraded(monkeypatch):
    """回归（8.30 线上首测发现）：LLM 把 include_meal_time_in_daily_limit 填 null
    → 必须降级为 False（A 侧对显式 null 报错，缺省才回退 False）。"""
    parsed = _body("（已解析）")
    parsed["content"]["constraints"]["include_meal_time_in_daily_limit"] = None
    _patch_parse(monkeypatch, lambda raw, **kw: parsed)

    result = views._parse_free_text_requirement(_body("备注内容"))

    assert (
        result["content"]["constraints"]["include_meal_time_in_daily_limit"] is False
    ), "LLM 的 null 应降级为历史默认 False"


def test_llm_false_include_meal_time_kept(monkeypatch):
    """LLM 给了明确 False → 保持不变（不强行覆盖）。"""
    parsed = _body("（已解析）")
    parsed["content"]["constraints"]["include_meal_time_in_daily_limit"] = False
    _patch_parse(monkeypatch, lambda raw, **kw: parsed)

    result = views._parse_free_text_requirement(_body("备注内容"))

    assert result["content"]["constraints"]["include_meal_time_in_daily_limit"] is False


def test_plan_view_rejects_missing_budget(monkeypatch):
    """预算必填（8.30 拍板）：constraints.budget 缺失/null/负数 → 400 要求补全。"""
    from django.http import HttpRequest
    import json as _json

    class _FakeTimeline:
        days = [{"day": 1}]

    class _FakeRuntime:
        def __init__(self):
            self.calls = 0

        def init_from_requirement(self, payload):
            self.calls += 1  # 正常预算路径才应进规划
            return type("TL", (), {"days": [{"day": 1}]})()

        def enrich_transport_details(self, timeline):  # 2026-09-01：no-op
            return None

    fake = _FakeRuntime()
    monkeypatch.setattr(views, "runtime", fake)
    monkeypatch.setattr(views, "to_dict", lambda tl: {"days": tl.days})

    def _req(body):
        req = HttpRequest()
        req.method = "POST"
        req._body = _json.dumps(body, ensure_ascii=False).encode("utf-8")
        return req

    # 正常预算（备注 'x' 不触发 LLM——空备注跳过解析；budget=3000 在 _body 里）
    base = _body("x")
    ok = views.plan(_req(base))
    assert ok.status_code == 200, str(ok.content, "utf-8")
    assert fake.calls == 1
    # 缺预算
    no_budget = _body("x")
    del no_budget["content"]["constraints"]["budget"]
    resp = views.plan(_req(no_budget))
    body_text = _json.loads(resp.content.decode("unicode_escape").encode("latin-1").decode("utf-8"))         if False else _json.loads(resp.content)["error"]
    assert resp.status_code == 400 and "预算" in body_text
    # null 预算
    null_budget = _body("x")
    null_budget["content"]["constraints"]["budget"] = None
    resp = views.plan(_req(null_budget))
    assert resp.status_code == 400
    # 负数
    neg_budget = _body("x")
    neg_budget["content"]["constraints"]["budget"] = -100
    resp = views.plan(_req(neg_budget))
    assert resp.status_code == 400
