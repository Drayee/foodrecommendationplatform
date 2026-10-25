# 部署手册（本机生产环境 · 不用容器）

本文件描述「寻味」美食平台在**一台本机服务器**上的生产部署方式：Python 3.13 裸机 + 本机 PostgreSQL 18 +
gunicorn + nginx + systemd，Ollama 与高德开放平台按需接入。全部命令与参数均来自本仓库实际代码与配置
（`RAGDjange/settings.py`、`requirements.txt`、`requirements-prod.txt`、`manage.py` 各命令），可直接照抄。

> 占位符约定：`<APP_DIR>` 项目根目录（示例 `/srv/xunwei`）、`<DOMAIN_OR_IP>` 对外访问域名或 IP、
> `<DB_PASSWORD>` 数据库密码、`<DB_USER>` 数据库账号、`<AMAP_WEB_KEY_n>` / `<AMAP_JS_KEY_n>` / `<AMAP_JS_CODE_n>`
> 高德 Key 与安全密钥。**任何真实密钥、密码、内网地址都不要写进仓库**。

---

## 0. 部署拓扑

```
浏览器
  │  http/https
  ▼
nginx :80/:443          ← 静态文件直接由 nginx 出，其余反代
  │  proxy_pass 127.0.0.1:8000
  ▼
gunicorn（3 worker × 4 thread，gthread 模型）
  │  Django 6.1（WSGI: RAGDjange.wsgi:application）
  ├── PostgreSQL 18 @ 127.0.0.1:5432/food_platform   ← 业务数据
  ├── data/index/                                    ← RAG 向量 + BM25 索引（本地文件）
  ├── 本地 Ollama @ 127.0.0.1:11434/v1               ← 可选，缺省降级为检索式回答
  └── 高德 Web 服务 API（公网）                       ← 缺 Key / 配额耗尽降级为离线样例
```

单机部署的关键点只有四个：**数据库连本地实例**、**索引文件与代码同机**、**SSE 不能被 nginx 缓冲**、
**静态文件交给 nginx（不走 Django）**。

---

## 1. 前置依赖

### 1.1 Python 3.13

```bash
python3.13 --version          # 需 ≥ 3.13
cd <APP_DIR>
python3.13 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -r requirements.txt -r requirements-prod.txt   # 后者含 gunicorn / psycopg
```

- `requirements.txt`：Django / numpy / requests / bs4 / lxml / openai / pytest 等运行与测试依赖；
- `requirements-prod.txt`：`gunicorn>=23.0`、`psycopg[binary]>=3.2`（PostgreSQL 驱动，Django 用 `psycopg` 3）；
- 可选：`REDIS_URL` 生效时需 `redis>=5.0`；`DB_ENGINE=mysql` 时需 `PyMySQL>=1.1`（二者都不装也能跑）。

### 1.2 PostgreSQL 18（本机实例）

```bash
sudo apt install postgresql-18       # Debian/Ubuntu 系示例，其他发行版用对应包管理器
sudo systemctl enable --now postgresql
psql --version                       # 期望 psql (PostgreSQL) 18.x
```

服务端需监听 `127.0.0.1:5432`（默认即可：`postgresql.conf` 的 `listen_addresses = 'localhost'`）。

### 1.3 Ollama（可选）

```bash
ollama serve                         # 默认监听 127.0.0.1:11434
ollama pull qwen3:14b
curl -s http://127.0.0.1:11434/v1/models | head -c 200   # 确认 OpenAI 兼容端点可用
```

不装 Ollama 也能上线：助手自动降级为「检索式回答」（只影响自然语言润色，检索与引用照常）。

### 1.4 高德开放平台 Key

在高德开放平台控制台创建 **Web 服务** 类型 Key（服务端 REST：周边搜索 / 逆地理 / IP 定位 / 输入提示）
与 **Web 端（JS API）** 类型 Key（前端地图渲染），并把部署机**出口 IP** 加入 IP 白名单。

| Key 类型 | 环境变量 | 白名单要求 | 说明 |
| --- | --- | --- | --- |
| Web 服务 | `AMAP_WEB_KEYS` | 建议按 IP 白名单 | 逗号分隔多把 → 组成 Key 池，池内轮询 + 配额冷却 |
| JS API | `AMAP_JS_KEYS` / `AMAP_JS_PAIRS` | 建议按域名白名单 | 2021-12 之后申请的 JS Key **必须**配安全密钥（jscode） |

- `AMAP_JS_PAIRS` 写法：`<AMAP_JS_KEY_1>:<AMAP_JS_CODE_1>,<AMAP_JS_KEY_2>:<AMAP_JS_CODE_2>`（逐 Key 配对）；
- 只配一把 Key 时可用兼容写法 `AMAP_SECURITY_CODE`；
- 白名单配错的典型表现是 `10005`（IP 白名单校验失败）/ `10006`（域名白名单校验失败），见第 10.1 节。

---

## 2. 数据库

### 2.1 建库与账号

用超级用户 `postgres` 执行（密码用你自己的强口令替换 `<DB_PASSWORD>`）：

