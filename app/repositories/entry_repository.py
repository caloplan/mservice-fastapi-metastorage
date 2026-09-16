from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.metadata_entry import MetadataEntry, MetadataVersion
from app.models.metadata_type import MetadataType


class EntryRepository:
    """元数据实体数据访问层，封装 CRUD、版本管理与复杂查询。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ── 基础 CRUD ──────────────────────────────────────────

    async def create(self, entry: MetadataEntry) -> MetadataEntry:
        """创建元数据实体。"""
        self.db.add(entry)
        await self.db.flush()
        await self.db.refresh(entry)
        return entry

    async def get_by_id(self, entry_id: int) -> MetadataEntry | None:
        """根据 ID 获取实体（含软删除过滤）。"""
        stmt = select(MetadataEntry).where(
            MetadataEntry.id == entry_id,
            MetadataEntry.is_deleted == 0,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_type_and_key(
        self, type_id: int, entity_key: str
    ) -> MetadataEntry | None:
        """根据 type_id + entity_key 获取实体（含软删除过滤）。"""
        stmt = select(MetadataEntry).where(
            MetadataEntry.type_id == type_id,
            MetadataEntry.entity_key == entity_key,
            MetadataEntry.is_deleted == 0,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_by_type_and_keys(
        self,
        type_id: int,
        keys: list[str],
        service_name: str | None = None,
        owner_user_id: int | None = None,
    ) -> list[MetadataEntry]:
        """根据 type_id + entity_key 列表批量获取实体（含软删除过滤）。

        用于批量查询（batch get）：一次取回多个 key 对应的实体。
        service_name / owner_user_id 由上层 MetaScope 解析后传入。
        """
        stmt = select(MetadataEntry).where(
            MetadataEntry.type_id == type_id,
            MetadataEntry.entity_key.in_(keys),
            MetadataEntry.is_deleted == 0,
        )
        if service_name:
            stmt = stmt.where(MetadataEntry.service_name == service_name)
        if owner_user_id is not None:
            stmt = stmt.where(MetadataEntry.owner_user_id == owner_user_id)
        result = await self.db.execute(stmt)
        return list(result.scalars().all())

    async def get_by_type_name_and_key(
        self, type_name: str, entity_key: str
    ) -> MetadataEntry | None:
        """根据 type_name + entity_key 联表查询实体（含软删除过滤，跨 service 查找）。

        用于 get/update/delete/versions/rollback 等操作：先找到实体，再做可见性判定。
        """
        stmt = (
            select(MetadataEntry)
            .join(MetadataType, MetadataType.id == MetadataEntry.type_id)
            .where(
                MetadataType.type_name == type_name,
                MetadataType.is_deleted == 0,
                MetadataEntry.entity_key == entity_key,
                MetadataEntry.is_deleted == 0,
            )
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, entry: MetadataEntry) -> MetadataEntry:
        """更新元数据实体。"""
        entry.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(entry)
        return entry

    async def soft_delete(self, entry: MetadataEntry) -> MetadataEntry:
        """软删除元数据实体。"""
        entry.is_deleted = 1
        entry.deleted_at = datetime.now(timezone.utc)
        await self.db.flush()
        return entry

    async def restore(self, entry: MetadataEntry) -> MetadataEntry:
        """恢复软删除的元数据实体。"""
        entry.is_deleted = 0
        entry.deleted_at = None
        entry.updated_at = datetime.now(timezone.utc)
        await self.db.flush()
        await self.db.refresh(entry)
        return entry

    async def hard_delete(self, entry: MetadataEntry) -> None:
        """永久删除元数据实体（同时删除其所有历史版本）。"""
        # 删除历史版本
        version_stmt = select(MetadataVersion).where(MetadataVersion.entry_id == entry.id)
        versions = (await self.db.execute(version_stmt)).scalars().all()
        for v in versions:
            await self.db.delete(v)
        await self.db.delete(entry)
        await self.db.flush()

    # ── 版本管理 ──────────────────────────────────────────

    async def create_version(self, version: MetadataVersion) -> MetadataVersion:
        """保存历史版本。"""
        self.db.add(version)
        await self.db.flush()
        await self.db.refresh(version)
        return version

    async def get_versions(
        self, entry_id: int, skip: int = 0, limit: int = 50
    ) -> tuple[list[MetadataVersion], int]:
        """分页获取实体的历史版本列表（按版本号降序）。"""
        count_stmt = select(func.count()).select_from(MetadataVersion).where(
            MetadataVersion.entry_id == entry_id
        )
        total = (await self.db.execute(count_stmt)).scalar_one()

        stmt = (
            select(MetadataVersion)
            .where(MetadataVersion.entry_id == entry_id)
            .order_by(MetadataVersion.version.desc())
            .offset(skip)
            .limit(limit)
        )
        result = await self.db.execute(stmt)
        versions = list(result.scalars().all())
        return versions, total

    async def get_version_by_number(
        self, entry_id: int, version: int
    ) -> MetadataVersion | None:
        """根据版本号获取指定历史版本。"""
        stmt = select(MetadataVersion).where(
            MetadataVersion.entry_id == entry_id,
            MetadataVersion.version == version,
        )
        result = await self.db.execute(stmt)
        return result.scalar_one_or_none()

    async def get_max_version(self, entry_id: int) -> int:
        """获取实体的最大历史版本号（不含当前实体自身）。"""
        stmt = select(func.max(MetadataVersion.version)).where(
            MetadataVersion.entry_id == entry_id
        )
        result = (await self.db.execute(stmt)).scalar_one()
        return result or 0

    async def delete_old_versions(self, entry_id: int, keep_count: int) -> int:
        """清理超过保留上限的最旧版本，返回删除数量。"""
        # 查出需要保留的版本号（最新的 keep_count 个）
        keep_stmt = (
            select(MetadataVersion.version)
            .where(MetadataVersion.entry_id == entry_id)
            .order_by(MetadataVersion.version.desc())
            .limit(keep_count)
        )
        keep_versions = set((await self.db.execute(keep_stmt)).scalars().all())

        # 删除不在保留集合中的版本
        delete_stmt = select(MetadataVersion).where(
            MetadataVersion.entry_id == entry_id,
            MetadataVersion.version.notin_(keep_versions) if keep_versions else text("1=1"),
        )
        to_delete = (await self.db.execute(delete_stmt)).scalars().all()
        count = len(to_delete)
        for v in to_delete:
            await self.db.delete(v)
        await self.db.flush()
        return count

    # ── 复杂查询 ──────────────────────────────────────────

    async def query_entries(
        self,
        *,
        type_name: str | None = None,
        service_name: str | None = None,
        owner_user_id: int | None = None,
        field_filters: dict[str, object] | None = None,
        tags: list[str] | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        skip: int = 0,
        limit: int = 20,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[MetadataEntry], int]:
        """复杂查询：字段过滤 + tags 交集 + 时间范围 + 分页 + 排序 + service 隔离。

        service_name 由上层 MetaScope 统一解析（普通身份强制自身 service，superuser 可全量/按指定）。
        """
        # 基础查询（join type 表以支持 type_name 过滤和响应中的 type_name）
        stmt = select(MetadataEntry).join(
            MetadataType, MetadataType.id == MetadataEntry.type_id
        )

        conditions = [MetadataEntry.is_deleted == 0, MetadataType.is_deleted == 0]

        # type_name 过滤
        if type_name:
            conditions.append(MetadataType.type_name == type_name)

        # service_name 隔离（由上层 Scope 解析）
        if service_name:
            conditions.append(MetadataEntry.service_name == service_name)

        # owner 过滤
        if owner_user_id is not None:
            conditions.append(MetadataEntry.owner_user_id == owner_user_id)

        # 字段值过滤（通过 SQLite json_extract）
        if field_filters:
            for field_name, field_value in field_filters.items():
                # 安全字段名：只允许字母数字下划线，防注入
                safe_field = "".join(c for c in field_name if c.isalnum() or c == "_")
                if safe_field:
                    conditions.append(
                        func.json_extract(MetadataEntry.data, f"$.{safe_field}")
                        == field_value
                    )

        # tags 交集过滤（通过 json_each EXISTS 子查询）
        if tags:
            tag_conditions = []
            tag_params: dict[str, str] = {}
            for i, tag in enumerate(tags):
                tag_conditions.append(
                    f"EXISTS (SELECT 1 FROM json_each(metadata_entries.tags) WHERE value = :tag_{i})"
                )
                tag_params[f"tag_{i}"] = tag
            tag_clause = text(" AND ".join(tag_conditions))
            for key, val in tag_params.items():
                tag_clause = tag_clause.bindparams(**{key: val})
            conditions.append(tag_clause)

        # 时间范围
        if created_after:
            conditions.append(MetadataEntry.created_at >= created_after)
        if created_before:
            conditions.append(MetadataEntry.created_at <= created_before)

        # 总数查询
        count_stmt = select(func.count()).select_from(MetadataEntry).join(
            MetadataType, MetadataType.id == MetadataEntry.type_id
        ).where(*conditions)
        total = (await self.db.execute(count_stmt)).scalar_one()

        # 排序
        sort_column = getattr(MetadataEntry, sort_by, MetadataEntry.created_at)
        if sort_order.lower() == "asc":
            order = sort_column.asc()
        else:
            order = sort_column.desc()

        stmt = stmt.where(*conditions).order_by(order).offset(skip).limit(limit)
        result = await self.db.execute(stmt)
        entries = list(result.scalars().all())
        return entries, total
