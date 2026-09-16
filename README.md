# Meta Service — 元数据存储微服务

> 服务端口：**9093** ｜ 技术栈与工程规范严格对齐 `mservice-fastapi-user`（user-service）

本服务**不存储文件/对象二进制本体**，只存储**结构化元数据**：业务方（forum / shop / game 等，对齐 user-service 的 `service_name` 维度）登记一种"元数据类型"（Metadata Type），然后对该类型下的实体写入/查询描述性元数据（JSON、标签、版本）。目标是可复用的通用元数据存储底座，供多个业务微服务共享。

## 技术栈

| 类别 | 技术 |
|------|------|
| Web 框架 | FastAPI（异步） |
| 数据库 | SQLAlchemy 2.0 (Async) + aiosqlite（SQLite） |
| 数据验证 | Pydantic v2 + pydantic-settings |
| JWT 校验 | python-jose + cryptography（消费 user-service 令牌，RS256） |
| HTTP 客户端 | httpx（拉取 user-service JWKS） |
| 日志 | LogProxy（Repository 层自动脱敏）+ RotatingFileHandler 轮转 |
| 测试 | pytest + pytest-asyncio + httpx |
| 容器化 | Docker + docker-compose |

> 不含 alembic（建表走 `init_db` 自动 `create_all`，对齐参照项目现状）。

## 目录结构

```
meta-service/
├── app/
│   ├── main.py                    # 应用入口（lifespan 初始化数据库）
│   ├── core/
│   │   ├── config.py              # Settings（含 USER_SERVICE_URL / 元数据限额 / superuser 白名单）
│   │   ├── database.py            # SQLite 异步连接 + get_db/init_db
│   │   ├── security.py            # JWKS 获取/缓存 + JWT 校验（消费 user-service 令牌）
│   │   └── dependencies.py        # 认证依赖（get_current_user / require_superuser）
│   ├── models/
│   │   ├── metadata_type.py       # 元数据类型定义表
│   │   └── metadata_entry.py      # 元数据实体表 + 历史版本表
│   ├── schemas/
│   │   ├── type.py                # 类型请求/响应
│   │   └── entry.py               # 实体元数据请求/响应
│   ├── repositories/
│   │   ├── type_repository.py     # 类型 CRUD
│   │   └── entry_repository.py    # 实体元数据 CRUD + 版本 + 复杂查询
│   ├── services/
│   │   ├── type_service.py        # 类型管理 + schema 动态校验
│   │   └── entry_service.py       # 元数据写读查/版本/回滚/可见性隔离
│   ├── api/v1/routes/
│   │   ├── types.py               # 元数据类型路由（管理接口，仅 superuser）
│   │   └── entries.py             # 元数据实体路由（登录用户）
│   ├── proxy/
│   │   └── log_proxy.py           # 操作日志代理（自动脱敏）
│   └── utils/
│       └── logger.py              # 轮转日志
├── tests/
│   ├── conftest.py                # 测试 fixtures（RSA 密钥对、JWT 签发、JWKS mock）
│   └── test_api/
│       ├── test_types.py          # 类型管理测试
│       └── test_entries.py        # 实体元数据测试
├── .env.example
├── requirements.txt
├── requirements-dev.txt
├── Dockerfile
├── docker-compose.yml
└── pytest.ini
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements-dev.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少确认 USER_SERVICE_URL 指向运行中的 user-service
```

### 3. 启动服务

```bash
uvicorn app.main:app --reload --port 9093
```

访问 `http://localhost:9093/docs` 查看交互式 API 文档。

### 4. 健康检查

```bash
curl http://localhost:9093/health
# {"status":"healthy","service":"Meta Service","version":"1.0.0"}
```

## 数据模型

### MetadataType（元数据类型）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| type_name | String(100) | 类型名（按 service_name 唯一） |
| service_name | String(50) | 所属业务名 |
| description | String(500) | 描述 |
| schema_json | JSON | 字段 schema 定义 |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |
| deleted_at | DateTime | 删除时间（软删除） |
| is_deleted | Integer | 是否删除（0/1） |

唯一约束：`(type_name, service_name)` 仅对未删除行生效（SQLite 部分唯一索引）。

### MetadataEntry（元数据实体）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| type_id | Integer | 关联 MetadataType.id |
| entity_key | String(255) | 实体唯一标识 |
| data | JSON | 元数据内容（按类型 schema 校验） |
| tags | JSON | 标签数组 |
| version | Integer | 当前版本号（默认 1） |
| service_name | String(50) | 所属业务名（资源隔离） |
| owner_user_id | Integer | 所属用户 ID（资源隔离） |
| created_at | DateTime | 创建时间 |
| updated_at | DateTime | 更新时间 |
| deleted_at | DateTime | 删除时间（软删除） |
| is_deleted | Integer | 是否删除（0/1） |