```sql
-- 1) 业务账号
CREATE USER <DB_USER> WITH PASSWORD '<DB_PASSWORD>';

-- 2) 业务库（UTF8；显式指定模板，避免继承 template1 的 locale 差异）
CREATE DATABASE food_platform
    OWNER <DB_USER>
    ENCODING 'UTF8'
    LC_COLLATE 'C.UTF-8'
    LC_CTYPE 'C.UTF-8'
    TEMPLATE template0;

-- 3) PostgreSQL 15+ 需显式授予 public schema 权限（否则 migrate 会报 permission denied）
\c food_platform
GRANT ALL ON SCHEMA public TO <DB_USER>;
GRANT ALL PRIVILEGES ON DATABASE food_platform TO <DB_USER>;
```

一次性执行：`sudo -u postgres psql -v ON_ERROR_STOP=1 -f create_db.sql`。

验证：

```bash
PGPASSWORD='<DB_PASSWORD>' psql -h 127.0.0.1 -U <DB_USER> -d food_platform -c "select version();"
```

### 2.2 `.env` 关键项

`.env` 放在项目根目录（`RAGDjange/env.py` 会自动读取；systemd 的 `EnvironmentFile` 也指向它）。生产最小集合：

```ini
# ---- Django ----
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<RANDOM_50_CHARS>
DJANGO_ALLOWED_HOSTS=<DOMAIN_OR_IP>,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=https://<DOMAIN_OR_IP>
TIME_ZONE=Asia/Shanghai
LOG_LEVEL=INFO

# ---- 数据库（本机 PG 18）----
DB_ENGINE=postgresql
DB_NAME=food_platform
DB_USER=<DB_USER>
DB_PASSWORD=<DB_PASSWORD>
DB_HOST=127.0.0.1
DB_PORT=5432
DB_CONN_MAX_AGE=60
DB_CONNECT_TIMEOUT=8

# ---- LLM（本地 Ollama，可留空降级）----
LLM_ENABLED=1
LLM_BASE_URL=http://127.0.0.1:11434/v1
LLM_MODEL=qwen3:14b
LLM_API_KEY=ollama
LLM_TIMEOUT=60

# ---- RAG ----
RAG_EMBED_BACKEND=hashing
RAG_EMBED_DIM=1024

# ---- 高德 ----
AMAP_WEB_KEYS=<AMAP_WEB_KEY_1>,<AMAP_WEB_KEY_2>
AMAP_JS_PAIRS=<AMAP_JS_KEY_1>:<AMAP_JS_CODE_1>,<AMAP_JS_KEY_2>:<AMAP_JS_CODE_2>
AMAP_QUOTA_COOLDOWN=21600
AMAP_CACHE_TTL=600
AMAP_DEFAULT_RADIUS=3000

# ---- 邮件验证码（留空 = 演示模式，验证码打印到日志/回显前端）----
# EMAIL_HOST=smtp.qq.com
# EMAIL_PORT=465
# EMAIL_HOST_USER=<MAIL_USER>
# EMAIL_HOST_PASSWORD=<MAIL_AUTH_CODE>
# EMAIL_USE_SSL=1
```

注意两点：
1. `DJANGO_DEBUG=0` 时 Django 不会自己服务静态文件，必须做第 5 节的 `collectstatic` + nginx `alias`；
2. `.env` 里的值两端引号会被去掉（见 `RAGDjange/env.py`），含空格的值不要加引号包裹整段。

权限收紧：`chmod 600 .env`。

### 2.3 迁移 + 灌数据 + 建索引

```bash
cd <APP_DIR> && source .venv/bin/activate

python manage.py migrate
python manage.py seed_data            # 幂等：62 道菜谱 / 12 菜系 / 34 标签 / 132 食材 / 8 演示用户
python manage.py build_index          # 增量；首次构建约 99 个文本块 / 1024 维
python manage.py recompute_hot        # 热度分（也可交给 cron，见第 7.3 节）
python manage.py amap_check           # 体检高德 Web Key（逐把探测，见第 10 节）
```

常用变体：

| 命令 | 用途 |
| --- | --- |
| `python manage.py seed_data --reset` | 清空业务数据后重灌（演示/重建环境用） |
| `python manage.py seed_data --skip-users` | 只灌菜谱主数据 |
| `python manage.py build_index --force` | 全量重建索引（换了 embedding 后端时必须） |
| `python manage.py build_index --stats` | 只读查看索引状态 |
| `python manage.py platform_report --top 10` | 运营 & 技术指标报告（巡检/答辩用） |

索引落在 `data/index/`（`settings.RAG["INDEX_DIR"]`），**不随代码走**，换机要重建而不是拷贝旧索引；
`build_index` 按 `Recipe.content_version` 做增量，内容没变不会重复算。

### 2.4 建管理员

```bash
python manage.py createsuperuser     # /admin/ 后台；/api/v1/places/keys/ 明细也要求 is_staff
```

---

## 3. 进程：gunicorn

### 3.1 直接启动（先手工验证）

```bash
cd <APP_DIR> && source .venv/bin/activate

gunicorn RAGDjange.wsgi:application \
  --bind 127.0.0.1:8000 \
  --worker-class gthread \
  --workers 3 \
  --threads 4 \
  --timeout 120 \
  --keep-alive 5 \
  --access-logfile <APP_DIR>/logs/gunicorn-access.log \
  --error-logfile <APP_DIR>/logs/gunicorn-error.log \
  --capture-output
```

