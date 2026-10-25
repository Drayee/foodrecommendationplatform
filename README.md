# 🍜 寻味 · 美食推荐平台

一个面向「**找菜**」与「**找店**」两条主线的美食平台：既能按口味/食材/场景推荐菜谱（RAG + 混合推荐），
也能基于用户定位调高德 POI 找**附近的美食店**（距离/评分/人均/品类筛选、地图、导航、打卡）。

> Django 6.1 · PostgreSQL 18 · 本地 Ollama(qwen3:14b) · 高德开放平台 · Vue 3（无构建）

---

## ✨ 功能

### 附近美食（主线）
- **定位**：浏览器 GPS → 逆地理（省市区/商圈）→ IP 定位兜底 → 默认位置，四级降级保证总有数据
- **附近搜索**：高德周边搜索（餐饮大类 050000），半径 500m~10km，品类筛选（中餐/快餐/咖啡/甜品…）
- **智能排序**：距离衰减 × 评分 × 人均预算匹配 × 口味画像 × 近期热度，五路加权（可配置权重）
- **地图可视化**：高德 JS API 2.0，标记点 + 信息窗体 + 自动视野适配，脚本失败自动退化为列表模式
- **导航 / 叫车**：高德 URI 唤起（驾车/步行/打车），不消耗服务端配额
- **收藏与打卡**：收藏店铺快照、到店打卡评价（评分/推荐菜/人均）、店铺维度聚合均分
- **AI 联动**：带定位问「附近吃什么」，助手自动调用 `nearby_foods` 工具，用真实店铺数据作答
- **Key 池**：多 Key 轮询，配额（10003/10029）与 QPS（10021）分类冷却，全池耗尽自动降级为离线样例
- **Key 体检**：`manage.py amap_check` 绕过池逐把探测（ip/place/regeo），给出错误码释义

### 菜谱与推荐
- **菜谱库**：62 道真实菜谱 / 12 菜系 / 34 标签 / 132 食材，含用量、步骤、营养估算
- **RAG 检索**：中文切分 + hashing TF-IDF + BM25 + RRF 三路融合，可选 Ollama embedding
- **混合推荐**：热度时间衰减 + 内容画像 + item-CF + MMR 去同质化，冷启动自动降级
- **AI 助手**：SSE 流式对话、引用溯源、工具调用（检索/推荐/联网/统计/邮件），无模型时降级为检索式回答
- **网络采集**：多引擎搜索（DDG/Bing/360）+ schema.org Recipe 解析 + 入库去重
- **用户体系**：邮箱验证码登录、口味偏好、评分收藏评论、行为埋点

---

## 🧱 技术栈

| 层 | 选型 | 说明 |
| --- | --- | --- |
| Web | Django 6.1 | 单项目多 app，函数视图 + JsonResponse，无 DRF 依赖 |
| 数据库 | PostgreSQL 18 | `.env` 可切 SQLite / MySQL |
| 检索 | 自研 `rag/` | 零外部依赖：中文 n-gram hashing 向量 + 自实现 BM25 + RRF |
| 推荐 | 自研 `recommend/` | 纯 numpy/dict，无 scipy/sklearn |
| LLM | Ollama `qwen3:14b` | OpenAI 兼容接口，可换任意网关 |
| 地图 | 高德开放平台 | Web 服务 Key（REST）+ JS API Key（地图）+ 安全密钥 |
| 前端 | Vue 3 全局构建 | 手写组件 + 暖色设计系统，无打包步骤 |
| 测试 | pytest + pytest-django | 196 项，含降级/并发/方言守卫 |

---

## 🚀 快速开始

```bash
# 1) 依赖
pip install -r requirements.txt

# 2) 配置：复制样例并填写
cp .env.example .env
#   AMAP_WEB_KEYS=你的Web服务Key（可逗号分隔多个，组成 Key 池）
#   AMAP_JS_KEYS=你的JS API Key
#   AMAP_SECURITY_CODE=JS安全密钥
#   DB_ENGINE=postgresql / DB_PASSWORD=...
#   LLM_BASE_URL=http://127.0.0.1:11434/v1  LLM_MODEL=qwen3:14b

# 3) 建库 + 灌数据 + 建索引
python manage.py migrate
python manage.py seed_data          # 62 道菜 / 8 演示用户 / 143 评分 / 208 行为
python manage.py build_index        # RAG 索引（99 块 / 1024 维）
python manage.py amap_check         # 体检高德 Key：逐把探测可用性与配额状态

# 4) 起服务
python manage.py runserver
#   登录页  http://127.0.0.1:8000/
#   主界面  http://127.0.0.1:8000/app/
#   后台    http://127.0.0.1:8000/admin/
```

> 没配高德 Key 也能跑：附近页会返回内置离线样例并标记 `degraded=true`；
> 没配 LLM 也能跑：助手降级为「检索式回答」，只影响自然语言润色。

---

## 🐳 容器化部署

```bash
cp .env.example .env          # 至少填 DB_PASSWORD；有高德/LLM 就一并填
docker compose up -d --build
docker compose exec web python manage.py seed_data
docker compose exec web python manage.py build_index
# 打开 http://127.0.0.1:8000/
```

