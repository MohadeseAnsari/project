"""Delivery queue logic: what still needs sending, claiming it, recording outcomes.

The unit of work is a ``Delivery`` — one (article, channel) pair. Two functions
answer the two halves of the loop:

- ``ensure_pending_deliveries`` turns *processed* articles into pending sends.
  A processed article is a ``main`` row in ``review`` (generation finished,
  ``output_json`` populated). It is not a flag anywhere: "processed but not
  sent" is defined by the absence of a ``sent``/``failed`` delivery for each
  routed channel. Routing is MULTI-category (v2-combined): every qualifying
  ``main_category_matches`` row (score >= ``MIN_CATEGORY_RELEVANCE``) fans the
  article out to that category's channels; a record classified only as
  ``other`` qualifies for nothing, so it never auto-publishes.
- ``claim_next_delivery`` takes the oldest due delivery and marks it
  ``sending``, so the send happens at most once per attempt even if several
  sender replicas are ever run.

Claiming uses ``FOR UPDATE SKIP LOCKED``, mirroring ``app.worker.worker``, and
sends are retried with exponential backoff up to ``sender_max_attempts``.
"""

import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db.models import Category, CategoryChannelRoute, Channel, Delivery, Main, MainCategoryMatch
from app.messaging.formatting import render_article
from app.messaging.platforms import DeliveryError, PlatformClient, client_for_channel
from app.news.isced import OTHER_CATEGORY
from app.services.transitions import assert_delivery_transition, assert_record_transition

logger = logging.getLogger(__name__)

# Error text is stored for manual inspection; provider responses are already
# truncated by the clients, and this keeps a pathological message from bloating
# the row.
_MAX_ERROR_LENGTH = 2000


def _backoff_delay(attempts: int) -> float:
    """Seconds to wait before attempt ``attempts + 1``.

    Exponential from ``sender_backoff_base_seconds`` (10s, 20s, 40s, 80s...), so
    a provider outage is not hammered while a one-off failure is still retried
    within the same minute.
    """
    return settings.sender_backoff_base_seconds * (2 ** max(0, attempts - 1))


async def preview_routing(
    session, *, field: str | None = None, record_id: int | None = None
) -> dict:
    """Preview the routing chain for a raw field name or an existing record.

    Shared by the machine preview endpoint and the panel routing page, so
    both show the same answer: the resolved category and the channels (with
    the category name that reached each of them) the sender would use right
    now. For records the category is the stored primary (what the Worker
    stored / the reviewer chose); channels are resolved live because routes
    may have changed since processing.

    Classification now happens INSIDE the generation call (v2-combined), so
    a bare field name no longer resolves to a category of its own: what can
    be shown for it is the legacy stored field mapping (curated rows, if
    any) — ``category``/``matched_keyword`` may be ``None``. There is no
    fallback to rule-based matching: that classifier is retired.

    Raises:
        ValueError: exactly one of ``field``/``record_id`` must be given.
        LookupError: unknown record id.
    """
    from app.db.models import Field
    from app.fields.normalization import normalize_field

    if (field is None) == (record_id is None):
        raise ValueError("pass exactly one of field or record_id")

    if record_id is not None:
        result = await session.execute(select(Main).where(Main.id == record_id))
        record = result.scalar_one_or_none()
        if record is None:
            raise LookupError(f"record {record_id} not found")
        category_name = None
        if record.category_id is not None:
            category = await session.get(Category, record.category_id)
            category_name = category.name if category else None
        keyword = None
        if record.field_id is not None:
            field_row = await session.get(Field, record.field_id)
            if field_row is not None:
                keyword = field_row.matched_keyword  # legacy rule-based value
        pairs = await channels_for_article_multi(session, record)
        return {
            "input": record.field,
            "record_id": record.id,
            "category": category_name,
            "matched_keyword": keyword,
            "channels": [
                {
                    "id": channel.id,
                    "platform": channel.platform,
                    "username": channel.username,
                    "title": channel.title,
                    "via": source,
                }
                for channel, source in pairs
            ],
        }

    assert field is not None
    norm = normalize_field(field)
    result = await session.execute(
        select(Field).where(Field.normalized_name == norm)
    )
    stored = result.scalar_one_or_none()
    category_name = keyword = None
    category_id = None
    if stored is not None:
        keyword = stored.matched_keyword  # legacy value; None for new rows
        if stored.category_id is not None:
            category = await session.get(Category, stored.category_id)
            if category is not None:
                category_id, category_name = category.id, category.name
    pairs = await resolve_channels(session, None, category_id)
    return {
        "input": field,
        "record_id": None,
        "category": category_name,
        "matched_keyword": keyword,
        "channels": [
            {
                "id": channel.id,
                "platform": channel.platform,
                "username": channel.username,
                "title": channel.title,
                "via": source,
            }
            for channel, source in pairs
        ],
    }
