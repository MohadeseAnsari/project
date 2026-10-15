"""The fields page category dropdown must submit a real id.

``category_cards()`` returned dicts with no ``id``, so every ``<option>`` in
the per-row category select rendered ``value=""``. The names looked right and
the form submitted nothing, which the handler rejected with a 422 — so changing
a field's category from the panel did nothing at all.

It survived this long because every test POSTed ``category_id`` directly
instead of reading the value out of the rendered form. The handler and the
template are joined only by a string, so a typo in one is invisible to the
other. These tests close that gap: they read the value from the page and post
exactly that.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Field, PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="cat-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


async def _world(session: AsyncSession, tag: str = "a") -> tuple[Category, Category, Field]:
    """Names are unique per test.

    The category table is seeded by migrations, so a fixed name collides with a
    row that is already there -- and the resulting failure (an empty response,
    an IndexError three frames away) says nothing about what went wrong.
    """
    medical = Category(name=f"پزشکی {tag}", description="Medical")
    engineering = Category(name=f"فنی {tag}", description="Engineering")
    session.add_all([medical, engineering])
    await session.commit()
    field = Field(name=f"هوش مصنوعی {tag}", normalized_name=f"ai-{tag}",
                  category_id=medical.id, category_source="rules")
    session.add(field)
    await session.commit()
    return medical, engineering, field


async def _fields_page(client) -> str:
    page = await client.get("/fields")
    assert page.status_code == 200, f"/fields returned {page.status_code}"
    assert page.text, "/fields returned an empty body"
    return page.text


def _row_select(html: str, field: Field) -> str:
    """The category <select> exactly as the browser would see it."""
    block = html.split(f'action="/fields/{field.id}/category"')[1].split("</select>")[0]
    return block


@pytest.mark.asyncio
async def test_every_option_in_the_category_select_carries_a_value(session: AsyncSession):
    await _admin(session)
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cat-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _fields_page(client)
    field = (await session.execute(select(Field))).scalars().one()
    values = re.findall(r'<option value="([^"]*)"', _row_select(page, field))
    assert values, "no options rendered at all"
    assert all(v for v in values), (
        f"every option must carry a value; these were empty: {values}"
    )
    assert all(v.isdigit() for v in values), f"values must be numeric ids, got {values}"


@pytest.mark.asyncio
async def test_the_service_supplies_the_id_the_template_needs(session: AsyncSession):
    """The root cause, pinned at the layer it belongs to."""
    from app.services.fields import category_cards

    await _world(session)
    cards, _total = await category_cards(session)
    assert cards, "the page needs at least one category to render a select"
    for card in cards:
        assert "id" in card, f"category_cards() must return an id; got keys {sorted(card)}"
        assert isinstance(card["id"], int) and card["id"] > 0


@pytest.mark.asyncio
async def test_saving_a_category_through_the_rendered_form_works(session: AsyncSession):
    """End to end: read the value out of the page, post exactly that."""
    await _admin(session)
    medical, engineering, field = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cat-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _fields_page(client)
        chosen = re.search(r'<option value="(\d+)"\s+selected', _row_select(page, field))
        assert chosen, "the row's select must offer a selectable value"
        assert int(chosen.group(1)) == medical.id, "the current category must be preselected"
        response = await client.post(
            f"/fields/{field.id}/category",
            data={"category_id": engineering.id}, follow_redirects=False,
        )
        assert response.status_code == 303
    await session.refresh(field)
    assert field.category_id == engineering.id, "the form did not change anything"
    assert field.category_source == "manual"


@pytest.mark.asyncio
async def test_the_option_value_matches_the_category_it_names(session: AsyncSession):
    """A value pointing at the wrong category would be worse than an empty one."""
    await _admin(session)
    medical, engineering, _field = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "cat-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _fields_page(client)
    pairs = dict(re.findall(r'<option value="(\d+)"[^>]*>([^<]+)</option>', page))
    assert pairs.get(str(medical.id)) == medical.name
    assert pairs.get(str(engineering.id)) == engineering.name

@pytest.mark.asyncio
async def test_the_current_category_is_preselected(session: AsyncSession):
    """The second half of the same broken control.

    Even with values, the dropdown opened on the FIRST category rather than the
    field's own, because list_fields() did not return category_id so the
    `f.category_id == cat.id` comparison was never true. Saving without
    touching the select would then have silently reassigned the field -- worse
    than the control doing nothing at all.
    """
    await _admin(session)
    medical, engineering, field = await _world(session, "b")
    other = Field(name=f"هوش مصنوعی دوم b", normalized_name="ai2-b",
                  category_id=engineering.id, category_source="rules")
    session.add(other)
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "cat-admin", "password": "secret123"},
                          follow_redirects=False)
        page = await _fields_page(client)
    for row, expected in ((field, medical), (other, engineering)):
        chosen = re.search(r'<option value="(\d+)"\s+selected', _row_select(page, row))
        assert chosen, f"field {row.name} has no preselected category"
        assert int(chosen.group(1)) == expected.id, (
            f"field {row.name} should preselect {expected.name}, "
            f"not {int(chosen.group(1))}"
        )


@pytest.mark.asyncio
async def test_list_fields_carries_the_category_id_the_template_compares_against(
    session: AsyncSession,
):
    """Pinned at the layer that caused it, so the key cannot silently vanish."""
    from app.services.fields import list_fields

    medical, _engineering, field = await _world(session, "c")
    rows = await list_fields(session)
    row = [r for r in rows if r["id"] == field.id][0]
    assert "category_id" in row, "the template compares f.category_id to cat.id"
    assert row["category_id"] == medical.id
