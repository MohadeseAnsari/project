"""Audit domain service: who did what, when — for records and global views.

Single implementation used by the panel routes, the record history timeline,
and the dashboard's recent-events list. Nothing renders or returns Persian
text: routes map actions to display labels, services own the queries.
"""

from sqlalchemy import select

from app.db.models import AuditLog, PanelUser


async def record_event(
    session, user_id: int | None, action: str, main_id: int | None = None, detail: str | None = None
) -> AuditLog:
    """Write one audit row and commit it.

    Commits on its own (as before) so a route's audit survives a later error
    path; ``detail`` must never contain a credential — panel services pass
    ids, names and statuses only.
    """
    entry = AuditLog(user_id=user_id, action=action, main_id=main_id, detail=detail)
    session.add(entry)
    await session.commit()
    await session.refresh(entry)
    return entry


async def recent_events(session, limit: int = 4) -> list[dict]:
    """The newest audit rows with their usernames (dashboard timeline)."""
    rows = (
        await session.execute(select(AuditLog).order_by(AuditLog.id.desc()).limit(limit))
    ).scalars().all()
    events = []
    for entry in rows:
        actor = await session.get(PanelUser, entry.user_id) if entry.user_id else None
        events.append(
            {
                "action": entry.action,
                "main_id": entry.main_id,
                "username": actor.username if actor else None,
                "created_at": entry.created_at,
                "detail": entry.detail,
            }
        )
    return events