"""places —— 附近美食（高德 POI）。

职责边界
- 高德数据是**外部实时数据**：不长期落库，走短 TTL 缓存（`PlaceCache`）；
- 用户产生的数据**持久化**：收藏（`PlaceFavorite`）、打卡评价（`PlaceReview`）、行为（`PlaceInteraction`）；
- 所有对外能力失败都降级：无 Key / 配额耗尽 / 网络异常 → 返回 fixture 并标记 `degraded`。
"""
