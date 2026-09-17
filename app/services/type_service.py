"""元数据类型业务逻辑层：类型管理 + 动态 schema 校验。"""

from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel, Field, create_model
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.metadata_type import MetadataType
from app.proxy.log_proxy import LogProxy
from app.repositories.type_repository import TypeRepository
from app.schemas.type import MetadataTypeCreate, MetadataTypeUpdate

# 支持的字段类型 → Python 类型映射（list/dict/object 为复合类型，运行时按子定义递归解析）
_FIELD_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "list": list,
    "dict": dict,
    "object": dict,
}


def _validate_schema_fields(schema_json: dict) -> None:
    """校验 schema_json 的结构合法性（含复合类型递归校验）。"""
    if not isinstance(schema_json, dict) or "fields" not in schema_json:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="schema_json 必须包含 fields 字段",
        )
    fields = schema_json["fields"]
    if not isinstance(fields, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="schema_json.fields 必须是对象",
        )
    for field_name, field_def in fields.items():
        _validate_field_definition(field_name, field_def, path=field_name)


def _validate_field_definition(field_name: str, field_def: dict, *, path: str) -> None:
    """递归校验单个字段定义（支持 list / dict / object 复合类型）。"""
    if not isinstance(field_def, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"字段 {path} 的定义必须是对象",
        )
    field_type = field_def.get("type")
    if field_type not in _FIELD_TYPE_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"字段 {path} 的类型 {field_type} 不支持，仅支持 {list(_FIELD_TYPE_MAP.keys())}",
        )

    if field_type == "list":
        items = field_def.get("items")
        if items is not None:
            _validate_field_definition(field_name, items, path=f"{path}[items]")
    elif field_type == "dict":
        values = field_def.get("values")
        if values is not None:
            _validate_field_definition(field_name, values, path=f"{path}[values]")
    elif field_type == "object":
        sub_fields = field_def.get("fields")
        if sub_fields is not None:
            if not isinstance(sub_fields, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"字段 {path} 的 fields 必须是对象",
                )
            for sub_name, sub_def in sub_fields.items():
                _validate_field_definition(sub_name, sub_def, path=f"{path}.{sub_name}")


def build_dynamic_model(schema_json: dict, model_name: str = "DynamicMetadata") -> type[BaseModel]:
    """根据类型定义的 schema 动态构造 Pydantic 模型，用于运行时校验元数据 JSON。

    禁止硬编码业务字段，完全由 schema_json 驱动。
    支持复合类型：list（可带 items 子定义）、dict（可带 values 子定义）、
    object（可带 fields 子定义，递归构造嵌套模型）。
    """
    fields_def = schema_json.get("fields", {})
    return _build_dynamic_model(fields_def, model_name)


def _build_dynamic_model(fields: dict, model_name: str) -> type[BaseModel]:
    """递归构造动态 Pydantic 模型（object 子结构会生成独立的嵌套模型）。"""
    model_fields: dict[str, Any] = {}
    for field_name, field_def in fields.items():
        py_type = _resolve_field_type(field_def, prefix=f"{model_name}_{field_name}")
        required = field_def.get("required", False)
        default = field_def.get("default", None)

        if required:
            model_fields[field_name] = (py_type, Field(...))
        elif default is not None:
            model_fields[field_name] = (py_type, Field(default=default))
        else:
            model_fields[field_name] = (py_type | None, Field(default=None))

    return create_model(model_name, **model_fields)


def _resolve_field_type(field_def: dict, *, prefix: str) -> type:
    """解析字段定义的 Python 类型，支持复合类型递归。"""
    field_type = field_def.get("type")
    if field_type == "list":
        items = field_def.get("items")
        if items is None:
            return list
        return list[_resolve_field_type(items, prefix=prefix + "_item")]
    if field_type == "dict":
        values = field_def.get("values")
        if values is None:
            return dict
        return dict[str, _resolve_field_type(values, prefix=prefix + "_value")]
    if field_type == "object":
        sub_fields = field_def.get("fields")
        if sub_fields is None:
            return dict
        nested_name = _safe_model_name(prefix) + "_Obj"
        return _build_dynamic_model(sub_fields, model_name=nested_name)
    return _FIELD_TYPE_MAP.get(field_type, str)


