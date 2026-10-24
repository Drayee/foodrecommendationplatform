# 模块接口契约（并行开发必须严格遵守）

> 本文件是各模块之间的**唯一约定**。任何实现都必须满足这里的签名与返回结构；
> 需要变更契约时，先改本文件并在 PR/提交信息中注明，禁止私自改名或改变返回值形状。

项目根：`D:\IT\program\python-program\FoodRecommendationPlatform`（Django 6.1 + Python 3.13）
运行测试：`python manage.py test` 或 `python -m pytest`（pytest 未安装时用前者）
数据库：SQLite（`db.sqlite3`），已迁移；`food/models.py` 是唯一数据源。

---

## 1. 数据模型（已实现，勿改）

关键字段（完整定义见 `food/models.py`）：

| 模型 | 关键字段 |
| --- | --- |
| `Cuisine` | `name/slug/region/icon` |
| `Tag` | `name/slug/kind`（taste/method/scene/diet/nutrition） |
| `Ingredient` | `name/category/calories_per_100g/protein_..._/is_common` |
| `Recipe` | `title/slug/cuisine/tags/summary/difficulty/prep_minutes/cook_minutes/servings/calories/protein/fat/carbs/rating_avg/favorite_count/view_count/hot_score/status/source/search_doc/content_version` |
| `RecipeIngredient` | `recipe/ingredient/name_text/amount/unit/is_main/sort_order` |
| `RecipeStep` | `recipe/index/text/duration_min` |
| `RecipeEmbedding` | `recipe/model_name/dim/vector(Binary)/content_version` |
| `UserProfile` | `user/nickname/preferred_cuisines/preferred_tags/disliked_ingredients/diet_flags/spicy_level/max_cook_minutes/onboarded` |
| `Rating` / `Favorite` / `Comment` | `user/recipe/...` |
| `InteractionLog` | `user/session_key/recipe/action/weight/extra/created_at` |
| `SearchLog` | `user/query/mode/result_count/latency_ms/from_cache` |
| `WebSearchCache` | `query_hash/query/provider/payload/degraded/hit_count/expires_at` |
| `ChatSession` / `ChatMessage` | `session_key/role/content/citations/latency_ms` |

`Recipe.to_card()` / `Recipe.to_detail()` 是唯一的序列化出口，API 层只调用它们。

---

## 2. `rag` 包（RAG 检索底座）

### 2.1 `rag/chunker.py`

```python
@dataclass
class Chunk:
    chunk_id: str          # 全局唯一，形如 "recipe:12:0"
    doc_id: str            # "recipe:12"
    recipe_id: int | None
    text: str
    meta: dict             # 至少包含 title / cuisine / tags / total_minutes / difficulty_label

def split_text(text: str, chunk_size: int = 420, overlap: int = 80) -> list[str]
def chunk_recipe(recipe) -> list[Chunk]          # 把一个 Recipe 拆成 1~N 个 Chunk
def iter_all_chunks(batch_size: int = 200) -> Iterator[Chunk]   # 遍历所有已发布菜谱
```

要点：按段落/句子切分，优先不切断步骤；`meta` 要携带展示字段。

### 2.2 `rag/embedder.py`

```python
class BaseEmbedder:
    name: str
    dim: int
    def encode(self, texts: list[str]) -> "np.ndarray"   # (n, dim) float32，已 L2 归一化

def get_embedder() -> BaseEmbedder
```

默认实现 `HashingTfidfEmbedder`：中文 char 1-3 gram + 词级 bigram，hashing trick 到 `settings.RAG["EMBED_DIM"]`（默认 1024），TF 取 log 饱和、IDF 用固定平滑值或滚动统计，最后 L2 归一化。**不得依赖网络下载模型**；若检测到 `sentence_transformers` 且本地已有模型，可选启用 `SentenceTransformerEmbedder`，但默认必须纯本地可跑。

### 2.3 `rag/bm25.py`

```python
class BM25Index:
    def __init__(self, k1: float = 1.5, b: float = 0.75)
    def add_many(self, items: list[tuple[str, str]]) -> None   # (chunk_id, text)
    def search(self, query: str, k: int = 10) -> list[tuple[str, float]]
    def __len__(self) -> int
    def to_dict(self) / from_dict(cls, data) -> "BM25Index"     # 便于持久化
```

