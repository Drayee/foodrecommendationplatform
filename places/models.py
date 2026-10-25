"""places 的持久化模型。

分工
- `PlaceCache`：外部 POI 结果的短 TTL 缓存（高德数据会过期，不长期落库）；
- `PlaceFavorite` / `PlaceReview` / `PlaceInteraction`：用户资产与行为，需长期保存；
- `PlaceSearchLog`：附近搜索的调用明细（观测用，只记首页、失败静默）。

坐标约定：库内 `lng/lat` 一律存 **GCJ-02**（与高德一致），避免展示错位。
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class PlaceCache(models.Model):
    """周边搜索结果缓存。键 = 坐标网格 + 半径 + 关键词 + 类型。"""

    cache_key = models.CharField("缓存键", max_length=80, unique=True)
    lng = models.DecimalField("经度", max_digits=10, decimal_places=6, default=0)
    lat = models.DecimalField("纬度", max_digits=10, decimal_places=6, default=0)
    radius = models.IntegerField("半径(米)", default=3000)
    keywords = models.CharField("关键词", max_length=60, blank=True, default="")
    types = models.CharField("POI 类型", max_length=30, blank=True, default="050000")
    payload = models.JSONField("结果", default=list, blank=True)
    degraded = models.BooleanField("降级数据", default=False)
    poi_count = models.IntegerField("POI 数", default=0)
    key_label = models.CharField("使用的 Key", max_length=30, blank=True, default="")
    hit_count = models.IntegerField("命中次数", default=0)
    fetched_at = models.DateTimeField("抓取时间", auto_now_add=True)
    expires_at = models.DateTimeField("过期时间", db_index=True)

    class Meta:
        verbose_name = "POI 缓存"
        verbose_name_plural = "POI 缓存"
        ordering = ["-fetched_at"]
        indexes = [models.Index(fields=["expires_at"])]

    def __str__(self) -> str:
        return f"{self.cache_key}({self.poi_count})"

    def is_expired(self) -> bool:
        return self.expires_at <= timezone.now()

    def touch(self) -> None:
        type(self).objects.filter(pk=self.pk).update(hit_count=models.F("hit_count") + 1)


class PlaceSearchLog(models.Model):
    """「附近美食」每次搜索的调用明细，用于运营看板与线上排查。

    写入约定
    - 只在 `page == 1` 时落库：翻页复用的是同一批 POI，记下来只会虚增搜索量；
    - 写入失败必须吞掉（服务层 try/except），观测数据不能影响搜索主流程；
    - 不存具体 POI 列表（那在 `PlaceCache` 里），这里只留可聚合的量化字段。
    """

    user = models.ForeignKey(
        User, verbose_name="用户", on_delete=models.SET_NULL, null=True, blank=True, related_name="place_search_logs"
    )
    session_key = models.CharField("会话", max_length=64, blank=True, default="", db_index=True)
    query = models.CharField("关键词", max_length=60, blank=True, default="")
    type_preset = models.CharField("品类预设", max_length=20, blank=True, default="all")
    radius = models.IntegerField("半径(米)", default=3000)
    lng = models.DecimalField("经度", max_digits=10, decimal_places=6, default=0)
    lat = models.DecimalField("纬度", max_digits=10, decimal_places=6, default=0)
    result_count = models.IntegerField("结果条数", default=0)
    degraded = models.BooleanField("降级数据", default=False)
    cached = models.BooleanField("命中缓存", default=False)
    latency_ms = models.FloatField("耗时(毫秒)", default=0.0)
    key_label = models.CharField("使用的 Key", max_length=30, blank=True, default="")
    created_at = models.DateTimeField("搜索时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "附近搜索日志"
        verbose_name_plural = "附近搜索日志"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["-created_at"]),
            models.Index(fields=["query", "-created_at"]),
            models.Index(fields=["degraded", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%m-%d %H:%M}·{(self.query or '全部')}·{self.result_count}条"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "query": self.query,
            "type_preset": self.type_preset,
            "radius": self.radius,
            "lng": float(self.lng),
            "lat": float(self.lat),
            "result_count": self.result_count,
            "degraded": self.degraded,
            "cached": self.cached,
            "latency_ms": self.latency_ms,
            "key_label": self.key_label,
            "created_at": self.created_at.isoformat(),
        }


class PlaceFavorite(models.Model):
    """用户收藏的店（POI 会变，所以冗余存一份快照字段）。"""

    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="place_favorites")
    poi_id = models.CharField("POI id", max_length=40, db_index=True)
    name = models.CharField("店名", max_length=120)
    address = models.CharField("地址", max_length=255, blank=True, default="")
    lng = models.FloatField("经度", default=0)
    lat = models.FloatField("纬度", default=0)
    tel = models.CharField("电话", max_length=60, blank=True, default="")
    typecode = models.CharField("类型码", max_length=20, blank=True, default="")
    type_label = models.CharField("类型", max_length=60, blank=True, default="")
    rating = models.FloatField("评分", default=0)
    cost = models.FloatField("人均", default=0)
    note = models.CharField("备注", max_length=200, blank=True, default="")
    created_at = models.DateTimeField("收藏时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "收藏的店"
        verbose_name_plural = "收藏的店"
        unique_together = [("user", "poi_id")]
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.user_id}♥{self.name}"

    def to_dict(self) -> dict:
        return {
            "poi_id": self.poi_id,
            "name": self.name,
            "address": self.address,
            "lng": self.lng,
            "lat": self.lat,
            "tel": self.tel,
            "typecode": self.typecode,
            "type_label": self.type_label,
            "rating": self.rating,
            "cost": self.cost,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
        }


class PlaceReview(models.Model):
    """到店打卡评价。"""

    user = models.ForeignKey(User, verbose_name="用户", on_delete=models.CASCADE, related_name="place_reviews")
    poi_id = models.CharField("POI id", max_length=40, db_index=True)
    place_name = models.CharField("店名", max_length=120)
    rating = models.PositiveSmallIntegerField("评分", default=5)
    # 快速打卡（checkin）只记录到访，不写评价文字；它不该抬高店铺均分，
    # 因此聚合（count/avg/distribution）只统计 is_rating=True 的记录。
    is_rating = models.BooleanField("计入评分", default=True, db_index=True)
    content = models.TextField("评价", max_length=1000, blank=True, default="")
    photos = models.JSONField("照片", default=list, blank=True)
    dishes = models.JSONField("推荐菜", default=list, blank=True)
    cost = models.FloatField("人均消费", default=0)
    visited_at = models.DateField("到店日期", null=True, blank=True)
    is_hidden = models.BooleanField("已隐藏", default=False)
    created_at = models.DateTimeField("创建时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "到店打卡"
        verbose_name_plural = "到店打卡"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["poi_id", "-created_at"]), models.Index(fields=["user", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.place_name}:{self.rating}"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "poi_id": self.poi_id,
            "place_name": self.place_name,
            "user": getattr(self.user, "username", ""),
            "rating": self.rating,
            "is_rating": self.is_rating,
            "content": self.content,
            "photos": self.photos,
            "dishes": self.dishes,
            "cost": self.cost,
            "visited_at": self.visited_at.isoformat() if self.visited_at else "",
            "created_at": self.created_at.isoformat(),
        }


class PlaceInteraction(models.Model):
    """POI 行为埋点：view / favorite / route / checkin / search，供排序与热度使用。"""

    class Action(models.TextChoices):
        VIEW = "view", "查看"
        FAVORITE = "favorite", "收藏"
        ROUTE = "route", "导航"
        CHECKIN = "checkin", "打卡"
        SEARCH = "search", "搜索"

    user = models.ForeignKey(
        User, verbose_name="用户", on_delete=models.SET_NULL, null=True, blank=True, related_name="place_interactions"
    )
    session_key = models.CharField("会话", max_length=64, blank=True, default="", db_index=True)
    poi_id = models.CharField("POI id", max_length=40, db_index=True)
    place_name = models.CharField("店名", max_length=120, blank=True, default="")
    action = models.CharField("行为", max_length=16, choices=Action.choices, db_index=True)
    weight = models.FloatField("权重", default=1.0)
    extra = models.JSONField("附加", default=dict, blank=True)
    created_at = models.DateTimeField("发生时间", auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "POI 行为"
        verbose_name_plural = "POI 行为"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["poi_id", "-created_at"]), models.Index(fields=["action", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.action}:{self.poi_id}"