唯一约束：`(type_id, entity_key)` 仅对未删除行生效。

### MetadataVersion（历史版本）

| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer | 主键 |
| entry_id | Integer | 关联 MetadataEntry.id |
| version | Integer | 版本号 |
| data | JSON | 该版本的元数据 |
| tags | JSON | 该版本的标签 |
| created_at | DateTime | 创建时间 |
| created_by_user_id | Integer | 创建者用户 ID |

## API 端点

前缀 `/api/v1`，服务端口 9093。除 `/health` 和 `/` 外全部需认证。

| 方法 | 端点 | 功能 | 权限 |
|------|------|------|------|
| POST | `/types` | 创建元数据类型 | **仅 superuser** |
| GET | `/types` | 类型列表（service_name 筛选、分页） | 登录用户 |
| GET | `/types/{type_name}` | 获取类型详情（含 schema） | 登录用户 |
| PUT | `/types/{type_name}` | 更新类型 schema（无实体数据时可删除字段，否则仅允许新增字段） | **仅 superuser** |
| DELETE | `/types/{type_name}` | 软删除类型（有实体数据时拒绝） | **仅 superuser** |
| POST | `/entries` | 创建实体元数据 | 登录用户 |
| GET | `/entries/{type_name}/{entity_key}` | 获取元数据（支持 `?version=`） | 登录用户 |
| PUT | `/entries/{type_name}/{entity_key}` | 更新元数据（deep merge，版本自增） | 登录用户 |
| DELETE | `/entries/{type_name}/{entity_key}` | 软删除元数据 | 登录用户 |
| GET | `/entries` | 查询（字段过滤 + tags + 分页 + 排序，`service_name` 跨服务筛选仅 superuser） | 登录用户 |
| POST | `/entries/batch` | 批量查询（按 `keys` 列表返回 `{key: obj}`，未找到的 key 为 `null`） | 登录用户 |
| GET | `/entries/{type_name}/{entity_key}/versions` | 版本历史列表 | 登录用户 |
| POST | `/entries/{type_name}/{entity_key}/rollback` | 回滚到指定版本 | 登录用户 |
| GET | `/health` | 健康检查 | 公开 |
| GET | `/` | 服务信息 | 公开 |

## 认证与权限

### JWT 校验（消费 user-service 令牌）

本服务**不签发令牌**，只校验 user-service 签发的 JWT：

1. 读取 JWT Header 中的 `kid`；
2. 本地 JWKS 缓存命中则用对应公钥验证（RS256）；
3. 未命中或缓存过期则从 `http://user-service:8000/.well-known/jwks.json` 强制刷新后再验证；
4. 兼容 user-service 的密钥轮换（多 kid）。

JWKS 缓存带 TTL（`JWKS_CACHE_TTL_SECONDS`，默认 3600 秒）。

### superuser 判定（管理接口）—— 三重 AND 校验，缺一不可

管理接口（`POST/PUT/DELETE /types`）要求以下三项**同时成立**：

1. JWT payload 的 `role == "superuser"`；
2. `sub`（用户名）在配置白名单 `SUPERUSER_USERNAMES` 中；
3. `user_id` 在配置白名单 `SUPERUSER_USER_IDS` 中。

**无兜底**：任意一项不满足即返回 **403**。即使攻击者注册了用户名为 `superuser` 的账号，只要 `user_id` 不匹配或 `role` 不是 `superuser`，就无法提权。

部署时需将 `SUPERUSER_USER_IDS` 配置为 user-service 中实际 superuser 的用户 ID（user-service 部署引导自动创建的 superuser 通常为 ID=1）。

普通业务 token 只读 `/types`、可写 `/entries`。

### 统一权限 Scope（所有 Meta 操作共用）

所有 Meta 操作（类型与实体的 list / get / create / update / delete 等）统一经 `MetaScope` 做权限判定，权限判断逻辑收敛在 `app/core/dependencies.py`，不在各端点散落：

| Scope | 身份 | 可操作范围 |
|-------|------|-----------|
| `global` | superuser（role + 用户名 + user_id 三重校验通过） | **任意 service、任意 owner**：跨服务/跨用户创建/查询/访问均可 |
| `service` | 普通 service 身份 | **仅自身 service 且自身 owner 的 entry**：显式跨 service 一律 **403**；同 service 跨 owner 单实体 **403**、列表/批量查询不可见 |

