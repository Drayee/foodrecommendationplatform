"""邮箱验证码登录服务。

相对原实现的改进
- 验证码存 Django cache（Redis / 文件缓存皆可），不再强依赖 redis 服务；
- 未注册邮箱自动建号（平台是「注册即登录」的轻量形态），并同步创建 UserProfile；
- SMTP 未配置时进入演示模式：验证码写日志并在响应里返回，方便本地/答辩演示；
- 校验失败次数限制，防爆破。
"""
from __future__ import annotations

import logging
import random
import re
from typing import Any

from django.conf import settings
from django.contrib.auth import login as django_login
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import transaction
from django.http import JsonResponse

from food.models import UserProfile
from RAGDjange.tools.tools import send_email

logger = logging.getLogger("food.auth")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE_KEY = "login:code:{}"
ATTEMPT_KEY = "login:attempt:{}"
MAX_ATTEMPTS = 5


def _resp(code: int = 0, msg: str = "ok", data: Any = None) -> JsonResponse:
    """统一响应：code=0 表示成功（沿用平台 API 约定，非 HTTP 状态码）。"""
    return JsonResponse({"code": code, "msg": msg, "data": data})


def get_or_create_user(email: str) -> User:
    """邮箱即账号：不存在则创建，并初始化偏好档案。"""
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        username = email.split("@")[0][:140] or email
        base, idx = username, 1
        while User.objects.filter(username=username).exists():
            idx += 1
            username = f"{base}{idx}"
        with transaction.atomic():
            user = User.objects.create(username=username, email=email)
            user.set_unusable_password()
            user.save(update_fields=["password"])
            UserProfile.objects.get_or_create(user=user, defaults={"nickname": username})
    else:
        UserProfile.objects.get_or_create(user=user, defaults={"nickname": user.username})
    return user


def issue_code(email: str) -> tuple[bool, str]:
    """生成并投递验证码，返回 (是否成功, 提示)。"""
    code = f"{random.randint(0, 999999):06d}"
    cache.set(CODE_KEY.format(email.lower()), code, getattr(settings, "LOGIN_CODE_TTL", 300))
    cache.delete(ATTEMPT_KEY.format(email.lower()))

    html = f"""
    <div style="font-family:-apple-system,'PingFang SC',sans-serif;max-width:520px;margin:0 auto;
                border:1px solid #f0e6dd;border-radius:16px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#ff7a45,#ffa940);padding:22px 24px;color:#fff">
        <h2 style="margin:0;font-size:20px">🍜 寻味 · 美食推荐平台</h2>
        <p style="margin:6px 0 0;opacity:.9;font-size:13px">登录验证码</p>
      </div>
      <div style="padding:26px 24px;background:#fffdfa">
        <p style="margin:0 0 16px;color:#444">你的验证码是：</p>
        <div style="font-size:32px;letter-spacing:8px;font-weight:700;color:#fa541c">{code}</div>
        <p style="margin:18px 0 0;color:#999;font-size:13px">
          验证码 {getattr(settings, "LOGIN_CODE_TTL", 300) // 60} 分钟内有效，请勿泄露给他人。
        </p>
      </div>
    </div>
    """
    if getattr(settings, "SMTP_READY", False):
        result = send_email(to=email, subject="【寻味】登录验证码", content=html)
        ok = "成功" in result
        if not ok:
            logger.warning("验证码邮件发送失败：%s", result)
        return ok, result
    logger.info("演示模式验证码 %s -> %s", email, code)
    return True, "演示模式：邮件服务未配置，验证码已回显"


def send_code(email: str) -> JsonResponse:
    email = (email or "").strip()
    if not EMAIL_RE.match(email):
        return _resp(400, "邮箱格式不正确")
    user = get_or_create_user(email)
    ok, msg = issue_code(email)
    if not ok:
        return _resp(500, f"验证码发送失败：{msg}")
    payload: dict[str, Any] = {"email": email, "is_new": user.date_joined is not None, "smtp": bool(getattr(settings, "SMTP_READY", False))}
    if getattr(settings, "DEMO_VERIFY_CODE", False):
        payload["demo_code"] = cache.get(CODE_KEY.format(email.lower()))
    return _resp(0, msg, payload)


def login_code(request, email: str, code: str) -> JsonResponse:
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if not EMAIL_RE.match(email) or not code:
        return _resp(400, "邮箱或验证码不能为空")

    attempt_key = ATTEMPT_KEY.format(email)
    attempts = cache.get(attempt_key, 0)
    if attempts >= MAX_ATTEMPTS:
        return _resp(429, "尝试次数过多，请重新获取验证码")

    saved = cache.get(CODE_KEY.format(email))
    if not saved or saved != code:
        cache.set(attempt_key, attempts + 1, 300)
        return _resp(400, "验证码错误或已过期")

    user = get_or_create_user(email)
    django_login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    cache.delete(CODE_KEY.format(email))
    cache.delete(attempt_key)
    profile, _ = UserProfile.objects.get_or_create(user=user, defaults={"nickname": user.username})
    return _resp(
        0,
        "登录成功",
        {
            "user_id": user.id,
            "email": user.email,
            "nickname": profile.display_name,
            "onboarded": profile.onboarded,
        },
    )
