"""places 管理后台：目前只挂「附近搜索日志」，且为只读视图。

为什么只读
- 日志由 `service.nearby()` 自动写入，人工新增/编辑只会污染统计数据；
- 保留「删除」用于清理过期明细（例如压测或演示后的残余数据），
  批量清理也可以走 `python manage.py shell` 或平台报表确认后再删。
"""
from __future__ import annotations

from django.contrib import admin

from places.models import PlaceSearchLog


@admin.register(PlaceSearchLog)
class PlaceSearchLogAdmin(admin.ModelAdmin):
    """附近搜索明细（只读）。"""

    list_display = (
        "id",
        "created_at",
        "query",
        "type_preset",
        "radius",
        "result_count",
        "latency_ms",
        "cached",
        "degraded",
        "key_label",
        "user",
        "session_key",
    )
    list_filter = ("degraded", "cached", "type_preset")
    search_fields = ("query", "session_key", "key_label")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 50
    readonly_fields = (
        "user",
        "session_key",
        "query",
        "type_preset",
        "radius",
        "lng",
        "lat",
        "result_count",
        "degraded",
        "cached",
        "latency_ms",
        "key_label",
        "created_at",
    )

    def has_add_permission(self, request) -> bool:  # noqa: D102 - admin 钩子
        return False

    def has_change_permission(self, request, obj=None) -> bool:  # noqa: D102
        return False

    def get_readonly_fields(self, request, obj=None):  # noqa: D102
        return self.readonly_fields