判定入口（`MetaScope` 方法）：

- `require_global(action)`：管理操作（类型 create/update/delete），仅 `global` 允许，否则 403；
- `resolve_target(requested, action)`：单实体 / 创建操作的目标 service——未指定默认自身，显式跨 service 仅 `global` 允许；
- `resolve_filter(requested, action)`：列表 / 查询的 service 过滤——普通身份强制自身 service，`global` 可全量或按指定；
- `resolve_owner_filter()`：列表 / 批量查询的 owner 过滤——`global` 返回 `None`（不限 owner），普通身份强制返回当前 `user_id`（仅可见自身 owner 的 entry）；
- `check_entry_access(entry, action)`：单实体访问——`global` 放行；普通身份需同时满足 `service_name` 一致 **且** `owner_user_id` 一致，否则 **403**。

**实体（entries）owner 级隔离规则：**

- 普通身份（`service` scope）对 entry 的所有读取/修改/删除/版本/回滚/批量/列表查询，数据必须同时满足 `owner_user_id == 当前登录用户 user_id` 且 `service_name == 当前身份所属 service`；
- 单实体越权访问（跨 owner 或跨 service）→ **403**；列表/批量查询中越权数据不可见（返回空 / `null`，不泄露存在性）；
- superuser（`global` scope）不受 owner 限制，保持跨 service、跨用户能力；
- 创建时 `owner_user_id` 始终为当前创建者（从 JWT `user_id` 提取）；
- **类型（types）接口保持 service 级共享**，不引入 owner 隔离——类型是 service 共享的 schema 定义，普通用户可读本 service 的类型。

写入时从 JWT 提取 `user_id` 与 `service_name` 作为归属；superuser 可通过创建请求体或查询参数 `service_name` 指定其他 service。

## 核心功能说明

### 动态 Schema 校验

类型创建时定义字段 schema（`{"fields": {"字段名": {"type": "...", "required": bool, "indexed": bool, "default": any}}}`），写入实体时运行时用 Pydantic `create_model` 动态构造校验模型，**禁止硬编码业务字段**。

支持的字段类型：

| 类型 | 说明 | 可配子定义 |
|------|------|------------|
| `string` / `integer` / `number` / `boolean` | 基础类型 | 无 |
| `list` | 数组 | `items`：元素类型定义（可嵌套复合类型，如 `{"type":"list","items":{"type":"object","fields":{...}}}`） |
| `dict` | 键值映射 | `values`：值类型定义（可嵌套复合类型） |
| `object` | 结构化嵌套对象 | `fields`：子字段定义（递归结构，可多层嵌套） |

子定义格式与字段定义一致，未提供时 `list`/`dict`/`object` 按任意结构校验。

- 未知字段 / 类型错误 / 超长返回 **422**；
- 限制 `data` 字段数（`MAX_ENTRY_DATA_KEYS`）和嵌套深度（`MAX_ENTRY_DATA_DEPTH`），防深递归 / ReDoS。

### Schema 更新规则（向后兼容）

更新类型 schema 时，**类型下无实体数据时允许移除字段；存在实体数据时仅允许新增字段**：
- 移除已有字段：类型下**无实体数据** → 允许；存在实体数据 → **422**；
- 修改已有字段类型 → **422**（无论是否有实体数据）；
- 新增可选/必填字段 → 允许；
- 复合类型同样受限：修改 `list.items` / `dict.values` 的子类型 → **422**；移除 `object` 已有子字段 → 无实体数据时允许，存在实体数据时 **422**；`object` 内新增子字段 → 允许。

### 版本管理与回滚

- 每次更新（PUT）自动保存旧版本到 `MetadataVersion` 表，版本号自增；
- 支持 `?version=N` 读取历史版本；
- 回滚（rollback）生成**新版本**，数据取回滚目标，不删除中间版本；
- 历史版本保留上限可配置（`MAX_VERSION_KEPT`，默认 50），超过自动清理最旧版本。

### 软删除与唯一性

- 类型和实体均采用软删除（`is_deleted` + `deleted_at`）；
- 删除后同名 `entity_key` 可重建（部分唯一索引仅约束未删除行）；
- 已有实体数据的类型禁止删除（返回 **409**）；
- 查询统一过滤 `is_deleted=False`。

### 查询与检索

