# API 参考

统一约定
- 所有接口返回 `{"code": 0, "msg": "ok", "data": ...}`；`code != 0` 为业务错误，HTTP 状态码同步语义
- 坐标一律 **GCJ-02**；前端上传可传 `coord=wgs84`，服务端自动转换
- 需要登录的接口返回 `401`；`/keys/` 等运营接口要求 `is_staff`
- 响应头带 `X-Response-Time: 12.3ms`，便于排查耗时

---

## 一、附近美食 `/api/v1/places/`

### `GET config/`
前端启动配置：JS Key 配对（key + 安全密钥）、默认半径、品类与排序选项、Key 池健康度。

```json
{ "code": 0, "data": {
  "js_key": "****", "security_code": "****",
  "js_pairs": [{"key": "****", "security_code": "****"}],
  "enabled": true, "default_radius": 3000, "max_radius": 50000,
  "types": [{"key": "all", "label": "全部美食", "code": "050000"}],
  "radius_options": [500, 1000, 2000, 3000, 5000, 10000],
  "sorts": [{"key": "smart", "label": "综合推荐"}],
  "key_pool": {"web_total": 4, "web_available": 4, "js_total": 4}
}}
```

### `GET nearby/`
周边美食搜索。

| 参数 | 类型 | 说明 |
| --- | --- | --- |
| `lng` / `lat` | float | **必填**，坐标 |
| `coord` | string | `wgs84`（默认，浏览器定位）或 `gcj02`（已转换） |
| `radius` | int | 半径（米），100~50000，默认 3000 |
| `q` | string | 关键词，如「火锅」「面馆」 |
| `type` | string | 品类预设：`all/chinese/foreign/fast/leisure/cafe/tea/dessert` |
| `types` | string | 直接传高德 typecode，优先级高于 `type` |
| `sort` | string | `smart`(默认) / `distance` / `rating` / `cost_asc` / `cost_desc` |
| `budget` | float | 人均预算，参与 smart 排序 |
| `page` | int | 页码，默认 1 |

```json
{ "code": 0, "data": {
  "items": [{
    "poi_id": "B0L2SL6KO8", "name": "蘭亭序.地方特色文化主题餐厅(廊桥店)",
    "type_label": "中餐厅", "address": "雅州廊桥与西康路东段交叉口北40米",
    "lng": 103.001082, "lat": 29.987937,
    "distance_m": 49.0, "distance_text": "49m",
    "rating": 4.5, "cost": 69.0, "business_hours": "10:00-21:00",
    "photos": ["https://..."], "tag": "", "score": 0.6856, "source": "amap"
  }],
  "total": 25, "center": {"lng": 103.0012, "lat": 29.9875},
  "radius": 3000, "cached": false, "degraded": false, "provider": "amap",
  "latency_ms": 702.3, "key_pool": {"available": 4, "total": 4},
  "search_id": 1288,
  "center_wgs84": {"lng": 103.0009, "lat": 29.9874}
}}
```

新增字段 `search_id`：本次搜索在 `place_search_log` 表中的主键，按它可直接定位这一条调用明细
（耗时、是否降级、是否命中缓存、用了哪个 Key）。写入失败或 `page > 1` 时为 `null`。

降级行为：无 Key / 配额耗尽 / 网络异常时返回 `degraded: true`、`provider: "fixture"`，
`items[].source = "fixture"`，`message` 说明原因，**HTTP 仍为 200**。

### 搜索观测（`place_search_log`）
每次 `nearby/` **首页**请求都会写一条 `PlaceSearchLog`（翻页不写，避免虚增搜索量；写失败静默跳过，
不影响搜索本身）。落库字段：`user`（未登录为空）、`session_key`、`query`、`type_preset`、`radius`、
`lng`/`lat`（GCJ-02）、`result_count`、`degraded`、`cached`、`latency_ms`、`key_label`、`created_at`。

- 后台只读查看：`/admin/places/placesearchlog/`（不提供新增与编辑）；
- 报表：`python manage.py platform_report` 的「附近美食」段落输出今日/近 7 天搜索量、平均耗时、
  降级率、缓存命中率、热门关键词 Top N、Key 池健康度与收藏/打卡量。

### `GET regeo/`
逆地理编码（`lng`/`lat`/`coord`）→ 省市区、商圈、最近 POI。失败时 `degraded: true`。

### `GET locate/`
IP 定位（可选 `ip`）。返回省市区、adcode 与 `center`（矩形中心）。GPS 被拒时的兜底。

### `GET tips/?q=`
输入提示（联想），返回 `[{name, district, address, location, typecode}]`。

### `GET <poi_id>/`
店铺详情 + 本地评价聚合。

```json
{ "code": 0, "data": {
  "place": { "...同 nearby 的 item..." },
  "reviews": [{ "id": 1, "user": "demo", "rating": 5, "content": "...", "dishes": ["砂锅雅鱼"] }],
  "review_stats": {
    "count": 1, "avg_rating": 5.0, "favorited": 2,
    "distribution": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 1}
  },
  "is_favorite": true
}}
```

