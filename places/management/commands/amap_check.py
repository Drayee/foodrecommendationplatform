"""`python manage.py amap_check` —— 逐把验证高德 Key 是否可用。

为什么单独做：Key 池调度会把「冷却中」的 Key 跳过，看不出是「配额用完」还是「Key 本身失效」。
本命令**绕过池**，直接对每把 Web 服务 Key 发一次最小请求（默认 `/v3/ip`，几乎不耗配额），
把 status / infocode / info / 耗时逐条列出来，方便判断哪把能上线、哪把已被限流或权限不足。

用法::

    python manage.py amap_check                 # 默认用 /v3/ip 探测（最省配额）
    python manage.py amap_check --probe place   # 用周边搜索探测（验证 POI 权限，耗配额更多）
    python manage.py amap_check --probe regeo --lng 103.0012 --lat 29.9875
"""
from __future__ import annotations

import time

from django.conf import settings
from django.core.management.base import BaseCommand

from places.amap import AmapClient
from places.keypool import mask_key

# 常见错误码释义
INFOCODE_HINTS = {
    "10000": "成功",
    "10001": "Key 不正确或过期",
    "10003": "日访问量已超限",
    "10004": "单位时间访问过于频繁",
    "10005": "IP 白名单校验失败（该 Key 绑定了 IP 白名单）",
    "10006": "域名白名单校验失败",
    "10007": "数字签名校验失败",
    "10008": "MD5 校验失败",
    "10009": "Key 权限不足 / 服务未开通",
    "10010": "IP 访问超限",
    "10012": "权限不足（该 Key 未开通此服务）",
    "10021": "QPS 超出限制",
    "10029": "日调用量超限",
    "10044": "账号维度日调用量超限",
    "10045": "账号维度 QPS 超限",
}


class Command(BaseCommand):
    help = "逐个探测高德 Web 服务 Key / JS Key 配置是否可用"

    def add_arguments(self, parser):
        parser.add_argument("--probe", choices=["ip", "place", "regeo"], default="ip",
                            help="探测接口：ip（最省）/ place（验证 POI 权限）/ regeo")
        parser.add_argument("--lng", type=float, default=103.0012, help="place/regeo 探测用经度")
        parser.add_argument("--lat", type=float, default=29.9875, help="place/regeo 探测用纬度")

    def handle(self, *args, **options):
        cfg = getattr(settings, "AMAP", {}) or {}
        keys = [k for k in (cfg.get("WEB_KEYS") or []) if k]
        pairs = cfg.get("JS_PAIRS") or []

        if not keys:
            self.stderr.write(self.style.ERROR("未配置 AMAP_WEB_KEYS，无法探测"))
            return

        self.stdout.write(self.style.MIGRATE_HEADING(f"探测 {len(keys)} 把 Web 服务 Key（接口：{options['probe']}）"))
        client = AmapClient()
        session = client._get_session()
        ok_count = 0

        for idx, key in enumerate(keys, start=1):
            params = {"key": key}
            path = "/v3/ip"
            if options["probe"] == "place":
                path = "/v3/place/around"
                params.update(
                    {
                        "location": f"{options['lng']:.6f},{options['lat']:.6f}",
                        "radius": 1000,
                        "offset": 1,
                        "page": 1,
                        "types": cfg.get("DEFAULT_TYPES", "050000"),
                        "extensions": "base",
                    }
                )
            elif options["probe"] == "regeo":
                path = "/v3/geocode/regeo"
                params.update({"location": f"{options['lng']:.6f},{options['lat']:.6f}", "extensions": "base"})

            started = time.perf_counter()
            try:
                resp = session.get(f"{client.BASE_URL}{path}", params=params, timeout=client.timeout)
                latency = (time.perf_counter() - started) * 1000
                data = resp.json()
            except Exception as exc:
                self.stdout.write(
                    self.style.ERROR(f"  [{idx}] {mask_key(key)}  请求异常：{exc}")
                )
                continue

            status = str(data.get("status", ""))
            infocode = str(data.get("infocode", ""))
            info = str(data.get("info", ""))
            hint = INFOCODE_HINTS.get(infocode, "")

            if status == "1":
                ok_count += 1
                extra = ""
                if options["probe"] == "place":
                    extra = f"｜POI {data.get('count', 0)} 条"
                elif options["probe"] == "ip":
                    extra = f"｜{data.get('province', '')}{data.get('city', '')}"
                self.stdout.write(
                    self.style.SUCCESS(f"  [{idx}] {mask_key(key)}  OK {latency:.0f}ms{extra}")
                )
            else:
                self.stdout.write(
                    self.style.ERROR(
                        f"  [{idx}] {mask_key(key)}  失败 status={status} infocode={infocode} "
                        f"{info}{'（' + hint + '）' if hint else ''} {latency:.0f}ms"
                    )
                )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(f"JS API Key 配对（{len(pairs)} 组）"))
        if not pairs:
            js_keys = cfg.get("JS_KEYS") or []
            single_code = cfg.get("SECURITY_CODE", "")
            if js_keys:
                self.stdout.write(
                    f"  1 组（共用安全密钥 {mask_key(single_code) if single_code else '未配置'}）："
                    f"{'、'.join(mask_key(k) for k in js_keys)}"
                )
            else:
                self.stdout.write(self.style.WARNING("  未配置 JS Key，地图不可用"))
        else:
            for idx, pair in enumerate(pairs, start=1):
                code = pair.get("security_code") or ""
                self.stdout.write(
                    f"  [{idx}] {mask_key(pair.get('key', ''))}  ↔  安全密钥 "
                    f"{mask_key(code) if code else self.style.WARNING('缺失')}"
                )
            if any(not p.get("security_code") for p in pairs):
                self.stdout.write(
                    self.style.WARNING("  警告：存在缺少安全密钥的 JS Key，地图会报 INVALID_USER_SCODE")
                )

        self.stdout.write("")
        summary = f"Web Key 可用 {ok_count}/{len(keys)}"
        style = self.style.SUCCESS if ok_count == len(keys) else self.style.WARNING
        self.stdout.write(style(summary))
