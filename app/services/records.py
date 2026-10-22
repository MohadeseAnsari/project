"""Record domain service: listing, detail lookup, and review transitions.

Single implementation shared by the machine API (``PATCH /records/{id}``)
and the panel (review form, bulk approve). Raises plain ``ValueError`` with
stable messages; routes map them to HTTP statuses or error pages.

The classification layer (v2-combined) also lives here: everything that
reads or writes ``main_category_matches`` — post-processing the LLM's
combined answer, the record-level matches payload, the reviewer's manual
override, and the per-category stat counts.
"""

from datetime import datetime
import logging

from sqlalchemy import delete, func, or_, select

from app.config import settings
from app.db.models import AuditLog, Category, Channel, Delivery, Main, MainCategoryMatch, Model, PanelUser, Prompt
from app.news.isced import OTHER_CATEGORY
from app.messaging.dispatcher import confirm_deliveries_for_article
from app.services.transitions import STATUS_FA, assert_record_transition

logger = logging.getLogger(__name__)

#: Persian labels for delivery states in the record timeline.
_DELIVERY_FA = {
    "awaiting_confirmation": "در انتظار تأیید انسان",
    "pending": "در صف ارسال",
    "sending": "در حال ارسال",
    "sent": "ارسال شد",
    "failed": "ناموفق",
    "deleted_from_channel": "حذف‌شده از کانال",
}