async def qualifying_categories(session, article: Main) -> list[tuple[int, str]]:
    """The article's qualifying matches: ``(category_id, name)`` score-desc.

    A match qualifies when its category is a CURRENT (non-deprecated) one,
    is not the ``other`` fallback, and its stored score reaches
    ``settings.min_category_relevance``. This is the single definition of
    "which categories route this article" — the channel resolver, the
    delivery predicate and the primary-lookup all delegate to it.
    """
    rows = (
        await session.execute(
            select(MainCategoryMatch.category_id, Category.name)
            .join(Category, Category.id == MainCategoryMatch.category_id)
            .where(
                MainCategoryMatch.main_id == article.id,
                Category.deprecated.is_(False),
                Category.name != OTHER_CATEGORY,
                MainCategoryMatch.score >= settings.min_category_relevance,
            )
            .order_by(MainCategoryMatch.score.desc(), MainCategoryMatch.rank)
        )
    ).all()
    # Duplicates across (main_id, category_id) are impossible (UNIQUE) but a
    # name can only repeat if two rows shared an id — defensive dedup only.
    seen: set[int] = set()
    out: list[tuple[int, str]] = []
    for category_id, name in rows:
        if category_id not in seen:
            seen.add(category_id)
            out.append((category_id, name))
    return out


async def channels_for_article_multi(
    session, article: Main
) -> list[tuple[Channel, str]]:
    """Resolve ``(channel, source)`` pairs from the article's category matches.

    - Query ``main_category_matches`` JOIN ``categories`` where
      ``deprecated = FALSE`` and ``main_id = article.id`` and
      ``score >= settings.min_category_relevance`` (the ``other`` fallback is
      deliberately excluded too: a fallback never auto-publishes, even if
      the threshold were set to 0.0).
    - For each qualifying category, look up ``category_channel_routes`` JOIN
      ``channels`` where ``is_active = TRUE`` and
      ``activated_at <= article.created_at``.
    - Union all channels, dedupe by ``channel.id``.
    - Return sorted by ``channel.id`` with ``source`` = the category name
      that produced it (or ``"multi"`` when several categories route to the
      same channel).

    An article with no qualifying categories (only ``other``, or none at all)
    resolves to NO destinations: it stays in review for manual assignment.
    """
    qualifying = await qualifying_categories(session, article)
    if not qualifying:
        return []
    cat_ids = [category_id for category_id, _name in qualifying]
    name_by_id = dict(qualifying)

    route_rows = (
        await session.execute(
            select(CategoryChannelRoute.category_id, Channel)
            .join(Channel, Channel.id == CategoryChannelRoute.channel_id)
            .where(CategoryChannelRoute.category_id.in_(cat_ids))
            .where(Channel.is_active.is_(True))
            .where(Channel.activated_at <= article.created_at)
            .order_by(Channel.id)
        )
    ).all()

    channel_by_id: dict[int, Channel] = {}
    sources_by_channel: dict[int, list[str]] = {}
    for category_id, channel in route_rows:
        channel_by_id[channel.id] = channel
        names = sources_by_channel.setdefault(channel.id, [])
        name = name_by_id[category_id]
        if name not in names:
            names.append(name)

    pairs: list[tuple[Channel, str]] = []
    for channel_id in sorted(channel_by_id):
        names = sources_by_channel[channel_id]
        pairs.append((channel_by_id[channel_id], names[0] if len(names) == 1 else "multi"))
    return pairs