- 生产用 **gunicorn**（3 worker × 4 thread），容器内含 `/health/` HEALTHCHECK
- PostgreSQL 独立容器 + 数据卷；`web` 首次启动自动 `migrate`
- Ollama 跑在宿主机时用 `host.docker.internal`（compose 已配 `extra_hosts`）；跑在容器里就改成服务名
- 镜像只装 `requirements.txt` + `requirements-prod.txt`（gunicorn / psycopg），开发依赖不进镜像
- 不想用 Docker 也可以直接 `python manage.py runserver`，两种方式共用同一份 `.env`

---

## ⚙️ 关键配置

| 变量 | 作用 | 默认 |
| --- | --- | --- |
| `AMAP_WEB_KEYS` | Web 服务 Key **池**（逗号分隔） | 空（降级离线） |
| `AMAP_JS_KEYS` | JS API Key 池 | 空（地图不可用） |
| `AMAP_JS_PAIRS` | `key:安全密钥,key:安全密钥`（JS Key 与 jscode **成对**绑定） | 空 |
| `AMAP_SECURITY_CODE` | 单 Key 场景的兼容写法 | 空 |
| `AMAP_QUOTA_COOLDOWN` | 配额类错误的冷却秒数 | `21600`（6h） |
| `AMAP_CACHE_TTL` | POI 缓存 TTL | `600` |
| `AMAP_DEFAULT_RADIUS` | 默认搜索半径 | `3000` |
| `RECOMMEND_W_*` | 推荐四路权重 | 内容 .45 / CF .30 / 热度 .15 / 新鲜 .10 |
| `RAG_EMBED_BACKEND` | `hashing` 或 `ollama` | `hashing` |
| `DB_ENGINE` | `sqlite` / `postgresql` / `mysql` | `sqlite` |

---

## 🔌 API 一览

### 附近美食 `/api/v1/places/`
| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `config/` | 前端配置（JS Key、品类、排序、半径、Key 池健康度） |
| GET | `nearby/?lng&lat&radius&q&type&sort&budget&coord` | 周边美食搜索（`coord=wgs84` 自动转 GCJ-02） |
| GET | `regeo/?lng&lat` | 逆地理编码 |
| GET | `locate/` | IP 定位兜底 |
| GET | `tips/?q` | 输入提示 |
| GET | `<poi_id>/` | 店铺详情 + 评价聚合 |
| POST | `<poi_id>/favorite/` | 收藏 / 取消收藏 |
| GET | `favorites/` | 我的收藏 |
| GET/POST | `<poi_id>/reviews/` | 评价列表 / 发布打卡 |
| POST | `<poi_id>/checkin/` | 快速打卡 |
| GET | `keys/` | Key 池状态（脱敏；staff 可见明细） |
| POST | `keys/reset/` | 清除 Key 冷却（staff） |

### 菜谱与助手 `/api/v1/`
`meta/` `recipes/` `recipes/<id>/` `recipes/<id>/rate|favorite|comments|similar/`
`search/` `search/semantic/` `feed/` `assistant/chat/`（SSE）`assistant/history/`
`websearch/` `websearch/import/` `profile/` `favorites/` `auth/send_code|login|logout|me/`

---

## 🗂 目录结构

```
FoodRecommendationPlatform/
├── RAGDjange/      Django 配置、页面视图、LLM 客户端、工具注册表
├── food/           菜谱领域模型、REST API、服务层、管理后台、运维命令
├── places/         附近美食：高德客户端、Key 池、坐标转换、POI 缓存、排序
├── rag/            切分 / hashing 向量 / BM25 / 向量库 / 混合检索
├── recommend/      热度衰减 / 内容画像 / item-CF / 混合排序
├── websearch/      多引擎搜索 / JSON-LD 菜谱解析 / 入库
├── data/seed/      种子数据（菜谱 / 菜系 / 标签 / 食材 / 演示用户）
├── places/fixtures 离线 POI 样例（断网降级）
├── templates/      index.html（单页壳）/ login.html
├── static/         app.css / login.css / app.js / nearby.js / api.js / components.js
├── docs/           CONTRACTS.md（模块契约）、agents/（团队协作配置）
└── tests           随各 app 就近放置（pytest 自动收集）
```

---

## 🧪 测试

```bash
python -m pytest -q            # 196 项
python manage.py test          # Django runner 等价
```

覆盖重点：坐标转换精度、Key 池冷却与轮询、POI 归一化与降级、五路排序单调性、
推荐冷启动与 MMR、RAG 混合检索与维度熔断、PG 方言守卫（禁裸 SQL/硬编码 DSN）。

---

## ⚠️ 已知限制

- 高德 POI 的 `tag` / `business_hours` 覆盖率依赖商户数据，部分为空属正常；
- 高德检索结果**不做长期落库**（会过期），仅 10 分钟缓存；用户资产（收藏/打卡）才持久化；
- 单个 JS API Key 无自动轮换（地图 SDK 单页只加载一次），JS 池主要用于多环境分发；
- 首屏地图依赖外网 `webapi.amap.com`，离线时自动降级为列表模式；
- 本地 Ollama 14B 在 8GB 显存上为 CPU/GPU 混合推理，首 token 较慢，前端已用 SSE 流式缓解。

---

## 📄 License

仅用于学习与课程项目演示。高德 POI 数据版权归高德所有，请遵守其服务条款与配额限制。
