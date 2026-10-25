"""
Django settings —— 美食推荐平台（基于 RAGDjange 扩展）

设计要点
1. 全量配置走环境变量（见 .env.example），默认值保证「零配置即可本地跑起来」；
2. 数据库默认 SQLite，设置 DB_ENGINE=mysql 可无痛切换 MySQL（原项目使用 MySQL）；
3. 缓存默认本地文件缓存，配置 REDIS_URL 后自动切换 Redis；
4. LLM / 向量模型 / 网络搜索均为「可降级」：没有 Key、断网时平台仍然完整可用。
"""
import time
from pathlib import Path

from .env import env, env_bool, env_int, env_list

BASE_DIR = Path(__file__).resolve().parent.parent

# --------------------------------------------------------------------------
# 基础
# --------------------------------------------------------------------------
SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    "django-insecure-mp!&=4pz7gq_$3etyd=&@w=9^73^#a9xve41cl2%ltjcg_cy81",
)
DEBUG = env_bool("DJANGO_DEBUG", True)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", ["127.0.0.1", "localhost", "0.0.0.0", "[::1]"])
CSRF_TRUSTED_ORIGINS = env_list(
    "DJANGO_CSRF_TRUSTED_ORIGINS",
    ["http://127.0.0.1:8000", "http://localhost:8000"],
)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # 业务应用
    "food",
    "places",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "food.middleware.RequestTimingMiddleware",
]

ROOT_URLCONF = "RAGDjange.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "RAGDjange.wsgi.application"
ASGI_APPLICATION = "RAGDjange.asgi.application"

# --------------------------------------------------------------------------
# 数据库
#   DB_ENGINE=sqlite（默认） / postgresql / mysql
#   本机演示环境使用 PostgreSQL：postgres@127.0.0.1:5432/food_platform
# --------------------------------------------------------------------------
DB_ENGINE = env("DB_ENGINE", "sqlite").lower()

if DB_ENGINE in {"postgres", "postgresql"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": env("DB_NAME", "food_platform"),
            "USER": env("DB_USER", "postgres"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "5432"),
            "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
            "OPTIONS": {"connect_timeout": env_int("DB_CONNECT_TIMEOUT", 8)},
        }
    }
elif DB_ENGINE in {"mysql", "mariadb"}:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": env("DB_NAME", "food_platform"),
            "USER": env("DB_USER", "root"),
            "PASSWORD": env("DB_PASSWORD", ""),
            "HOST": env("DB_HOST", "127.0.0.1"),
            "PORT": env("DB_PORT", "3306"),
            "OPTIONS": {
                "charset": "utf8mb4",
                "init_command": "SET sql_mode='STRICT_TRANS_TABLES'",
            },
            "CONN_MAX_AGE": env_int("DB_CONN_MAX_AGE", 60),
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "db.sqlite3",
            "OPTIONS": {"timeout": env_int("DB_TIMEOUT", 20)},
        }
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# --------------------------------------------------------------------------
# 缓存 / 会话
# --------------------------------------------------------------------------
REDIS_URL = env("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "TIMEOUT": env_int("CACHE_TIMEOUT", 300),
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.cache"
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.filebased.FileBasedCache",
            "LOCATION": str(BASE_DIR / ".cache"),
            "TIMEOUT": env_int("CACHE_TIMEOUT", 300),
            "OPTIONS": {"MAX_ENTRIES": env_int("CACHE_MAX_ENTRIES", 2000)},
        }
    }
    SESSION_ENGINE = "django.contrib.sessions.backends.db"

SESSION_COOKIE_AGE = env_int("SESSION_COOKIE_AGE", 60 * 60 * 24 * 7)
SESSION_COOKIE_SAMESITE = "Lax"

# --------------------------------------------------------------------------
# 国际化
# --------------------------------------------------------------------------
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = env("TIME_ZONE", "Asia/Shanghai")
USE_I18N = True
USE_TZ = True

# --------------------------------------------------------------------------
# 静态文件 / 媒体
# --------------------------------------------------------------------------
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

# 静态资源版本号：模板里以 `?v=` 附加，避免浏览器在 JS/CSS 更新后继续用旧缓存
# （生产环境可用 CI 注入的 git 短哈希覆盖；本地默认取启动时间戳，重启即失效）
STATIC_VERSION = env("STATIC_VERSION", "") or str(int(time.time()))

# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "standard": {"format": "[%(asctime)s] %(levelname)s %(name)s: %(message)s"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": str(LOG_DIR / "app.log"),
            "maxBytes": 5 * 1024 * 1024,
            "backupCount": 3,
            "encoding": "utf-8",
            "formatter": "standard",
        },
    },
    "root": {"handlers": ["console", "file"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        "django.request": {"handlers": ["console", "file"], "level": "WARNING", "propagate": False},
        "rag": {"handlers": ["console", "file"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
        "recommend": {"handlers": ["console", "file"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
        "websearch": {"handlers": ["console", "file"], "level": env("LOG_LEVEL", "INFO"), "propagate": False},
    },
}

# --------------------------------------------------------------------------
# LLM（沿用原项目 ChatOpenAI 兼容接口，可指向任何 OpenAI 兼容网关）
# --------------------------------------------------------------------------
LLM = {
    "MODEL": env("LLM_MODEL", "qwen3.8-max"),
    "API_KEY": env("LLM_API_KEY", ""),
    "BASE_URL": env("LLM_BASE_URL", ""),
    "TEMPERATURE": env("LLM_TEMPERATURE", "0.7"),
    "TIMEOUT": env_int("LLM_TIMEOUT", 60),
    "MAX_TOKENS": env_int("LLM_MAX_TOKENS", 1500),
    "ENABLED": env_bool("LLM_ENABLED", True),
}

# --------------------------------------------------------------------------
# RAG
# --------------------------------------------------------------------------
RAG = {
    "CHUNK_SIZE": env_int("RAG_CHUNK_SIZE", 420),
    "CHUNK_OVERLAP": env_int("RAG_CHUNK_OVERLAP", 80),
    "TOP_K": env_int("RAG_TOP_K", 5),
    "EMBED_DIM": env_int("RAG_EMBED_DIM", 1024),
    "EMBED_MODEL": env("RAG_EMBED_MODEL", "hashing-tfidf-zh"),
    "INDEX_DIR": str(BASE_DIR / "data" / "index"),
    "HYBRID_BM25_WEIGHT": env("RAG_BM25_WEIGHT", "0.5"),
    # 标题路权重：三路 RRF 里「查询 ↔ 菜名」这一路的权重，设 0 退回两路融合
    "HYBRID_TITLE_WEIGHT": env("RAG_TITLE_WEIGHT", "1.0"),
    "INDEX_BATCH_SIZE": env_int("RAG_INDEX_BATCH_SIZE", 200),
    "EMBED_BACKEND": env("RAG_EMBED_BACKEND", "hashing"),
    "CACHE_TTL": env_int("RAG_CACHE_TTL", 600),
}

# --------------------------------------------------------------------------
# 网络搜索（菜谱抓取 / 通用搜索）
# --------------------------------------------------------------------------
WEBSEARCH = {
    "TIMEOUT": env_int("WEBSEARCH_TIMEOUT", 8),
    "MAX_RESULTS": env_int("WEBSEARCH_MAX_RESULTS", 8),
    "CACHE_TTL": env_int("WEBSEARCH_CACHE_TTL", 3600),
    "USER_AGENTS": env_list(
        "WEBSEARCH_USER_AGENTS",
        [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
            "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        ],
    ),
    "OFFLINE_FALLBACK": env_bool("WEBSEARCH_OFFLINE_FALLBACK", True),
    "FIXTURE_DIR": str(BASE_DIR / "websearch" / "fixtures"),
}

# --------------------------------------------------------------------------
# 推荐引擎
# --------------------------------------------------------------------------
RECOMMEND = {
    "FEED_SIZE": env_int("RECOMMEND_FEED_SIZE", 12),
    "WEIGHT_CONTENT": env("RECOMMEND_W_CONTENT", "0.45"),
    "WEIGHT_CF": env("RECOMMEND_W_CF", "0.30"),
    "WEIGHT_HOT": env("RECOMMEND_W_HOT", "0.15"),
    "WEIGHT_FRESH": env("RECOMMEND_W_FRESH", "0.10"),
    "HOT_DECAY_HOURS": env_int("RECOMMEND_HOT_DECAY_HOURS", 72),
    "MMR_LAMBDA": env("RECOMMEND_MMR_LAMBDA", "0.7"),
}

# --------------------------------------------------------------------------
# 高德开放平台（附近美食）
#   WEB_KEY  → 服务端 REST（周边搜索 / 逆地理 / IP 定位 / 输入提示）
#   JS_KEY   → 前端 JS API 地图渲染（需配套 SECURITY_CODE）
# --------------------------------------------------------------------------
AMAP = {
    "WEB_KEY": env("AMAP_WEB_KEY", ""),
    "WEB_KEYS": env_list("AMAP_WEB_KEYS") or ([env("AMAP_WEB_KEY")] if env("AMAP_WEB_KEY") else []),
    "JS_KEY": env("AMAP_JS_KEY", ""),
    "JS_KEYS": env_list("AMAP_JS_KEYS") or ([env("AMAP_JS_KEY")] if env("AMAP_JS_KEY") else []),
    # JS Key 与安全密钥是**逐 Key 配对**的（2021-12 之后申请的 JS Key 必须带 jscode），
    # 这里用 "key:code,key:code" 的形式描述；缺省时回落到单个 SECURITY_CODE。
    "JS_PAIRS": [
        {"key": pair.split(":", 1)[0].strip(), "security_code": pair.split(":", 1)[1].strip()}
        for pair in env_list("AMAP_JS_PAIRS")
        if ":" in pair and pair.split(":", 1)[0].strip()
    ],
    "SECURITY_CODE": env("AMAP_SECURITY_CODE", ""),
    "ENABLED": env_bool("AMAP_ENABLED", True),
    "TIMEOUT": env_int("AMAP_TIMEOUT", 8),
    "CACHE_TTL": env_int("AMAP_CACHE_TTL", 600),
    "QUOTA_COOLDOWN": env_int("AMAP_QUOTA_COOLDOWN", 6 * 3600),
    "DEFAULT_RADIUS": env_int("AMAP_DEFAULT_RADIUS", 3000),
    "DEFAULT_TYPES": env("AMAP_DEFAULT_TYPES", "050000"),  # 050000 = 餐饮服务
    "MAX_RADIUS": env_int("AMAP_MAX_RADIUS", 50000),
    "SORT_WEIGHTS": {
        "distance": 0.35,
        "rating": 0.25,
        "cost": 0.15,
        "taste": 0.15,
        "heat": 0.10,
    },
}

# --------------------------------------------------------------------------
# 邮件（验证码登录）—— Django 6.1 起使用 MAILERS，旧 EMAIL_* 已废弃
# --------------------------------------------------------------------------
MAIL = {
    "HOST": env("EMAIL_HOST", "smtp.qq.com"),
    "PORT": env_int("EMAIL_PORT", 465),
    "USER": env("EMAIL_HOST_USER", ""),
    "PASSWORD": env("EMAIL_HOST_PASSWORD", ""),
    "USE_SSL": env_bool("EMAIL_USE_SSL", True),
    "TIMEOUT": env_int("EMAIL_TIMEOUT", 10),
}
# 配了 SMTP 账号就走真实发信，否则退回控制台（本地演示时验证码打印在终端）
SMTP_READY = bool(MAIL["USER"] and MAIL["PASSWORD"])

MAILERS = {
    "default": {
        "BACKEND": (
            "django.core.mail.backends.smtp.EmailBackend"
            if SMTP_READY
            else "django.core.mail.backends.console.EmailBackend"
        ),
        "OPTIONS": {
            "host": MAIL["HOST"],
            "port": MAIL["PORT"],
            "username": MAIL["USER"],
            "password": MAIL["PASSWORD"],
            "use_ssl": MAIL["USE_SSL"],
            "timeout": MAIL["TIMEOUT"],
        },
    },
    "console": {"BACKEND": "django.core.mail.backends.console.EmailBackend"},
}

DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", MAIL["USER"] or "noreply@food.local")
LOGIN_CODE_TTL = env_int("LOGIN_CODE_TTL", 300)

# 演示模式：不配置 SMTP 时，验证码同时返回给前端 / 打印到控制台，方便本地跑通
DEMO_VERIFY_CODE = env_bool("DEMO_VERIFY_CODE", not SMTP_READY)