自带中文分词（无 jieba 时用 char bigram + 英文/数字词），统计 df/idf/avgdl。

### 2.4 `rag/store.py`

```python
class VectorStore:
    def __init__(self, index_dir: str | Path)
    def build(self, ids: list[str], vectors: "np.ndarray", payloads: list[dict]) -> None
    def save(self) -> None           # 写 vectors.npy + meta.json + bm25.json + manifest.json
    def load(self) -> bool           # 文件不存在返回 False，不抛异常
    def search(self, query_vec: "np.ndarray", k: int = 10) -> list[tuple[str, float]]
    def get_payload(self, chunk_id: str) -> dict
    def stats(self) -> dict          # {"size": n, "dim": d, "updated_at": iso, "model": name}
    def clear(self) -> None
```

持久化文件全部落在 `settings.RAG["INDEX_DIR"]`。

### 2.5 `rag/retriever.py`

```python
@dataclass
class Hit:
    chunk_id: str
    recipe_id: int | None
    score: float
    text: str
    payload: dict

class Retriever:
    def search(self, query: str, k: int = 5, filters: dict | None = None) -> list[Hit]
    def build_index(self, force: bool = False, progress=None) -> dict
        # 返回 {"chunks": n, "recipes": n, "dim": d, "elapsed_ms": ms, "model": name}
    def stats(self) -> dict          # 必须包含 "ready": bool
    def invalidate(self) -> None

def get_retriever() -> Retriever     # 进程内单例，文件 mtime 变化时自动重新加载
```

检索策略：向量余弦 top-N 与 BM25 top-N 用 **RRF**（`score = Σ 1/(60+rank)`）融合，
`settings.RAG["HYBRID_BM25_WEIGHT"]` 控制 BM25 权重；单路为空时退化为另一路。
任何异常都不得抛出到调用方 —— `search()` 返回 `[]` 并记录 warning 日志。

### 2.6 管理命令（由 Lead 提供，需兼容）

`python manage.py build_index --force` 会调用 `get_retriever().build_index(force=True)`。

---

## 3. `recommend` 包（推荐引擎）

### 3.1 `recommend/hot.py`

```python
def hot_score(recipe, now=None) -> float        # 时间衰减：engagement / (hours + 2)^1.5
def recompute_hot_scores(limit: int | None = None) -> int   # 批量回写 Recipe.hot_score，返回更新条数
```

### 3.2 `recommend/content.py`

```python
@dataclass
class ContentProfile:
    cuisine_weights: dict[str, float]
    tag_weights: dict[str, float]
    ingredient_names: set[str]
    disliked: set[str]
    avg_minutes: float
    spicy_level: int
    diet_flags: set[str]

def build_profile(user) -> ContentProfile        # user=None 时返回空画像（冷启动）
def score_recipe(profile: ContentProfile, recipe) -> tuple[float, list[str]]
    # 归一化到 0~1，第二项是推荐理由（中文短语，如 "你常吃川菜"）
def similar_recipes(recipe, k: int = 6) -> list[tuple[Recipe, float]]
    # 基于 tags / 食材 / 菜系 / 文本的余弦相似，排除自身
```

### 3.3 `recommend/cf.py`

```python
def build_user_item_matrix() -> tuple[list[int], list[int], "np.ndarray"]
def item_similarity(k: int = 20) -> dict[int, list[tuple[int, float]]]   # item-based，带进程内缓存
def cf_scores(user, candidate_ids: list[int]) -> dict[int, float]        # 0~1
def invalidate_cache() -> None
```

数据来源：`Rating`（权重 1.0）+ `InteractionLog`（view=1/click=1.5/favorite=3/cook_done=4/rate=score）。
稀疏矩阵用 dict/numpy 实现，**不得引入 scipy/sklearn 依赖**。

### 3.4 `recommend/hybrid.py`

```python
@dataclass
class ScoredRecipe:
    recipe: Recipe
    score: float
    reason: str          # 中文，用于前端展示

def recommend_for_user(user, k: int = 12, scene: str | None = None,
                       exclude_ids: Iterable[int] | None = None,
                       diversity: bool = True) -> list[ScoredRecipe]
    # 权重取 settings.RECOMMEND；无行为数据时自动冷启动（热度 + 菜系多样性）
    # diversity=True 时用 MMR（settings.RECOMMEND["MMR_LAMBDA"]）做去同质化
def recommend_similar(recipe_id: int, k: int = 6) -> list[ScoredRecipe]
```