### `POST <poi_id>/favorite/`（需登录）
收藏 / 取消收藏（toggle）。可选 body `{"place": {...}}` 带店铺快照；缺省时服务端回查。

### `GET favorites/`（需登录）
我的收藏店铺：`[{poi_id, name, address, lng, lat, rating, cost, type_label, created_at}]`

### `POST <poi_id>/reviews/` / `GET <poi_id>/reviews/`
打卡评价。POST 需登录：

```json
{ "rating": 5, "content": "砂锅很香", "place_name": "砂锅店",
  "cost": 45, "dishes": ["砂锅酥肉"], "photos": [], "visited_at": "2026-10-24" }
```

GET 返回 `{items, count, avg_rating}`。

### `POST <poi_id>/checkin/`（需登录）
快速打卡（不写文字），body `{place_name, rating?, cost?}`。

### `GET reviews/mine/`（需登录）
我的打卡记录。

### `POST track/`
前端埋点：`{"action": "view|route|click", "poi_id": "...", "name": "..."}`。

### `GET keys/` / `POST keys/reset/`
Key 池健康度（匿名只看汇总，staff 看明细）；`reset` 清冷却（staff）。

---

## 二、菜谱与推荐 `/api/v1/`

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `meta/` | 菜系/标签/难度/场景/统计/LLM 与索引状态 |
| GET | `health/` | DB / LLM / 索引健康检查 |
| GET | `recipes/?q&cuisine&tag&difficulty&max_minutes&sort&page&size` | 菜谱列表 |
| GET | `recipes/<id>/` | 菜谱详情（含食材/步骤/营养/评论） |
| POST | `recipes/<id>/rate/` | 评分 1~5（需登录） |
| POST | `recipes/<id>/favorite/` | 收藏 toggle（需登录） |
| GET/POST | `recipes/<id>/comments/` | 评论列表 / 发表 |
| GET | `recipes/<id>/similar/` | 相似菜谱 |
| GET | `search/?q=` | 关键词检索（记 SearchLog） |
| GET | `search/semantic/?q&k` | RAG 语义检索（返回归一化 `score` + `raw_score`） |
| GET | `feed/?k&scene` | 个性化推荐流（`mode`: personalized / cold_start / fallback_hot） |
| POST | `assistant/chat/` | SSE 流式对话，body 支持 `lng`/`lat` 触发附近工具 |
| POST | `assistant/chat/sync/` | 同步对话（调试用） |
| GET | `assistant/history/?session_id=` | 会话历史（含 citations） |
| GET | `assistant/debug/?q=` | 检索调试（top-k 命中明细） |
| GET | `websearch/?q&limit` | 联网菜谱线索 |
| POST | `websearch/import/` | 按 URL 抓取菜谱并入库 |
| GET/POST | `profile/` | 口味偏好读写（需登录） |
| GET | `favorites/` | 我的菜谱收藏 |
| POST | `interactions/` | 菜谱行为埋点 |
| POST | `admin/rebuild_index/` | 重建 RAG 索引（staff） |

### SSE 对话事件

```
data: {"type":"meta","degraded":false}
data: {"type":"token","text":"兰"}
data: {"type":"citations","items":[{"recipe_id":67,"title":"麻婆豆腐","relevance":1.0}]}
data: {"type":"done","latency_ms":1832.4,"tool_calls":1}
```

首字延迟：模型首 token 可能很慢，因此第一个 `token` 事件常常是**过程提示**
（命中附近意图且带坐标时 `正在查附近的店…`，其余 `正在翻菜谱库…`，8~16 字）。
它只是过程提示，不属于正文 `answer`，也不会写进会话记录。想退回「一句不说、直接等模型」，
把 `settings.LLM["PREAMBLE"]` 设为 `False` 即可。

---

## 三、页面路由

| 路径 | 说明 |
| --- | --- |
| `/` | 登录页（邮箱验证码；未配 SMTP 时回显 demo code） |
| `/app/` | 主界面：发现 / 附近 / 找菜 / AI 助手 / 我的 |
| `/app/#/recipe/<id>` | 菜谱详情（前端 hash 路由） |
| `/admin/` | Django 运营后台 |
| `/health/` | 服务健康检查（DB / LLM / 索引） |

---

## 四、错误码

| code | 含义 |
| --- | --- |
| 0 | 成功 |
| 1 | 参数错误 / 业务失败（`msg` 说明） |
| 400 | 参数缺失或非法 |
| 401 | 未登录 |
| 403 | 权限不足（如非 staff） |
| 404 | 资源不存在 |
| 429 | 验证码尝试次数过多 |
| 500/502/503 | 服务端异常 / 外部依赖失败 |

高德侧错误码（`infocode`）见 `python manage.py amap_check` 的输出释义，常见：
`10001` Key 无效、`10003/10029` 配额耗尽、`10005` IP 白名单、`10021` QPS 超限、`10009` 权限不足。
