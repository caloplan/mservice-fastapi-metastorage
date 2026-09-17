"""元数据实体 API 集成测试。"""

import pytest

from tests.conftest import create_test_token

FORUM_SCHEMA = {
    "fields": {
        "title": {"type": "string", "required": True, "indexed": True},
        "board": {"type": "string", "required": True, "indexed": True},
        "likes": {"type": "integer", "required": False, "indexed": True},
        "is_pinned": {"type": "boolean", "required": False, "indexed": False},
    }
}


def _superuser_headers():
    return {"Authorization": f"Bearer {create_test_token(user_id=999, username='superuser', role='superuser')}"}


def _user_headers(user_id: int = 1, username: str = "alice", service_name: str = "forum"):
    return {
        "Authorization": f"Bearer {create_test_token(user_id=user_id, username=username, service_name=service_name, role='user')}"
    }


async def _create_type(client):
    """辅助：创建 forum_post 类型。"""
    return await client.post(
        "/api/v1/types",
        headers=_superuser_headers(),
        json={
            "type_name": "forum_post",
            "service_name": "forum",
            "description": "论坛帖子",
            "schema_json": FORUM_SCHEMA,
        },
    )


# ── 创建 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_entry_success(client):
    """测试创建实体元数据成功。"""
    await _create_type(client)
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "测试帖子", "board": "技术", "likes": 10, "is_pinned": True},
            "tags": ["fastapi", "jwt"],
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["entity_key"] == "post-001"
    assert data["type_name"] == "forum_post"
    assert data["version"] == 1
    assert data["data"]["title"] == "测试帖子"
    assert data["data"]["likes"] == 10
    assert data["tags"] == ["fastapi", "jwt"]
    assert data["owner_user_id"] == 1
    assert data["service_name"] == "forum"


@pytest.mark.asyncio
async def test_create_entry_missing_required_field_422(client):
    """测试创建时缺少必填字段返回 422。"""
    await _create_type(client)
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"board": "技术"},  # 缺少 title
            "tags": [],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_entry_wrong_field_type_422(client):
    """测试创建时字段类型错误返回 422。"""
    await _create_type(client)
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "测试", "board": "技术", "likes": "不是数字"},  # likes 应为 integer
            "tags": [],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_entry_duplicate_409(client):
    """测试重复创建同 entity_key 返回 409。"""
    await _create_type(client)
    for _ in range(2):
        response = await client.post(
            "/api/v1/entries",
            headers=_user_headers(),
            json={
                "type_name": "forum_post",
                "entity_key": "post-001",
                "data": {"title": "测试", "board": "技术"},
                "tags": [],
            },
        )
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_create_entry_type_not_found_404(client):
    """测试创建时类型不存在返回 404。"""
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "nonexistent",
            "entity_key": "post-001",
            "data": {"title": "测试"},
            "tags": [],
        },
    )
    assert response.status_code == 404


# ── 获取 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_entry_success(client):
    """测试获取实体元数据。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "测试帖子", "board": "技术", "likes": 10},
            "tags": ["tag1"],
        },
    )
    response = await client.get("/api/v1/entries/forum_post/post-001", headers=_user_headers())
    assert response.status_code == 200
    assert response.json()["entity_key"] == "post-001"
    assert response.json()["data"]["title"] == "测试帖子"


@pytest.mark.asyncio
async def test_get_entry_not_found(client):
    """测试获取不存在的实体返回 404。"""
    await _create_type(client)
    response = await client.get("/api/v1/entries/forum_post/nonexistent", headers=_user_headers())
    assert response.status_code == 404


# ── 更新与版本 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_entry_version_increment(client):
    """测试更新实体后版本自增。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "原标题", "board": "技术", "likes": 10},
            "tags": ["old"],
        },
    )
    response = await client.put(
        "/api/v1/entries/forum_post/post-001",
        headers=_user_headers(),
        json={"data": {"likes": 25, "is_pinned": True}, "tags": ["new"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 2
    # deep merge：title 保留，likes 更新，新增 is_pinned
    assert data["data"]["title"] == "原标题"
    assert data["data"]["likes"] == 25
    assert data["data"]["is_pinned"] is True
    assert data["tags"] == ["new"]


@pytest.mark.asyncio
async def test_update_entry_null_deletes_field(client):
    """update 中值为 null 的字段表示删除（deep merge 支持嵌套字段删除）。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-002",
            "data": {"title": "标题", "board": "技术", "likes": 10, "is_pinned": True},
            "tags": [],
        },
    )
    response = await client.put(
        "/api/v1/entries/forum_post/post-002",
        headers=_user_headers(),
        json={"data": {"likes": None}},
    )
    assert response.status_code == 200
    data = response.json()
    # likes 被删除，其余字段保留
    assert "likes" not in data["data"]
    assert data["data"]["title"] == "标题"
    assert data["data"]["board"] == "技术"
    assert data["data"]["is_pinned"] is True


@pytest.mark.asyncio
async def test_get_entry_historical_version(client):
    """测试获取历史版本数据。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "v1标题", "board": "技术", "likes": 10},
            "tags": [],
        },
    )
    await client.put(
        "/api/v1/entries/forum_post/post-001",
        headers=_user_headers(),
        json={"data": {"title": "v2标题", "likes": 20}},
    )
    # 获取版本 1
    response = await client.get(
        "/api/v1/entries/forum_post/post-001?version=1", headers=_user_headers()
    )
    assert response.status_code == 200
    assert response.json()["version"] == 1
    assert response.json()["data"]["title"] == "v1标题"
    assert response.json()["data"]["likes"] == 10