参数说明：

| 参数 | 取值 | 原因 |
| --- | --- | --- |
| `--worker-class gthread` | 3 worker × 4 thread = 12 并发 | `assistant/chat/` 是 SSE 长连接，纯 sync worker 会被长请求占死 |
| `--threads 4` | 4 | 配合 `KeyPool` 的 `threading.Lock` 与 Django 多线程 dev server 语义 |
| `--timeout 120` | 秒 | 首 token 慢的场景靠 SSE 流式输出，不需更长的单请求超时；若用非流式调试接口可调大 |
| `--bind 127.0.0.1:8000` | 只监听本机 | 前面有 nginx；需局域网直连时改 `0.0.0.0:8000` |

> 说明：Key 池是**进程内**状态（`places/keypool.py` 的 `_pools` 单例，跨进程不共享），
> 3 个 worker 各持一份冷却表。配额粒度由高德侧约束，功能上正确；只是「某把 Key 已冷却」的判定
> 在不同 worker 间可能有几分钟时差。要全局一致就得引入共享缓存，本项目刻意不做（无额外依赖）。

### 3.2 systemd unit 完整示例

`/etc/systemd/system/xunwei.service`：

```ini
[Unit]
Description=Xunwei Food Recommendation Platform (gunicorn)
Documentation=file://<APP_DIR>/docs/DEPLOY.md
After=network-online.target postgresql.service
Wants=network-online.target
# 本机 PostgreSQL 是硬依赖：起不来就不必要启动应用。
# 若实例是集群化的 postgresql@18-main.service，改成同名单元或改用 Wants= 让应用自己重试。
Requires=postgresql.service

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=<APP_DIR>
EnvironmentFile=<APP_DIR>/.env
Environment=PYTHONUNBUFFERED=1
Environment=PATH=<APP_DIR>/.venv/bin:/usr/local/bin:/usr/bin:/bin

ExecStart=<APP_DIR>/.venv/bin/gunicorn RAGDjange.wsgi:application \
    --bind 127.0.0.1:8000 \
    --worker-class gthread \
    --workers 3 \
    --threads 4 \
    --timeout 120 \
    --keep-alive 5 \
    --access-logfile <APP_DIR>/logs/gunicorn-access.log \
    --error-logfile <APP_DIR>/logs/gunicorn-error.log \
    --capture-output

ExecReload=/bin/kill -s HUP $MAINPID
KillSignal=SIGQUIT
TimeoutStopSec=30
Restart=always
RestartSec=3
# 常用加固（按需启用）
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
```

启用与日常操作：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now xunwei
sudo systemctl status xunwei --no-pager
sudo journalctl -u xunwei -n 100 --no-pager     # stdout/stderr（含 Django console 日志）
sudo systemctl restart xunwei
```

要点：
- `EnvironmentFile` 与 Django 自己读 `.env` 是**两条独立通道**，值一致即可；systemd 侧不做变量展开，值里别写 `$`；
- `logs/` 归 `www-data` 所有：`sudo install -d -o www-data -g www-data <APP_DIR>/logs`；
- `Restart=always` + `RestartSec=3`：连不上数据库会不停重启，排查时先看 `journalctl` 再看第 10 节。

---

## 4. 反向代理：nginx

`/etc/nginx/sites-available/xunwei`（`ln -s` 到 `sites-enabled/`）：

```nginx
upstream xunwei_app {
    server 127.0.0.1:8000 fail_timeout=0;
    keepalive 16;
}

server {
    listen 80;
    server_name <DOMAIN_OR_IP>;

    # 证书配好后把 80 全量跳 https
    # return 301 https://$host$request_uri;

    client_max_body_size 8m;
    access_log /var/log/nginx/xunwei-access.log;
    error_log  /var/log/nginx/xunwei-error.log warn;

    # ---- 静态资源：Django 不再参与（STATIC_ROOT = <APP_DIR>/staticfiles）----
    location /static/ {
        alias <APP_DIR>/staticfiles/;
        expires 7d;
        add_header Cache-Control "public, max-age=604800";
        access_log off;
    }

    # ---- 用户上传（MEDIA_ROOT = <APP_DIR>/media）----
    location /media/ {
        alias <APP_DIR>/media/;
        expires 1d;
        access_log off;
    }

    # ---- AI 助手 SSE：必须关缓冲 + 长读超时 ----
    location = /api/v1/assistant/chat/ {
        proxy_pass http://xunwei_app;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        proxy_buffering off;              # 关键：否则 token 攒满缓冲区才吐
        proxy_cache off;
        proxy_read_timeout 300s;          # qwen3:14b 首 token 慢 + 长回答
        proxy_send_timeout 300s;
        chunked_transfer_encoding on;
    }

    # ---- 其余全部走 Django ----
    location / {
        proxy_pass http://xunwei_app;
        proxy_http_version 1.1;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Connection        "";

        proxy_read_timeout 120s;          # 与 gunicorn --timeout 对齐
        proxy_redirect off;
    }
}
```

说明：
- Django 侧 SSE 响应已带 `X-Accel-Buffering: no`（`food/api.py`），nginx 会尊重它；这里再显式 `proxy_buffering off`
  是双保险，两者取其一也能正常流式；
- `location = /api/v1/assistant/chat/` 用**精确匹配**（`=`），保证长超时只作用于流式接口，不影响普通请求；
- 用 `proxy_pass http://xunwei_app;`（不带 URI）时不会重复拼接 `/api/v1/...` 前缀；
- 走 HTTPS 时同时把 `.env` 的 `DJANGO_CSRF_TRUSTED_ORIGINS` 改成 `https://<DOMAIN_OR_IP>`，否则 POST 会被 403。

