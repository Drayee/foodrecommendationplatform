"""请求计时中间件：给每个请求打上耗时，便于性能观测（日志 + 响应头）。"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger("food.request")


class RequestTimingMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        response["X-Response-Time"] = f"{elapsed_ms:.1f}ms"
        if request.path.startswith("/api/"):
            logger.info(
                "%s %s -> %s %.1fms",
                request.method,
                request.get_full_path()[:200],
                getattr(response, "status_code", "?"),
                elapsed_ms,
            )
        return response
