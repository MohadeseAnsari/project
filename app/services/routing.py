"""Routing domain service: the auditable field -> category -> channel chain.

The panel's routing preview and the machine ``GET /routing-preview`` endpoint
share this one implementation, so "where would this go?" has exactly one
answer. The underlying resolution stays in ``app.messaging.dispatcher``
(the sender's own logic) — this service is the single entry point both HTTP
layers call, never a second copy of the chain.
"""

from app.messaging.dispatcher import preview_routing


async def preview(
    session, *, field: str | None = None, record_id: int | None = None
) -> dict:
    """Preview the routing chain for a raw field name or an existing record.

    Raises:
        ValueError: exactly one of ``field``/``record_id`` must be given.
        LookupError: unknown record id.
    """
    return await preview_routing(session, field=field, record_id=record_id)