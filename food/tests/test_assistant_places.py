"""AI 助手 × 「附近美食」POI 的接缝测试。

覆盖两条链路
1. 位置注入：`_build_messages` 把 GCJ-02 坐标写进 system，缺失时写「未知」；
2. 工具链路：`nearby_foods` 的入参校验 / 半径钳制 / 精简输出 / 降级透传、注册表 schema、
   `call_tool` 派发、`stream_answer(location=...)` 的状态传递与无模型降级。

约束：不联网、不调真实 LLM、不依赖真实高德 Key —— 高德调用全部 monkeypatch，
LLM 要么 `llm_ready=False`，要么用只吐固定文本的假客户端。
"""
from __future__ import annotations

import json

import pytest

import places.service as places_service
from food.services import assistant
from RAGDjange.tools import tools as tool_module
from RAGDjange.tools.tools import TOOL_REGISTRY, call_tool, tool_schemas

pytestmark = pytest.mark.django_db

YAAN = {"lng": 103.0012, "lat": 29.9875}


def _service_result(items: list[dict] | None = None, degraded: bool = False) -> dict:
    """`places.service.nearby` 的返回值形状（与真实实现字段一致）。"""
    items = items or []
    return {
        "items": items,
        "total": len(items),
        "center": dict(YAAN),
        "radius": 1500,
        "degraded": degraded,
        "provider": "fixture" if degraded else "amap",
        "message": "离线样例数据" if degraded else "",
    }


def patch_nearby(monkeypatch, items: list[dict] | None = None, degraded: bool = False) -> list[dict]:
    """把 places.service.nearby 换成记录参数的桩，返回调用记录列表。"""
    calls: list[dict] = []

    def fake_nearby(lng, lat, **kwargs):
        calls.append({"lng": lng, "lat": lat, **kwargs})
        return _service_result(items, degraded)

    monkeypatch.setattr(places_service, "nearby", fake_nearby)
    return calls


FULL_ITEM = {
    "poi_id": "B0FFTEST",
    "name": "老码头火锅",
    "type_label": "火锅店",
    "address": "西大街 88 号",
    "distance_m": 180.0,
    "distance_text": "180m",
    "rating": 4.6,
    "cost": 72.0,
    "business_hours": "11:00-23:00",
    "tel": "0835-2222688",
    "tag": "朋友聚餐",
    "score": 0.8123,
    # 下面这些是「大字段」，工具层必须裁掉，否则一次工具调用就吃掉 prompt 预算
    "photos": ["https://example.com/1.jpg", "https://example.com/2.jpg"],
    "lng": 103.002410,
    "lat": 29.988320,
    "typecode": "050117",
    "city": "雅安市",
    "district": "雨城区",
    "source": "amap",
}


# ---------------------------------------------------------------------------
# 位置注入
# ---------------------------------------------------------------------------
class TestBuildMessagesLocation:
    def test_location_injected_into_system_prompt(self):
        msgs = assistant._build_messages("附近有什么好吃的", "", [], "chat", YAAN)
        assert msgs[0]["role"] == "system"
        system = msgs[0]["content"]
        assert "103.001200" in system, "经度要按 6 位小数写入，避免精度丢失"
        assert "29.987500" in system
        assert "GCJ-02" in system
        assert "nearby_foods" in system
        assert msgs[-1] == {"role": "user", "content": "附近有什么好吃的"}

    def test_missing_location_marks_unknown(self):
        system = assistant._build_messages("附近吃啥", "", [], "chat")[0]["content"]
        assert "未知" in system
        assert "定位" in system
        assert "103.001200" not in system

    def test_zero_location_is_treated_as_unknown(self):
        """(0,0) 是「没定位」的占位值，不能当成几内亚湾的坐标注入。"""
        system = assistant._build_messages("附近吃啥", "", [], "chat", {"lng": 0, "lat": 0})[0]["content"]
        assert "未知" in system
        assert "经度" not in system

    def test_history_and_context_are_kept_alongside_location(self):
        history = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "想吃点什么？"}]
        msgs = assistant._build_messages("附近有面馆吗", "【资料】面馆", history, "chat", YAAN)
        assert "【资料】面馆" in msgs[0]["content"]
        assert msgs[1:3] == history
        assert msgs[-1]["content"] == "附近有面馆吗"


