"""极简 .env 读取器（不引入 python-dotenv 依赖）。

用法::

    from RAGDjange.env import env, env_bool, env_int, env_list

    API_KEY = env("LLM_API_KEY", "")
    DEBUG = env_bool("DJANGO_DEBUG", True)

规则：
- 只读取项目根目录的 ``.env``（若存在），不覆盖已经存在的真实环境变量；
- 支持 ``#`` 行注释与 ``KEY=VALUE`` 形式，值两端引号会被去掉；
- 任何异常都静默忽略，保证 Web 进程启动阶段绝不因为配置解析而崩溃。
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
_ENV_FILE = BASE_DIR / ".env"
_loaded = False


def load_env_file(path: Path | None = None) -> None:
    """把 .env 载入 os.environ（幂等，只生效一次）。"""
    global _loaded
    if _loaded:
        return
    _loaded = True
    target = path or _ENV_FILE
    if not target.exists():
        return
    try:
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception:  # pragma: no cover - 配置容错
        return


def env(key: str, default: str = "") -> str:
    load_env_file()
    value = os.environ.get(key)
    return default if value is None or value == "" else value


def env_bool(key: str, default: bool = False) -> bool:
    raw = env(key, "1" if default else "0").strip().lower()
    return raw in {"1", "true", "yes", "on", "y"}


def env_int(key: str, default: int = 0) -> int:
    try:
        return int(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(key: str, default: float = 0.0) -> float:
    try:
        return float(env(key, str(default)))
    except (TypeError, ValueError):
        return default


def env_list(key: str, default: list[str] | None = None, sep: str = ",") -> list[str]:
    raw = env(key, "")
    if not raw:
        return list(default or [])
    return [item.strip() for item in raw.split(sep) if item.strip()]