校验并生效：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

### 4.1 SSE 链路自检

```bash
curl -N -H 'Content-Type: application/json' \
     -d '{"message":"附近有什么好吃的","lng":103.0012,"lat":29.9875}' \
     http://127.0.0.1/api/v1/assistant/chat/
```

期望看到事件逐条到来：`{"type":"meta"...}` → 多个 `{"type":"token"...}` → `{"type":"citations"...}` → `{"type":"done"...}`。
若 token 一次性成批出现，说明 `proxy_buffering off` 没生效或命中了别的 location。

---

## 5. 静态资源

```bash
cd <APP_DIR> && source .venv/bin/activate
python manage.py collectstatic --noinput
ls staticfiles | head             # 期望看到 app.css / login.css / app.js / nearby.js / api.js / components.js ...
```

- 收集目标由 `settings.STATIC_ROOT = BASE_DIR / "staticfiles"` 决定，源目录是 `BASE_DIR/static`（`STATICFILES_DIRS`）；
- `STATIC_URL = "static/"`（相对），因此 nginx 里 `location /static/` + `alias <APP_DIR>/staticfiles/` 必须成对出现；
- 缓存刷新：模板以 `?v={{ static_version }}` 附加版本号（`settings.STATIC_VERSION`）。生产建议把版本号固定成发布标识，
  避免每次重启都换：
  ```ini
  STATIC_VERSION=$(git rev-parse --short HEAD)
  ```
  写进 `.env` 后重启服务即可。
- 每次发布都要重跑 `collectstatic`（`--noinput` 用于非交互；直接覆盖会产生大量陈旧文件的，可加 `--clear`）。

---

## 6. 日志

### 6.1 应用日志（代码已配好轮转）

`settings.LOGGING` 的 `file` handler 是 `RotatingFileHandler`：

| 配置 | 值 |
| --- | --- |
| 文件 | `<APP_DIR>/logs/app.log` |
| 单文件上限 | 5 MB（`maxBytes = 5 * 1024 * 1024`） |
| 保留份数 | 3（`backupCount`，即 `app.log` + `app.log.1..3`） |
| 编码 | `utf-8` |
| 级别 | `LOG_LEVEL`（默认 `INFO`），`django.request` 固定 `WARNING` |

即**应用侧不需要再配 logrotate**；`logs/` 目录由 `settings.py` 启动时自动 `mkdir`。

gunicorn 自己的访问/错误日志（第 3 节命令里的 `logs/gunicorn-*.log`）**不带轮转**，用 logrotate 管：

`/etc/logrotate.d/xunwei`：

```
<APP_DIR>/logs/gunicorn-access.log
<APP_DIR>/logs/gunicorn-error.log {
    daily
    rotate 14
    dateext
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su www-data www-data
}
```

`copytruncate` 保证不需要给 gunicorn 发信号重新打开文件句柄。

### 6.2 nginx 访问日志

```nginx
log_format xunwei '$remote_addr - $remote_user [$time_local] "$request" '
                  '$status $body_bytes_sent "$http_referer" '
                  '"$http_user_agent" rt=$request_time urt=$upstream_response_time';
access_log /var/log/nginx/xunwei-access.log xunwei;
```

`/etc/logrotate.d/nginx` 由发行版自带（通常 `daily`/`rotate 14`）。排查耗时优先看 `rt` 与 `urt`
（`urt` 明显大于 `rt` 时，瓶颈在 Django/DB/外部 API 一侧）。

```bash
tail -f <APP_DIR>/logs/app.log                      # 应用侧（含 Key 池冷却、检索降级）
tail -f /var/log/nginx/xunwei-access.log | grep -v ' 200 '
journalctl -u xunwei -f                             # 进程崩溃 / 重启循环
```

---

## 7. 备份与恢复

### 7.1 数据库备份（pg_dump 自定义格式）

```bash
# 目录约定
sudo install -d -o postgres -g postgres /var/backups/xunwei

# 逻辑备份（自定义格式，可并行/选择性恢复）
PGPASSWORD='<DB_PASSWORD>' pg_dump \
  -h 127.0.0.1 -p 5432 -U <DB_USER> -d food_platform \
  -Fc -Z6 \
  -f /var/backups/xunwei/food_platform_$(date +%F_%H%M).dump
```

版本敏感：`pg_dump` 版本应 **≥** 服务端版本（PG 18 用 `pg_dump` 18）。

### 7.2 恢复

