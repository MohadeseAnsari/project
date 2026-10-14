"""Dashboard figures: one aggregation across three services.

The panel used to assemble these counts itself, calling the record, delivery
and model services in turn. That is cross-service composition sitting in the
presentation layer, which is why the panel's own layering rule flagged it. It
lives here instead, so the panel asks one service a question and the answer is
identical on first load and on every poll.
"""

from datetime import datetime

from app.config import settings
from app.services import deliveries as delivery_service
from app.services import models as model_service
from app.services import records as record_service


async def live_counters(session) -> dict:
    """The dashboard's volatile region: counters, worker leases, model health.

    "Volatile" is the point: everything here is a cheap ``GROUP BY`` count or
    a lease timestamp, so it can be re-fetched every few seconds. The 7-day
    chart, the event timeline and the channel table are deliberately NOT here —
    they do not change over that window.

    Returns a plain dict so the panel needs no ORM access.
    """
    counts = await record_service.record_status_counts(session)
    delivery_counts = await delivery_service.delivery_status_counts(session)
    heartbeat = await record_service.worker_heartbeat(session)
    worker_fresh = bool(
        heartbeat
        and (datetime.utcnow() - heartbeat).total_seconds() < settings.worker_lease_minutes * 60
    )
    return {
        "kpi_review": await record_service.needs_review_count(session),
        "kpi_queued": counts.get("ready", 0) + counts.get("processing", 0),
        "kpi_sent": delivery_counts.get("sent", 0),
        "kpi_failed": counts.get("failed", 0) + delivery_counts.get("failed", 0),
        "worker_fresh": worker_fresh,
        "pending_deliveries": delivery_counts.get("pending", 0),
        "model_health": await model_service.active_model_health(session),
    }


async def sidebar_badges(session) -> dict:
    """The two sidebar counters: records awaiting review, deliveries needing action."""
    review_total = await record_service.needs_review_count(session)
    counts = await delivery_service.delivery_status_counts(session)
    gated_total = counts.get("failed", 0) + counts.get("awaiting_confirmation", 0)
    return {"review_count": review_total or None, "gated_count": gated_total or None}
