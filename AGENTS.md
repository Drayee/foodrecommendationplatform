# AGENTS.md — 寻味 · 美食推荐平台

Django 6.1 + Python 3.13 的美食推荐平台：真实菜谱数据、RAG 语义检索、混合推荐（内容画像 + 协同过滤 + 热度）、
联网菜谱采集、AI 美食助手（SSE 流式）。仓库：`Drayee/foodrecommendationplatform`。

## 常用命令

```bash
python manage.py migrate                     # 建表
python manage.py seed_data                   # 灌入演示菜谱/用户/行为数据（幂等）
python manage.py build_index                 # 构建 RAG 索引（--force 全量）
python manage.py crawl_recipes -q 番茄牛腩    # 联网采集菜谱线索（--import 落库）
python manage.py recompute_hot               # 重算热度分
python manage.py platform_report             # 运营 & 技术指标报告
python manage.py runserver                   # 本地起服务：/ 登录页，/app/ 主界面，/admin/ 后台
python -m pytest -q                          # 全部测试（pytest-django）
```

## 结构速查

| 路径 | 职责 |
| --- | --- |
| `RAGDjange/` | Django 配置、页面视图、LLM 客户端、工具注册表 |
| `food/` | 领域模型、REST API、服务层（认证 / 助手）、管理后台、管理命令 |
| `rag/` | 中文切分、hashing TF-IDF 嵌入、BM25、向量库、混合检索（RRF） |
| `recommend/` | 热度衰减、内容画像、item-CF、混合排序（MMR 去同质化） |
| `websearch/` | 多引擎搜索、schema.org Recipe 解析、菜谱入库、离线 fixtures |
| `docs/CONTRACTS.md` | 模块间接口契约（改签名先改这里） |
| `data/seed/` | 种子数据（菜谱 / 菜系 / 标签 / 食材 / 演示用户） |

## 工程约定

- 外部能力（LLM / 网络 / 索引）一律可降级：缺失时功能退化，不抛 500。
- 新增第三方依赖要写进 `requirements.txt`；核心链路只依赖 Django + numpy + requests + bs4/lxml。
- 不提交 `db.sqlite3`、`logs/`、`.cache/`、`data/index/` 等运行期产物。

## Agent skills

### Issue tracker

Issues 与规格都作为 GitHub issues 存在 `Drayee/foodrecommendationplatform`，全部操作走 `gh` CLI。See `docs/agents/issue-tracker.md`.

### Triage labels

五个规范 triage 角色（`needs-triage` / `needs-info` / `ready-for-agent` / `ready-for-human` / `wontfix`）加五个 `wayfinder:*` 标签，已在仓库创建齐全。See `docs/agents/triage-labels.md`.

### Domain docs

single-context：根目录一份 `GLOSSARY.md`，架构决定放 `docs/adr/`。See `docs/agents/domain.md`.