```bash
# 1) 停写（避免恢复过程中被写入）
sudo systemctl stop xunwei

# 2) 重建空库（恢复目标是空 schema；pg_restore -c 也可，但重建更干净）
sudo -u postgres psql -c "DROP DATABASE IF EXISTS food_platform;"
sudo -u postgres psql -c "CREATE DATABASE food_platform OWNER <DB_USER> ENCODING 'UTF8' TEMPLATE template0;"

# 3) 恢复（--no-owner 让对象归属当前账号，避免角色名不一致报错）
PGPASSWORD='<DB_PASSWORD>' pg_restore \
  -h 127.0.0.1 -U <DB_USER> -d food_platform \
  --no-owner --role=<DB_USER> \
  /var/backups/xunwei/food_platform_2026-10-25_0300.dump

# 4) 起服务并复核
sudo systemctl start xunwei
curl -s http://127.0.0.1:8000/health/ | python3 -m json.tool
```

索引文件（`data/index/`）**不需要备份**：恢复完直接重建即可（内容以数据库为准）：

```bash
python manage.py build_index --force
```

### 7.3 定时任务示例

`/etc/cron.d/xunwei`（root 拥有，权限 644）：

```cron
# 每天 03:00 全量逻辑备份，保留 14 天
0 3 * * * postgres  PGPASSWORD='<DB_PASSWORD>' pg_dump -h 127.0.0.1 -U <DB_USER> -d food_platform -Fc -Z6 -f /var/backups/xunwei/food_platform_$(date +\%F).dump
15 3 * * * postgres find /var/backups/xunwei -name 'food_platform_*.dump' -mtime +14 -delete

# 每小时重算热度分（热度 = 互动量 / (发布小时数 + 2)^1.5）
5 * * * * www-data cd <APP_DIR> && .venv/bin/python manage.py recompute_hot >> logs/cron.log 2>&1

# 每天 04:10 巡检高德 Key（低配额探测）
10 4 * * * www-data cd <APP_DIR> && .venv/bin/python manage.py amap_check >> logs/cron.log 2>&1

# 每天 05:00 重建索引（内容有更新时；--force 改 embedding 时才用）
0 5 * * * www-data cd <APP_DIR> && .venv/bin/python manage.py build_index >> logs/cron.log 2>&1
```

`cron` 里的 `%` 必须转义成 `\%`；`www-data` 需要能读 `<APP_DIR>` 与 `.env`（`chmod 640 .env`，属组 `www-data`）。

### 7.4 恢复演练

每季度做一次：把最新 dump 恢复到一个临时库（`food_platform_restore_test`），跑
`python manage.py migrate --check` + `platform_report`，确认可用后删库。**备份没验过等于没有备份。**

---

## 8. 升级流程

```bash
cd <APP_DIR> && source .venv/bin/activate

# 1) 备份（先备份再动，回滚成本最低）
PGPASSWORD='<DB_PASSWORD>' pg_dump -h 127.0.0.1 -U <DB_USER> -d food_platform \
  -Fc -f /var/backups/xunwei/pre_release_$(date +%F_%H%M).dump

# 2) 拉代码
git fetch --all && git checkout <RELEASE_TAG_OR_BRANCH> && git pull --ff-only

# 3) 依赖
pip install -r requirements.txt -r requirements-prod.txt

# 4) 数据库结构
python manage.py migrate
python manage.py migrate --check        # 无待执行迁移时返回 0

# 5) 索引：改了 embedding 后端/维度/切分参数 → 必须全量重建；否则增量即可
python manage.py build_index           # 常规
# python manage.py build_index --force # 换 RAG_EMBED_BACKEND / RAG_EMBED_DIM / RAG_EMBED_MODEL 时

# 6) 静态资源
python manage.py collectstatic --noinput

# 7) 重启 + 验收
sudo systemctl restart xunwei
curl -s http://127.0.0.1:8000/health/ | python3 -m json.tool
python manage.py platform_report --top 5 | head -30
```

回滚：

```bash
git checkout <上一个 tag> && pip install -r requirements.txt -r requirements-prod.txt
sudo systemctl restart xunwei
# 若本次迁移不可逆，用第 7.2 节的 dump 恢复
```

**什么时候必须 `build_index --force`**：变更 `RAG_EMBED_BACKEND`、`RAG_EMBED_DIM`、`RAG_EMBED_MODEL`、
`RAG_CHUNK_SIZE` / `RAG_CHUNK_OVERLAP`，或菜谱正文被批量改动而 `content_version` 没跟着变。
维度不一致时检索层会按「维度熔断」逻辑拒绝加载旧索引（`/health/` 的 `index.ready` 会变 `false`）。

---

## 9. 健康检查与巡检

### 9.1 `/health/` 字段含义

两处可用：`/health/`（页面级，`RAGDjange/views.py`）与 `/api/v1/health/`（REST 级，`food/api.py`，DB 异常时返回 **503**）。

```bash
curl -s http://127.0.0.1:8000/health/ | python3 -m json.tool
```

```json
{
  "status": "ok",
  "database": true,
  "llm": { "enabled": true, "has_key": true, "model": "qwen3:14b",
           "base_url": "http://127.0.0.1:11434/v1", "ready": true, "error": "" },
  "index": { "ready": true, "size": 99, "dim": 1024, "model": "hashing-tfidf-zh",
             "updated_at": "2026-10-25T00:25:07+00:00", "index_dir": "<APP_DIR>/data/index",
             "bm25_docs": 99 }
}
```

