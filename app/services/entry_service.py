"""元数据实体业务逻辑层：写读查 / 版本 / 回滚 / 统一 Scope 权限隔离。"""

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.dependencies import CurrentUser, MetaScope
from app.models.metadata_entry import MetadataEntry, MetadataVersion
from app.proxy.log_proxy import LogProxy
from app.repositories.entry_repository import EntryRepository
from app.schemas.entry import BatchQueryRequest, MetadataEntryCreate, MetadataEntryUpdate
from app.services.type_service import TypeService, validate_data_against_schema


def _deep_merge(base: dict, update: dict) -> dict:
    """深度合并两个字典：update 中的值覆盖 base，嵌套字典递归合并；值为 None 表示删除该字段。"""
    result = base.copy()
    for key, value in update.items():
        if value is None:
            result.pop(key, None)
            continue
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _validate_tags(tags: list[str]) -> None:
    """校验 tags 数量与单 tag 长度。"""
    if len(tags) > settings.MAX_TAGS_PER_ENTRY:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"tags 数量超过上限 {settings.MAX_TAGS_PER_ENTRY}",
        )
    for tag in tags:
        if len(tag) > settings.MAX_TAG_LENGTH:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"tag 长度超过上限 {settings.MAX_TAG_LENGTH}: {tag[:20]}...",
            )