async def resolve_channels(
    session, since: datetime | None, category_id: int | None
) -> list[tuple[Channel, str]]:
    """Resolve ``(channel, route_source)`` for ONE fixed category.

    Legacy but still necessary: the raw-field routing preview shows what a
    stored/curated field mapping would resolve to. Live delivery resolution
    no longer goes through here — see
    :func:`channels_for_article_multi`.

    A channel only receives news from the moment it was switched on:
    ``since`` is the article's ``created_at`` and a channel qualifies when
    ``activated_at <= since``. ``None`` applies no floor ("where would news
    generated right now go?").
    """
    if category_id is None:
        return []
    routes = list(
        (
            await session.execute(
                select(CategoryChannelRoute.channel_id).where(
                    CategoryChannelRoute.category_id == category_id
                )
            )
        ).scalars().all()
    )
    if not routes:
        return []
    query = select(Channel).where(Channel.id.in_(routes), Channel.is_active.is_(True))
    if since is not None:
        query = query.where(Channel.activated_at <= since)
    channels = list((await session.execute(query.order_by(Channel.id))).scalars().all())
    return [(channel, "category") for channel in channels]


def _has_undelivered_channel():
    """SQL predicate: some qualifying category matches an active channel that
    has no delivery row yet.

    Mirrors :func:`channels_for_article_multi` exactly, which is the whole
    reason it is a correlated EXISTS rather than a Python pass over the batch.
    If the two disagree the sender fails silently in both directions: a
    predicate that is too wide selects articles the resolver will route
    nowhere, and they sit in the batch window forever crowding out articles
    that do need sending; a predicate that is too narrow never selects an
    article whose channel is still missing, so it is never delivered at all.

    The chain is the multi-category version of the old route join:
    ``main_category_matches`` (qualifying only) → ``categories`` (live,
    non-deprecated, never ``other``) → ``category_channel_routes`` →
    ``channels``. The legacy ``route_source='all'`` broadcast is gone — an
    article reaches a channel ONLY through a qualifying category's mapping.

    The activation floor (``channels.activated_at <= main.created_at``) is the
    other half of the agreement: it stops a channel that was just switched on
    from inheriting the whole archive.
    """
    return exists(
        select(1)
        .select_from(MainCategoryMatch)
        .join(Category, Category.id == MainCategoryMatch.category_id)
        .join(CategoryChannelRoute, CategoryChannelRoute.category_id == Category.id)
        .join(Channel, Channel.id == CategoryChannelRoute.channel_id)
        .outerjoin(
            Delivery,
            and_(
                Delivery.main_id == Main.id,
                Delivery.channel_id == Channel.id,
            ),
        )
        .where(
            and_(
                MainCategoryMatch.main_id == Main.id,
                Category.deprecated.is_(False),
                Category.name != OTHER_CATEGORY,
                MainCategoryMatch.score >= settings.min_category_relevance,
                Channel.is_active.is_(True),
                Channel.activated_at <= Main.created_at,
                Delivery.id.is_(None),
            )
        )
        .correlate(Main)
    )