| 字段 | 含义 | 异常时怎么办 |
| --- | --- | --- |
| `status` | 顶层结论：`ok` / `degraded`（DB 探测失败即为 `degraded`） | 看 `database` 与 `journalctl -u xunwei` |
| `database` | 是否执行 `SELECT 1` 成功 | 查 PG 是否在跑、连接数、`.env` 账号密码 |
| `llm.enabled` | `LLM_ENABLED` | 想关闭模型时设 0，助手整体降级但不报错 |
| `llm.has_key` | 是否配了 `LLM_API_KEY` | Ollama 本地一般填任意非空值 |
| `llm.model` / `llm.base_url` | 实际生效的模型与网关 | 与 `.env` 比对；`base_url` 为 `(默认)` 说明没配 |
| `llm.ready` | 客户端能否完成初始化/探活 | false 时看 `llm.error`、`ollama serve` 是否在跑 |
| `index.ready` | 索引是否加载成功且非空 | `build_index --stats`，必要时 `--force` |
| `index.size` / `bm25_docs` | 文本块数 / BM25 文档数（两者应相等，当前 99） | 为 0 说明索引没建或目录指向错 |
| `index.dim` | 向量维度（默认 1024） | 与 `RAG_EMBED_DIM` 不一致会导致熔断 |
| `index.model` | 嵌入实现（`hashing-tfidf-zh` / ollama 模型名） | 改了后端必须重建 |
| `index.updated_at` | 索引构建时间 | 早于最近一次菜谱批量更新 → 重建 |
| `index.index_dir` | 索引目录绝对路径 | 确认与本机部署路径一致 |

对外只暴露 `status` 是否 200/503 即可（给 LB 或 Uptime 探针），内网再暴露完整 JSON。

### 9.2 高德 Key 巡检 `amap_check`

```bash
python manage.py amap_check                          # 默认 /v3/ip 探测（几乎不耗配额）
python manage.py amap_check --probe place            # 验证周边搜索（POI）权限，耗配额
python manage.py amap_check --probe regeo --lng 103.0012 --lat 29.9875
```

输出形如：

```
探测 2 把 Web 服务 Key（接口：ip）
  [1] a1b2c3...9f0e  OK 42ms｜四川省雅安市
  [2] 7c8d9e...1a2b  失败 status=0 infocode=10003 访问已超出日访问量（日访问量已超限） 61ms

JS API Key 配对（2 组）
  [1] 3d8f11...0a7c  ↔  安全密钥 ab12cd...9e8f
  [2] 5b6e77...1f3d  ↔  安全密钥 缺失
  警告：存在缺少安全密钥的 JS Key，地图会报 INVALID_USER_SCODE

Web Key 可用 1/2
```

该命令**绕过 Key 池**逐把探测，因此能区分「池在冷却」和「Key 本身失效」。Key 一律脱敏（前 6 位 + 后 4 位）。

运行时池状态（带鉴权）：

```bash
curl -s http://127.0.0.1:8000/api/v1/places/keys/ | python3 -m json.tool   # 匿名只给汇总
# staff 登录态下可看每把 Key 的 uses / successes / failures / cooldown_remaining / avg_latency_ms
curl -X POST -b cookies.txt http://127.0.0.1:8000/api/v1/places/keys/reset/   # staff 清冷却
```

### 9.3 其他巡检项

| 检查 | 命令 |
| --- | --- |
| 迁移是否有未执行的 | `python manage.py migrate --check` |
| Django 配置体检 | `python manage.py check --deploy` |
| 数据规模 / 行为分布 / 推荐覆盖率 | `python manage.py platform_report --top 10` |
| 索引状态 | `python manage.py build_index --stats` |
| 测试基线 | `python -m pytest -q`（当前 217 项 + 4 个 subtest） |

> `check --deploy` 会稳定报 6 条 `security.W*`：`settings.py` 未定义 `SECURE_HSTS_SECONDS` /
> `SECURE_SSL_REDIRECT` / `SESSION_COOKIE_SECURE` / `CSRF_COOKIE_SECURE`，且 `DJANGO_DEBUG` 默认 `1`。
> 本项目 HTTPS 由 nginx 终结（第 4 节），这 6 条属**已知项**；要在 Django 侧消除需改 `settings.py`，不在本次部署范围内。
> 唯一必须处理的是 `DJANGO_DEBUG=0` 与足够长的 `DJANGO_SECRET_KEY`（`W009`/`W018`）。

---

## 10. 排错手册

### 10.1 高德错误码

