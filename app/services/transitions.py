"""The ONE place that defines allowed state transitions (PART E1).

Records:  ready -> processing -> review -> approved -> published,
          plus rejected (from review) and failed (from processing/review).
          processing -> ready is Worker stale-job recovery; failed -> review
          is the panel regenerate action.

Deliveries: awaiting_confirmation -> pending -> sending -> sent,
          plus failed (from sending, retryable back to pending) and
          deleted_from_channel (from sent, the operator's delete action).

No route sets a status directly: routes (machine API and panel) call the
domain services, and the services call :func:`assert_record_transition` /
:data:`RECORD_TRANSITIONS` here before any status write. The Workers use the
same maps so the whole system shares one state machine.

Timestamps travel with the transitions in the services:
``approved_at`` on review->approved, ``published_at`` on ->published,
``sent_at`` on ->sent.
"""

#: Allowed record transitions: current status -> set of next statuses.
#:
#: ``review -> published`` exists for deployments running with the human gate
#: OFF (``require_human_confirmation=false``): there the first successful
#: send IS the decision point, so a record goes straight from review to
#: published. With the gate ON, approval moves it to ``approved`` first.
RECORD_TRANSITIONS: dict[str, set[str]] = {
    "ready": {"processing"},
    "processing": {"review", "failed", "ready"},  # ->ready: stale-job recovery
    "review": {"approved", "rejected", "failed", "published"},  # ->failed: regenerate
    "approved": {"published"},
    "published": set(),  # terminal
    "failed": {"review"},  # regenerate
    "rejected": set(),  # terminal
}

#: Allowed delivery transitions.
DELIVERY_TRANSITIONS: dict[str, set[str]] = {
    "awaiting_confirmation": {"pending"},
    "pending": {"sending"},
    "sending": {"sent", "failed", "pending"},  # ->pending: retry/backoff/recovery
    "sent": {"deleted_from_channel"},
    "failed": {"pending"},  # retry (manual release)
    "deleted_from_channel": set(),  # terminal
}

#: Persian labels for the panel status badges (single source for templates).
STATUS_FA: dict[str, str] = {
    "ready": "در صف، منتظر worker",
    "processing": "در حال تولید خبر",
    "review": "در انتظار بازبینی انسانی",
    "approved": "تأیید شده",
    "published": "منتشر شده در کانال",
    "failed": "تولید ناموفق",
    "rejected": "رد شده",
}


class InvalidTransition(ValueError):
    """A status change outside the allowed state machine."""

    def __init__(self, entity: str, current: str, new: str):
        super().__init__(f"invalid {entity} transition: {current} -> {new}")
        self.entity = entity
        self.current = current
        self.new = new


def assert_record_transition(current: str, new: str) -> None:
    """Raise :class:`InvalidTransition` unless current -> new is allowed."""
    if new not in RECORD_TRANSITIONS.get(current, set()):
        raise InvalidTransition("record", current, new)


def assert_delivery_transition(current: str, new: str) -> None:
    """Raise :class:`InvalidTransition` unless current -> new is allowed."""
    if new not in DELIVERY_TRANSITIONS.get(current, set()):
        raise InvalidTransition("delivery", current, new)