- 按类型 + 字段值等于过滤（任意 query param，通过 SQLite `json_extract`）；
- tags 交集过滤（`?tags=tag1,tag2`，通过 SQLite `json_each` EXISTS 子查询）；
- 创建时间范围（`created_after` / `created_before`）；
- 分页返回 `{"total": ..., "items": [...]}`；
- 排序（`sort_by` + `sort_order`）；
- 仅返回当前用户可见数据（service + owner 双重隔离）。

### 日志脱敏

Repository 层统一 LogProxy 包裹，`data` 中敏感 key（password/secret/token 等，`SENSITIVE_FIELDS`）自动脱敏为 `******`，邮箱/手机号部分脱敏。

## 配置项说明

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `APP_NAME` | Meta Service | 应用名 |
| `APP_VERSION` | 1.0.0 | 版本号 |
| `APP_ENV` | development | 运行环境 |
| `HOST` | 0.0.0.0 | 监听地址 |
| `PORT` | 9093 | 服务端口 |
| `DATABASE_URL` | sqlite+aiosqlite:///./data/meta_service.db | 数据库连接 |
| `DATABASE_ECHO` | False | 是否输出 SQL 日志 |
| `USER_SERVICE_URL` | http://localhost:8000 | user-service 地址（拉取 JWKS） |
| `JWKS_CACHE_TTL_SECONDS` | 3600 | JWKS 缓存 TTL（秒） |
| `ALGORITHM` | RS256 | JWT 签名算法 |
| `SUPERUSER_USERNAMES` | ["superuser"] | superuser 用户名白名单（三重 AND 校验之一） |
| `SUPERUSER_USER_IDS` | [1] | superuser 用户 ID 白名单（三重 AND 校验之一，部署时改为实际 ID） |
| `MAX_ENTRY_DATA_KEYS` | 100 | 单条 entry data 最大字段数 |
| `MAX_ENTRY_DATA_DEPTH` | 5 | data 嵌套最大深度 |
| `MAX_TAGS_PER_ENTRY` | 20 | 单条 entry 最多 tags 数 |
| `MAX_TAG_LENGTH` | 50 | 单个 tag 最大长度 |
| `MAX_VERSION_KEPT` | 50 | 保留历史版本上限 |
| `MAX_BATCH_KEYS` | 200 | 批量查询单次 keys 数量上限 |
| `ENTRY_KEY_MAX_LENGTH` | 255 | entity_key 最大长度 |
| `LOG_LEVEL` | INFO | 日志级别 |
| `LOG_FILE` | logs/meta_service.log | 日志文件路径 |
| `LOG_MAX_BYTES` | 10485760 | 单日志文件最大字节 |
| `LOG_BACKUP_COUNT` | 5 | 日志轮转保留份数 |
| `ALLOWED_ORIGINS` | ["http://localhost:3000","http://localhost:9093"] | CORS 允许来源 |

## Docker 部署

### 构建并启动

```bash
docker-compose up -d --build
```

### docker-compose 配置说明

- 服务名：`meta-service`，容器名：`meta-service`
- 端口映射：`9093:9093`
- 数据卷：`./data:/app/data`（SQLite 持久化）、`./logs:/app/logs`
- 健康检查：探测 `/health`，每 30 秒一次，超时 10 秒，重试 3 次
- `USER_SERVICE_URL` 默认指向 Docker 同网络内的 `http://user-service:8000`，若 user-service 在外部需修改

### Dockerfile 特点

- 基于 `python:3.12-slim`
- 使用中科大 pip 源（`https://mirrors.ustc.edu.cn/pypi/simple/`）
- 仅安装生产依赖（测试/开发依赖不进入镜像）
- 无需 gcc 等编译工具（所有依赖均有 manylinux 预编译 wheel）

## 测试

```bash
pytest tests/ -v
```

测试覆盖：
- 类型 CRUD 与 schema 变更规则（新增字段允许、移除/改类型拒绝 422）
- 元数据 CRUD、schema 校验 422（缺必填字段、类型错误）
- 版本自增、历史版本读取、版本历史列表、回滚
- tags 过滤查询、字段过滤查询、分页
- 越权 403（跨 service / 同 service 跨 owner）、owner 级隔离（列表/批量不泄露存在性）
- superuser 跨 service / 跨 owner 访问
- superuser 权限（普通 token 写 /types 403、superuser token 可管理）
- 无 token / 伪造 token 拒绝（401）
- 重复创建冲突（409）、有实体数据的类型禁止删除（409）

测试通过 mock JWKS 拉取、使用测试 RSA 密钥对签发 JWT，无需真实运行 user-service。

## 与 user-service 的 JWT/role 对接说明

