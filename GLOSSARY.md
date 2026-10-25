# 领域词汇表（GLOSSARY）

本文件是「寻味」平台的**领域词汇单一事实来源**：写议题标题、重构提案、测试名、验收标准、接口说明时，
用这里的说法，不要漂移到词表里明确回避的同义词。

- 词条按**拼音首字母**分组；英文术语按其读法/常见写法归入首字母。
- 每条格式：**术语** — 一句话定义 → 在本项目的具体落点（字段名 / 函数 / 文件）。
- 术语与接口签名冲突时以 `docs/CONTRACTS.md` 为准，改签名先改契约再改这里。
- 需要而词表里没有的概念，说明要么概念是新造的语言（先重新考虑），要么词表有缺口（补词条）。

---

## B

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **BM25** | 词频饱和 + 文档长度归一的经典关键词检索打分，擅长精确词命中。 | `rag/bm25.py` 的 `BM25Index`；索引统计里的 `bm25_docs`；权重由 `RAG_BM25_WEIGHT`（默认 `0.5`）控制。 |
| **标签（Tag）** | 菜谱的横切分类，与菜系正交。 | `food/models.py:Tag`，`kind` 取值 `taste` / `method` / `scene` / `diet` / `nutrition`；关联字段 `Recipe.tags`；用户侧 `UserProfile.preferred_tags`。 |

## C

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **菜系（Cuisine）** | 菜谱的地域流派分类（川菜 / 粤菜 / 家常菜…），互斥单选。 | `food/models.py:Cuisine`（`name` / `slug` / `icon`）；`Recipe.cuisine`；用户偏好 `UserProfile.preferred_cuisines`。 |
| **场景（Scene）** | 用餐情境（早餐 / 下饭 / 待客 / 减脂…），用于推荐流与助手推荐的口径。 | `Tag.kind="scene"`；接口 `GET /api/v1/feed/?scene=`；工具 `recommend_recipes(scene=...)`；混合排序里的场景命中逻辑（`recommend/hybrid.py`）。 |

## D

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **打卡** | 用户到店后留下的评分/评价记录，挂在 POI 维度而不是菜谱维度。 | `places/models.py:PlaceReview`（`poi_id` / `place_name` / `rating` / `content` / `dishes` / `cost` / `visited_at` / `photos`）；接口 `POST <poi_id>/reviews/`、`POST <poi_id>/checkin/`（快速打卡，不写文字）；行为埋点 `PlaceInteraction.action="checkin"`。 |

## E

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **embedding（向量）** | 把文本映射成固定维度稠密向量的表示，用于语义相似检索。 | `rag/embedder.py`（`HashingTfidfEmbedder` / `OllamaEmbedder` / `FallbackEmbedder`）；`rag/store.py:VectorStore`；`RAG_EMBED_BACKEND`、`RAG_EMBED_DIM`、`RAG_EMBED_MODEL`；可选落库表 `food/models.py:RecipeEmbedding`。 |

## F

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **方言守卫（Dialect Guard）** | 禁止裸 SQL 与硬编码 DSN 的测试约束，保证 SQLite/PostgreSQL/MySQL 都能跑。 | `tests` 里的 PG 方言守卫用例；数据层一律走 Django ORM，连接参数只从 `settings.DATABASES` 读。 |

## G

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **GCJ-02** | 中国国测局加偏后的坐标系（「火星坐标」），高德/腾讯全系使用。 | **平台对外统一坐标**：`nearby/` 的 `lng`/`lat`、POI 的 `location`、地图渲染都用它；转换见 `places/coord.py:wgs84_to_gcj02`。 |
| **工具调用（Tool Call）** | 让模型按 JSON Schema 请求平台执行函数并回填结果的能力。 | `RAGDjange/tools/tools.py:TOOL_REGISTRY`，共 7 个：`search_recipes` / `get_recipe_detail` / `recommend_recipes` / `web_search_recipes` / `nearby_foods` / `platform_stats` / `send_email`；SSE `done` 事件里的 `tool_calls` 计数。 |

## H

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **hashing TF-IDF** | 不用词表，把中文 n-gram 直接哈希进固定维度再做 TF-IDF 加权的轻量嵌入。 | `rag/embedder.py:HashingTfidfEmbedder`，`RAG_EMBED_BACKEND=hashing`（默认），维度 `RAG_EMBED_DIM=1024`；`/health/` 的 `index.model="hashing-tfidf-zh"`。 |
| **会话（ChatSession）** | 一次多轮对话的容器，绑定用户与一串消息。 | `food/models.py:ChatSession`（`session_key` 唯一 / `title` / `updated_at`）+ `ChatMessage`（`role` / `content` / `citations` / `latency_ms` / `token_usage`）；接口 `GET /api/v1/assistant/history/?session_id=`。 |
| **混合检索（Hybrid Retrieval）** | 向量、BM25、标题三路召回后按排名融合，取长补短。 | `rag/retriever.py:Retriever`；入口 `GET /api/v1/search/semantic/`、助手 `retrieve()`；权重 `RAG_BM25_WEIGHT` / `RAG_TITLE_WEIGHT`。 |

