"""统一 LLM 客户端。

设计取舍
- 直接使用官方 ``openai`` SDK（兼容任何 OpenAI 协议网关：通义 / DeepSeek / vLLM / Ollama），
  不引入 langchain，减少依赖面、也让「无 Key 降级」变得直接可控；
- 对外暴露三个层面：
    1. ``LLMClient.available()``   —— 是否可以调用真实模型
    2. ``LLMClient.chat_stream()`` —— 流式生成（生成器，逐 token yield str）
    3. ``LLMClient.chat()``        —— 一次性生成
- 兼容旧代码：``ChatModel.load_model_once()`` 仍然可用，返回同一客户端。
"""
from __future__ import annotations

import logging
import threading
from typing import Iterable, Iterator

from django.conf import settings

logger = logging.getLogger("rag.llm")


class LLMUnavailable(RuntimeError):
    """没有可用模型（未配置 Key / 关闭 / 连接失败）。上层据此走抽取式降级。"""


def _cfg() -> dict:
    cfg = getattr(settings, "LLM", {}) or {}
    return {
        "model": cfg.get("MODEL", "qwen-plus"),
        "api_key": cfg.get("API_KEY", ""),
        "base_url": cfg.get("BASE_URL", "") or None,
        "temperature": float(cfg.get("TEMPERATURE", 0.7) or 0.7),
        "timeout": int(cfg.get("TIMEOUT", 60) or 60),
        "max_tokens": int(cfg.get("MAX_TOKENS", 1500) or 1500),
        "enabled": bool(cfg.get("ENABLED", True)),
    }


class LLMClient:
    _instance: "LLMClient | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._cfg = _cfg()
        self._client = None
        self._init_error: str | None = None

    # -- 单例 -------------------------------------------------------------
    @classmethod
    def instance(cls) -> "LLMClient":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @property
    def model_name(self) -> str:
        return self._cfg["model"]

    # -- 可用性 -----------------------------------------------------------
    def available(self) -> bool:
        if not self._cfg["enabled"] or not self._cfg["api_key"]:
            return False
        return self._get_client() is not None

    def _get_client(self):
        if self._client is not None:
            return self._client
        if self._init_error:
            return None
        try:
            from openai import OpenAI

            self._client = OpenAI(
                api_key=self._cfg["api_key"],
                base_url=self._cfg["base_url"],
                timeout=self._cfg["timeout"],
                max_retries=1,
            )
        except Exception as exc:  # pragma: no cover - 依赖缺失 / 配置异常
            self._init_error = str(exc)
            logger.warning("LLM 客户端初始化失败：%s", exc)
            return None
        return self._client

    def status(self) -> dict:
        return {
            "enabled": self._cfg["enabled"],
            "has_key": bool(self._cfg["api_key"]),
            "model": self._cfg["model"],
            "base_url": self._cfg["base_url"] or "(默认)",
            "ready": self.available(),
            "error": self._init_error or "",
        }

    # -- 调用 -------------------------------------------------------------
    @staticmethod
    def _normalize(messages: Iterable[dict] | str) -> list[dict]:
        if isinstance(messages, str):
            return [{"role": "user", "content": messages}]
        out = []
        for msg in messages:
            if isinstance(msg, str):
                out.append({"role": "user", "content": msg})
            elif isinstance(msg, dict):
                out.append({"role": msg.get("role", "user"), "content": msg.get("content", "")})
        return out

    def chat(self, messages, temperature: float | None = None) -> str:
        client = self._get_client()
        if client is None:
            raise LLMUnavailable("LLM 未配置或初始化失败")
        try:
            resp = client.chat.completions.create(
                model=self._cfg["model"],
                messages=self._normalize(messages),
                temperature=self._cfg["temperature"] if temperature is None else temperature,
                max_tokens=self._cfg["max_tokens"],
            )
            return (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            logger.warning("LLM 调用失败：%s", exc)
            raise LLMUnavailable(str(exc)) from exc

    def chat_stream(self, messages, temperature: float | None = None) -> Iterator[str]:
        client = self._get_client()
        if client is None:
            raise LLMUnavailable("LLM 未配置或初始化失败")
        try:
            stream = client.chat.completions.create(
                model=self._cfg["model"],
                messages=self._normalize(messages),
                temperature=self._cfg["temperature"] if temperature is None else temperature,
                max_tokens=self._cfg["max_tokens"],
                stream=True,
            )
            for chunk in stream:
                try:
                    delta = chunk.choices[0].delta.content
                except (IndexError, AttributeError):
                    continue
                if delta:
                    yield delta
        except Exception as exc:
            logger.warning("LLM 流式调用失败：%s", exc)
            raise LLMUnavailable(str(exc)) from exc


def get_client() -> LLMClient:
    return LLMClient.instance()


# ---------------------------------------------------------------------------
# 向后兼容：原项目使用 ChatModel.load_model_once() 返回一个可 invoke/stream 的对象
# ---------------------------------------------------------------------------
class ChatModel:
    _model = None
    _lock = threading.Lock()

    @classmethod
    def load_model_once(cls) -> LLMClient:
        if cls._model is None:
            with cls._lock:
                cls._model = LLMClient.instance()
        return cls._model