1. **user-service 部署**：按 `.env` 配置 `SUPERUSER_USERNAME` / `SUPERUSER_PASSWORD`，启动时自动创建 `role=superuser` 的超级用户；
2. **获取 token**：superuser 或普通用户通过 user-service 的 `POST /api/v1/auth/login` 获取 JWT，payload 携带 `sub / user_id / service_name / role / type`；
3. **本服务校验**：请求头携带 `Authorization: Bearer <token>`，本服务从 user-service 拉取 JWKS 公钥验证签名（RS256），兼容密钥轮换；
4. **权限判定**：管理接口需三重 AND 校验（`role=superuser` + `username` 白名单匹配 + `user_id` 白名单匹配）；所有 Meta 操作统一经 `MetaScope` 判定——superuser 拥有 `global` scope（可操作任意 service），普通 service 身份仅有 `service` scope（仅可操作自身 service）。

## 使用示例

### 1. 创建元数据类型（仅 superuser）

```bash
curl -X POST http://localhost:9093/api/v1/types \
  -H "Authorization: Bearer <superuser_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "type_name": "forum_post",
    "service_name": "forum",
    "description": "论坛帖子元数据",
    "schema_json": {
      "fields": {
        "title":     {"type": "string",  "required": true,  "indexed": true},
        "board":     {"type": "string",  "required": true,  "indexed": true},
        "likes":     {"type": "integer", "required": false, "indexed": true},
        "is_pinned": {"type": "boolean", "required": false, "indexed": false}
      }
    }
  }'
```

### 2. 写入实体元数据（登录用户）

```bash
curl -X POST http://localhost:9093/api/v1/entries \
  -H "Authorization: Bearer <normal_user_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "type_name": "forum_post",
    "entity_key": "post-1001",
    "data": {"title": "如何配置 JWT 密钥轮换", "board": "技术", "likes": 128},
    "tags": ["fastapi", "jwt", "教程"]
  }'
```

### 2.1 复合类型示例（list / dict / object）

```bash
curl -X POST http://localhost:9093/api/v1/types \
  -H "Authorization: Bearer <superuser_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "type_name": "article",
    "service_name": "forum",
    "schema_json": {
      "fields": {
        "title":    {"type": "string", "required": true},
        "tags":     {"type": "list", "items": {"type": "string"}},
        "metadata": {"type": "dict", "values": {"type": "string"}},
        "author":   {"type": "object", "fields": {
          "name": {"type": "string", "required": true},
          "age":  {"type": "integer"}
        }}
      }
    }
  }'
```

写入对应实体时 `data` 需符合复合结构，例如：

```json
{
  "title": "复合类型演示",
  "tags": ["python", "fastapi"],
  "metadata": {"level": "beginner"},
  "author": {"name": "alice", "age": 25}
}
```

### 3. 查询元数据

```bash
curl "http://localhost:9093/api/v1/entries?type_name=forum_post&board=技术&tags=fastapi&page=1&page_size=20" \
  -H "Authorization: Bearer <normal_user_token>"
```

### 4. 更新元数据（版本自增）

```bash
curl -X PUT http://localhost:9093/api/v1/entries/forum_post/post-1001 \
  -H "Authorization: Bearer <normal_user_token>" \
  -H "Content-Type: application/json" \
  -d '{"data": {"likes": 256, "is_pinned": false}, "tags": ["fastapi", "jwt", "热门"]}'
```

### 5. 回滚到指定版本

```bash
curl -X POST http://localhost:9093/api/v1/entries/forum_post/post-1001/rollback \
  -H "Authorization: Bearer <normal_user_token>" \
  -H "Content-Type: application/json" \
  -d '{"version": 1}'
```

### 6. 批量查询（按 entity_key 列表）

```bash
curl -X POST http://localhost:9093/api/v1/entries/batch \
  -H "Authorization: Bearer <normal_user_token>" \
  -H "Content-Type: application/json" \
  -d '{
    "type_name": "forum_post",
    "keys": ["post-1001", "post-1002", "post-9999"]
  }'
# 返回 {"post-1001": {...obj...}, "post-1002": {...obj...}, "post-9999": null}
```

- 未找到 / 已软删 / 无权限访问（跨 service 或跨 owner）的 key 返回 `null`（不报错）；
- `keys` 数量上限 `MAX_BATCH_KEYS`（默认 200），超限返回 422；
- 统一 Scope：普通身份仅查自身 service 且自身 owner 的 entry，superuser 可在请求体指定 `service_name` 跨服务查询（不受 owner 限制）。