## I

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **item-CF** | 基于物品相似度的协同过滤：用共同消费过的用户构造物品共现矩阵再算余弦相似。 | `recommend/cf.py`（`build_user_item_matrix` / `item_similarity` / `cf_scores`）；推荐权重 `RECOMMEND_W_CF`（默认 `0.30`）。 |

## J

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **降级（degraded）** | 外部依赖不可用时功能退化而不是抛错，用可用数据继续服务。 | 约定：**HTTP 仍 200**，响应带 `degraded: true` 与 `message`；附近搜索时 `provider="fixture"`、`items[].source="fixture"`；LLM 缺失时助手切「检索式回答」。 |
| **检索日志（SearchLog）** | 每次检索的查询、模式、结果数与耗时的埋点。 | `food/models.py:SearchLog`（`query` / `mode` / `result_count` / `latency_ms` / `from_cache`），`mode` ∈ `keyword` / `semantic` / `assistant` / `web`；`platform_report` 据此统计检索延迟。 |

## K

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **Key 池** | 多把高德 Web 服务 Key 的轮询调度器，单把被限流时自动换 Key 继续服务。 | `places/keypool.py:KeyPool`（`acquire` / `report_success` / `report_failure` / `stats` / `reset`），进程内单例 `get_pool("amap")`；配置 `AMAP_WEB_KEYS`；调度策略「使用次数升序 + 上次使用时间升序」；Key 一律脱敏成 `9b4e9d...147a`。 |

## L

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **冷启动（Cold Start）** | 新用户/无行为数据时的兜底推荐，退化为热度 + 新鲜度。 | `GET /api/v1/feed/` 的 `mode="cold_start"`（另有 `personalized` / `fallback_hot`）；`recommend/hybrid.py` 的候选装载与权重回退。 |

## M

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **MMR** | 最大边际相关性重排：在「相关性高」与「彼此不像」之间做权衡，避免推荐流同质化。 | `recommend/hybrid.py`；惩罚强度 `RECOMMEND_MMR_LAMBDA`（默认 `0.7`，越接近 1 越偏相关性）。 |
| **埋点（Interaction）** | 用户行为的原始记录，喂给画像、协同过滤与热度。 | 菜谱侧 `food/models.py:InteractionLog`（`action` ∈ view/click/search/rate/favorite/cook_done，`weight`，`extra`）；店铺侧 `places/models.py:PlaceInteraction`（view/favorite/route/checkin/search）；接口 `POST /api/v1/interactions/`、`POST /api/v1/places/track/`。 |

## N

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **内容画像（Content Profile）** | 由用户显式偏好 + 历史行为聚合出的口味画像，用于给菜谱打「合不合你」的分。 | `recommend/content.py`（`ContentProfile` / `build_profile` / `score_features` / `profile_summary`）；输入字段 `UserProfile.preferred_cuisines` / `preferred_tags` / `disliked_ingredients` / `diet_flags` / `spicy_level` / `max_cook_minutes`；权重 `RECOMMEND_W_CONTENT`（默认 `0.45`）。 |
| **内容版本（content_version）** | 菜谱正文的版本号，索引据此做增量重建。 | `Recipe.content_version`（内容变化 +1）；`manage.py build_index` 只重建版本变化的菜谱，`--force` 才全量。 |

## P

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **POI** | 「Point of Interest」高德的地点对象：一家店一条，带名称、坐标、类型码、评分、人均等。 | 归一化后是 `places/service.py:Place`：`poi_id` / `name` / `typecode` / `type_label` / `address` / `lng` / `lat` / `distance_m` / `rating` / `cost` / `tel` / `photos` / `business_hours` / `tag` / `city` / `district` / `adcode` / `source` / `score`。 |
| **POI 缓存（PlaceCache）** | 周边搜索结果的短周期缓存，避免重复消耗高德配额。 | `places/models.py:PlaceCache`（`cache_key` / `radius` / `keywords` / `types` / `payload` / `degraded` / `poi_count` / `key_label` / `expires_at`）；缓存键由 `coord.grid_key(precision=3)`（约 100m 一格）参与构造；TTL `AMAP_CACHE_TTL`（默认 600s）。**不做长期落库**，POI 数据会过期。 |
| **搜索明细（PlaceSearchLog）** | 每次「附近」首页搜索一条调用记录，用于观测耗时、降级率与热门关键词。 | `places/models.py:PlaceSearchLog`（`user` / `session_key` / `query` / `type_preset` / `radius` / `lng` / `lat` / `result_count` / `degraded` / `cached` / `latency_ms` / `key_label`）；`nearby/` 响应里的 `search_id` 即其主键（翻页与写入失败时为 `null`）；后台只读页 `/admin/places/placesearchlog/`。 |

