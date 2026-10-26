"""Every button a row renders must point at a route that exists.

The owner asked, plainly: *"i just want the buttons work in panel for each
record"*. The rows carry real actions — approve and publish, regenerate, retry
a failed send, delete the message from the channel — and each one is a form
that does something irreversible on a live messaging platform. A button whose
action 404s, or worse, silently does nothing, is worse than no button: the
operator believes something happened to the channel.

Template text is not type-checked, so nothing else would catch an endpoint
being renamed out from under a rendered form. This walks the real HTML of the
row partials, extracts every ``action``/``hx-*`` URL, and asserts each one is a
route the panel actually serves.

The fixtures deliberately cover every delivery status, because which buttons a
row shows depends on its status — a test that only looked at a failed delivery
would never render ``delete-from-channel`` and would not know it had gone dead.
"""

import re

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Category, Channel, Delivery, Field, Main, PanelUser, Role
from app.panel.app import panel_app
from app.services.passwords import hash_password
from tests.panel_test_client import panel_client

OUTPUT = {"title": "تیتر", "lead": "lead", "body": "body"}

# action="..." / hx-post="..." / hx-get="..." / hx-delete="..."
_URL = re.compile(r'(?:action|hx-post|hx-get|hx-delete)="([^"]+)"')

#: The HTMX partials that actually render the rows. The outer pages load these
#: on a timer, so scraping /records would find the wrapper, not the buttons.
ROW_PARTIALS = {"records": "/records/table", "deliveries": "/deliveries/table"}


def _panel_paths() -> list[str]:
    return [r.path for r in panel_app.routes if getattr(r, "path", None)]


def _is_served(paths: list[str], url: str) -> bool:
    """Does some served route match this rendered URL once :params are bound?"""
    parts = url.split("?")[0].split("/")
    for path in paths:
        route_parts = path.split("/")
        if len(route_parts) == len(parts) and all(
            rp.startswith("{") or rp == p for rp, p in zip(route_parts, parts)
        ):
            return True
    return False


async def _world(session: AsyncSession) -> Main:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="btn-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    category = Category(name="پزشکی و علوم سلامت", description="Medical")
    session.add(category)
    await session.commit()
    field = Field(name="test-field", normalized_name="test-field")
    article = Main(username="u", title="T", abstract="A", field="پزشکی", university="U",
                   author="A", supervisor="S", status="review", field_id=field.id,
                   output_json=OUTPUT)
    session.add_all([field, article])
    await session.commit()
    # One row per status, so every branch of the actions column renders. Each
    # needs its own channel: (main_id, channel_id) is unique.
    for index, status in enumerate(
        ("failed", "awaiting_confirmation", "pending", "sent")
    ):
        channel = Channel(platform="bale", chat_id=99123 + index, username=f"@c{index}",
                          title=f"C{index}", is_active=True)
        session.add(channel)
        await session.commit()
        session.add(Delivery(main_id=article.id, channel_id=channel.id, status=status,
                             attempts=index, error_message="need administrator rights"))
    await session.commit()
    return article


async def _rows(client, url: str) -> str:
    page = await client.get(url)
    assert page.status_code == 200
    return page.text


# --------------------------------------------------------------------------
# nothing renders a button that goes nowhere
# --------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("name,url", list(ROW_PARTIALS.items()))
async def test_every_row_button_points_at_a_route_that_exists(session: AsyncSession,
                                                              name: str, url: str):
    await _world(session)
    paths = _panel_paths()
    async with panel_client() as client:
        await client.post("/login", data={"username": "btn-admin", "password": "secret123"},
                          follow_redirects=False)
        html = await _rows(client, url)
    checked = [u for u in _URL.findall(html) if u.startswith("/")]
    for target in checked:
        assert _is_served(paths, target), (
            f"the {name} rows render a button pointing at {target!r}, which is not "
            f"a route the panel serves"
        )
    assert checked, f"the {name} rows rendered no actions at all — the fixture is empty"


@pytest.mark.asyncio
async def test_the_most_dangerous_delivery_buttons_are_all_exercised(session: AsyncSession):
    """The actions that delete something on a real platform must all render.

    delete-from-channel removes a published message; mark-deleted records that
    the operator already did it by hand. They only appear on a ``sent`` row, so
    without a sent delivery in the fixture these routes could rot unnoticed.
    """
    await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "btn-admin", "password": "secret123"},
                          follow_redirects=False)
        html = await _rows(client, ROW_PARTIALS["deliveries"])
    for action in ("retry", "release", "cancel", "delete-from-channel", "mark-deleted"):
        assert "/deliveries/" in html and f"/{action}\"" in html, (
            f"the {action} button must render for the status that offers it"
        )


# --------------------------------------------------------------------------
# the buttons do what they claim, not merely respond
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_approve_button_actually_approves(session: AsyncSession):
    """The confirm step is the one irreversible thing a row button does."""
    article = await _world(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "btn-admin", "password": "secret123"},
                          follow_redirects=False)
        html = await _rows(client, ROW_PARTIALS["records"])
        assert f'action="/records/{article.id}/review"' in html
        response = await client.post(
            f"/records/{article.id}/review", data={"action": "approve"}, follow_redirects=False
        )
        assert response.status_code in (302, 303)
    await session.refresh(article)
    assert article.status == "approved", "the row button did not do what its label says"


@pytest.mark.asyncio
async def test_the_row_buttons_offer_the_actions_each_status_allows(session: AsyncSession):
    """Approving a record that is past review would be a bug, so it is not offered.

    ``ready`` — submitted, waiting for the worker — is the right subject: it is
    a real, unapproved record that is no longer in ``review``, so the approve
    button must be gone while the detail link stays.
    """
    article = await _world(session)
    article.status = "ready"
    await session.commit()
    async with panel_client() as client:
        await client.post("/login", data={"username": "btn-admin", "password": "secret123"},
                          follow_redirects=False)
        html = await _rows(client, ROW_PARTIALS["records"])
    assert f'action="/records/{article.id}/review"' not in html
    assert f'/records/{article.id}"' in html, "but it stays inspectable"