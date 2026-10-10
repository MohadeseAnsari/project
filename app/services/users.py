"""Console user management: listing, roles, activation, passwords, deletion.

Single implementation for the admin-only panel pages. Passwords only ever
travel as argon2 hashes — callers hash first (see ``app.panel.auth``), so
plaintext never reaches storage code. Raises plain ``ValueError`` with
stable messages; routes map them to error pages.
"""

from sqlalchemy import func, select

from app.db.models import AuditLog, PanelUser, Role


async def list_users(session) -> list[dict]:
    """All console users with role names for the management table."""
    rows = (await session.execute(select(PanelUser).order_by(PanelUser.id))).scalars().all()
    users = []
    for user in rows:
        role = await session.get(Role, user.role_id)
        users.append(
            {
                "id": user.id,
                "username": user.username,
                "role": role.name if role else "—",
                "created_at": user.created_at,
                "is_active": user.is_active,
            }
        )
    return users


async def list_roles(session) -> list[Role]:
    """All roles in id order (role picker)."""
    return list((await session.execute(select(Role).order_by(Role.id))).scalars().all())


async def get_user(session, user_id: int) -> PanelUser | None:
    """Fetch one user by id (routes 404 the same way)."""
    return await session.get(PanelUser, user_id)


async def _role_or_raise(session, role: str) -> Role:
    if role not in ("admin", "editor", "viewer"):
        raise ValueError("bad-role")
    role_row = (await session.execute(select(Role).where(Role.name == role))).scalar_one_or_none()
    if not role_row:
        raise ValueError("role-not-found")
    return role_row


async def create_user(session, *, username: str, password_hash: str, role: str) -> PanelUser:
    """Insert a console user (already-hashed password).

    Raises:
        ValueError: ``bad-role`` / ``role-not-found`` for unknown roles,
            ``duplicate-username`` when the name is taken.
    """
    role_row = await _role_or_raise(session, role)
    row = PanelUser(username=username.strip(), password_hash=password_hash,
                    role_id=role_row.id, is_active=True)
    session.add(row)
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise ValueError("duplicate-username")
    await session.refresh(row)
    return row


async def change_role(session, row: PanelUser, role: str) -> PanelUser:
    """Move a user to another role.

    Raises:
        ValueError: ``bad-role`` / ``role-not-found`` as in creation.
    """
    role_row = await _role_or_raise(session, role)
    row.role_id = role_row.id
    await session.commit()
    await session.refresh(row)
    return row


async def toggle_user(session, row: PanelUser, *, actor_id: int) -> PanelUser:
    """Flip a user's active switch (never deletes).

    Raises:
        ValueError: ``self-toggle`` when an admin targets their own account.
    """
    if row.id == actor_id:
        raise ValueError("self-toggle")
    row.is_active = not row.is_active
    await session.commit()
    await session.refresh(row)
    return row


async def set_password(session, row: PanelUser, password_hash: str) -> PanelUser:
    """Replace a user's password hash (admin reset and self-change share this)."""
    row.password_hash = password_hash
    await session.commit()
    await session.refresh(row)
    return row


async def active_admin_count(session) -> int:
    """Active admins remaining (delete safeguard denominator)."""
    admin_role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if admin_role is None:
        return 0
    return (
        await session.execute(
            select(func.count()).select_from(PanelUser).where(
                PanelUser.role_id == admin_role.id, PanelUser.is_active.is_(True)
            )
        )
    ).scalar_one()


async def user_audit_count(session, user_id: int) -> int:
    """Audit rows authored by a user (decides deactivate vs hard-delete)."""
    return (
        await session.execute(
            select(func.count()).select_from(AuditLog).where(AuditLog.user_id == user_id)
        )
    ).scalar_one()


async def audit_trail(
    session,
    *,
    action: str | None = None,
    username: str | None = None,
    main_id: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> tuple[list[dict], int]:
    """Filtered audit events with usernames, newest first, plus the total."""
    count_query = select(func.count()).select_from(AuditLog)
    list_query = select(AuditLog)
    if username:
        user_ids = (
            await session.execute(select(PanelUser.id).where(PanelUser.username == username))
        ).scalars().all()
        count_query = count_query.where(AuditLog.user_id.in_(user_ids))
        list_query = list_query.where(AuditLog.user_id.in_(user_ids))
    if action:
        count_query = count_query.where(AuditLog.action == action)
        list_query = list_query.where(AuditLog.action == action)
    if main_id is not None:
        count_query = count_query.where(AuditLog.main_id == main_id)
        list_query = list_query.where(AuditLog.main_id == main_id)
    total = (await session.execute(count_query)).scalar_one()
    rows = (
        await session.execute(
            list_query.order_by(AuditLog.id.desc()).limit(page_size).offset((page - 1) * page_size)
        )
    ).scalars().all()
    events = []
    for entry in rows:
        actor = await session.get(PanelUser, entry.user_id) if entry.user_id else None
        events.append({
            "created_at": entry.created_at,
            "username": actor.username if actor else "سیستم (worker)",
            "action": entry.action,
            "main_id": entry.main_id,
            "detail": entry.detail or "—",
        })
    return events, total


async def audit_usernames(session) -> list[str]:
    """Distinct usernames for the audit filter picker."""
    return [
        row[0]
        for row in (await session.execute(select(PanelUser.username).order_by(PanelUser.username))).all()
    ]


async def delete_user(session, row: PanelUser, *, actor_id: int) -> str:
    """Delete a console user with safeguards; return ``deactivated``/``deleted``.

    An admin cannot delete their own account, and the last remaining active
    admin cannot be deleted. Because ``audit_logs.user_id`` is ``ON DELETE
    SET NULL``, a user WITH audit history is kept as a deactivated row so
    the trail keeps its author names; only history-free users are
    hard-deleted.

    Raises:
        ValueError: ``self-delete`` for one's own account, ``last-admin``
            when no active admin would remain.
    """
    if row.id == actor_id:
        raise ValueError("self-delete")
    role = await session.get(Role, row.role_id)
    if (
        role is not None
        and role.name == "admin"
        and row.is_active
        and await active_admin_count(session) <= 1
    ):
        raise ValueError("last-admin")
    if await user_audit_count(session, row.id) > 0:
        row.is_active = False
        await session.commit()
        await session.refresh(row)
        return "deactivated"
    await session.delete(row)
    await session.commit()
    return "deleted"