async def ensure_pending_deliveries(session, batch_size: int = 500) -> int:
    """Create pending deliveries for processed articles that lack them.

    An article qualifies when it is ``review``/``approved``/``published`` with
    a usable ``output_json`` AND at least one channel it resolves to has no
    delivery row yet. "Resolves to" means ACTIVE and switched on no later than
    the article, so an article predating a channel is never enqueued for it —
    adding a channel does not send the archive.

    ``approved``/``published`` are included because a record's generated news
    can reach this stage after earlier ones; its delivery is created directly
    as ``pending`` because the human decision already happened.

    The "still needs sending" test is an anti-join expressed in SQL, not a
    Python filter applied after ``LIMIT``. That distinction is load-bearing: if
    the exclusion ran afterwards, the first ``batch_size`` articles would fill
    the window with rows that are already queued, the query would return the
    same window on every subsequent pass, and any article beyond it would never
    be delivered — permanently, not slowly, because the window stays saturated
    as the queue drains. Keeping the exclusion inside the query means the
    ``LIMIT`` only ever counts articles that have real work to do.

    Human gate: when ``settings.require_human_confirmation`` is True AND
    ``settings.auto_publish_on_classification`` is False (both default),
    rows for ``review`` articles are created as ``awaiting_confirmation`` so
    nothing publishes without explicit approval; approving the record moves it
    to ``approved`` and flips its gated deliveries to ``pending``. When the
    gate is off OR auto-publish on classification is True, rows are created
    as ``pending`` directly — EXCEPT for an article with no qualifying
    categories, which queues nothing at all regardless of settings: the
    ``other`` fallback always needs a reviewer (the gate holds even when
    auto-publish is on). The gate only changes the initial status — the
    anti-join/LIMIT selection above is unchanged.

    The per-article ``already`` set below is kept as a race guard for a second
    sender replica inserting between the SELECT and the INSERT; the unique
    constraint on ``(main_id, channel_id)`` is the final backstop.

    Known limitation (audit only): in the multi-category case,
    ``deliveries.category_id`` snapshots the TOP-1 qualifying match's
    category — the row itself proves the (article, channel) pair came from
    per-category routing, not which specific category produced it.

    Returns:
        Number of deliveries created.
    """
    result = await session.execute(
        select(Main)
        .where(Main.status.in_(("review", "approved", "published")))
        .where(Main.output_json.isnot(None))
        .where(_has_undelivered_channel())
        .order_by(Main.id)
        .limit(batch_size)
    )
    articles = list(result.scalars().all())
    if not articles:
        return 0

    created = 0
    for article in articles:
        existing = await session.execute(
            select(Delivery.channel_id).where(Delivery.main_id == article.id)
        )
        already = set(existing.scalars().all())

        # Provenance snapshot: the top-1 qualifying match (audit only).
        primary_category_id = None
        qualifying = await qualifying_categories(session, article)
        if qualifying:
            primary_category_id = qualifying[0][0]

        for channel, route_source in await channels_for_article_multi(session, article):
            if channel.id in already:
                continue
            if not render_article(article.output_json, channel.username):
                # Nothing renderable (empty output_json): record nothing, so a
                # later corrected article can still be delivered.
                logger.warning(
                    "Skipping article id=%s: no renderable content for channel id=%s",
                    article.id,
                    channel.id,
                )
                continue
            # The human gate applies only before the approval decision: a
            # 'review' article queues as awaiting_confirmation (gate on); an
            # already-approved/published article queues directly as pending (the
            # decision already happened). The article status is re-read here on
            # purpose (see note below).
            # the SELECT above, but between that query and this insert a
            # reviewer can approve the article — and approval releases only
            # the deliveries that exist at that moment. Queueing a fresh
            # awaiting_confirmation row afterwards would park it forever: the
            # record is no longer in 'review', so it can never be approved
            # again, and nothing else would ever flip it to pending.
            current = await session.get(Main, article.id)
            current_status = current.status if current is not None else article.status
            gated = current_status == "review" and settings.require_human_confirmation
            session.add(
                Delivery(
                    main_id=article.id,
                    channel_id=channel.id,
                    category_id=primary_category_id,
                    route_source="category",
                    status="awaiting_confirmation" if gated else "pending",
                    attempts=0,
                )
            )
            created += 1

    if created:
        await session.commit()
        logger.info("Queued %d pending deliveries", created)
    return created