class EntryService:
    """元数据实体业务逻辑层。

    所有操作（list / get / create / update / delete / versions / rollback）统一经 MetaScope 做权限判定：
    - GLOBAL scope（superuser）：可操作任意 service、任意 owner；
    - SERVICE scope（普通身份）：仅可操作自身 service 且自身 owner 的 entry。
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo: EntryRepository = LogProxy(EntryRepository(db))  # type: ignore[assignment]
        self.type_service = TypeService(db)

    async def _find_entry(
        self, type_name: str, entity_key: str, scope: MetaScope
    ) -> MetadataEntry:
        """联表查找实体并做 Scope 访问校验，不存在返回 404，越权返回 403。"""
        entry = await self.repo.get_by_type_name_and_key(type_name, entity_key)
        if entry is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"实体 {entity_key} 不存在",
            )
        scope.check_entry_access(entry, action="访问元数据")
        return entry

    async def create_entry(
        self, entry_data: MetadataEntryCreate, current_user: CurrentUser, scope: MetaScope
    ) -> MetadataEntry:
        """创建实体元数据（统一 Scope：普通身份仅本 service，superuser 可指定跨 service）。"""
        # 解析目标 service（显式跨服务仅 GLOBAL scope 允许）
        target_service = scope.resolve_target(entry_data.service_name, action="创建元数据")
        # 校验类型存在（目标 service 下的类型）
        metadata_type = await self.type_service.get_type_by_name(
            entry_data.type_name, target_service
        )
        # 校验 tags
        _validate_tags(entry_data.tags)
        # 按 schema 动态校验 data
        validated_data = validate_data_against_schema(metadata_type.schema_json, entry_data.data)
        # 检查 (type_id, entity_key) 唯一性
        existing = await self.repo.get_by_type_and_key(metadata_type.id, entry_data.entity_key)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"实体 {entry_data.entity_key} 在类型 {entry_data.type_name} 下已存在",
            )

        entry = MetadataEntry(
            type_id=metadata_type.id,
            entity_key=entry_data.entity_key,
            data=validated_data,
            tags=entry_data.tags,
            version=1,
            service_name=target_service,
            owner_user_id=current_user.user_id,
        )
        return await self.repo.create(entry)

    async def get_entry(
        self,
        type_name: str,
        entity_key: str,
        scope: MetaScope,
        version: int | None = None,
    ) -> MetadataEntry:
        """获取实体元数据（支持指定历史版本）。"""
        entry = await self._find_entry(type_name, entity_key, scope)

        if version is not None:
            if version == entry.version:
                return entry
            historical = await self.repo.get_version_by_number(entry.id, version)
            if historical is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"版本 {version} 不存在",
                )
            entry.data = historical.data
            entry.tags = historical.tags
            entry.version = historical.version
            entry.updated_at = historical.created_at
        return entry

    async def batch_get_entries(
        self, batch_data: BatchQueryRequest, scope: MetaScope
    ) -> list[MetadataEntry]:
        """按 entity_key 列表批量查询实体（统一 Scope 判定）。

        - 目标 service 由 scope.resolve_target 解析（普通身份仅本 service，superuser 可指定跨服务）；
        - 类型不存在 → 404；单 key 找不到 → 由路由层映射为 null。
        """
        target_service = scope.resolve_target(batch_data.service_name, action="批量查询元数据")
        # 去重并校验数量
        unique_keys = list(dict.fromkeys(batch_data.keys))
        if not unique_keys:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="keys 不能为空",
            )
        if len(unique_keys) > settings.MAX_BATCH_KEYS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"批量查询 keys 数量超过上限 {settings.MAX_BATCH_KEYS}",
            )

        metadata_type = await self.type_service.get_type_by_name(
            batch_data.type_name, target_service
        )
        return await self.repo.get_by_type_and_keys(
            metadata_type.id,
            unique_keys,
            service_name=target_service,
            owner_user_id=scope.resolve_owner_filter(),
        )

    async def update_entry(
        self,
        type_name: str,
        entity_key: str,
        update_data: MetadataEntryUpdate,
        current_user: CurrentUser,
        scope: MetaScope,
    ) -> MetadataEntry:
        """更新实体元数据（部分更新 deep merge，版本自增，保留历史版本）。"""
        entry = await self._find_entry(type_name, entity_key, scope)

        # 通过 entry.type_id 获取类型定义（用于 schema 校验）
        metadata_type = await self.type_service.get_type_by_id(entry.type_id)

        # 保存当前版本到历史版本表
        historical = MetadataVersion(
            entry_id=entry.id,
            version=entry.version,
            data=entry.data,
            tags=entry.tags,
            created_by_user_id=current_user.user_id,
        )
        await self.repo.create_version(historical)

        # 深度合并 data
        new_data = entry.data
        if update_data.data is not None:
            new_data = _deep_merge(entry.data, update_data.data)
            new_data = validate_data_against_schema(metadata_type.schema_json, new_data)

        # 替换 tags（非合并）
        new_tags = entry.tags
        if update_data.tags is not None:
            _validate_tags(update_data.tags)
            new_tags = update_data.tags

        entry.data = new_data
        entry.tags = new_tags
        entry.version = entry.version + 1
        entry.updated_at = datetime.now(timezone.utc)

        result = await self.repo.update(entry)
        await self.repo.delete_old_versions(entry.id, settings.MAX_VERSION_KEPT)
        return result

    async def delete_entry(
        self, type_name: str, entity_key: str, scope: MetaScope
    ) -> None:
        """软删除实体元数据。"""
        entry = await self._find_entry(type_name, entity_key, scope)
        await self.repo.soft_delete(entry)

    async def query_entries(
        self,
        scope: MetaScope,
        *,
        service_name: str | None = None,
        type_name: str | None = None,
        field_filters: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        skip: int = 0,
        limit: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[MetadataEntry], int]:
        """复杂查询：字段过滤 + tags 交集 + 时间范围 + 分页 + 排序 + 统一 Scope 隔离。

        可见性规则（resolve_filter + resolve_owner_filter）：
        - GLOBAL：未指定 service_name → 全部；指定 → 按指定过滤；owner 不限；
        - SERVICE：强制仅返回自身 service 且自身 owner 的数据，显式跨 service → 403。
        """
        target_service = scope.resolve_filter(service_name, action="查看元数据")
        return await self.repo.query_entries(
            type_name=type_name,
            service_name=target_service,
            owner_user_id=scope.resolve_owner_filter(),
            field_filters=field_filters,
            tags=tags,
            created_after=created_after,
            created_before=created_before,
            skip=skip,
            limit=limit,
            sort_by=sort_by,
            sort_order=sort_order,
        )

    async def get_versions(
        self,
        type_name: str,
        entity_key: str,
        scope: MetaScope,
        skip: int = 0,
        limit: int = 50,
    ) -> tuple[list[MetadataVersion], int]:
        """获取实体的版本历史列表。"""
        entry = await self._find_entry(type_name, entity_key, scope)
        return await self.repo.get_versions(entry.id, skip=skip, limit=limit)

    async def rollback_entry(
        self,
        type_name: str,
        entity_key: str,
        target_version: int,
        current_user: CurrentUser,
        scope: MetaScope,
    ) -> MetadataEntry:
        """回滚到指定版本（生成新版本，数据取回滚目标，不删除中间版本）。"""
        entry = await self._find_entry(type_name, entity_key, scope)

        if target_version == entry.version:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="目标版本与当前版本相同，无需回滚",
            )

        historical = await self.repo.get_version_by_number(entry.id, target_version)
        if historical is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"版本 {target_version} 不存在",
            )

        # 保存当前版本到历史
        current_historical = MetadataVersion(
            entry_id=entry.id,
            version=entry.version,
            data=entry.data,
            tags=entry.tags,
            created_by_user_id=current_user.user_id,
        )
        await self.repo.create_version(current_historical)

        entry.data = historical.data
        entry.tags = historical.tags
        entry.version = entry.version + 1
        entry.updated_at = datetime.now(timezone.utc)

        result = await self.repo.update(entry)
        await self.repo.delete_old_versions(entry.id, settings.MAX_VERSION_KEPT)
        return result
