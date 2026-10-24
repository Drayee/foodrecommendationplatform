"""`python manage.py crawl_recipes` —— 联网采集菜谱线索 / 导入菜谱。

用法::

    python manage.py crawl_recipes --query 番茄牛腩 --limit 5
    python manage.py crawl_recipes --query 麻婆豆腐 --import
    python manage.py crawl_recipes --url https://www.xiachufang.com/recipe/106900000/
    python manage.py crawl_recipes --query 减脂餐 --import --max 10
"""
from __future__ import annotations

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "从网络搜索菜谱线索，并可选导入到平台菜谱库"

    def add_arguments(self, parser):
        parser.add_argument("--query", "-q", action="append", default=[], help="搜索关键词，可重复")
        parser.add_argument("--url", "-u", action="append", default=[], help="直接导入菜谱链接，可重复")
        parser.add_argument("--limit", "-l", type=int, default=5, help="每个关键词返回条数")
        parser.add_argument("--max", type=int, default=5, help="每个关键词最多导入条数")
        parser.add_argument("--import", dest="do_import", action="store_true", help="抓取并写入数据库")
        parser.add_argument("--json", action="store_true", help="以 JSON 输出原始结果")

    def handle(self, *args, **options):
        try:
            from websearch.service import search_recipes_online
        except Exception as exc:
            self.stderr.write(f"无法导入 websearch 模块：{exc}")
            return

        queries = options["query"] or []
        urls = options["url"] or []
        if not queries and not urls:
            self.stderr.write("请至少提供 --query 或 --url")
            return

        total_imported = 0
        for query in queries:
            self.stdout.write(self.style.MIGRATE_HEADING(f"搜索：{query}"))
            try:
                result = search_recipes_online(query, limit=options["limit"])
            except Exception as exc:
                self.stderr.write(self.style.WARNING(f"  搜索失败：{exc}"))
                continue

            degraded = result.get("degraded")
            tag = self.style.WARNING("（离线降级）") if degraded else self.style.SUCCESS("（在线）")
            self.stdout.write(f"  来源 {result.get('provider')} {tag}｜耗时 {result.get('latency_ms', 0)}ms")

            for idx, item in enumerate(result.get("results", []), start=1):
                self.stdout.write(f"  {idx}. {item.get('title')}\n     {item.get('url')}")
                if options["do_import"] and idx <= options["max"]:
                    try:
                        from websearch.ingest import import_recipe

                        info = import_recipe(item["url"])
                        total_imported += 1 if info.get("created") else 0
                        self.stdout.write(
                            self.style.SUCCESS(
                                f"     → 已导入 #{info.get('recipe_id')} {info.get('title')}"
                                f"（食材 {info.get('ingredients')} / 步骤 {info.get('steps')}）"
                            )
                        )
                    except Exception as exc:
                        self.stderr.write(self.style.WARNING(f"     → 导入失败：{exc}"))

        for url in urls:
            self.stdout.write(self.style.MIGRATE_HEADING(f"导入：{url}"))
            try:
                from websearch.ingest import import_recipe

                info = import_recipe(url)
                total_imported += 1 if info.get("created") else 0
                self.stdout.write(
                    self.style.SUCCESS(
                        f"  #{info.get('recipe_id')} {info.get('title')}"
                        f"（新增：{info.get('created')}）"
                    )
                )
            except Exception as exc:
                self.stderr.write(self.style.WARNING(f"  导入失败：{exc}"))

        if options["do_import"]:
            self.stdout.write(self.style.MIGRATE_HEADING("刷新检索文本并重建索引…"))
            from food.models import Recipe

            for recipe in Recipe.objects.published().prefetch_related("tags", "ingredients", "steps"):
                recipe.refresh_search_doc()
            try:
                from rag.retriever import get_retriever

                get_retriever().build_index(force=False)
                self.stdout.write(self.style.SUCCESS("索引已更新"))
            except Exception as exc:
                self.stderr.write(self.style.WARNING(f"索引更新失败（可稍后手动 build_index）：{exc}"))

        self.stdout.write(self.style.SUCCESS(f"完成，本次新增 {total_imported} 道菜谱"))
