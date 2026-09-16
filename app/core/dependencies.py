"""认证依赖：从 JWT 解析当前用户，提供统一权限 Scope 判定。

本服务不存储用户表，get_current_user 返回轻量的 CurrentUser 对象（仅含 JWT payload 字段）。

权限模型：所有 Meta 操作（list / get / create / update / delete）统一经过 MetaScope 判定。
- GLOBAL scope（superuser）：可操作任意 service、任意 owner；
- SERVICE scope（普通 service 身份）：仅可操作自身 service 且自身 owner 的 entry；
  类型（types）接口保持 service 级共享，不引入 owner 隔离。
"""

from enum import Enum
from typing import Annotated, Any

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from pydantic import BaseModel

from app.core.config import settings
from app.core.security import decode_token

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


class CurrentUser(BaseModel):
    """当前登录用户（从 JWT payload 解析，不落库）。"""

    sub: str
    user_id: int
    service_name: str
    role: str
    type: str


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
) -> CurrentUser:
    """从 JWT 解析当前用户信息。"""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="无法验证凭据",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = await decode_token(token)
        if payload.get("type") != "access":
            raise credentials_exception
        user_id = payload.get("user_id")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception
    except HTTPException:
        raise
    except Exception:
        raise credentials_exception

    return CurrentUser(
        sub=payload.get("sub", ""),
        user_id=user_id,
        service_name=payload.get("service_name", "default"),
        role=payload.get("role", "user"),
        type=payload.get("type", "access"),
    )


def is_superuser(user: CurrentUser) -> bool:
    """判定是否为超级用户（三重 AND 校验，缺一不可）。

    必须同时满足：
    1. JWT payload 的 role == "superuser"；
    2. username（sub）在 SUPERUSER_USERNAMES 白名单中；
    3. user_id 在 SUPERUSER_USER_IDS 白名单中。

    无兜底：任意一项不满足即返回 False，管理接口返回 403。
    """
    if user.role != "superuser":
        return False
    if user.sub not in settings.SUPERUSER_USERNAMES:
        return False
    if user.user_id not in settings.SUPERUSER_USER_IDS:
        return False
    return True


async def get_superuser_flag(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
) -> bool:
    """返回当前用户是否为 superuser（供 get_meta_scope 合成权限 Scope）。"""
    return is_superuser(current_user)


class Scope(str, Enum):
    """权限作用域：所有 Meta 操作共用的判定维度。"""

    GLOBAL = "global"  # superuser：可操作任意 service
    SERVICE = "service"  # 普通 service 身份：仅可操作自身 service


class MetaScope:
    """统一权限 Scope：所有 Meta 操作（list / get / create / update / delete）共用的判定入口。

    规则：
    - GLOBAL scope（superuser）：可访问/操作任意 service、任意 owner；
    - SERVICE scope（普通身份）：仅可访问/操作自身 service 且自身 owner 的 entry，
      显式跨 service 或跨 owner 一律 403（单实体）/ 不可见（列表/批量）。
    """

    def __init__(self, user: CurrentUser, is_superuser: bool) -> None:
        self.user = user
        self.is_superuser = is_superuser
        self.scope = Scope.GLOBAL if is_superuser else Scope.SERVICE

    @property
    def service_name(self) -> str:
        """当前身份所属 service。"""
        return self.user.service_name

    @property
    def owner_user_id(self) -> int:
        """当前登录用户 ID（SERVICE scope 下 entry 的 owner 隔离维度）。"""
        return self.user.user_id

    def require_global(self, *, action: str) -> None:
        """管理类操作（如类型 create/update/delete）：仅 GLOBAL scope 允许。"""
        if self.scope != Scope.GLOBAL:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"仅超级用户可{action}",
            )

    def resolve_target(self, requested: str | None, *, action: str) -> str:
        """解析单实体/创建操作的目标 service（默认自身）。

        - 未指定 → 当前身份所属 service；
        - 显式指定且与自身不同 → 仅 GLOBAL scope 允许，否则 403。
        """
        if requested is None:
            return self.service_name
        if requested != self.service_name and self.scope != Scope.GLOBAL:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"仅超级用户可跨服务{action}，当前身份仅可操作服务 {self.service_name}",
            )
        return requested

    def resolve_filter(self, requested: str | None, *, action: str) -> str | None:
        """解析列表/查询操作的 service 过滤条件；返回 None 表示不限。

        - GLOBAL：未指定 → None（全部服务）；指定 → 按指定过滤；
        - SERVICE：强制返回自身 service；显式指定其他服务 → 403。

        owner 维度见 resolve_owner_filter()。
        """
        if self.scope == Scope.GLOBAL:
            return requested
        if requested is not None and requested != self.service_name:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"仅超级用户可跨服务{action}，当前身份仅可查看服务 {self.service_name}",
            )
        return self.service_name

    def resolve_owner_filter(self) -> int | None:
        """解析列表/批量查询操作的 owner 过滤条件；返回 None 表示不限 owner。

        - GLOBAL（superuser）：None，可跨 owner 查看；
        - SERVICE：强制返回当前 user_id，仅可见自身 owner 的 entry。
        """
        if self.scope == Scope.GLOBAL:
            return None
        return self.owner_user_id

    def check_entry_access(self, entry: Any, *, action: str) -> None:
        """校验单实体访问：GLOBAL 放行；SERVICE 仅允许自身 service 且 owner 一致，越权 403。"""
        if self.scope == Scope.GLOBAL:
            return
        if entry.service_name != self.service_name:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此元数据",
            )
        if getattr(entry, "owner_user_id", None) != self.owner_user_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="无权访问此元数据",
            )


async def get_meta_scope(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    is_superuser: Annotated[bool, Depends(get_superuser_flag)],
) -> MetaScope:
    """统一权限 Scope 依赖：所有 Meta 操作通过它做权限判定。"""
    return MetaScope(user=current_user, is_superuser=is_superuser)
