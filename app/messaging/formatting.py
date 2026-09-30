"""Render a generated article into the text published to a channel.

The shape is fixed by product decision:

    📰 *Title*

    📰 lead

    📄 body

    📢 @channel

Each section is introduced by an emoji so the article is skimmable in a busy
channel, the title is wrapped in a single set of asterisks (Bale and Telegram
both render that as bold), and the destination channel's username is appended
so readers can tell which channel an article came from.

Escaping matters here: article text is LLM output that can legitimately contain
asterisks or underscores, which would otherwise be interpreted as markup and
corrupt the formatting. ``escape_markdown`` neutralises exactly the characters
both platforms treat specially.
"""

# Both Bale and Telegram cap a single text message at 4096 characters. The cap
# is applied to the rendered body, not the whole message, so the title and
# footer always survive truncation.
MAX_MESSAGE_LENGTH = 4096

_TRUNCATION_SUFFIX = "\n\n…"


def escape_markdown(text: str) -> str:
    """Escape the markdown characters that Bale and Telegram interpret.

    Applied to every generated fragment so LLM output containing ``*`` or ``_``
    is shown literally instead of silently turning into emphasis.
    """
    out = []
    for ch in text:
        if ch in ("\\", "*", "_", "[", "]", "(", ")", "~", "`"):
            out.append("\\")
        out.append(ch)
    return "".join(out)


def _clean(value: object) -> str:
    """Return a stripped string, or ``""`` for missing/non-string values.

    ``output_json`` is JSONB written by the generation Worker, so a missing or
    wrongly-typed key must degrade to an omitted section rather than raising
    inside the sender loop.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


def _truncate(text: str, limit: int) -> str:
    """Shorten ``text`` to ``limit`` characters, marking that it was cut."""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(_TRUNCATION_SUFFIX))].rstrip() + _TRUNCATION_SUFFIX


def render_article(
    output_json: dict | None,
    channel_username: str | None = None,
    max_length: int = MAX_MESSAGE_LENGTH,
) -> str:
    """Build the channel message for one generated article.

    Args:
        output_json: The ``{title, lead, body}`` payload stored on the record.
        channel_username: Username of the destination channel, appended as a
            footer (``@name``). Omitted when the channel has no username.
        max_length: Hard cap on the rendered message.

    Returns:
        The ready-to-send message. Returns ``""`` when the article has no
        usable content, which the caller treats as "nothing to send" rather
        than posting an empty message.
    """
    data = output_json if isinstance(output_json, dict) else {}

    sections: list[str] = []
    title = _clean(data.get("title"))
    lead = _clean(data.get("lead"))
    body = _clean(data.get("body"))

    if title:
        sections.append(f"📰 *{escape_markdown(title)}*")
    if lead:
        sections.append(f"📰 {escape_markdown(lead)}")
    if body:
        sections.append(f"📄 {escape_markdown(body)}")

    if not sections:
        return ""

    if channel_username:
        sections.append(f"📢 {escape_markdown(channel_username)}")

    return _truncate("\n\n".join(sections), max_length)