# ---------------------------------------------------------------------------
# nearby_foods 工具
# ---------------------------------------------------------------------------
class TestNearbyFoodsTool:
    def test_rejects_non_numeric_coordinates(self):
        out = json.loads(call_tool("nearby_foods", {"lng": "东经103", "lat": 29.9}))
        assert "error" in out
        assert "坐标格式" in out["error"]

    @pytest.mark.parametrize(
        "args",
        [
            {"lng": 0, "lat": 0},
            {"lng": 999, "lat": 29.9},
            {"lng": 103.0, "lat": -100},
            {},
        ],
    )
    def test_rejects_missing_or_out_of_range_coordinates(self, args, monkeypatch):
        calls = patch_nearby(monkeypatch)
        out = json.loads(call_tool("nearby_foods", args))
        assert "error" in out and "定位" in out["error"]
        assert calls == [], "坐标非法时不该打到高德"

    def test_empty_coordinates_default_to_zero(self):
        assert json.loads(tool_module.nearby_foods())["error"]

    def test_clamps_radius_and_sort(self, monkeypatch):
        calls = patch_nearby(monkeypatch)
        call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875, "radius": 99999, "sort": "weird"})
        call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875, "radius": 10})
        assert calls[0]["radius"] == 5000, "工具侧上限 5km，和前端 MAX_RADIUS 区分开"
        assert calls[0]["sort"] == "smart", "不认识的排序要回落到 smart"
        assert calls[1]["radius"] == 200, "下限 200m"

    def test_output_is_trimmed(self, monkeypatch):
        patch_nearby(monkeypatch, items=[dict(FULL_ITEM)])
        out = json.loads(call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875}))
        assert out["count"] == 1
        item = out["items"][0]
        assert item["name"] == "老码头火锅"
        assert item["distance_text"] == "180m"
        for dropped in ("photos", "lng", "lat", "typecode", "city", "district", "source"):
            assert dropped not in item, f"{dropped} 属于大字段/冗余字段，应被裁掉"
        assert out["provider"] == "amap"
        assert out["note"] == ""

    def test_limit_is_bounded(self, monkeypatch):
        items = [dict(FULL_ITEM, poi_id=f"P{i}", name=f"店{i}") for i in range(12)]
        patch_nearby(monkeypatch, items=items)
        out = json.loads(call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875, "limit": 99}))
        assert out["count"] == 10, "limit 上限 10"

    def test_degraded_result_is_passed_through(self, monkeypatch):
        patch_nearby(monkeypatch, items=[dict(FULL_ITEM, source="fixture")], degraded=True)
        out = json.loads(call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875}))
        assert out["degraded"] is True
        assert out["provider"] == "fixture"
        assert out["note"] == "离线样例数据，仅供演示", "降级时要给模型一句可复述的说明"

    def test_service_exception_becomes_error_string(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("upstream down")

        monkeypatch.setattr(places_service, "nearby", boom)
        out = json.loads(call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875}))
        assert "error" in out and "附近搜索失败" in out["error"]


# ---------------------------------------------------------------------------
# 注册表 / 派发
# ---------------------------------------------------------------------------
class TestToolRegistry:
    def test_registry_contains_nearby_foods_schema(self):
        entry = TOOL_REGISTRY["nearby_foods"]
        assert entry["func"] is tool_module.nearby_foods
        schema = entry["schema"]["function"]
        assert schema["name"] == "nearby_foods"
        params = schema["parameters"]
        assert params["required"] == ["lng", "lat"]
        assert {"lng", "lat"} <= set(params["properties"])
        assert params["properties"]["radius"]["type"] == "integer"

    def test_schemas_ship_nearby_foods(self):
        names = [s["function"]["name"] for s in tool_schemas()]
        assert "nearby_foods" in names
        assert names.count("nearby_foods") == 1

    def test_call_tool_dispatches_dict_and_json_string(self, monkeypatch):
        calls = patch_nearby(monkeypatch, items=[dict(FULL_ITEM)])
        from_dict = json.loads(call_tool("nearby_foods", {"lng": 103.0012, "lat": 29.9875, "keyword": "火锅"}))
        from_json = json.loads(call_tool("nearby_foods", '{"lng": 103.0012, "lat": 29.9875}'))
        assert from_dict["count"] == 1 and from_json["count"] == 1
        assert calls[0]["lng"] == pytest.approx(103.0012)
        assert calls[0]["keywords"] == "火锅"
        assert calls[1]["keywords"] == ""

    def test_unknown_tool_does_not_raise(self):
        assert "未知工具" in call_tool("no_such_tool", {})


