"""Роли и права (ТЗ §35).

Три роли. ADMIN может всё; OPERATOR ведёт работу, но не трогает пользователей
и аккаунты; VIEWER только смотрит. Проверка идёт по уровню, а не по списку
разрешений: набор ролей закрытый, и матрица прав здесь была бы сложнее самой
задачи.
"""

from __future__ import annotations

from app.core.errors import PermissionDeniedError
from app.models import UserRole

_LEVELS: dict[UserRole, int] = {
    UserRole.VIEWER: 0,
    UserRole.OPERATOR: 1,
    UserRole.ADMIN: 2,
}


def has_at_least(role: UserRole, required: UserRole) -> bool:
    return _LEVELS[role] >= _LEVELS[required]


def require_role(role: UserRole, required: UserRole) -> None:
    if not has_at_least(role, required):
        raise PermissionDeniedError(
            f"Действие доступно роли {required.value} и выше, у вас {role.value}"
        )