async def create_thesis_record(
    session,
    *,
    username: str,
    title: str,
    abstract: str,
    field: str,
    university: str,
    table_of_contents: str | None,
    author: str,
    supervisor: str,
    model_id: int | None = None,
    prompt_id: int | None = None,
) -> Main:
    """Insert a ``ready`` thesis job with validated per-record overrides.

    The single intake flow for ``POST /submit`` and the panel submit form:
    both must produce exactly the row the generation Worker polls for.

    Raises:
        ValueError: ``model_id``/``prompt_id`` names a row that does not exist.
            Callers map this to their own 422/error surface (API JSON vs panel
            page) without changing the check itself.
    """
    if model_id is not None and await session.get(Model, model_id) is None:
        raise ValueError("model_id does not exist")
    if prompt_id is not None and await session.get(Prompt, prompt_id) is None:
        raise ValueError("prompt_id does not exist")
    record = Main(
        username=username,
        title=title,
        abstract=abstract,
        field=field,
        university=university,
        table_of_contents=table_of_contents,
        author=author,
        supervisor=supervisor,
        status="ready",
        model_id=model_id,
        prompt_id=prompt_id,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


def record_filters(
    status: str | None = None,
    field: str | None = None,
    university: str | None = None,
    q: str | None = None,
    approved: bool | None = None,
) -> list:
    """Build the shared WHERE clauses for record listings (panel + API).

    ``approved`` is now status-based (the lifecycle owns the truth):
    True -> statuses ``approved``/``published``; False -> everything else
    (still undecided or failed/rejected).
    """
    filters = []
    if status:
        filters.append(Main.status == status)
    if approved is True:
        filters.append(Main.status.in_(("approved", "published")))
    elif approved is False:
        filters.append(Main.status.notin_(("approved", "published")))
    if field:
        filters.append(Main.field.ilike(f"%{field}%"))
    if university:
        filters.append(Main.university.ilike(f"%{university}%"))
    if q:
        like = f"%{q}%"
        filters.append(
            or_(
                Main.title.ilike(like),
                Main.abstract.ilike(like),
                Main.field.ilike(like),
                Main.university.ilike(like),
                Main.author.ilike(like),
                Main.supervisor.ilike(like),
            )
        )
    return filters


def record_ordering(order: str):
    """Newest-first (default) or oldest-first creation order for listings."""
    if order != "oldest":
        return [Main.created_at.desc(), Main.id.desc()]
    return [Main.created_at.asc(), Main.id.asc()]


async def list_records(
    session,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str | None = None,
    field: str | None = None,
    university: str | None = None,
    q: str | None = None,
    order: str = "newest",
    approved: bool | None = None,
) -> tuple[list[Main], int]:
    """Return ``(page rows, filtered total)`` with SQL LIMIT/OFFSET + COUNT."""
    filters = record_filters(status, field, university, q, approved)
    count_query = select(func.count()).select_from(Main)
    if filters:
        count_query = count_query.where(*filters)
    total = (await session.execute(count_query)).scalar_one()
    query = select(Main)
    if filters:
        query = query.where(*filters)
    rows = (
        await session.execute(
            query.order_by(*record_ordering(order))
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
    ).scalars().all()
    return list(rows), total


async def get_record(session, record_id: int) -> Main | None:
    """Fetch one record by id (both layers 404 the same way)."""
    result = await session.execute(select(Main).where(Main.id == record_id))
    return result.scalar_one_or_none()


async def record_detail(session, record: Main) -> dict:
    """Build the extended record payload (API GET/PATCH share this)."""
    from app.db.models import Channel, Delivery

    deliveries_result = await session.execute(
        select(Delivery, Channel)
        .join(Channel, Channel.id == Delivery.channel_id)
        .where(Delivery.main_id == record.id)
        .order_by(Delivery.id)
    )
    return {
        "id": record.id,
        "status": record.status,
        "field": record.field,
        "output_json": record.output_json,
        "error_message": record.error_message,
        "author": record.author,
        "supervisor": record.supervisor,
        "university": record.university,
        "table_of_contents": record.table_of_contents,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "approved_at": record.approved_at,
        "published_at": record.published_at,
        "field_id": record.field_id,
        "category_id": record.category_id,
        "model_id": record.model_id,
        "prompt_id": record.prompt_id,
        "deliveries": [
            {
                "id": delivery.id,
                "channel_id": delivery.channel_id,
                "main_id": delivery.main_id,
                "platform": channel.platform,
                "username": channel.username,
                "status": delivery.status,
                "attempts": delivery.attempts,
                "error_message": delivery.error_message,
                "sent_at": delivery.sent_at,
            }
            for delivery, channel in deliveries_result.all()
        ],
        # The stored multi-category classification (post-processed LLM answer
        # or a reviewer's manual override).
        "category_matches": await record_category_matches(session, record.id),
    }


# ---------------------------------------------------------------------------
# Classification (v2-combined): main_category_matches read/write.
#
# ONE generation call classifies each thesis into up to
# settings.top_n_categories ISCED-F 2013 categories. Everything that touches
# ``main_category_matches`` lives here so the Worker, the panel regenerate
# action and the reviewer's manual override cannot drift apart.
# ---------------------------------------------------------------------------

#: The reason stored on the ``other`` fallback row when nothing qualifies.
NO_AUTO_MATCH_REASON = "no auto-match"


async def _active_categories(session) -> dict[str, Category]:
    """Live classification vocabulary: name -> Category, deprecated excluded.

    Hallucinated names (anything not in here) are rejected by construction.
    """
    rows = (
        await session.execute(select(Category).where(Category.deprecated.is_(False)))
    ).scalars().all()
    return {row.name: row for row in rows}


def _clamp_score(value: float) -> float:
    """Clamp to [0.0, 1.0]; guard against NaN-ish inputs by clamping to 0.0."""
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    if value != value:  # NaN
        return 0.0
    return max(0.0, min(1.0, value))


def _dedupe_and_sort(matches: list[dict]) -> list[dict]:
    """Dedupe by category keeping the highest score, then sort desc."""
    best: dict[str, dict] = {}
    for match in matches:
        name = match["category"]
        if name not in best or match["score"] > best[name]["score"]:
            best[name] = match
    return sorted(best.values(), key=lambda m: (-m["score"], m["category"]))


async def _active_categories_by_code(session) -> dict[int, Category]:
    """Live classification vocabulary: short_code -> Category (deprecated excluded)."""
    rows = (
        await session.execute(
            select(Category).where(
                Category.deprecated.is_(False),
                Category.short_code.isnot(None),
            )
        )
    ).scalars().all()
    return {row.short_code: row for row in rows}


async def normalize_llm_categories(session, output) -> list[dict]:
    """Post-process the LLM's ``categories`` list into trustworthy matches.

    Accepts BOTH the new compact form (``code``/``score`` 0-100/``reason``)
    and the legacy name form (``category``/``score`` 0.0-1.0) for backward
    compat. Defence layers, in order: code/name validation against the live
    (non-deprecated) ``categories`` rows rejects hallucinations; scores are
    clamped; duplicates collapse to the highest score; results sort by score
    desc. Codes/ids are resolved here so callers only insert.

    ``other`` (code 0) is kept in the validated list (the LLM may say it) but
    it never auto-qualifies — callers must treat it as the fallback.
    """
    code_to_category = await _active_categories_by_code(session)
    name_to_category = await _active_categories(session)

    validated: list[dict] = []
    for match in getattr(output, "categories", None) or []:
        # Compact form (v3-compact): code + int score + reason.
        code = getattr(match, "code", None)
        if code is not None:
            category = code_to_category.get(code)
            if category is None:
                continue  # hallucinated / unknown code: always filtered
            raw_score = getattr(match, "score", 0)
            try:
                score_int = int(raw_score)
            except (TypeError, ValueError):
                score_int = 0
            score = max(0.0, min(1.0, score_int / 100.0))
            validated.append({
                "category": category.name,
                "category_id": category.id,
                "score": score,
                "reason": (getattr(match, "reason", None) or "").strip() or None,
                "code": code,
            })
            continue

        # Legacy form (v2-combined): name + float score + reason.
        name = (getattr(match, "category", None) or "").strip()
        category = name_to_category.get(name)
        if category is None:
            continue  # hallucinated / deprecated: always filtered
        validated.append({
            "category": name,
            "category_id": category.id,
            "score": _clamp_score(getattr(match, "score", 0)),
            "reason": (getattr(match, "reason", None) or "").strip() or None,
            "code": getattr(category, "short_code", None),
        })

    # Dedupe by category_id keeping the highest score, then sort desc.
    best: dict[int, dict] = {}
    for match in validated:
        cid = match["category_id"]
        if cid not in best or match["score"] > best[cid]["score"]:
            best[cid] = match
    return sorted(best.values(), key=lambda m: (-m["score"], m["category"]))


async def _delete_matches(session, record_id: int) -> None:
    """Reset a record's classification (regeneration/manual override)."""
    await session.execute(delete(MainCategoryMatch).where(MainCategoryMatch.main_id == record_id))


async def store_generated_classification(
    session,
    record: Main,
    output,
    *,
    model_id: int | None = None,
    prompt_id: int | None = None,
) -> list[dict]:
    """Store the combined call's classification answer for one record.

    Replaces any previous rows (regeneration must not mix answers), keeps the
    qualifying subset (score >= ``min_category_relevance``, exact live
    category names, ``other`` excluded), ranked 1..N, and sets
    ``record.category_id``:

    - with matches → primary = the top match (backward-compatible display);
    - without → exactly one ``other`` fallback row (score 0.0, rank 1,
      reason ``no auto-match``) — no deliveries follow, the record waits for
      a reviewer.

    Returns the qualifying matches (empty when the fallback path ran).
    The caller commits.
    """
    await _delete_matches(session, record.id)

    if settings.store_llm_raw_output:
        try:
            record.llm_raw_output = output.model_dump_json()
        except Exception:  # pragma: no cover - audit only
            pass

    validated = await normalize_llm_categories(session, output)
    qualifying = [
        m for m in validated
        if m["score"] >= settings.min_category_relevance and m["category"] != OTHER_CATEGORY
    ][: max(0, settings.top_n_categories)]

    if qualifying:
        for rank, match in enumerate(qualifying, start=1):
            # Skip the reason for high-confidence matches when configured
            # (Optimization D + feature flag). Default threshold 90.
            if match["score"] * 100 >= settings.skip_reason_above_score:
                reason = None
            else:
                reason = match["reason"]
            session.add(MainCategoryMatch(
                main_id=record.id,
                category_id=match["category_id"],
                score=match["score"],
                rank=rank,
                reason=reason,
                model_id=model_id,
                prompt_id=prompt_id,
            ))
        record.category_id = qualifying[0]["category_id"]
        logger_classified = ", ".join(
            f"{match['category']} ({match['score']:.2f})" for match in qualifying
        )
        logger.info("LLM classified as: %s", logger_classified)
        codes_log = ", ".join(
            f"code={match.get('code', '?')} score={int(round(match['score'] * 100))}"
            for match in qualifying
        )
        logger.info("LLM classified: %s", codes_log)
        if settings.store_llm_raw_output:
            try:
                record.llm_raw_output = output.model_dump_json()
            except Exception:  # pragma: no cover - audit only
                pass
        return qualifying

    other = (await _active_categories(session)).get(OTHER_CATEGORY)
    if other is not None:
        session.add(MainCategoryMatch(
            main_id=record.id,
            category_id=other.id,
            score=0.0,
            rank=1,
            reason=NO_AUTO_MATCH_REASON,
            model_id=model_id,
            prompt_id=prompt_id,
        ))
        record.category_id = other.id
    else:  # defensive: the migration guarantees an ``other`` row exists
        record.category_id = None
    logger.info("No qualifying LLM match: stored \"other\" fallback, no auto-delivery")
    return []


async def record_category_matches(session, record_id: int) -> list[dict]:
    """The record's stored matches with category names (API + panel)."""
    rows = (
        await session.execute(
            select(MainCategoryMatch, Category, Main.category_id)
            .join(Category, Category.id == MainCategoryMatch.category_id)
            .join(Main, Main.id == MainCategoryMatch.main_id)
            .where(MainCategoryMatch.main_id == record_id)
            .order_by(MainCategoryMatch.rank)
        )
    ).all()
    return [
        {
            "category": category.name,
            "category_id": match.category_id,
            "score": match.score,
            "rank": match.rank,
            "reason": match.reason,
            "is_primary": match.category_id == main_category_id,
        }
        for match, category, main_category_id in rows
    ]


async def record_matches_fallback_only(session, record_id: int) -> bool:
    """True when the only match is the ``other`` fallback (needs a reviewer).

    Tells the panel to show the manual-assignment warning instead of scores.
    """
    rows = (
        await session.execute(
            select(MainCategoryMatch.reason)
            .where(MainCategoryMatch.main_id == record_id)
        )
    ).all()
    if not rows:
        return False  # not generated yet — not a failed classification
    return len(rows) == 1 and rows[0][0] == NO_AUTO_MATCH_REASON


async def replace_category_matches(session, record_id: int, matches) -> Main:
    """Reviewer's manual override: replace the stored matches wholesale.

    ``matches`` is a list of {category, score, reason} (schemas/objects both
    work). Validation mirrors the LLM path — exact names, clamped scores,
    dedupe, highest score first — and up to 5 rows may be stored (the DB
    ``rank`` CHECK bound). An empty list clears back to the ``other``
    fallback: a record must never be left with NO classification, or the
    routing predicate could not tell "unclassified" from "not generated".

    The primary category becomes the top match's category.
    """
    record = await get_record(session, record_id)
    if record is None:
        raise ValueError("not-found")
    if len(matches) > 5:
        raise ValueError("too-many-matches")

    vocabulary = await _active_categories(session)
    validated: list[dict] = []
    for match in matches:
        if isinstance(match, dict):
            category_id = match.get("category_id")
            name = (match.get("category") or "").strip()
            score = match.get("score")
            reason = match.get("reason")
        else:
            category_id = getattr(match, "category_id", None)
            name = (getattr(match, "category", None) or "").strip()
            score = getattr(match, "score", None)
            reason = getattr(match, "reason", None)

        if category_id is not None:
            category = await session.get(Category, category_id)
            if category is None:
                raise ValueError(f"unknown-category:{category_id}")
            name = category.name
        else:
            category = vocabulary.get(name)
            if category is None:
                raise ValueError(f"unknown-category:{name}")

        validated.append({
            "category": name,
            "category_id": category.id,
            "score": _clamp_score(score),
            "reason": (reason or "").strip() or None,
        })
    validated = _dedupe_and_sort(validated)[:5]

    await _delete_matches(session, record_id)
    if validated:
        for rank, match in enumerate(validated, start=1):
            session.add(MainCategoryMatch(
                main_id=record.id,
                category_id=match["category_id"],
                score=match["score"],
                rank=rank,
                reason=match["reason"],
            ))
        record.category_id = validated[0]["category_id"]
    else:
        other = vocabulary.get(OTHER_CATEGORY)
        if other is not None:
            session.add(MainCategoryMatch(
                main_id=record.id, category_id=other.id,
                score=0.0, rank=1, reason=NO_AUTO_MATCH_REASON,
            ))
            record.category_id = other.id
    record.updated_at = datetime.utcnow()
    await session.commit()
    await session.refresh(record)
    return record


async def primary_category_snapshot(session, record_ids: list[int]) -> dict[int, dict]:
    """``{record_id: {name, extras}}`` for the records-table badges.

    ``name`` is the primary (rank-1) match's category; ``extras`` is the
    number of ADDITIONAL matches, so the template can render "+N" for N > 0.
    Records without matches (ready/processing/old) map to ``name=None``.
    """
    if not record_ids:
        return {}
    rows = (
        await session.execute(
            select(MainCategoryMatch.main_id, Category.name, MainCategoryMatch.rank)
            .join(Category, Category.id == MainCategoryMatch.category_id)
            .where(MainCategoryMatch.main_id.in_(record_ids))
            .order_by(MainCategoryMatch.main_id, MainCategoryMatch.rank, MainCategoryMatch.score.desc())
        )
    ).all()
    snapshot: dict[int, dict] = {}
    for main_id, name, _rank in rows:
        entry = snapshot.setdefault(main_id, {"name": None, "extras": 0})
        if entry["name"] is None:
            entry["name"] = name
        else:
            entry["extras"] += 1
    return snapshot


async def records_by_category(session) -> dict[str, int]:
    """Count of records whose PRIMARY (rank-1) match is each category."""
    rows = (
        await session.execute(
            select(Category.name, func.count())
            .select_from(MainCategoryMatch)
            .join(Category, Category.id == MainCategoryMatch.category_id)
            .where(MainCategoryMatch.rank == 1)
            .group_by(Category.name)
            .order_by(func.count().desc(), Category.name)
        )
    ).all()
    return {name: count for name, count in rows}


async def apply_review(
    session,
    record: Main,
    *,
    action: str,
    title: str | None = None,
    lead: str | None = None,
    body: str | None = None,
) -> Main:
    """Apply approve/reject/save_draft with optional output edits.

    Transitions run through the ONE state machine
    (``app.services.transitions``): approve moves the record
    ``review -> approved`` (stamping ``approved_at``, first wins) and
    releases gated deliveries; reject moves to ``rejected``; save_draft
    edits only. The status column now tells the operator the truth — an
    approved record leaves the needs-review list immediately.

    Raises:
        ValueError: ``bad-action`` for unknown actions, ``not-in-review``
            for rows outside ``review`` (callers map to 422/409 or pages).
    """
    if action not in ("approve", "reject", "save_draft"):
        raise ValueError("bad-action")
    if record.status != "review":
        raise ValueError("not-in-review")
    if title is not None or lead is not None or body is not None:
        current = dict(record.output_json or {})
        if title is not None:
            current["title"] = title
        if lead is not None:
            current["lead"] = lead
        if body is not None:
            current["body"] = body
        record.output_json = current
    if action == "approve":
        # review -> approved (one place defines this: transitions module).
        assert_record_transition(record.status, "approved")
        record.status = "approved"
        if record.approved_at is None:
            record.approved_at = datetime.utcnow()
    elif action == "reject":
        assert_record_transition(record.status, "rejected")
        record.status = "rejected"
    record.updated_at = datetime.utcnow()
    await session.commit()
    if action == "approve":
        await confirm_deliveries_for_article(session, record.id)
    await session.refresh(record)
    return record


async def bulk_approve(session, record_ids: list[int]) -> tuple[list[int], list[int]]:
    """Approve many records; return ``(approved ids, skipped ids)``.

    Non-review and unknown rows are skipped, never forced — the backlog
    use-case must not overwrite decided or processing jobs.
    """
    approved, skipped = [], []
    for record_id in sorted(set(record_ids)):
        record = await get_record(session, record_id)
        if record is None or record.status != "review":
            skipped.append(record_id)
            continue
        await apply_review(session, record, action="approve")
        approved.append(record_id)
    return approved, skipped


async def record_status_counts(session) -> dict[str, int]:
    """Record counts per status (stats endpoint, dashboard, list KPIs)."""
    rows = (await session.execute(select(Main.status, func.count()).group_by(Main.status))).all()
    return {status: count for status, count in rows}


async def filter_options(session, limit: int = 50) -> dict[str, list[str]]:
    """Distinct field/university names for the list-page pickers.

    Panel list page (its own SQL, previously inline in the route) and any
    other caller of the shared filter vocabulary.
    """
    fields = [
        r[0] for r in (
            await session.execute(select(Main.field).distinct().order_by(Main.field).limit(limit))
        ).all() if r[0]
    ]
    universities = [
        r[0] for r in (
            await session.execute(
                select(Main.university).distinct().order_by(Main.university).limit(limit)
            )
        ).all() if r[0]
    ]
    return {"fields": fields, "universities": universities}


async def worker_heartbeat(session) -> object | None:
    """Latest ``main.heartbeat_at`` — the Worker freshness marker (dashboard).

    None when nothing was ever processed; the dashboard then reports the
    Worker as not-fresh rather than showing a stale timestamp.
    """
    return (
        await session.execute(select(func.max(Main.heartbeat_at)))
    ).scalar_one_or_none()


async def regenerate(session, record_id: int) -> Main:
    """Re-run LLM generation for a ``review``/``failed`` record (panel action).

    Uses the exact same resolution + generation path as the Worker
    (``get_active_*`` + shared ``resolve_generation_config`` +
    ``generate_news``), so a regenerated article is indistinguishable from
    one the Worker produced. Success stores the new output AND the new
    classification (``main_category_matches`` replaced wholesale) and returns
    the record to ``review`` (``failed -> review``, a declared transition);
    provider errors park it ``failed`` with the error text.

    Raises:
        ValueError: ``not-regenerable`` for a record outside
            ``review``/``failed`` (routes map to 409); any provider error
            propagates after the failure has been recorded on the record.
    """
    from app.news.generation import generate_news, resolve_generation_config
    from app.worker.worker import get_active_model, get_active_prompt

    record = await get_record(session, record_id)
    if record is None or record.status not in ("review", "failed"):
        raise ValueError("not-regenerable")

    active_model = await get_active_model(session)
    active_prompt = await get_active_prompt(session)
    # Same gap-fill as the Worker: keep per-record overrides, fill the rest.
    orig_model_id = record.model_id
    orig_prompt_id = record.prompt_id
    if record.model_id is None and active_model:
        record.model_id = active_model.id
    if record.prompt_id is None and active_prompt:
        record.prompt_id = active_prompt.id
    override_model = await session.get(Model, orig_model_id) if orig_model_id else None
    override_prompt = await session.get(Prompt, orig_prompt_id) if orig_prompt_id else None
    config = resolve_generation_config(override_model, override_prompt, active_model, active_prompt)

    thesis = {
        "title": record.title, "abstract": record.abstract, "field": record.field,
        "university": record.university, "table_of_contents": record.table_of_contents,
        "author": record.author, "supervisor": record.supervisor,
    }
    try:
        output = await generate_news(thesis, **config)
    except Exception as e:
        assert_record_transition(record.status, "failed")
        record.status = "failed"
        record.error_message = str(e)[:2000]
        record.updated_at = datetime.utcnow()
        await session.commit()
        raise
    record.output_json = {"title": output.title, "lead": output.lead, "body": output.body}
    assert_record_transition(record.status, "review")
    record.status = "review"
    record.error_message = None
    record.updated_at = datetime.utcnow()
    # Fresh generation = fresh classification: the old matches are replaced
    # exactly like the Worker does (one shared implementation).
    await store_generated_classification(
        session, record, output, model_id=record.model_id, prompt_id=record.prompt_id,
    )
    await session.commit()
    await session.refresh(record)
    return record


async def needs_review_count(session) -> int:
    """Records awaiting a human decision: exactly the ``review`` rows.

    Approval moves the row to ``approved`` (and sending to ``published``),
    so this count is exact by construction — an approved record can never
    still appear as needing review.
    """
    return (
        await session.execute(
            select(func.count()).select_from(Main).where(Main.status == "review")
        )
    ).scalar_one()


async def reassign_category(session, record: Main, category_id: int) -> Main:
    """Reclassify one record manually (operator correction).

    Routing reads ``main_category_matches``, so the correction writes a
    single definitive manual match (score 1.0, reason records the reviewer)
    and replaces whatever the LLM had stored — otherwise the record would
    keep routing to the old categories. ``main.category_id`` (the display
    primary) is set to the same category.

    Raises:
        ValueError: ``unknown-category`` when the category does not exist.
    """
    from app.db.models import Category

    category = await session.get(Category, category_id)
    if category is None:
        raise ValueError("unknown-category")
    await _delete_matches(session, record.id)
    session.add(MainCategoryMatch(
        main_id=record.id,
        category_id=category_id,
        score=1.0,
        rank=1,
        reason="تعیین دستی توسط بازبین",
    ))
    record.category_id = category_id
    record.updated_at = datetime.utcnow()
    # No refresh(): with expire_on_commit=False the row is already current,
    # and refreshing would expire attributes the caller may read afterwards.
    await session.commit()
    return record


async def record_timeline(session, record: Main) -> list[dict]:
    """The flow of one record: submitted → resolved → generated → reviewed → delivered.

    Built from real columns and audit rows only — no hardcoded prose — so the
    timeline reflects what actually happened, including failures and the
    model/prompt used for generation.
    """
    from app.db.models import Category, Channel, Delivery, Field, Model, Prompt

    steps: list[dict] = [
        {"title": "ثبت درخواست", "sub": _tl_status(record.status), "at": record.created_at}
    ]

    # field_row stays None when the record has no field link yet (a record can
    # carry a category without one), so it is initialised before the branch.
    field_row = None
    field_name = None
    if record.field_id is not None:
        field_row = await session.get(Field, record.field_id)
        field_name = field_row.name if field_row else None
    if record.category_id is not None:
        category = await session.get(Category, record.category_id)
        if category is not None:
            detail = f"رشته: {field_name or record.field} · دسته: {category.name}"
            if field_row is not None and field_row.matched_keyword:
                detail += f" · کلیدواژه: {field_row.matched_keyword}"
            steps.append({"title": "تعیین رشته و دسته", "sub": detail, "at": record.claimed_at})
    elif field_row is not None:
        steps.append({"title": "تعیین رشته و دسته", "sub": f"رشته: {field_name}", "at": record.claimed_at})

    model_name = prompt_version = None
    if record.model_id is not None:
        model_row = await session.get(Model, record.model_id)
        model_name = model_row.name if model_row else None
    if record.prompt_id is not None:
        prompt_row = await session.get(Prompt, record.prompt_id)
        prompt_version = prompt_row.version if prompt_row else None

    if record.output_json:
        sub = " · ".join(x for x in (f"مدل: {model_name}" if model_name else "",
                                     f"پرامپت: {prompt_version}" if prompt_version else "") if x)
        steps.append({"title": "تولید خروجی", "sub": sub or "خروجی مدل ذخیره شد",
                      "at": record.updated_at})
    elif record.error_message:
        steps.append({"title": "تولید ناموفق", "sub": (record.error_message or "")[:120],
                      "at": record.updated_at})

    # Human actions come from the audit trail (who + when + what).
    audits = (
        await session.execute(
            select(AuditLog).where(AuditLog.main_id == record.id).order_by(AuditLog.id)
        )
    ).scalars().all()
    for entry in audits:
        if entry.action not in ("approve", "reject", "save_draft", "regenerate"):
            continue
        actor = await session.get(PanelUser, entry.user_id) if entry.user_id else None
        label = {
            "approve": "تأیید بازبین", "reject": "رد توسط بازبین",
            "save_draft": "ویرایش متن خبر", "regenerate": "تولید دوباره",
        }.get(entry.action, entry.action)
        steps.append({
            "title": label,
            "sub": f"{actor.username if actor else 'سیستم'}",
            "at": entry.created_at,
        })

    deliveries = (
        await session.execute(
            select(Delivery).where(Delivery.main_id == record.id).order_by(Delivery.id)
        )
    ).scalars().all()
    for delivery in deliveries:
        channel = await session.get(Channel, delivery.channel_id)
        name = (channel.username or channel.title or channel.platform) if channel else "—"
        if delivery.status == "sent":
            steps.append({
                "title": f"ارسال به {name}",
                "sub": f"شناسهٔ پیام: {delivery.platform_message_id}",
                "at": delivery.sent_at,
            })
        elif delivery.status == "deleted_from_channel":
            steps.append({"title": f"حذف پیام از {name}", "sub": "پیام منتشرشده حذف شد",
                          "at": delivery.updated_at})
        elif delivery.status == "failed":
            steps.append({"title": f"ارسال ناموفق به {name}",
                          "sub": (delivery.error_message or "")[:120], "at": delivery.updated_at})
        else:
            steps.append({"title": f"در انتظار ارسال به {name}",
                          "sub": _DELIVERY_FA.get(delivery.status, delivery.status),
                          "at": None})
    return steps


def _tl_status(status: str) -> str:
    """Persian lifecycle label for a timeline header (from the state machine)."""
    return f"وضعیت: {STATUS_FA.get(status, status)}"


async def approval_info(session, record: Main) -> tuple[str, object] | None:
    """``(username, timestamp)`` of the first approval audit, for the banner."""
    if record.approved_at is None:
        return None
    first = (
        await session.execute(
            select(AuditLog)
            .where(AuditLog.main_id == record.id, AuditLog.action == "approve")
            .order_by(AuditLog.id)
            .limit(1)
        )
    ).scalar_one_or_none()
    if first is None:
        return None
    approver = await session.get(PanelUser, first.user_id) if first.user_id else None
    return (approver.username if approver else "—", first.created_at)


async def records_per_day(session, days: int = 7) -> list[tuple[object, int]]:
    """``(date, count)`` of created records for the trailing ``days`` days."""
    from datetime import datetime, timedelta

    today = datetime.utcnow().date()
    out = []
    for back in range(days - 1, -1, -1):
        day = today - timedelta(days=back)
        start = datetime(day.year, day.month, day.day)
        out.append((
            day,
            (
                await session.execute(
                    select(func.count()).select_from(Main).where(
                        Main.created_at >= start,
                        Main.created_at < start + timedelta(days=1),
                    )
                )
            ).scalar_one(),
        ))
    return out


async def record_overview(session, record: Main) -> dict:
    """Everything the review page needs beyond the API payload, in one place.

    Deliveries with channel + category names, model/prompt names and texts,
    snapshot vs current category names, the field's matched keyword, routed
    channel display names, and audit history with usernames. Timestamps stay
    raw datetimes — routes format them for display.
    """
    from app.db.models import Category, Channel, Delivery, Field, Model, Prompt

    deliveries_rows = (
        await session.execute(
            select(Delivery).where(Delivery.main_id == record.id).order_by(Delivery.id)
        )
    ).scalars().all()
    deliveries = []
    for delivery in deliveries_rows:
        channel = await session.get(Channel, delivery.channel_id)
        category_name = None
        if delivery.category_id is not None:
            category = await session.get(Category, delivery.category_id)
            category_name = category.name if category else None
        deliveries.append({
            "id": delivery.id, "status": delivery.status, "attempts": delivery.attempts,
            "platform_message_id": delivery.platform_message_id,
            "error_message": delivery.error_message,
            "channel_name": (channel.username or channel.title or channel.platform) if channel else "—",
            "channel_username": channel.username if channel else None,
            "category_name": category_name, "route_source": delivery.route_source,
        })

    model_name = prompt_version = prompt_text = model_base_url = None
    model_row = await session.get(Model, record.model_id) if record.model_id else None
    if model_row is not None:
        model_name, model_base_url = model_row.name, model_row.base_url
    if model_name is None:
        active = (
            await session.execute(select(Model).where(Model.is_active.is_(True)).limit(1))
        ).scalar_one_or_none()
        if active is not None:
            model_name, model_base_url = active.name, active.base_url
    if record.prompt_id:
        prompt = await session.get(Prompt, record.prompt_id)
        if prompt is not None:
            prompt_version, prompt_text = prompt.version, prompt.system_prompt
    snapshot_category = current_category = field_keyword = None
    if record.category_id is not None:
        snapshot = await session.get(Category, record.category_id)
        snapshot_category = snapshot.name if snapshot else None
    if record.field_id is not None:
        field_row = await session.get(Field, record.field_id)
        if field_row is not None:
            field_keyword = field_row.matched_keyword
            if field_row.category_id is not None:
                current = await session.get(Category, field_row.category_id)
                current_category = current.name if current else None

    audits = (
        await session.execute(
            select(AuditLog).where(AuditLog.main_id == record.id).order_by(AuditLog.id.desc()).limit(20)
        )
    ).scalars().all()
    history = []
    for entry in audits:
        actor = await session.get(PanelUser, entry.user_id) if entry.user_id else None
        history.append({
            "title": entry.action, "username": actor.username if actor else None,
            "created_at": entry.created_at, "detail": entry.detail,
        })
    return {
        "deliveries": deliveries,
        "model_name": model_name, "model_base_url": model_base_url,
        "prompt_version": prompt_version, "prompt_text": prompt_text,
        "snapshot_category": snapshot_category, "current_category": current_category,
        "field_keyword": field_keyword, "history": history,
        # The LLM's classification with per-match explanation + the flag for
        # the "unclassified" warning the categories tab shows.
        "category_matches": await record_category_matches(session, record.id),
        "classification_fallback": await record_matches_fallback_only(session, record.id),
    }
