"""`python manage.py recompute_hot` —— 批量重算菜谱热度分。

热度分 = 互动量 / (发布小时数 + 2)^1.5，建议用 cron 每小时跑一次。
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "重算全部菜谱的 hot_score（时间衰减热度）"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=0, help="只处理前 N 道（0 表示全部）")

    def handle(self, *args, **options):
        try:
            from recommend.hot import recompute_hot_scores
        except Exception as exc:
            self.stderr.write(f"无法导入 recommend 模块：{exc}")
            return
        limit = options["limit"] or None
        updated = recompute_hot_scores(limit=limit)
        self.stdout.write(self.style.SUCCESS(f"已重算 {updated} 道菜谱热度"))