## Q

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **配额冷却** | 某把 Key 命中配额/QPS 类错误后，把它停用一段时间，让其余 Key 顶住。 | `places/keypool.py`：配额类（`10003/10004/10009/10010/10029/10030/10044/10045`）冷却 `AMAP_QUOTA_COOLDOWN`（默认 6h）；QPS 类（`10021/10019/10020`）冷却 5s；其他错误连续 3 次进 30s 冷却；全池冷却 → `acquire()` 返回 `None` → 直接降级离线样例。 |
| **周边搜索** | 以「圆心 + 半径」查附近 POI 的高德接口（`/v3/place/around`）。 | `places/amap.py:AmapClient.around`；大类 `AMAP_DEFAULT_TYPES=050000`（餐饮服务）；半径 100~`AMAP_MAX_RADIUS`（50000），默认 `AMAP_DEFAULT_RADIUS=3000`；每页 25 条；品类前缀见 `places/service.py:TYPE_PRESETS`（`050100` 中餐 / `050300` 快餐 / `050500` 咖啡…）。 |

## R

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **RRF** | Reciprocal Rank Fusion：按 `1/(k+rank)` 融多路排名，只依赖名次不依赖分值量纲。 | `rag/retriever.py` 三路融合（向量 / BM25 / 标题亲和）；因原始分只有 `0.00x~0.0x`，展示前按本轮最高分归一化成 `score`，原始值留在 `raw_score`。 |
| **热度衰减** | 互动量随时间指数衰减的热门度，避免老内容长期霸榜。 | 公式 `engagement / (hours + 2)^1.5`（`recommend/hot.py:hot_score`）；落到 `Recipe.hot_score`；`manage.py recompute_hot` 批量刷新（建议每小时）；权重 `RECOMMEND_W_HOT`（`0.15`）；衰减窗口 `RECOMMEND_HOT_DECAY_HOURS`（`72`）。 |

## S

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **收藏快照** | 收藏店铺时把店铺关键字段冗余存一份，因为 POI 数据会变/消失。 | `places/models.py:PlaceFavorite`（`poi_id` + `name` / `address` / `lng` / `lat` / `tel` / `typecode` / `type_label` / `rating` / `cost` / `note`），`unique_together=(user, poi_id)`；接口 `POST <poi_id>/favorite/`（toggle，可带 `place` 快照）、`GET /api/v1/places/favorites/`。 |

## T

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **文本块（Chunk）** | 切分后的最小检索单元，一个菜谱会被切成若干块。 | `rag/chunker.py`（`split_text` / `chunk_recipe` / `iter_all_chunks`，`Chunk`）；参数 `RAG_CHUNK_SIZE=420`、`RAG_CHUNK_OVERLAP=80`；`/health/` 的 `index.size` 即块数（当前 99）。 |

## W

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **WGS-84** | GPS 原始坐标（国际标准）。 | 浏览器 `navigator.geolocation` 返回它；前端上报 WGS-84 时传 `coord=wgs84`，服务端转 GCJ-02 再查高德（否则城区偏移 100~700m）；反向展示用 `coord.gcj02_to_wgs84`。 |
| **味型** | 用户/菜谱的口味倾向（麻辣 / 清淡 / 酸甜…）。 | 菜谱侧是 `Tag.kind="taste"`；用户侧是 `UserProfile.spicy_level`（0~5）与 `preferred_tags`；附近排序里由 `places/service.py:_taste_affinity` 计算命中度。 |

## X

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **向量熔断** | 索引维度与当前配置不一致时拒绝加载旧索引，避免静默返回错结果。 | `rag/retriever.py` / `rag/store.py` 的维度校验；触发时 `/health/` 的 `index.ready=false`，处理方式是 `build_index --force`。 |

## Y

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **引用（citation）** | 助手回答实际用到的菜谱来源，用于溯源与防编造。 | SSE 事件 `{"type":"citations","items":[{recipe_id,title,relevance}]}`；落库 `ChatMessage.citations`；来源是混合检索命中的块，`relevance` 按本轮最高分归一化。 |

## Z

| 术语 | 定义 | 本项目落点 |
| --- | --- | --- |
| **智能排序（smart）** | 附近美食的五路加权：距离衰减 × 评分 × 人均预算匹配 × 口味命中 × 近期热度。 | `places/service.py:rank_places`，权重在 `settings.AMAP["SORT_WEIGHTS"]`（距离 `.35` / 评分 `.25` / 人均 `.15` / 口味 `.15` / 热度 `.10`，可改 `places/api.py` 暴露的排序选项）；另有 `distance` / `rating` / `cost_asc` / `cost_desc` 四种显式排序。 |

---

## 附：词表未收但容易混用的说法

| 容易混用的说法 | 应改为 |
| --- | --- |
| 「推荐算法」 | 说明是哪一路：内容画像 / item-CF / 热度 / 混合排序 |
| 「搜索」 | 说明是哪一种：关键词检索（`search/`）、语义检索（`search/semantic/`）、联网采集（`websearch/`）、附近搜索（`places/nearby/`） |
| 「缓存」 | 说明是 `PlaceCache`（POI，10 分钟）、`WebSearchCache`（外部搜索，1 小时）、Django `CACHES`（框架级）还是内存里的检索器单例 |
| 「模型不可用」 | 拆成 `llm.ready=false`（模型网关）、`index.ready=false`（RAG 索引）、`degraded=true`（外部 POI）三种 |
