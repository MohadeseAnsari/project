"""Curating fields: bulk assignment, creation, and deletion.

Three operations that did not exist at all. With routing now decided by
category, all three are load-bearing rather than conveniences:

* **bulk assign** — the classifier drops most fields into one catch-all, and a
  deployment has hundreds of fields, so fixing them one dropdown at a time is a
  task nobody finishes.
* **create field** — a discipline missing from the spreadsheet could otherwise
  only ever land in ``other``, which with strict routing publishes nowhere.
* **create category** — buckets could only be deleted or reused, so a missing
  bucket was unfixable.

The assertions that matter most are the negative ones: what must NOT be
rewritten, what must NOT be deleted, and who must NOT be allowed to do it.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog, Category, Channel, Field, Main, PanelUser, Role
from app.panel.auth import hash_password
from app.services import fields as field_service
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession, username: str = "curate-admin", role_name: str = "admin"):
    role = (
        await session.execute(select(Role).where(Role.name == role_name))
    ).scalar_one_or_none()
    if role is None:
        role = Role(name=role_name, description=role_name)
        session.add(role)
        await session.flush()
    user = PanelUser(
        username=username, password_hash=hash_password("secret123"),
        role_id=role.id, is_active=True,
    )
    session.add(user)
    await session.commit()
    return user


async def _login(client, username: str = "curate-admin"):
    await client.post(
        "/login", data={"username": username, "password": "secret123"},
        follow_redirects=False,
    )


async def _category(session: AsyncSession, name: str) -> Category:
    row = Category(name=name, description=name)
    session.add(row)
    await session.commit()
    return row


async def _field(session: AsyncSession, name: str, category: Category, source: str = "spreadsheet"):
    from app.fields.normalization import normalize_field

    row = Field(
        name=name, normalized_name=normalize_field(name), category_id=category.id,
        source=source,
    )
    session.add(row)
    await session.commit()
    return row


# --------------------------------------------------------------------------
# bulk assign
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bulk_moves_every_ticked_field(session: AsyncSession):
    a = await _category(session, "پزشکی")
    b = await _category(session, "فنی")
    rows = [await _field(session, f"field-{i}", a) for i in range(3)]

    moved, skipped = await field_service.bulk_assign_category(
        session, [r.id for r in rows], b.id
    )
    assert (moved, skipped) == (3, 0)
    for row in rows:
        await session.refresh(row)
        assert row.category_id == b.id


@pytest.mark.asyncio
async def test_bulk_marks_the_move_as_a_human_decision(session: AsyncSession):
    """Otherwise the «دستی» badge would mean nothing on a batch of 300."""
    a = await _category(session, "پزشکی")
    b = await _category(session, "فنی")
    row = await _field(session, "guessed-field", a)

    await field_service.bulk_assign_category(session, [row.id], b.id)
    await session.refresh(row)
    assert row.category_source == "manual"


@pytest.mark.asyncio
async def test_bulk_leaves_fields_already_there_alone(session: AsyncSession):
    """Skipping is the point: rewriting them would badge rows nobody read."""
    a = await _category(session, "پزشکی")
    b = await _category(session, "فنی")
    there = await _field(session, "already-there", b)
    there.category_source = "curated"
    elsewhere = await _field(session, "elsewhere", a)
    await session.commit()

    moved, skipped = await field_service.bulk_assign_category(
        session, [there.id, elsewhere.id], b.id
    )
    assert (moved, skipped) == (1, 1)
    await session.refresh(there)
    assert there.category_source == "curated", "a skipped row keeps its provenance"


@pytest.mark.asyncio
async def test_bulk_ignores_an_unknown_id_rather_than_failing_the_batch(session: AsyncSession):
    """A stale checkbox must not block the rows the operator did mean."""
    a = await _category(session, "پزشکی")
    b = await _category(session, "فنی")
    row = await _field(session, "real-field", a)

    moved, _skipped = await field_service.bulk_assign_category(
        session, [str(row.id), "999999", "not-a-number"], b.id
    )
    assert moved == 1
    await session.refresh(row)
    assert row.category_id == b.id


@pytest.mark.asyncio
async def test_bulk_refuses_an_unknown_category(session: AsyncSession):
    a = await _category(session, "پزشکی")
    row = await _field(session, "f", a)
    with pytest.raises(ValueError) as excinfo:
        await field_service.bulk_assign_category(session, [row.id], 999999)
    assert str(excinfo.value) == "unknown-category"


@pytest.mark.asyncio
async def test_bulk_with_nothing_selected_does_nothing(session: AsyncSession):
    """An empty submit must not be a silent mass-update."""
    a = await _category(session, "پزشکی")
    b = await _category(session, "فنی")
    row = await _field(session, "untouched", a)
    assert await field_service.bulk_assign_category(session, [], b.id) == (0, 0)
    await session.refresh(row)
    assert row.category_id == a.id


# --------------------------------------------------------------------------
# create a field
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_a_field_places_it_and_marks_it_manual(session: AsyncSession):
    """Manual, so the next classification pass cannot move it."""
    category = await _category(session, "پزشکی")
    field = await field_service.create_field(session, "مهندسی صنایع", category.id)
    assert field.id is not None
    assert field.category_id == category.id
    assert field.source == "manual"
    assert field.category_source == "manual"


@pytest.mark.asyncio
async def test_creating_a_field_normalizes_its_identity(session: AsyncSession):
    """Skip this and the unique index blows up on a name that looks new."""
    category = await _category(session, "پزشکی")
    field = await field_service.create_field(session, "  پزشکی   عمومی  ", category.id)
    assert field.normalized_name == "پزشکی عمومی"


@pytest.mark.asyncio
async def test_creating_a_duplicate_field_is_refused_in_persian(session: AsyncSession):
    from app.services.errors import from_value_error

    category = await _category(session, "پزشکی")
    await field_service.create_field(session, "پزشکی عمومی", category.id)
    with pytest.raises(ValueError) as excinfo:
        await field_service.create_field(session, "پزشکی عمومی", category.id)
    status, message = from_value_error(excinfo.value)
    assert status == 422
    assert "از قبل وجود دارد" in message


@pytest.mark.asyncio
async def test_creating_an_empty_field_is_refused(session: AsyncSession):
    category = await _category(session, "پزشکی")
    with pytest.raises(ValueError) as excinfo:
        await field_service.create_field(session, "   ", category.id)
    assert str(excinfo.value) == "empty-name"


@pytest.mark.asyncio
async def test_creating_a_field_in_no_category_is_refused(session: AsyncSession):
    with pytest.raises(ValueError) as excinfo:
        await field_service.create_field(session, "orphan", 999999)
    assert str(excinfo.value) == "unknown-category"


# --------------------------------------------------------------------------
# create a category
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_a_category(session: AsyncSession):
    category = await field_service.create_category(session, "  علوم کامپیوتر ", "CS")
    assert category.id is not None
    assert category.name == "علوم کامپیوتر", "the name is trimmed"
    assert category.description == "CS"


@pytest.mark.asyncio
async def test_creating_the_same_category_twice_returns_the_first(session: AsyncSession):
    """A uniqueness error on a one-line text box is not an answer the operator
    can act on; "you already have that" is."""
    first = await field_service.create_category(session, "علوم کامپیوتر")
    second = await field_service.create_category(session, "علوم کامپیوتر")
    assert first.id == second.id


@pytest.mark.asyncio
async def test_creating_an_empty_category_is_refused(session: AsyncSession):
    with pytest.raises(ValueError) as excinfo:
        await field_service.create_category(session, "  ")
    assert str(excinfo.value) == "empty-name"


# --------------------------------------------------------------------------
# delete a field
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_unused_field(session: AsyncSession):
    category = await _category(session, "پزشکی")
    row = await _field(session, "mistake", category)
    assert await field_service.delete_field(session, row) == "deleted"
    assert (await session.execute(select(Field))).scalars().all() == []


@pytest.mark.asyncio
async def test_deleting_a_field_a_record_uses_is_refused(session: AsyncSession):
    """Detaching is easy -- main.field_id is nullable -- and wrong.

    Nulling it would silently detach real history from the discipline it was
    filed under while the record still displays that discipline.
    """
    category = await _category(session, "پزشکی")
    row = await _field(session, "used-field", category)
    session.add(Main(
        username="u", title="T", abstract="A", field="used-field", university="U",
        author="A", supervisor="S", status="review", field_id=row.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    ))
    await session.commit()

    from app.services.errors import from_value_error

    with pytest.raises(ValueError) as excinfo:
        await field_service.delete_field(session, row)
    status, message = from_value_error(excinfo.value)
    assert status == 409
    assert "قابل حذف نیست" in message
    # The row is still there: refusing must not half-delete it.
    assert await field_service.get_field(session, row.id) is not None


@pytest.mark.asyncio
async def test_the_field_count_helpers_agree_with_the_rows(session: AsyncSession):
    category = await _category(session, "پزشکی")
    empty = await _category(session, "فنی")
    for i in range(4):
        await _field(session, f"counted-{i}", category)
    counts = await field_service.category_field_counts(session, [category.id, empty.id])
    assert counts == {category.id: 4}, "an empty category is simply absent"


# --------------------------------------------------------------------------
# the panel surface
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_fields_page_offers_bulk_and_creation(session: AsyncSession):
    await _admin(session)
    category = await _category(session, "پزشکی")
    row = await _field(session, "a-field-to-render", category)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/fields")
    assert page.status_code == 200
    assert 'action="/fields/bulk-category"' in page.text
    assert 'name="field_ids"' in page.text
    assert 'data-select-all-fields' in page.text
    assert 'action="/fields"' in page.text, "a form to add a field"
    assert f"/fields/{row.id}/delete" in page.text


@pytest.mark.asyncio
async def test_bulk_assign_over_http_moves_and_reports(session: AsyncSession):
    await _admin(session)
    a = await _category(session, "پزشکی")
    b = await _category(session, "فنی")
    rows = [await _field(session, f"http-{i}", a) for i in range(3)]
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            "/fields/bulk-category",
            data={"field_ids": [str(r.id) for r in rows], "category_id": str(b.id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "assigned=3" in response.headers["location"]
        landed = await client.get(response.headers["location"])
    assert "۳ رشته به دستهٔ انتخابی منتقل شد" in landed.text
    for row in rows:
        await session.refresh(row)
        assert row.category_id == b.id
    actions = (await session.execute(
        select(AuditLog.detail).where(AuditLog.action == "route")
    )).scalars().all()
    assert any("moved=3" in d for d in actions)


@pytest.mark.asyncio
async def test_creating_a_field_over_http(session: AsyncSession):
    await _admin(session)
    category = await _category(session, "پزشکی")
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            "/fields",
            data={"name": "مهندسی صنایع", "category_id": str(category.id)},
            follow_redirects=False,
        )
        assert response.status_code == 303
    rows = (await session.execute(select(Field))).scalars().all()
    assert [r.name for r in rows] == ["مهندسی صنایع"]
    assert (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "field_create")
    )).scalars().all()


@pytest.mark.asyncio
async def test_creating_a_category_over_http(session: AsyncSession):
    await _admin(session)
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            "/categories",
            data={"name": "علوم کامپیوتر", "description": "CS"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/categories"
    rows = (await session.execute(select(Category))).scalars().all()
    assert [r.name for r in rows] == ["علوم کامپیوتر"]
    assert (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "category_create")
    )).scalars().all()


@pytest.mark.asyncio
async def test_deleting_over_http_refuses_a_field_in_use(session: AsyncSession):
    await _admin(session)
    category = await _category(session, "پزشکی")
    used = await _field(session, "used", category)
    session.add(Main(
        username="u", title="T", abstract="A", field="used", university="U",
        author="A", supervisor="S", status="review", field_id=used.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    ))
    spare = await _field(session, "spare", category)
    await session.commit()

    async with panel_client() as client:
        await _login(client)
        refused = await client.post(f"/fields/{used.id}/delete", follow_redirects=False)
        assert refused.status_code == 409
        assert "قابل حذف نیست" in refused.text
        gone = await client.post(f"/fields/{spare.id}/delete", follow_redirects=False)
        assert gone.status_code == 303
    # Query rather than get_field(): the route committed in its OWN session, and
    # this one still holds the deleted row in its identity map, so get_field
    # would hand back a stale object and report success for a failed delete.
    remaining = {
        row.id for row in (await session.execute(select(Field))).scalars().all()
    }
    assert spare.id not in remaining, "the unused field is gone"
    assert used.id in remaining, "and the referenced one is untouched"


@pytest.mark.asyncio
async def test_a_viewer_may_not_curate_fields(session: AsyncSession):
    """Creating, moving and deleting all take FIELD_CURATE."""
    await _admin(session, username="curate-admin")
    await _admin(session, username="curate-viewer", role_name="viewer")
    category = await _category(session, "پزشکی")
    row = await _field(session, "locked", category)
    other = await _category(session, "فنی")

    async with panel_client() as client:
        await _login(client, "curate-viewer")
        for response in (
            await client.post("/fields/bulk-category",
                              data={"field_ids": [str(row.id)], "category_id": str(other.id)},
                              follow_redirects=False),
            await client.post("/fields",
                              data={"name": "sneaky", "category_id": str(other.id)},
                              follow_redirects=False),
            await client.post(f"/fields/{row.id}/delete", follow_redirects=False),
            await client.post("/categories", data={"name": "sneaky"}, follow_redirects=False),
        ):
            assert response.status_code == 403, "a viewer changed something"

    await session.refresh(row)
    assert row.category_id == category.id
    names = set(
        (await session.execute(select(Category.name))).scalars().all()
    )
    assert "sneaky" not in names, "the viewer created nothing"
    assert (
        await session.execute(
            select(Field).where(Field.name == "sneaky")
        )
    ).scalars().all() == []

# --------------------------------------------------------------------------
# delete a category
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deleting_an_empty_category(session: AsyncSession):
    category = await _category(session, "اشتباه")
    assert await field_service.delete_category(session, category.id) == "deleted"
    rows = (
        await session.execute(select(Category).where(Category.id == category.id))
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_deleting_a_category_takes_its_channel_routes_with_it(session: AsyncSession):
    """A route that outlives its bucket would resolve to nothing."""
    from app.db.models import CategoryChannelRoute

    category = await _category(session, "با مسیر")
    channel = Channel(platform="bale", chat_id=97501, is_active=True)
    session.add(channel)
    await session.commit()
    session.add(CategoryChannelRoute(category_id=category.id, channel_id=channel.id))
    await session.commit()

    await field_service.delete_category(session, category.id)
    assert (await session.execute(select(CategoryChannelRoute))).scalars().all() == []


@pytest.mark.asyncio
async def test_deleting_a_category_that_holds_fields_is_refused(session: AsyncSession):
    """category_id is NOT NULL: the fields have to be told where to go first."""
    from app.services.errors import from_value_error

    category = await _category(session, "پر از رشته")
    await _field(session, "a-field", category)
    with pytest.raises(ValueError) as excinfo:
        await field_service.delete_category(session, category.id)
    status, message = from_value_error(excinfo.value)
    assert status == 409
    # The message must carry the path out, not just the refusal.
    assert "رشته" in message and "صفحهٔ رشته‌ها" in message


@pytest.mark.asyncio
async def test_a_refused_category_is_still_there(session: AsyncSession):
    """Refusing must not half-delete: the fields would be orphaned."""
    category = await _category(session, "پر از رشته")
    row = await _field(session, "a-field", category)
    with pytest.raises(ValueError):
        await field_service.delete_category(session, category.id)
    rows = (await session.execute(select(Category))).scalars().all()
    assert category.id in [c.id for c in rows]
    assert (await session.execute(select(Field))).scalars().one().id == row.id


@pytest.mark.asyncio
async def test_deleting_a_category_records_use_is_refused(session: AsyncSession):
    """main.category_id is a plain FK; the record must keep resolving."""
    from app.services.errors import from_value_error

    category = await _category(session, "رکورد دارد")
    session.add(Main(
        username="u", title="T", abstract="A", field="x", university="U",
        author="A", supervisor="S", status="review", category_id=category.id,
        output_json={"title": "T", "lead": "L", "body": "B"},
    ))
    await session.commit()
    with pytest.raises(ValueError) as excinfo:
        await field_service.delete_category(session, category.id)
    _status, message = from_value_error(excinfo.value)
    assert "رکورد" in message


@pytest.mark.asyncio
async def test_deleting_a_category_that_does_not_exist_is_a_not_found(session: AsyncSession):
    with pytest.raises(ValueError) as excinfo:
        await field_service.delete_category(session, 999999)
    assert str(excinfo.value) == "not-found"


@pytest.mark.asyncio
async def test_the_documented_path_actually_works(session: AsyncSession):
    """Empty it, then delete -- the sequence the refusal text tells them to do."""
    source = await _category(session, "مبدأ")
    target = await _category(session, "مقصد")
    rows = [await _field(session, f"moving-{i}", source) for i in range(3)]

    with pytest.raises(ValueError):
        await field_service.delete_category(session, source.id)

    await field_service.bulk_assign_category(session, [r.id for r in rows], target.id)
    assert await field_service.delete_category(session, source.id) == "deleted"
    for row in rows:
        await session.refresh(row)
        assert row.category_id == target.id


@pytest.mark.asyncio
async def test_the_card_offers_delete_and_says_what_blocks_it(session: AsyncSession):
    await _admin(session)
    empty = await _category(session, "خالی")
    busy = await _category(session, "شاغول")
    await _field(session, "occupier", busy)
    async with panel_client() as client:
        await _login(client)
        page = await client.get("/categories")
    assert f'action="/categories/{empty.id}/delete"' in page.text
    assert f'action="/categories/{busy.id}/delete"' in page.text
    assert "پس حذف انجام نمی‌شود" in page.text, (
        "the button must not be a trap that only ever fails"
    )


@pytest.mark.asyncio
async def test_deleting_a_category_over_http(session: AsyncSession):
    await _admin(session)
    category = await _category(session, "اشتباه")
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            f"/categories/{category.id}/delete", follow_redirects=False
        )
        assert response.status_code == 303
        assert response.headers["location"] == "/categories"
    rows = (
        await session.execute(select(Category).where(Category.id == category.id))
    ).scalars().all()
    assert rows == []
    assert (await session.execute(
        select(AuditLog.action).where(AuditLog.action == "category_delete")
    )).scalars().all()


@pytest.mark.asyncio
async def test_deleting_a_busy_category_over_http_explains_why(session: AsyncSession):
    await _admin(session)
    category = await _category(session, "شاغول")
    await _field(session, "occupier", category)
    async with panel_client() as client:
        await _login(client)
        response = await client.post(
            f"/categories/{category.id}/delete", follow_redirects=False
        )
        assert response.status_code == 409
        assert "صفحهٔ رشته‌ها" in response.text
        assert "(409" not in response.text, "the message is the sentence, not a tuple"
    assert (await session.execute(select(Category))).scalars().one().id == category.id


@pytest.mark.asyncio
async def test_a_viewer_may_not_delete_a_category(session: AsyncSession):
    await _admin(session, username="curate-admin")
    await _admin(session, username="curate-viewer", role_name="viewer")
    category = await _category(session, "مال ادمین")
    async with panel_client() as client:
        await _login(client, "curate-viewer")
        response = await client.post(
            f"/categories/{category.id}/delete", follow_redirects=False
        )
        assert response.status_code == 403
    assert (await session.execute(select(Category))).scalars().one().id == category.id
