"""`python manage.py platform_report` —— 平台运营 & 技术指标报告。

用于答辩 / 巡检：数据规模、行为分布、推荐覆盖率、检索性能、索引与模型状态。
"""
from __future__ import annotations

import statistics
import time
from datetime import timedelta

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

        self._report_nearby(top_n, line)

        self.stdout.write(self.style.SUCCESS("报告结束"))

    # ------------------------------------------------------------------
    # 附近美食（places 侧观测）
    # ------------------------------------------------------------------
    def _report_nearby(self, top_n: int, line) -> None:
        """附近搜索量、降级/缓存健康度、关键词榜、Key 池与用户资产。"""
        from django.utils import timezone

        from places import keypool
        from places.models import PlaceFavorite, PlaceReview, PlaceSearchLog

        self.stdout.write(line("══ 附近美食 ══"))
        try:
            today = timezone.localdate()
            week_start = timezone.now() - timedelta(days=7)
            logs = PlaceSearchLog.objects.all()
            today_logs = logs.filter(created_at__date=today)
            week_logs = logs.filter(created_at__gte=week_start)

            def _gate(qs) -> str:
                n = qs.count()
                if not n:
                    return "0 次｜（今日暂无搜索）"
                agg = qs.aggregate(avg=Avg("latency_ms"))
                degraded = qs.filter(degraded=True).count()
                cached = qs.filter(cached=True).count()
                return (
                    f"{n} 次｜平均耗时 {round(agg['avg'] or 0, 1)}ms"
                    f"｜降级率 {degraded / n:.0%}｜缓存命中率 {cached / n:.0%}"
                )

            self.stdout.write(f"  今日 {_gate(today_logs)}")
            self.stdout.write(f"  近 7 天 {_gate(week_logs)}")
            self.stdout.write(f"  累计 {logs.count()} 次搜索｜日志表 place_search_log")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  搜索日志读取失败：{exc}"))

        try:
            self.stdout.write(f"── 热门搜索关键词 Top {top_n} ──")
            rows = (
                PlaceSearchLog.objects.exclude(query="")
                .values("query")
                .annotate(n=Count("id"))
                .order_by("-n")[:top_n]
            )
            rows = list(rows)
            if not rows:
                self.stdout.write("  （暂无带关键词的搜索）")
            for idx, row in enumerate(rows, start=1):
                self.stdout.write(f"  {idx:>2}. 「{row['query'][:20]}」 {row['n']} 次")
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  关键词榜读取失败：{exc}"))

        try:
            stats = keypool.pool_stats()
            web, js = stats.get("web", {}), stats.get("js", {})
            self.stdout.write(
                f"  Key 池（web）总 {web.get('total', 0)}｜可用 {web.get('available', 0)}"
                f"｜冷却 {web.get('cooling', 0)}"
            )
            self.stdout.write(
                f"  Key 池（js） 总 {js.get('total', 0)}｜可用 {js.get('available', 0)}"
                f"｜冷却 {js.get('cooling', 0)}"
            )
            cooling = [k for k in (web.get("keys") or []) if k.get("status") == "cooling"]
            for item in cooling[:5]:
                self.stdout.write(
                    f"    · {item.get('masked', '')} 冷却剩余 {item.get('cooldown_remaining', 0)}s"
                    f"（{item.get('last_error') or '未记录原因'}）"
                )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  Key 池状态读取失败：{exc}"))

        try:
            fav = PlaceFavorite.objects.count()
            checkins = PlaceReview.objects.count()
            avg_rating = PlaceReview.objects.aggregate(avg=Avg("rating"))["avg"] or 0
            self.stdout.write(
                f"  收藏店铺 {fav} 个｜打卡 {checkins} 条｜平均打卡评分 {round(avg_rating, 2)}"
            )
        except Exception as exc:
            self.stdout.write(self.style.WARNING(f"  用户资产读取失败：{exc}"))