@pytest.mark.asyncio
async def test_version_history_list(client):
    """测试版本历史列表。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "v1", "board": "技术"},
            "tags": [],
        },
    )
    for i in range(2):
        await client.put(
            "/api/v1/entries/forum_post/post-001",
            headers=_user_headers(),
            json={"data": {"title": f"v{i+2}"}},
        )
    response = await client.get(
        "/api/v1/entries/forum_post/post-001/versions", headers=_user_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2  # v1 和 v2 被保存为历史版本（v3 是当前）
    versions = [v["version"] for v in data["items"]]
    assert 1 in versions
    assert 2 in versions


@pytest.mark.asyncio
async def test_rollback_to_version(client):
    """测试回滚到指定版本。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "原始标题", "board": "技术", "likes": 10},
            "tags": ["original"],
        },
    )
    await client.put(
        "/api/v1/entries/forum_post/post-001",
        headers=_user_headers(),
        json={"data": {"title": "修改后标题", "likes": 99}, "tags": ["modified"]},
    )
    # 回滚到版本 1
    response = await client.post(
        "/api/v1/entries/forum_post/post-001/rollback",
        headers=_user_headers(),
        json={"version": 1},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["version"] == 3  # 回滚生成新版本
    assert data["data"]["title"] == "原始标题"
    assert data["data"]["likes"] == 10
    assert data["tags"] == ["original"]


# ── 删除 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_entry_soft(client):
    """测试软删除实体。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "测试", "board": "技术"},
            "tags": [],
        },
    )
    response = await client.delete("/api/v1/entries/forum_post/post-001", headers=_user_headers())
    assert response.status_code == 204
    # 删除后查询不到
    get_resp = await client.get("/api/v1/entries/forum_post/post-001", headers=_user_headers())
    assert get_resp.status_code == 404


# ── 查询 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_query_entries_pagination(client):
    """测试查询分页。"""
    await _create_type(client)
    for i in range(5):
        await client.post(
            "/api/v1/entries",
            headers=_user_headers(),
            json={
                "type_name": "forum_post",
                "entity_key": f"post-{i:03d}",
                "data": {"title": f"帖子{i}", "board": "技术", "likes": i * 10},
                "tags": ["common"],
            },
        )
    response = await client.get(
        "/api/v1/entries?type_name=forum_post&page=1&page_size=2", headers=_user_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 5
    assert len(data["items"]) == 2


@pytest.mark.asyncio
async def test_query_entries_by_field_filter(client):
    """测试按字段值过滤查询。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-tech",
            "data": {"title": "技术帖", "board": "技术", "likes": 100},
            "tags": [],
        },
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-life",
            "data": {"title": "生活帖", "board": "生活", "likes": 5},
            "tags": [],
        },
    )
    # 按 board=技术 过滤
    response = await client.get(
        "/api/v1/entries?type_name=forum_post&board=%E6%8A%80%E6%9C%AF", headers=_user_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["entity_key"] == "post-tech"


@pytest.mark.asyncio
async def test_query_entries_by_tags(client):
    """测试按 tags 交集过滤查询。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-a",
            "data": {"title": "A", "board": "技术"},
            "tags": ["fastapi", "python"],
        },
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-b",
            "data": {"title": "B", "board": "技术"},
            "tags": ["fastapi", "jwt"],
        },
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "forum_post",
            "entity_key": "post-c",
            "data": {"title": "C", "board": "技术"},
            "tags": ["java"],
        },
    )
    # 查同时包含 fastapi 和 python 的
    response = await client.get(
        "/api/v1/entries?type_name=forum_post&tags=fastapi,python", headers=_user_headers()
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["entity_key"] == "post-a"


# ── 权限与可见性 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_entry_visibility_other_user_forbidden(client):
    """测试非同 service_name 且非 owner 的用户访问返回 403。"""
    await _create_type(client)
    # alice (forum) 创建实体
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "alice的帖子", "board": "技术"},
            "tags": [],
        },
    )
    # bob (shop) 尝试访问
    response = await client.get(
        "/api/v1/entries/forum_post/post-001",
        headers=_user_headers(user_id=2, username="bob", service_name="shop"),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_entry_visibility_same_service_other_owner_forbidden(client):
    """测试同 service 但不同 owner 的用户访问返回 403（owner 级隔离）。"""
    await _create_type(client)
    # alice (forum, user_id=1) 创建实体
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "alice的帖子", "board": "技术"},
            "tags": [],
        },
    )
    # charlie (forum, user_id=3) 同 service 但非 owner，访问返回 403
    response = await client.get(
        "/api/v1/entries/forum_post/post-001",
        headers=_user_headers(user_id=3, username="charlie", service_name="forum"),
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_query_visibility_isolation(client):
    """测试列表查询仅返回当前用户可见的数据。"""
    await _create_type(client)
    # alice (forum) 创建
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "forum-post",
            "data": {"title": "论坛帖", "board": "技术"},
            "tags": [],
        },
    )
    # bob (shop) 创建（但类型是 forum 的，service_name 不同）
    # 注意：bob 的 service_name 是 shop，但类型 forum_post 属于 forum
    # bob 无法创建 forum_post 类型的实体，因为 get_type_by_name 用 current_user.service_name 查
    # 所以这里只验证 alice 能看到自己的
    response = await client.get(
        "/api/v1/entries?type_name=forum_post",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
    )
    assert response.status_code == 200
    assert response.json()["total"] == 1


@pytest.mark.asyncio
async def test_no_token_unauthorized(client):
    """测试无 token 访问 entries 返回 401。"""
    response = await client.get("/api/v1/entries")
    assert response.status_code == 401


# ── 复合类型字段校验 ──────────────────────────────────────

COMPOSITE_SCHEMA = {
    "fields": {
        "title": {"type": "string", "required": True},
        "tags": {"type": "list", "items": {"type": "string"}},
        "metadata": {"type": "dict", "values": {"type": "string"}},
        "author": {
            "type": "object",
            "fields": {
                "name": {"type": "string", "required": True},
                "age": {"type": "integer"},
            },
        },
    }
}


async def _create_composite_type(client):
    return await client.post(
        "/api/v1/types",
        headers=_superuser_headers(),
        json={
            "type_name": "article",
            "service_name": "forum",
            "schema_json": COMPOSITE_SCHEMA,
        },
    )


@pytest.mark.asyncio
async def test_create_entry_with_composite_fields_success(client):
    """测试写入含 list/dict/object 复合字段的元数据成功。"""
    await _create_composite_type(client)
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "article",
            "entity_key": "art-001",
            "data": {
                "title": "复合类型测试",
                "tags": ["python", "fastapi"],
                "metadata": {"level": "beginner"},
                "author": {"name": "alice", "age": 25},
            },
            "tags": [],
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["data"]["tags"] == ["python", "fastapi"]
    assert data["data"]["metadata"] == {"level": "beginner"}
    assert data["data"]["author"] == {"name": "alice", "age": 25}


@pytest.mark.asyncio
async def test_create_entry_composite_wrong_type_422(client):
    """测试复合字段类型错误返回 422。"""
    await _create_composite_type(client)
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "article",
            "entity_key": "art-002",
            "data": {
                "title": "错误类型",
                "tags": ["ok", 123],  # list items 应为 string
                "author": {"name": "alice", "age": "not-an-int"},  # age 应为 integer
            },
            "tags": [],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_create_entry_object_missing_required_subfield_422(client):
    """测试 object 缺少必填子字段返回 422。"""
    await _create_composite_type(client)
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={
            "type_name": "article",
            "entity_key": "art-003",
            "data": {"title": "缺子字段", "author": {"age": 20}},  # 缺 author.name
            "tags": [],
        },
    )
    assert response.status_code == 422


# ── superuser 跨服务访问 ──────────────────────────────────


def _superuser_headers_for(service_name: str = "default"):
    return {
        "Authorization": f"Bearer {create_test_token(user_id=999, username='superuser', role='superuser', service_name=service_name)}"
    }


@pytest.mark.asyncio
async def test_superuser_cross_service_get_entry_ok(client):
    """测试 superuser 可跨服务获取实体。"""
    await _create_type(client)
    # alice (forum) 创建实体
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "alice的帖子", "board": "技术"},
            "tags": [],
        },
    )
    # superuser 属于 default 服务，但可访问 forum 的实体
    response = await client.get(
        "/api/v1/entries/forum_post/post-001",
        headers=_superuser_headers_for("default"),
    )
    assert response.status_code == 200
    assert response.json()["data"]["title"] == "alice的帖子"


@pytest.mark.asyncio
async def test_superuser_cross_service_update_entry_ok(client):
    """测试 superuser 可跨服务更新实体。"""
    await _create_type(client)
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-001",
            "data": {"title": "原文", "board": "技术", "likes": 1},
            "tags": [],
        },
    )
    response = await client.put(
        "/api/v1/entries/forum_post/post-001",
        headers=_superuser_headers_for("default"),
        json={"data": {"likes": 999}},
    )
    assert response.status_code == 200
    assert response.json()["data"]["likes"] == 999


@pytest.mark.asyncio
async def test_superuser_query_all_services(client):
    """测试 superuser 查询返回所有 service 的实体。"""
    # forum 与 shop 两个服务的类型 + 实体
    await _create_type(client)  # forum_post / forum
    await client.post(
        "/api/v1/types",
        headers=_superuser_headers(),
        json={
            "type_name": "shop_item",
            "service_name": "shop",
            "schema_json": FORUM_SCHEMA,
        },
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={"type_name": "forum_post", "entity_key": "post-a", "data": {"title": "A", "board": "技术"}, "tags": []},
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=2, username="bob", service_name="shop"),
        json={"type_name": "shop_item", "entity_key": "item-a", "data": {"title": "B", "board": "技术"}, "tags": []},
    )
    response = await client.get("/api/v1/entries", headers=_superuser_headers_for("default"))
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 2
    assert {item["entity_key"] for item in data["items"]} == {"post-a", "item-a"}


@pytest.mark.asyncio
async def test_superuser_query_filter_by_service(client):
    """测试 superuser 通过 service_name 筛选跨服务查询。"""
    await _create_type(client)  # forum_post / forum
    await client.post(
        "/api/v1/types",
        headers=_superuser_headers(),
        json={
            "type_name": "shop_item",
            "service_name": "shop",
            "schema_json": FORUM_SCHEMA,
        },
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={"type_name": "forum_post", "entity_key": "post-a", "data": {"title": "A", "board": "技术"}, "tags": []},
    )
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=2, username="bob", service_name="shop"),
        json={"type_name": "shop_item", "entity_key": "item-a", "data": {"title": "B", "board": "技术"}, "tags": []},
    )
    response = await client.get(
        "/api/v1/entries?service_name=forum", headers=_superuser_headers_for("default")
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] == 1
    assert data["items"][0]["entity_key"] == "post-a"


@pytest.mark.asyncio
async def test_normal_user_query_cross_service_forbidden(client):
    """测试普通用户查询显式跨 service 筛选返回 403。"""
    await _create_type(client)
    response = await client.get(
        "/api/v1/entries?service_name=shop",
        headers=_user_headers(service_name="forum"),
    )
    assert response.status_code == 403
    assert "仅超级用户可跨服务" in response.json()["detail"]


# ── 创建操作的统一 Scope 判定 ──────────────────────────────


@pytest.mark.asyncio
async def test_create_entry_superuser_cross_service_via_body(client):
    """测试 superuser（GLOBAL scope）通过请求体 service_name 跨服务创建实体。"""
    await _create_type(client)  # forum_post / forum
    response = await client.post(
        "/api/v1/entries",
        headers=_superuser_headers_for("default"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-super",
            "data": {"title": "super创建的帖子", "board": "技术", "likes": 1},
            "tags": [],
            "service_name": "forum",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert data["service_name"] == "forum"
    assert data["data"]["title"] == "super创建的帖子"


@pytest.mark.asyncio
async def test_create_entry_normal_user_cross_service_forbidden(client):
    """测试普通用户请求体指定其他 service 创建返回 403（统一 Scope）。"""
    await _create_type(client)  # forum_post / forum
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-x",
            "data": {"title": "越权", "board": "技术"},
            "tags": [],
            "service_name": "shop",
        },
    )
    assert response.status_code == 403
    assert "仅超级用户可跨服务" in response.json()["detail"]


@pytest.mark.asyncio
async def test_create_entry_normal_user_own_service_explicit_ok(client):
    """测试普通用户请求体显式指定自身 service 创建成功。"""
    await _create_type(client)  # forum_post / forum
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-own",
            "data": {"title": "自身服务", "board": "技术"},
            "tags": [],
            "service_name": "forum",
        },
    )
    assert response.status_code == 201
    assert response.json()["service_name"] == "forum"


@pytest.mark.asyncio
async def test_normal_user_create_defaults_to_own_service(client):
    """测试普通用户未指定 service_name 时默认归属自身 service（统一 Scope）。"""
    await _create_type(client)  # forum_post / forum
    response = await client.post(
        "/api/v1/entries",
        headers=_user_headers(service_name="forum"),
        json={
            "type_name": "forum_post",
            "entity_key": "post-default",
            "data": {"title": "默认服务", "board": "技术"},
            "tags": [],
        },
    )
    assert response.status_code == 201
    assert response.json()["service_name"] == "forum"


# ── 批量查询（batch get）──────────────────────────────────


@pytest.mark.asyncio
async def test_batch_get_returns_map_with_null_for_missing(client):
    """测试批量查询返回 {key: obj}，未找到的 key 返回 null。"""
    await _create_type(client)  # forum_post / forum
    for key in ["post-001", "post-002"]:
        await client.post(
            "/api/v1/entries",
            headers=_user_headers(),
            json={
                "type_name": "forum_post",
                "entity_key": key,
                "data": {"title": f"帖子{key}", "board": "技术"},
                "tags": [],
            },
        )
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_user_headers(),
        json={"type_name": "forum_post", "keys": ["post-001", "post-002", "post-missing"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert set(data.keys()) == {"post-001", "post-002", "post-missing"}
    assert data["post-001"]["entity_key"] == "post-001"
    assert data["post-002"]["entity_key"] == "post-002"
    assert data["post-missing"] is None


@pytest.mark.asyncio
async def test_batch_get_only_own_service(client):
    """测试普通用户批量查询仅返回自身 service 的数据（其他 service 的 key → null）。"""
    await _create_type(client)  # forum_post / forum
    await client.post(
        "/api/v1/types",
        headers=_superuser_headers(),
        json={"type_name": "forum_post", "service_name": "shop", "schema_json": FORUM_SCHEMA},
    )
    # forum 下创建 post-a
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={"type_name": "forum_post", "entity_key": "post-a", "data": {"title": "A", "board": "技术"}, "tags": []},
    )
    # shop 下创建 post-b
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=2, username="bob", service_name="shop"),
        json={"type_name": "forum_post", "entity_key": "post-b", "data": {"title": "B", "board": "技术"}, "tags": []},
    )
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_user_headers(service_name="forum"),
        json={"type_name": "forum_post", "keys": ["post-a", "post-b"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["post-a"]["entity_key"] == "post-a"
    assert data["post-b"] is None


@pytest.mark.asyncio
async def test_batch_get_superuser_cross_service(client):
    """测试 superuser 通过请求体 service_name 跨服务批量查询。"""
    await _create_type(client)  # forum_post / forum
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(user_id=1, username="alice", service_name="forum"),
        json={"type_name": "forum_post", "entity_key": "post-a", "data": {"title": "A", "board": "技术"}, "tags": []},
    )
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_superuser_headers_for("default"),
        json={"type_name": "forum_post", "keys": ["post-a"], "service_name": "forum"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["post-a"]["entity_key"] == "post-a"
    assert data["post-a"]["service_name"] == "forum"


@pytest.mark.asyncio
async def test_batch_get_normal_user_cross_service_forbidden(client):
    """测试普通用户批量查询指定其他 service 返回 403（统一 Scope）。"""
    await _create_type(client)  # forum_post / forum
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_user_headers(service_name="forum"),
        json={"type_name": "forum_post", "keys": ["post-a"], "service_name": "shop"},
    )
    assert response.status_code == 403
    assert "仅超级用户可跨服务" in response.json()["detail"]


@pytest.mark.asyncio
async def test_batch_get_type_not_found_404(client):
    """测试批量查询不存在的类型返回 404。"""
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_user_headers(),
        json={"type_name": "nonexistent", "keys": ["post-a"]},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_batch_get_keys_over_limit_422(client):
    """测试批量查询 keys 数量超过上限返回 422。"""
    await _create_type(client)  # forum_post / forum
    many_keys = [f"key-{i:04d}" for i in range(201)]  # > MAX_BATCH_KEYS(200)
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_user_headers(),
        json={"type_name": "forum_post", "keys": many_keys},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_batch_get_soft_deleted_key_null(client):
    """测试批量查询中已软删的 key 返回 null。"""
    await _create_type(client)  # forum_post / forum
    await client.post(
        "/api/v1/entries",
        headers=_user_headers(),
        json={"type_name": "forum_post", "entity_key": "post-a", "data": {"title": "A", "board": "技术"}, "tags": []},
    )
    await client.delete("/api/v1/entries/forum_post/post-a", headers=_user_headers())
    response = await client.post(
        "/api/v1/entries/batch",
        headers=_user_headers(),
        json={"type_name": "forum_post", "keys": ["post-a"]},
    )
    assert response.status_code == 200
    assert response.json()["post-a"] is None


# ── owner 级用户隔离（同 service 跨 owner）───────────────────


def _alice_headers():
    """alice: forum 服务, user_id=1。"""
    return _user_headers(user_id=1, username="alice", service_name="forum")


def _bob_headers():
    """bob: 同 forum 服务, user_id=2（与 alice 同 service 但不同 owner）。"""
    return _user_headers(user_id=2, username="bob", service_name="forum")


async def _create_two_users_entries(client):
    """alice 与 bob（同 forum 服务）各创建一条实体。"""
    await _create_type(client)  # forum_post / forum
    await client.post(
        "/api/v1/entries",
        headers=_alice_headers(),
        json={"type_name": "forum_post", "entity_key": "post-alice", "data": {"title": "alice的帖", "board": "技术"}, "tags": []},
    )
    await client.post(
        "/api/v1/entries",
        headers=_bob_headers(),
        json={"type_name": "forum_post", "entity_key": "post-bob", "data": {"title": "bob的帖", "board": "技术"}, "tags": []},
    )


@pytest.mark.asyncio
async def test_owner_isolation_get_cross_owner_403(client):
    """同 service 下用户 A 无法读取用户 B 创建的实体（单实体 GET → 403）。"""
    await _create_two_users_entries(client)
    # bob 尝试读 alice 的实体
    resp = await client.get(
        "/api/v1/entries/forum_post/post-alice", headers=_bob_headers()
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_isolation_update_cross_owner_403(client):
    """同 service 下用户 A 无法修改用户 B 创建的实体（PUT → 403）。"""
    await _create_two_users_entries(client)
    resp = await client.put(
        "/api/v1/entries/forum_post/post-alice",
        headers=_bob_headers(),
        json={"data": {"title": "被篡改"}},
    )
    assert resp.status_code == 403
    # 确认 alice 的数据未被篡改
    check = await client.get(
        "/api/v1/entries/forum_post/post-alice", headers=_alice_headers()
    )
    assert check.json()["data"]["title"] == "alice的帖"


@pytest.mark.asyncio
async def test_owner_isolation_delete_cross_owner_403(client):
    """同 service 下用户 A 无法删除用户 B 创建的实体（DELETE → 403）。"""
    await _create_two_users_entries(client)
    resp = await client.delete(
        "/api/v1/entries/forum_post/post-alice", headers=_bob_headers()
    )
    assert resp.status_code == 403
    # 确认 alice 的实体仍在
    check = await client.get(
        "/api/v1/entries/forum_post/post-alice", headers=_alice_headers()
    )
    assert check.status_code == 200


@pytest.mark.asyncio
async def test_owner_isolation_versions_cross_owner_403(client):
    """同 service 下用户 A 无法查看用户 B 实体的版本历史（versions → 403）。"""
    await _create_two_users_entries(client)
    resp = await client.get(
        "/api/v1/entries/forum_post/post-alice/versions", headers=_bob_headers()
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_isolation_rollback_cross_owner_403(client):
    """同 service 下用户 A 无法回滚用户 B 的实体（rollback → 403）。"""
    await _create_two_users_entries(client)
    resp = await client.post(
        "/api/v1/entries/forum_post/post-alice/rollback",
        headers=_bob_headers(),
        json={"version": 1},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_isolation_query_list_hides_other_owner(client):
    """同 service 下列表查询仅返回自身 owner 的实体，不泄露他人存在性。"""
    await _create_two_users_entries(client)
    # bob 查询：只能看到自己的 post-bob，看不到 post-alice
    resp = await client.get(
        "/api/v1/entries?type_name=forum_post", headers=_bob_headers()
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["entity_key"] == "post-bob"
    # alice 查询：只能看到自己的 post-alice
    resp = await client.get(
        "/api/v1/entries?type_name=forum_post", headers=_alice_headers()
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["entity_key"] == "post-alice"


@pytest.mark.asyncio
async def test_owner_isolation_batch_get_hides_other_owner(client):
    """同 service 下批量查询中他人 owner 的 key 返回 null（不泄露存在性）。"""
    await _create_two_users_entries(client)
    # bob 批量查 alice 的 key 与自己的 key
    resp = await client.post(
        "/api/v1/entries/batch",
        headers=_bob_headers(),
        json={"type_name": "forum_post", "keys": ["post-alice", "post-bob"]},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["post-bob"]["entity_key"] == "post-bob"
    assert data["post-alice"] is None  # 不泄露 alice 实体的存在性


@pytest.mark.asyncio
async def test_owner_isolation_owner_can_access_own_entry(client):
    """owner 本人可正常读/改/删/查自己的实体。"""
    await _create_two_users_entries(client)
    # 读
    resp = await client.get("/api/v1/entries/forum_post/post-alice", headers=_alice_headers())
    assert resp.status_code == 200
    # 改
    resp = await client.put(
        "/api/v1/entries/forum_post/post-alice",
        headers=_alice_headers(),
        json={"data": {"title": "alice改了"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "alice改了"
    # 删
    resp = await client.delete("/api/v1/entries/forum_post/post-alice", headers=_alice_headers())
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_superuser_cross_owner_get_ok(client):
    """superuser（GLOBAL）不受 owner 限制，可跨 owner 读取。"""
    await _create_two_users_entries(client)
    # superuser 可读取 alice 的实体（跨 owner）
    resp = await client.get(
        "/api/v1/entries/forum_post/post-alice", headers=_superuser_headers_for("default")
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "alice的帖"
    # superuser 可读取 bob 的实体（跨 owner）
    resp = await client.get(
        "/api/v1/entries/forum_post/post-bob", headers=_superuser_headers_for("default")
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "bob的帖"


@pytest.mark.asyncio
async def test_superuser_cross_owner_update_ok(client):
    """superuser 不受 owner 限制，可跨 owner 修改。"""
    await _create_two_users_entries(client)
    resp = await client.put(
        "/api/v1/entries/forum_post/post-alice",
        headers=_superuser_headers_for("default"),
        json={"data": {"title": "super改了"}},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["title"] == "super改了"


@pytest.mark.asyncio
async def test_superuser_query_sees_all_owners(client):
    """superuser 查询返回所有 owner 的实体（不按 owner 过滤）。"""
    await _create_two_users_entries(client)
    resp = await client.get(
        "/api/v1/entries?type_name=forum_post", headers=_superuser_headers_for("default")
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 2
    keys = {item["entity_key"] for item in data["items"]}
    assert keys == {"post-alice", "post-bob"}


@pytest.mark.asyncio
async def test_superuser_batch_get_cross_owner(client):
    """superuser 批量查询可跨 owner 返回实体。"""
    await _create_two_users_entries(client)
    resp = await client.post(
        "/api/v1/entries/batch",
        headers=_superuser_headers_for("default"),
        json={"type_name": "forum_post", "keys": ["post-alice", "post-bob"], "service_name": "forum"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["post-alice"]["entity_key"] == "post-alice"
    assert data["post-bob"]["entity_key"] == "post-bob"