def _safe_model_name(name: str) -> str:
    """将字段路径转换为合法的 Python 类名片段（防字段名含非法字符）。"""
    import re

    return re.sub(r"\W", "_", name)


def validate_data_against_schema(schema_json: dict, data: dict) -> dict:
    """按类型 schema 动态校验元数据 JSON，失败返回 422。"""
    # 检查字段数上限
    if len(data) > settings.MAX_ENTRY_DATA_KEYS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"元数据字段数超过上限 {settings.MAX_ENTRY_DATA_KEYS}",
        )
    # 检查嵌套深度
    if _get_depth(data) > settings.MAX_ENTRY_DATA_DEPTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"元数据嵌套深度超过上限 {settings.MAX_ENTRY_DATA_DEPTH}",
        )

    model_cls = build_dynamic_model(schema_json)
    try:
        validated = model_cls.model_validate(data)
        # exclude_none=True：None 视为字段不存在，不存储（配合 update 的 null 删除语义）
        return validated.model_dump(exclude_none=True)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"元数据校验失败: {exc}",
        )


def _get_depth(obj: Any, current: int = 0) -> int:
    """计算嵌套结构的最大深度。"""
    if isinstance(obj, dict):
        if not obj:
            return current + 1
        return max(_get_depth(v, current + 1) for v in obj.values())
    if isinstance(obj, list):
        if not obj:
            return current + 1
        return max(_get_depth(item, current + 1) for item in obj)
    return current