# ---------------------------------------------------------------------------
# 流式接口：location 传递 / 无模型降级
# ---------------------------------------------------------------------------
class FakeLLM:
    """只回固定文本的假模型客户端，用来观察「最终送给模型的 messages」。"""

    model_name = "fake-model"

    def __init__(self, chunks=("附近", "有家", "面馆")):
        self.chunks = chunks
        self.messages: list[dict] = []

    def available(self) -> bool:
        return True

    def chat_stream(self, messages, temperature=None):
        self.messages = list(messages)
        yield from self.chunks


class TestAssistantStream:
    def test_stream_answer_records_location(self):
        stream = assistant.stream_answer("附近吃啥", location=YAAN)
        assert isinstance(stream, assistant.AnswerStream)
        assert stream.location == YAAN
        assert stream.mode == "chat"

    def test_stream_answer_without_location_is_empty_dict(self):
        assert assistant.stream_answer("红烧肉怎么做").location == {}

    def test_location_reaches_llm_messages(self, monkeypatch):
        fake = FakeLLM()
        monkeypatch.setattr(assistant, "get_client", lambda: fake)
        monkeypatch.setattr(assistant, "llm_ready", lambda: True)
        monkeypatch.setattr(assistant, "retrieve", lambda question, k=None: [])

        stream = assistant.stream_answer(
            "附近有什么好吃的", session_key="asst-loc-1", use_tools=False, location=YAAN
        )
        "".join(stream)  # 消费完
        assert stream.answer == "附近有家面馆", "正文只含模型输出"
        system = fake.messages[0]["content"]
        assert "103.001200" in system and "29.987500" in system
        assert assistant.nearby_intent("附近有什么好吃的") is True

    def test_answer_once_returns_preamble_separately(self, monkeypatch):
        fake = FakeLLM(chunks=("好", "的"))
        monkeypatch.setattr(assistant, "get_client", lambda: fake)
        monkeypatch.setattr(assistant, "llm_ready", lambda: True)
        monkeypatch.setattr(assistant, "retrieve", lambda question, k=None: [])

        result = assistant.answer_once("附近吃点什么", session_key="asst-loc-2", location=YAAN)
        assert result["answer"] == "好的"
        assert result["degraded"] is False
        # 过程提示（如有）与正文分开回传，正文保持干净
        assert result["preamble"] == "" or result["preamble"] not in result["answer"]

    def test_degrades_when_llm_unavailable(self, monkeypatch):
        monkeypatch.setattr(assistant, "llm_ready", lambda: False)
        monkeypatch.setattr(assistant, "retrieve", lambda question, k=None: [])

        stream = assistant.stream_answer(
            "附近有什么好吃的", session_key="asst-nollm-1", location=YAAN
        )
        text = "".join(stream)
        assert stream.degraded is True
        assert "LLM_API_KEY" in stream.answer, "无模型时必须说明当前是本地检索模式"
        assert "LLM_API_KEY" in text

        from food.models import ChatSession

        session = ChatSession.objects.filter(session_key="asst-nollm-1").first()
        assert session is not None
        assert session.messages.count() == 2, "降级路径同样要把问答落库"
        assert session.messages.filter(role="assistant").first().content == stream.answer

    def test_degrades_with_retrieved_hits(self, monkeypatch):
        class Hit:
            score = 0.9
            recipe_id = 7
            payload = {"title": "麻婆豆腐", "cuisine": "川菜", "total_minutes": 30}
            text = "麻辣鲜香，豆腐嫩滑。"

        monkeypatch.setattr(assistant, "llm_ready", lambda: False)
        monkeypatch.setattr(assistant, "retrieve", lambda question, k=None: [Hit()])

        stream = assistant.stream_answer("麻婆豆腐怎么做", session_key="asst-nollm-2")
        "".join(stream)
        assert stream.degraded is True
        assert "麻婆豆腐" in stream.answer
        assert "本地检索模式" in stream.answer
        assert [c["recipe_id"] for c in stream.citations] == [7]
