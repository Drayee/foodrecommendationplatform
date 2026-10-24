"""`python manage.py platform_report` —— 平台运营 & 技术指标报告。

用于答辩 / 巡检：数据规模、行为分布、推荐覆盖率、检索性能、索引与模型状态。
"""
from __future__ import annotations

import statistics
import time

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db.models import Avg, Count, Sum


class Command(BaseCommand):
    help = "输出平台数据规模、行为分布、推荐与检索质量指标"

    def add_arguments(self, parser):
        parser.add_argument("--top", type=int, default=8, help="Top N 榜单长度")

    def handle(self, *args, **options):
        from food.models import (
            Comment,
            Cuisine,
            Favorite,
            Ingredient,
            InteractionLog,
            Rating,
            Recipe,
            SearchLog,
            Tag,
        )

        top_n = options["top"]
        published = Recipe.objects.published()
        line = self.style.MIGRATE_HEADING

        self.stdout.write(line("══ 数据规模 ══"))
        self.stdout.write(
            f"  菜谱 {published.count()} 道（总 {Recipe.objects.count()}）｜菜系 {Cuisine.objects.count()}"
            f"｜标签 {Tag.objects.count()}｜食材 {Ingredient.objects.count()}"
        )
        self.stdout.write(
            f"  用户 {User.objects.count()}｜评分 {Rating.objects.count()}｜收藏 {Favorite.objects.count()}"
            f"｜评论 {Comment.objects.count()}｜行为 {InteractionLog.objects.count()}"
        )

        self.stdout.write(line("══ 内容质量 ══"))
        agg = published.aggregate(avg=Avg("rating_avg"), views=Sum("view_count"), favs=Sum("favorite_count"))
        self.stdout.write(
            f"  平均评分 {round(agg['avg'] or 0, 2)}｜累计浏览 {agg['views'] or 0}｜累计收藏 {agg['favs'] or 0}"
        )
        incomplete = published.filter(calories=0).count()
        self.stdout.write(f"  营养缺失菜谱 {incomplete} 道｜无步骤菜谱 {published.filter(steps__isnull=True).distinct().count()} 道")
        no_cover = published.filter(cover_url="").count()
        self.stdout.write(f"  无封面菜谱 {no_cover} 道（前端会使用渐变占位）")

        self.stdout.write(line("══ 菜系分布 Top {} ══".format(top_n)))
        for row in published.values("cuisine__name").annotate(n=Count("id")).order_by("-n")[:top_n]:
            self.stdout.write(f"  {row['cuisine__name'] or '未分类':<8} {row['n']:>4} 道")

        self.stdout.write(line("══ 用户行为分布 ══"))
        for row in InteractionLog.objects.values("action").annotate(n=Count("id")).order_by("-n"):
            self.stdout.write(f"  {row['action']:<12} {row['n']:>6}")

        self.stdout.write(line("══ 检索与推荐 ══"))
        search_agg = SearchLog.objects.aggregate(n=Count("id"), avg_ms=Avg("latency_ms"))
        self.stdout.write(f"  检索次数 {search_agg['n'] or 0}｜平均耗时 {round(search_agg['avg_ms'] or 0, 1)}ms")
        for row in SearchLog.objects.values("query").annotate(n=Count("id")).order_by("-n")[:top_n]:
            self.stdout.write(f"  「{row['query'][:20]}」 {row['n']} 次")

        # 推荐覆盖率：有行为用户中，能拿到个性化结果的占比
        try:
            from recommend.hybrid import recommend_for_user

            active_users = (
                User.objects.filter(interactions__isnull=False).distinct()[:20]
                if hasattr(User, "interactions")
                else []
            )
            if active_users:
                hit, total, sizes = 0, 0, []
                for user in active_users:
                    total += 1
                    try:
                        results = recommend_for_user(user, k=12)
                    except Exception:
                        results = []
                    sizes.append(len(results))
                    hit += 1 if results else 0
                self.stdout.write(
                    f"  推荐覆盖率 {hit}/{total}｜平均返回 {statistics.mean(sizes):.1f} 条"
                )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  推荐引擎未就绪：{exc}"))

        try:
            from food.services.assistant import index_status, llm_status

            started = time.perf_counter()
            idx = index_status()
            elapsed = (time.perf_counter() - started) * 1000
            self.stdout.write(
                f"  向量索引 ready={idx.get('ready')} size={idx.get('size', 0)} "
                f"dim={idx.get('dim', 0)}（读取 {elapsed:.0f}ms）"
            )
            llm = llm_status()
            self.stdout.write(
                f"  大模型 ready={llm.get('ready')} model={llm.get('model')} key={'有' if llm.get('has_key') else '无'}"
            )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  状态读取失败：{exc}"))

        self.stdout.write(self.style.SUCCESS("报告结束"))
