"""LLM 客户端冒烟测试（替代原作业里直接调用 langchain 的示例脚本）。

覆盖：配置解析、无 Key 降级、消息归一化、不可用时抛 LLMUnavailable。
真实模型调用不入测试（依赖外部网关，不适合 CI）。
"""
from __future__ import annotations

from django.test import SimpleTestCase, override_settings

from RAGDjange.llm import LLMClient, LLMUnavailable, get_client


class LLMClientTest(SimpleTestCase):
    @override_settings(LLM={"MODEL": "test-model", "API_KEY": "", "ENABLED": True})
    def test_unavailable_without_api_key(self):
        client = LLMClient()
        self.assertFalse(client.available())
        status = client.status()
        self.assertEqual(status["model"], "test-model")
        self.assertFalse(status["has_key"])
        self.assertFalse(status["ready"])

    @override_settings(LLM={"MODEL": "m", "API_KEY": "sk-test", "ENABLED": False})
    def test_disabled_even_with_key(self):
        self.assertFalse(LLMClient().available())

    @override_settings(LLM={"MODEL": "m", "API_KEY": "", "ENABLED": True})
    def test_chat_raises_when_unavailable(self):
        with self.assertRaises(LLMUnavailable):
            LLMClient().chat("你好")

    @override_settings(LLM={"MODEL": "m", "API_KEY": "", "ENABLED": True})
    def test_stream_raises_when_unavailable(self):
        with self.assertRaises(LLMUnavailable):
            list(LLMClient().chat_stream([{"role": "user", "content": "hi"}]))

    def test_normalize_messages(self):
        self.assertEqual(LLMClient._normalize("hi"), [{"role": "user", "content": "hi"}])
        self.assertEqual(
            LLMClient._normalize([{"role": "system", "content": "s"}, "u"]),
            [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}],
        )

    def test_singleton_shared(self):
        self.assertIs(get_client(), get_client())
