"""`python manage.py build_index` —— 构建 / 重建 RAG 向量索引。

用法::

    python manage.py build_index              # 增量（内容版本变化的才重建）
    python manage.py build_index --force      # 全量重建
    python manage.py build_index --stats      # 只查看索引状态
"""
from __future__ import annotations

import time

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "构建 RAG 索引（菜谱向量 + BM25），供语义检索与 AI 助手使用"

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="全量重建，忽略内容版本比对")
        parser.add_argument("--stats", action="store_true", help="只输出当前索引状态")

    def handle(self, *args, **options):
        try:
            from rag.retriever import get_retriever
        except Exception as exc:
            self.stderr.write(f"无法导入 rag 模块：{exc}")
            return

        retriever = get_retriever()

        if options["stats"]:
            for key, value in retriever.stats().items():
                self.stdout.write(f"  {key}: {value}")
            return

        started = time.perf_counter()
        self.stdout.write(self.style.MIGRATE_HEADING("开始构建索引…"))
        try:
            result = retriever.build_index(force=options["force"])
        except Exception as exc:
            self.stderr.write(self.style.ERROR(f"索引构建失败：{exc}"))
            return

        elapsed = (time.perf_counter() - started) * 1000
        self.stdout.write(
            self.style.SUCCESS(
                f"索引完成：{result.get('chunks', 0)} 个文本块 / {result.get('recipes', 0)} 道菜谱 / "
                f"维度 {result.get('dim', 0)} / 耗时 {elapsed:.0f}ms"
            )
        )
        for key, value in retriever.stats().items():
            self.stdout.write(f"  {key}: {value}")