性能要求：单用户候选集 ≤ 500，全流程 < 200ms（本地 SQLite，缓存命中时 < 50ms）。

---

## 4. `websearch` 包（网络搜索与菜谱采集）

### 4.1 `websearch/providers.py`

```python
@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    source: str          # "duckduckgo" / "bing" / "so" / "fixture"
    extra: dict = field(default_factory=dict)

class WebSearchError(RuntimeError): ...

def http_get(url: str, timeout: int | None = None, headers: dict | None = None) -> str
def web_search(query: str, limit: int = 8, providers: list[str] | None = None) -> list[SearchResult]
    # 依次尝试 duckduckgo(html) → bing(cn) → 360(so.com)；全部失败抛 WebSearchError
def parse_recipe_jsonld(html: str, url: str = "") -> dict | None
    # 解析 schema.org Recipe（含 @graph、list 嵌套），返回
    # {"title","summary","cover_url","ingredients":[{"name","amount","unit"}],
    #  "steps":[{"text","image_url"}],"servings","cook_minutes","prep_minutes","source_site","source_url"}
def fetch_recipe(url: str) -> dict          # 抓页面 → JSON-LD / 站点适配 → 统一结构
```

反爬注意：轮换 `settings.WEBSEARCH["USER_AGENTS"]`，超时用 `WEBSEARCH["TIMEOUT"]`，失败重试一次即可。

### 4.2 `websearch/service.py`

```python
def search_recipes_online(query: str, limit: int = 5) -> dict
    # 返回 {"query","results":[{"title","url","snippet","source"}],"cached":bool,
    #        "degraded":bool,"provider":str,"latency_ms":float,"expires_at":iso}
    # 缓存：WebSearchCache（query_hash=md5(f"{provider}:{query}")），TTL=WEBSEARCH["CACHE_TTL"]
    # 断网且 WEBSEARCH["OFFLINE_FALLBACK"]=True 时读 websearch/fixtures/*.json 并标记 degraded=True
```

### 4.3 `websearch/ingest.py`

```python
def import_recipe(url: str, user=None) -> dict
    # 抓取 → 解析 → 去重（source_url） → 落库 Recipe(status=PUBLISHED, source=WEB)
    # 自动关联/创建 Cuisine、Tag、Ingredient、RecipeIngredient、RecipeStep
    # 返回 {"recipe_id","title","created":bool,"ingredients":n,"steps":n}
```

解析失败必须抛 `ValueError`（API 层会转 502），不得静默创建空菜谱。

### 4.4 `websearch/fixtures/`

放 2~4 个真实结构的离线样例（`duckduckgo_番茄牛腩.json`、`recipe_jsonld_麻婆豆腐.json` 等），
供断网演示与单元测试使用。

---

## 5. 写权限边界（防冲突）

| 目录/文件 | 负责人 |
| --- | --- |
| `rag/**` | teammate-rag |
| `recommend/**` | teammate-recommend |
| `websearch/**` | teammate-websearch |
| `data/seed/**`、`food/management/commands/seed_data.py` | teammate-seed |
| `food/models.py`、`food/api.py`、`food/services/**`、`RAGDjange/**`、`templates/**`、`static/**`、`docs/**` | Lead |

**任何人不得修改他人负责的文件**；发现契约需要调整，先改本文件并在提交信息里写 `contract:` 前缀，
再通过 `send_message` 通知 Lead。

---

## 6. 统一质量标准

1. 全部函数带类型注解与中文 docstring；模块顶部写职责说明；
2. 外部依赖（网络、模型、DB 索引）失败必须降级，不得让上层 500；
3. 日志用 `logging.getLogger("rag" / "recommend" / "websearch")`；
4. 每个模块至少 6 个单元测试，覆盖：正常路径、空数据、异常降级、边界值；
5. 不新增第三方依赖（numpy / requests / beautifulsoup4 / lxml 已安装；禁止 scipy、sklearn、faiss、jieba 作为硬依赖，可选导入必须 try/except）；
6. 提交信息格式 `feat(rag): ...` / `feat(recommend): ...` / `feat(websearch): ...`，但**不要自己 git commit**，由 Lead 统一提交。