| infocode | 含义 | 处理 |
| --- | --- | --- |
| `10001` | Key 不正确或过期 | 核对 `AMAP_WEB_KEYS` 是否多空格/换行；控制台确认 Key 未删 |
| `10003` | 日访问量已超限 | 该 Key 进入 6h 冷却（`AMAP_QUOTA_COOLDOWN`）；加 Key、等次日重置，或提高配额 |
| `10005` | IP 白名单校验失败 | 把部署机**出口 IP** 加进白名单；NAT/多网卡环境注意实际出口 |
| `10006` | 域名白名单校验失败 | JS Key 的域名白名单要包含实际访问域名（含端口不写、协议不写） |
| `10009` | Key 权限不足 / 服务未开通 | 控制台确认该 Key 勾选了「Web 服务」且开通周边搜索、逆地理、IP 定位、输入提示 |
| `10012` | 权限不足（未开通此服务） | 同上，逐服务核对 |
| `10021` | QPS 超出限制 | 池内该 Key 短冷却 5s，自动换 Key；持续触发说明并发太高，加 Key 或降频 |
| `10029` | 日调用量超限 | 同 `10003` |
| `10044` / `10045` | 账号维度日调用量 / QPS 超限 | 换账号或申请提额；单账号下加 Key 也不再有效 |

自查顺序：`amap_check` 看单把 Key → `/api/v1/places/keys/` 看池状态 → `logs/app.log` 里
`places.keypool` 的「进入冷却」记录 → `nearby/` 响应里的 `degraded` / `message`。
**降级不是故障**：返回 `degraded=true` + `provider=fixture` 时接口仍是 200，前端展示离线样例。

### 10.2 LLM 超时 / 无响应

现象：`/health/` 的 `llm.ready=false`，或助手首 token 迟迟不来、`done` 事件缺失。

```bash
curl -s http://127.0.0.1:11434/v1/models                 # Ollama 是否活着
ollama ps                                                # 模型是否已加载 / 是否被换出
nvidia-smi                                               # 显存是否被别的进程占满
grep -i "llm\|timeout" <APP_DIR>/logs/app.log | tail -20
```

- `qwen3:14b` 在 8GB 显存上是 CPU/GPU 混合推理，**首 token 数秒到数十秒属正常**，前端用 SSE 缓解；
- 单请求超时由 `LLM_TIMEOUT`（默认 60s）控制；如果经常撞线，调大到 120 并把 nginx `proxy_read_timeout` 一起调大；
- 显存不够时换小模型（`LLM_MODEL=qwen3:8b` 等）比调参更有效；
- 想临时关掉模型：`LLM_ENABLED=0` 后重启，助手退化为检索式回答（HTTP 200，不报 500）；
- `llm.error` 里出现连接类错误时，确认 `LLM_BASE_URL` 必须是 **`/v1` 结尾**的 OpenAI 兼容地址。

### 10.3 PostgreSQL 连接数

现象：`FATAL: sorry, too many clients already`、`/health/` 的 `database=false`、日志里
`connection failed: ... too many clients`。

```bash
sudo -u postgres psql -c "show max_connections;"                       # 默认 100
sudo -u postgres psql -c "select count(*), state from pg_stat_activity group by state;"
sudo -u postgres psql -c "select pid, usename, state, query_start, left(query,60) from pg_stat_activity where datname='food_platform' order by query_start;"
```

本项目连接数是 `3 worker × (每线程最多 1 条常驻连接)`，加 `DB_CONN_MAX_AGE=60` 的连接复用，
正常在 10 条量级。超限的常见原因与处理：

| 原因 | 处理 |
| --- | --- |
| 多开进程（开发机同时跑 `runserver` + gunicorn + 测试） | 先全部停掉；`pytest` 会创建 `test_food_platform` 库，与生产库抢连接 |
| `DB_CONN_MAX_AGE` 过长 + worker 频繁重启 | 降到 `0`（每次请求关连接）或 `60` 以内 |
| 长事务/慢查询占住连接 | `pg_stat_activity` 里 `state='active'` 且 `query_start` 很早的 pid 用 `pg_terminate_backend(pid)` 收掉 |
| 确实需要更多连接 | `postgresql.conf` 提高 `max_connections`，同步 `shared_buffers`，然后 reload |

临时应急：`sudo systemctl restart postgresql` 会清掉所有连接，但会中断正在处理的请求，放最后一步。

### 10.4 静态文件 404

现象：页面能开、样式/JS 全 404（`/static/app.css` → 404）。

```bash
ls <APP_DIR>/staticfiles/app.css            # 1) 收集了吗
sudo -u www-data test -r <APP_DIR>/staticfiles/app.css && echo readable
sudo nginx -T | grep -A3 "location /static/"
```

- 忘了 `python manage.py collectstatic --noinput`；
- nginx `alias` 路径**必须以 `/` 结尾**且指向 `staticfiles/`（不是源码目录 `static/`）；
- 权限：`staticfiles/` 与上级目录都要对 `www-data` 可读可执行（`chmod 755`）；
- `DJANGO_DEBUG=0` 时 Django 不再服务静态文件，`runserver` 正常不代表 nginx 正常；
- 浏览器拿旧缓存：检查模板上的 `?v=` 是否随发布更新（第 5 节 `STATIC_VERSION`）。

### 10.5 其他常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| `DisallowedHost` / 400 | `DJANGO_ALLOWED_HOSTS` 没包含访问用的域名或 IP |
| POST 全部 403 CSRF | `DJANGO_CSRF_TRUSTED_ORIGINS` 要带协议且与实际访问一致（https 项目写 `https://`） |
| 迁移报 `permission denied for schema public` | 第 2.1 节第 3 步没执行（PG 15+ 起 public schema 需显式授权） |
| 语音/地图空白，控制台 `INVALID_USER_SCODE` | JS Key 缺安全密钥：改用 `AMAP_JS_PAIRS` 成对配置 |
| 地图空白但列表正常 | 外网 `webapi.amap.com` 不可达，前端自动降级为列表模式（设计行为） |
| 助手回答里没有引用 | 索引未建或 `index.ready=false`：`build_index --force` |
| `pytest` 全量跑偶发 DB 连接错误 | 同一台机器上并发跑多个 pytest（各自创建 `test_food_platform`）；串行重跑即可 |

