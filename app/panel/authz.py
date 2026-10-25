"""Authorization in ONE place (A4).

Three roles, one table, one function. Routes ask this module instead of
re-implementing role checks, so "who may do what" has a single answer that
``tests/test_authz_matrix.py`` pins against the documented matrix.

| role | capabilities |
|---|---|
| `viewer` | read-only: every page, every mutation 403 |
| `editor` | review: record text/approve/reject, delivery retry/cancel/release, submit |
| `admin` | everything above, plus models/prompts/channels/routing/settings and users + the audit trail |

Fail closed: an unknown role (``None``, a typo, a role row deleted by
mistake) gets no permissions at all rather than defaulting to access.
"""

from enum import Enum


class Action(str, Enum):
    """Every guarded capability, named once."""

    VIEW_RECORDS = "view_records"
    REVIEW_RECORD = "review_record"
    RETRY_DELIVERY = "retry_delivery"
    SUBMIT_RECORD = "submit_record"
    MANAGE_CONFIG = "manage_config"
    MANAGE_USERS = "manage_users"
    VIEW_AUDIT = "view_audit"


#: The whole policy: role -> actions it may perform.
ROLE_ACTIONS: dict[str, frozenset[Action]] = {
    "viewer": frozenset({Action.VIEW_RECORDS}),
    "editor": frozenset({
        Action.VIEW_RECORDS,
        Action.REVIEW_RECORD,
        Action.RETRY_DELIVERY,
        Action.SUBMIT_RECORD,
    }),
    "admin": frozenset({
        Action.VIEW_RECORDS,
        Action.REVIEW_RECORD,
        Action.RETRY_DELIVERY,
        Action.SUBMIT_RECORD,
        Action.MANAGE_CONFIG,
        Action.MANAGE_USERS,
        Action.VIEW_AUDIT,
    }),
}


def can(role: str | None, action: Action) -> bool:
    """Whether ``role`` may perform ``action`` (false for unknown roles)."""
    return action in ROLE_ACTIONS.get(role or "", frozenset())


def require(role: str | None, action: Action) -> None:
    """Raise :class:`PermissionDenied` when ``role`` may not perform ``action``.

    Routes call this (usually via ``_require_role`` in ``app.panel.app``) and
    map the raised code to a 403 page, so the check itself stays in one file.
    """
    from app.services.errors import PermissionDenied

    if not can(role, action):
        raise PermissionDenied("forbidden")