class TypeService:
    """元数据类型业务逻辑层。"""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db
        self.repo: TypeRepository = LogProxy(TypeRepository(db))  # type: ignore[assignment]

    async def create_type(self, type_data: MetadataTypeCreate) -> MetadataType:
        """创建元数据类型。"""
        # 校验 schema 结构
        schema_dict = type_data.schema_json.model_dump()
        _validate_schema_fields(schema_dict)

        # 检查唯一性
        existing = await self.repo.get_by_name_and_service(
            type_data.type_name, type_data.service_name
        )
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"类型 {type_data.type_name} 在业务 {type_data.service_name} 下已存在",
            )

        metadata_type = MetadataType(
            type_name=type_data.type_name,
            service_name=type_data.service_name,
            description=type_data.description,
            schema_json=schema_dict,
        )
        return await self.repo.create(metadata_type)

    async def get_type_by_name(self, type_name: str, service_name: str) -> MetadataType:
        """根据 type_name + service_name 获取类型。"""
        metadata_type = await self.repo.get_by_name_and_service(type_name, service_name)
        if metadata_type is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"元数据类型 {type_name} 不存在",
            )
        return metadata_type

    async def get_type_by_id(self, type_id: int) -> MetadataType:
        """根据 ID 获取类型。"""
        metadata_type = await self.repo.get_by_id(type_id)
        if metadata_type is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="元数据类型不存在",
            )
        return metadata_type

    async def update_type(
        self, type_name: str, service_name: str, update_data: MetadataTypeUpdate
    ) -> MetadataType:
        """更新元数据类型（类型下无实体数据时允许移除字段；有实体数据时仅允许新增字段）。"""
        metadata_type = await self.get_type_by_name(type_name, service_name)

        # 更新描述
        if update_data.description is not None:
            metadata_type.description = update_data.description

        # 更新 schema：无实体数据时允许移除字段；有实体数据时仅允许新增字段，拒绝移除或改字段类型
        if update_data.schema_json is not None:
            new_schema = update_data.schema_json.model_dump()
            _validate_schema_fields(new_schema)
            active_count = await self.repo.count_active_entries(metadata_type.id)
            self._validate_schema_backward_compatible(
                metadata_type.schema_json,
                new_schema,
                allow_remove_fields=active_count == 0,
            )
            metadata_type.schema_json = new_schema

        return await self.repo.update(metadata_type)

    @staticmethod
    def _validate_schema_backward_compatible(
        old_schema: dict, new_schema: dict, *, allow_remove_fields: bool = False
    ) -> None:
        """校验 schema 更新是否向后兼容：无实体数据（allow_remove_fields）时允许移除字段；
        否则仅允许新增字段，拒绝移除或改类型（含复合类型递归）。"""
        old_fields = old_schema.get("fields", {})
        new_fields = new_schema.get("fields", {})
        TypeService._validate_fields_backward(
            old_fields, new_fields, "", allow_remove_fields=allow_remove_fields
        )

    @staticmethod
    def _validate_fields_backward(
        old_fields: dict, new_fields: dict, path: str, *, allow_remove_fields: bool = False
    ) -> None:
        """递归校验字段集合：默认不允许移除字段、不允许修改已有字段类型；
        allow_remove_fields=True（无实体数据）时允许移除字段。"""
        removed = set(old_fields.keys()) - set(new_fields.keys())
        if removed and not allow_remove_fields:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"不允许移除字段: {sorted(removed)}（类型下存在实体数据时仅支持新增字段）",
            )
        for field_name in old_fields:
            if field_name not in new_fields:
                continue  # 已移除的字段：allow_remove_fields 时允许
            TypeService._validate_field_backward(
                old_fields[field_name],
                new_fields[field_name],
                f"{path}{field_name}",
                allow_remove_fields=allow_remove_fields,
            )

    @staticmethod
    def _validate_field_backward(
        old_def: dict, new_def: dict, path: str, *, allow_remove_fields: bool = False
    ) -> None:
        """递归校验单个字段：类型不得变更；list/dict/object 的子定义递归校验。"""
        if not isinstance(new_def, dict):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"字段 {path} 的定义必须是对象",
            )
        old_type = old_def.get("type")
        new_type = new_def.get("type")
        if old_type != new_type:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"不允许修改字段 {path} 的类型: {old_type} → {new_type}（仅支持新增字段）",
            )

        if old_type == "list":
            TypeService._validate_subdef_backward(
                old_def.get("items"),
                new_def.get("items"),
                f"{path}[items]",
                allow_remove_fields=allow_remove_fields,
            )
        elif old_type == "dict":
            TypeService._validate_subdef_backward(
                old_def.get("values"),
                new_def.get("values"),
                f"{path}[values]",
                allow_remove_fields=allow_remove_fields,
            )
        elif old_type == "object":
            old_sub = old_def.get("fields") or {}
            new_sub = new_def.get("fields") or {}
            if not isinstance(new_sub, dict):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"字段 {path}.fields 必须是对象",
                )
            TypeService._validate_fields_backward(
                old_sub, new_sub, f"{path}.", allow_remove_fields=allow_remove_fields
            )

    @staticmethod
    def _validate_subdef_backward(
        old_child: dict | None,
        new_child: dict | None,
        path: str,
        *,
        allow_remove_fields: bool = False,
    ) -> None:
        """复合类型的子定义比较：旧无子定义时允许新增；旧有子定义时不允许移除，且递归比较。

        allow_remove_fields=True（类型下无实体数据）时，允许移除 dict/list 内部嵌套
        object 的子字段（如 dict.values.fields 中的字段），但移除子定义本身仍不允许。
        """
        if old_child is None:
            return
        if new_child is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"不允许移除 {path} 的子类型定义",
            )
        TypeService._validate_field_backward(
            old_child, new_child, path, allow_remove_fields=allow_remove_fields
        )

    async def delete_type(self, type_name: str, service_name: str) -> None:
        """软删除元数据类型（已有实体数据的类型禁止删除）。"""
        metadata_type = await self.get_type_by_name(type_name, service_name)

        # 检查是否有未删除的实体数据
        active_count = await self.repo.count_active_entries(metadata_type.id)
        if active_count > 0:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"类型 {type_name} 下仍有 {active_count} 条实体数据，禁止删除（请先删除相关实体）",
            )

        await self.repo.soft_delete(metadata_type)

    async def list_types(
        self,
        skip: int = 0,
        limit: int = 20,
        service_name: str | None = None,
    ) -> tuple[list[MetadataType], int]:
        """分页获取元数据类型列表。"""
        return await self.repo.list_types(skip=skip, limit=limit, service_name=service_name)