---

## 附录 A：Windows 本机（无 systemd / 无 gunicorn）

本仓库当前开发机是 Windows，而 **gunicorn 依赖 `fcntl`，无法在 Windows 原生运行**，systemd 也不存在。
「本机生产」若指 Windows 机器，按下面二选一：

**A1. 生产语义跑在 WSL2 / Linux 虚拟机上**（推荐）：按正文 1~9 节部署，Windows 侧只负责访问。

**A2. 直接在 Windows 上用 WSGI 服务器**（需自行安装，未写入 `requirements.txt`）：

```powershell
pip install waitress
python manage.py migrate
python manage.py collectstatic --noinput
waitress-serve --listen=127.0.0.1:8000 --threads=8 RAGDjange.wsgi:application
```

临时/演示用途也可以直接用 Django 自带服务器（与 gunicorn 共用同一份 `.env`）：

```powershell
python manage.py runserver 0.0.0.0:8000
```

Windows 路径注意：`STATIC_ROOT` / `INDEX_DIR` / `logs/` 都是 `BASE_DIR` 相对路径，无需另行配置；
nginx 换 IIS/其他反代时，`proxy_buffering off` 的等价项要自己确认（SSE 可能被缓冲）。

---

## 附录 B：本文件涉及的配置项（全部来自 `RAGDjange/settings.py`）

| 变量 | 用途 | 默认 |
| --- | --- | --- |
| `DJANGO_DEBUG` | 调试开关（生产 `0`） | `1` |
| `DJANGO_SECRET_KEY` | 会话/CSRF 签名密钥 | 内置不安全默认值（生产必须覆盖） |
| `DJANGO_ALLOWED_HOSTS` | 允许的 Host | `127.0.0.1,localhost,0.0.0.0,[::1]` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | CSRF 信任源（带协议） | `http://127.0.0.1:8000,http://localhost:8000` |
| `TIME_ZONE` | 时区 | `Asia/Shanghai` |
| `LOG_LEVEL` | 日志级别 | `INFO` |
| `DB_ENGINE` | `sqlite` / `postgresql` / `mysql` | `sqlite` |
| `DB_NAME` `DB_USER` `DB_PASSWORD` `DB_HOST` `DB_PORT` | PG 连接 | `food_platform` / `postgres` / 空 / `127.0.0.1` / `5432` |
| `DB_CONN_MAX_AGE` `DB_CONNECT_TIMEOUT` | 连接复用与超时 | `60` / `8` |
| `REDIS_URL` `CACHE_TIMEOUT` | 配了才切 Redis 缓存/会话 | 空 / `300` |
| `STATIC_VERSION` | 静态资源版本号（`?v=`） | 启动时间戳 |
| `LLM_ENABLED` `LLM_BASE_URL` `LLM_MODEL` `LLM_API_KEY` | 模型网关 | `1` / 空 / `qwen3.8-max` / 空 |
| `LLM_TIMEOUT` `LLM_MAX_TOKENS` `LLM_TEMPERATURE` | 调用参数 | `60` / `1500` / `0.7` |
| `RAG_EMBED_BACKEND` `RAG_EMBED_DIM` `RAG_EMBED_MODEL` | 嵌入实现与维度 | `hashing` / `1024` / `hashing-tfidf-zh` |
| `RAG_CHUNK_SIZE` `RAG_CHUNK_OVERLAP` `RAG_TOP_K` | 切分与召回 | `420` / `80` / `5` |
| `RAG_BM25_WEIGHT` `RAG_TITLE_WEIGHT` | 混合检索权重 | `0.5` / `1.0` |
| `AMAP_WEB_KEYS` `AMAP_JS_KEYS` `AMAP_JS_PAIRS` `AMAP_SECURITY_CODE` | Key 池与配对 | 空（降级离线） |
| `AMAP_TIMEOUT` `AMAP_CACHE_TTL` `AMAP_QUOTA_COOLDOWN` | 超时/缓存/冷却 | `8` / `600` / `21600` |
| `AMAP_DEFAULT_RADIUS` `AMAP_MAX_RADIUS` `AMAP_DEFAULT_TYPES` | 搜索半径与品类 | `3000` / `50000` / `050000` |
| `RECOMMEND_*` | 推荐四路权重、MMR λ、热度衰减 | 内容 .45 / CF .30 / 热度 .15 / 新鲜 .10，λ=0.7 |
| `EMAIL_HOST` `EMAIL_PORT` `EMAIL_HOST_USER` `EMAIL_HOST_PASSWORD` `EMAIL_USE_SSL` | SMTP | 空（演示模式回显验证码） |
| `DEMO_VERIFY_CODE` | 演示模式（验证码回显） | 未配 SMTP 时为 `1` |
