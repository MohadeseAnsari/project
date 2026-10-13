"""Console user management: listing, roles, activation, passwords, deletion.

Single implementation for the admin-only panel pages. Passwords only ever
travel as argon2 hashes — callers hash first (see ``app.panel.auth``), so
plaintext never reaches storage code. Raises plain ``ValueError`` with
stable messages; routes map them to error pages.
"""

from sqlalchemy import func, select

from app.db.models import AuditLog, PanelUser, Role
from app.services.passwords import hash_password, verify_password


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


async def get_user_by_username(session, username: str) -> PanelUser | None:
    """Look a user up by exact username (panel login form)."""
    return (
        await session.execute(select(PanelUser).where(PanelUser.username == username))
    ).scalar_one_or_none()


async def get_active_user_with_role(session, user_id: int) -> PanelUser | None:
    """Load a logged-in user with its role attached, or None.

    Panel session resolution: an unknown, inactive, or role-less user is
    treated as anonymous. The role object is loaded explicitly because the
    panel renders it after the session closes (no lazy loading there).
    """
    user = await session.get(PanelUser, user_id)
    if not user or not user.is_active:
        return None
    role = await session.get(Role, user.role_id)
    if role is None:
        return None
    user.role = role
    return user


async def ensure_initial_admin(username: str, password: str) -> str | None:
    """Create the first admin from env values when no users exist yet.

    Runs at panel startup only. The password is hashed before storage
    (argon2, never plaintext); when no password is configured nothing is
    created and login stays disabled until it is. Returns the created
    username, or None when nothing was done.
    """
    import logging

    from app.db.connection import async_session

    logger = logging.getLogger(__name__)
    if not password:
        logger.warning("PANEL_ADMIN_PASSWORD is empty; panel login is disabled")
        return None
    async with async_session() as session:
        existing = (await session.execute(select(PanelUser).limit(1))).scalars().first()
        if existing:
            return None
        role = (
            await session.execute(select(Role).where(Role.name == "admin"))
        ).scalar_one_or_none()
        if not role:
            role = Role(name="admin", description="Full access")
            session.add(role)
            await session.flush()
        session.add(
            PanelUser(
                username=username,
                password_hash=hash_password(password),
                role_id=role.id,
                is_active=True,
            )
        )
        await session.commit()
        logger.info("Created initial panel admin username=%s", username)
        return username


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


async def create_user_with_password(session, *, username: str, password: str, role: str) -> PanelUser:
    """Create a user from a plaintext password; hashing happens HERE.

    The panel (and the machine API) never hold the hashing step: plaintext
    enters this function and only the argon2 hash is ever stored.

    Raises:
        ValueError: ``weak-password`` (min length), ``bad-role`` /
            ``role-not-found``, ``duplicate-username``.
    """
    if len(password or "") < 8:
        raise ValueError("weak-password")
    return await create_user(
        session, username=username, password_hash=hash_password(password), role=role
    )


async def reset_password(session, row: PanelUser, new_password: str) -> PanelUser:
    """Set a new password (admin reset); hashing happens HERE.

    No ``session.refresh()`` on purpose: the session uses
    ``expire_on_commit=False`` so the row is already current, and refreshing
    would expire the ``role`` relationship — which the panel reads while
    rendering, outside async context (lazy loading would raise).

    Raises:
        ValueError: ``weak-password`` when shorter than 8 characters.
    """
    if len(new_password or "") < 8:
        raise ValueError("weak-password")
    row.password_hash = hash_password(new_password)
    await session.commit()
    return row


async def change_own_password(
    session, row: PanelUser, *, current_password: str, new_password: str
) -> PanelUser:
    """Self-service password change after proving the current one.

    See :func:`reset_password` for why the row is not refreshed.

    Raises:
        ValueError: ``bad-current-password`` when the current one is wrong,
            ``weak-password`` when the new one is shorter than 8 characters.
    """
    if not verify_password(row.password_hash, current_password or ""):
        raise ValueError("bad-current-password")
    if len(new_password or "") < 8:
        raise ValueError("weak-password")
    row.password_hash = hash_password(new_password)
    await session.commit()
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