async def confirm_deliveries_for_article(session, article_id: int) -> int:
    """Flip an article's ``awaiting_confirmation`` deliveries to ``pending``.

    Called once when a reviewer approves the article. Only the gated status
    moves — ``sent``/``failed``/``pending`` rows are never touched — so
    approving twice is a no-op rather than a duplicate send.

    Returns:
        Number of deliveries flipped.
    """
    result = await session.execute(
        update(Delivery)
        .where(Delivery.main_id == article_id)
        .where(Delivery.status == "awaiting_confirmation")
        .values(status="pending", updated_at=datetime.utcnow())
    )
    await session.commit()
    if result.rowcount:
        logger.info(
            "Confirmed %d deliveries for article id=%s", result.rowcount, article_id
        )
    return result.rowcount or 0


async def recover_stale_deliveries(session) -> int:
    """Return deliveries abandoned in ``sending`` back to ``pending``.

    A send attempt is normally seconds. A ``sending`` row whose ``claimed_at``
    is older than ``sender_lease_minutes`` means the sender Worker died between
    claiming and recording the outcome; re-queueing it is the same at-least-once
    trade-off the generation Worker already makes (a duplicate post in the
    channel in exchange for never losing an article).
    """
    cutoff = datetime.utcnow() - timedelta(minutes=settings.sender_lease_minutes)
    result = await session.execute(
        update(Delivery)
        .where(Delivery.status == "sending")
        .where(Delivery.claimed_at < cutoff)
        .values(status="pending", claimed_at=None, next_attempt_at=datetime.utcnow())
    )
    await session.commit()
    if result.rowcount:
        logger.warning("Recovered %d stale deliveries", result.rowcount)
    return result.rowcount


