"""Domain errors with stable codes (A3).

Services raise these; routes map the ``code`` to an HTTP status and a Persian
message from one table. A code never changes meaning, and a message is never
invented at the call site, so the panel and the machine API cannot drift apart
in how they describe the same failure.
"""

from typing import Any


class DomainError(Exception):
    """Base domain error: a stable ``code`` plus optional safe ``context``."""

    code = "domain-error"

    def __init__(self, message: str | None = None, **context: Any):
        super().__init__(message or self.code)
        self.context = context


class PermissionDenied(DomainError):
    """The acting role may not perform the action."""

    code = "forbidden"


class NotFound(DomainError):
    """The referenced row does not exist."""

    code = "not-found"


class Conflict(DomainError):
    """The row is not in a state that allows this transition."""

    code = "conflict"


class ValidationFailed(DomainError):
    """Input did not satisfy a domain rule."""

    code = "validation-failed"


class CredentialError(DomainError):
    """A credential could not be resolved (never carries the credential)."""

    code = "credential-error"


#: code -> (HTTP status, Persian message). The single place routes look up.
ERROR_MESSAGES: dict[str, tuple[int, str]] = {
    "forbidden": (403, "دسترسی لازم را ندارید."),
    "not-found": (404, "یافت نشد."),
    "conflict": (409, "این عملیات با وضعیت فعلی سازگار نیست."),
    "validation-failed": (422, "ورودی نامعتبر است."),
    "credential-error": (422, "اطلاعات اعتبار نامعتبر یا ناقص است."),
    "not-in-review": (409, "رکورد در وضعیت بررسی نیست."),
    "not-regenerable": (409, "تولید دوباره برای این رکورد ممکن نیست."),
    "not-retryable": (409, "ارسال قابل تلاش مجدد نیست."),
    "not-awaiting": (409, "این ارسال در انتظار تأیید نیست."),
    "not-cancellable": (409, "ارسال در حال ارسال یا ارسال‌شده قابل لغو نیست."),
    "not-sent": (409, "فقط ارسال‌های تحویل‌شده را می‌توان از کانال حذف کرد."),
    "missing-message-id": (422, "این ارسال شناسهٔ پیام ندارد، پس حذف از کانال ممکن نیست."),
    "channel-not-found": (404, "کانال این ارسال یافت نشد."),
    "no-token": (422, "توکن ربات این کانال در دسترس نیست؛ حذف پیام انجام نشد."),
    "delete-refused": (409, "سکو حذف پیام را نپذیرفت؛ پیام هنوز در کانال است."),
    "delete-unavailable": (502, "ارتباط با سکو برقرار نشد؛ پیام هنوز در کانال است، دوباره تلاش کنید."),
    "weak-password": (422, "گذرواژه باید حداقل ۸ نویسه باشد."),
    "bad-current-password": (401, "گذرواژهٔ فعلی نادرست است."),
    "duplicate-username": (422, "نام کاربری تکراری است."),
    "bad-role": (422, "نقش نامعتبر است."),
    "role-not-found": (422, "نقش یافت نشد."),
    "self-toggle": (422, "نمی‌توانید حساب خودتان را غیرفعال کنید."),
    "self-delete": (422, "نمی‌توانید حساب خودتان را حذف کنید."),
    "last-admin": (422, "آخرین ادمین فعال قابل حذف نیست."),
    "duplicate-name": (422, "نام تکراری است."),
    "duplicate-version": (422, "نسخه تکراری است."),
    "bad-action": (422, "عملیات نامعتبر است."),
    "unknown-category": (422, "دستهٔ انتخاب‌شده وجود ندارد."),
    "empty-name": (422, "نام را وارد کنید."),
    "duplicate-field": (422, "رشته‌ای با این نام از قبل وجود دارد."),
    "field-in-use": (409, "این رشته در رکوردها به کار رفته و تا وقتی آن رکوردها وجود دارند قابل حذف نیست."),
    "bad-platform": (422, "سکوی نامعتبر است."),
    "no-platform-token": (422, "توکن ربات این سکو پیکربندی نشده است."),
    "duplicate-channel": (422, "این کانال قبلاً ثبت شده است."),
    "bad-result": (422, "پاسخ سکو شناسهٔ معتبر نداشت."),
    "bad-target": (422, "شناسه یا نام کاربری کانال نامعتبر است."),
    "model_id does not exist": (422, "مدل انتخاب‌شده وجود ندارد."),
    "prompt_id does not exist": (422, "پرامپت انتخاب‌شده وجود ندارد."),
    "domain-error": (422, "درخواست معتبر نیست."),
}


def status_for(code: str, default: int = 422) -> int:
    """HTTP status for a domain error code."""
    return ERROR_MESSAGES.get(code, (default, ERROR_MESSAGES["domain-error"][1]))[0]


def message_for(code: str, default: str | None = None) -> str:
    """Persian message for a domain error code (one place, never a traceback)."""
    if default is not None and code not in ERROR_MESSAGES:
        return default
    # [1], not the whole entry. This used to return the (status, message) tuple
    # for every KNOWN code, so every panel error rendered as
    # "(422, 'دستهٔ انتخاب‌شده وجود ندارد.')" instead of the sentence -- and
    # because only known codes hit that branch, the unknown-code fallback still
    # looked right, which is why it went unnoticed. The default is the full
    # tuple too, for the same reason: indexing a bare string would yield a
    # character.
    return ERROR_MESSAGES.get(code, ERROR_MESSAGES["domain-error"])[1]


def from_value_error(exc: ValueError, default_message: str | None = None) -> tuple[int, str]:
    """Map a service ``ValueError`` (``str(exc)`` is the code) to status+message.

    Services still raise plain ``ValueError`` with stable codes for
    backwards compatibility; this maps them centrally so every route answers
    the same way without repeating a lookup table. A code carrying a reason
    suffix (``delete-refused: MESSAGE_TOO_OLD``) maps on the part before the
    colon, so the operator sees one clear sentence per cause instead of a raw
    platform string.
    """
    code = str(exc)
    if code.startswith("unresolvable:"):
        return 422, f"کانال یافت نشد: {code[len('unresolvable: '):]}"
    base = code.split(":", 1)[0].strip()
    return status_for(base), message_for(base, default_message or code)