async def claim_next_delivery(session) -> Delivery | None:
    """Claim the oldest due ``pending`` delivery (FIFO by ``created_at``).

    "Due" means ``next_attempt_at`` has passed, which is what makes backoff work:
    a failed delivery is parked in the future instead of being retried
    immediately.

    Returns:
        The delivery with status ``sending`` and ``attempts`` incremented, or
        ``None`` when nothing is due.
    """
    now = datetime.utcnow()
    result = await session.execute(
        select(Delivery)
        .where(Delivery.status == "pending")
        .where(Delivery.next_attempt_at <= now)
        .order_by(Delivery.created_at, Delivery.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    delivery = result.scalar_one_or_none()
    if delivery:
        assert_delivery_transition(delivery.status, "sending")
        delivery.status = "sending"
        delivery.attempts += 1
        delivery.claimed_at = now
        await session.commit()
        logger.info(
            "Claimed delivery id=%s article=%s channel=%s attempt=%s",
            delivery.id,
            delivery.main_id,
            delivery.channel_id,
            delivery.attempts,
        )
    return delivery


async def load_delivery_context(session, delivery: Delivery) -> tuple[Main, Channel] | None:
    """Load the article and channel for a claimed delivery.

    Returns ``None`` when either side no longer exists (an operator deleted the
    channel); the caller then abandons the delivery rather than retrying a send
    that can never succeed.
    """
    result = await session.execute(
        select(Delivery)
        .where(Delivery.id == delivery.id)
        .options(selectinload(Delivery.article), selectinload(Delivery.channel))
    )
    loaded = result.scalar_one_or_none()
    if loaded is None or loaded.article is None or loaded.channel is None:
        return None
    return loaded.article, loaded.channel


async def mark_sent(session, delivery: Delivery, platform_message_id: int) -> None:
    """Record a successful send; stamp the article published on first send.

    The record moves to ``published`` (approved -> published when the human
    gate ran; review -> published when the gate is off and the first send
    is also the approval-equivalent). ``published_at`` stamps first-wins.
    """
    from sqlalchemy import update

    assert_delivery_transition(delivery.status, "sent")
    delivery.status = "sent"
    delivery.sent_at = datetime.utcnow()
    delivery.platform_message_id = platform_message_id
    delivery.claimed_at = None
    delivery.error_message = None
    delivery.updated_at = datetime.utcnow()
    # Move the article to published (only from a pre-publication status).
    article = await session.get(Main, delivery.main_id)
    if article is not None and article.status in ("review", "approved"):
        assert_record_transition(article.status, "published")
        article.status = "published"
        if article.published_at is None:
            article.published_at = datetime.utcnow()
        article.updated_at = datetime.utcnow()
    await session.execute(
        update(Main)
        .where(Main.id == delivery.main_id)
        .where(Main.published_at.is_(None))
        .values(published_at=datetime.utcnow())
    )
    await session.commit()
    logger.info(
        "Delivered id=%s article=%s channel=%s message_id=%s",
        delivery.id,
        delivery.main_id,
        delivery.channel_id,
        platform_message_id,
    )


async def mark_retry(session, delivery: Delivery, error: str, delay_seconds: float) -> None:
    """Return a failed delivery to ``pending``, scheduled after ``delay_seconds``.

    Keeps the attempt count so the backoff grows and the attempt cap can be
    enforced by the caller.
    """
    assert_delivery_transition(delivery.status, "pending")
    delivery.status = "pending"
    delivery.claimed_at = None
    delivery.error_message = error[:_MAX_ERROR_LENGTH]
    delivery.next_attempt_at = datetime.utcnow() + timedelta(seconds=delay_seconds)
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    logger.warning(
        "Send failed (will retry) delivery id=%s attempt=%s in %.0fs error=%s",
        delivery.id,
        delivery.attempts,
        delay_seconds,
        error,
    )


async def mark_failed(session, delivery: Delivery, error: str) -> None:
    """Park a delivery in ``failed`` with its error, for manual inspection."""
    assert_delivery_transition(delivery.status, "failed")
    delivery.status = "failed"
    delivery.claimed_at = None
    delivery.error_message = error[:_MAX_ERROR_LENGTH]
    delivery.updated_at = datetime.utcnow()
    await session.commit()
    logger.error("Send gave up delivery id=%s attempts=%s error=%s", delivery.id, delivery.attempts, error)


async def deliver_one(
    session,
    delivery: Delivery,
    clients: dict[str, PlatformClient],
) -> bool:
    """Attempt one claimed delivery. Returns ``True`` when it was sent.

    Failure policy (the decision, in one place):
    - success → ``sent``
    - retryable error and attempts remaining → ``pending`` after backoff
    - retryable error with no attempts left, or a permanent error → ``failed``

    A permanent error short-circuits the attempt cap on purpose: an unknown
    ``chat_id`` will still be unknown in ten minutes.
    """
    context = await load_delivery_context(session, delivery)
    if context is None:
        await mark_failed(session, delivery, "article or channel no longer exists")
        return False
    article, channel = context

    text = render_article(article.output_json, channel.username)
    if not text:
        await mark_failed(session, delivery, "article has no renderable content")
        return False

    client, token_error = client_for_channel(channel, clients)
    if client is None:
        await mark_failed(session, delivery, token_error)
        return False

    try:
        message_id = await client.send_text(channel.chat_id, text)
    except DeliveryError as e:
        exhausted = delivery.attempts >= settings.sender_max_attempts
        if not e.retryable or exhausted:
            await mark_failed(session, delivery, str(e))
            return False
        await mark_retry(session, delivery, str(e), _backoff_delay(delivery.attempts))
        return False
    except asyncio.CancelledError:
        # Shutdown mid-send: leave the row in `sending` and let stale-delivery
        # recovery re-queue it, rather than recording a failure that never happened.
        raise
    except Exception as e:  # defensive: an unexpected bug must not kill the loop
        # The attempt cap applies here too. A bug that throws on every attempt
        # would otherwise retry forever, which is the unbounded-retry failure
        # mode the cap exists to prevent -- and one bad article would sit in the
        # queue re-raising instead of being parked for inspection.
        error = f"unexpected error: {e}"
        if delivery.attempts >= settings.sender_max_attempts:
            await mark_failed(session, delivery, error)
        else:
            await mark_retry(session, delivery, error, _backoff_delay(delivery.attempts))
        return False

    await mark_sent(session, delivery, message_id)
    return True
