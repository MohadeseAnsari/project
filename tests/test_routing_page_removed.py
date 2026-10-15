"""The routing-preview page is gone (item 5).

Removed rather than deprecated: nothing else in the panel used it. The old
address still redirects so a bookmarked link does not 404, and the machine
API's ``/routing/preview`` is untouched — the owner asked for the PAGE to go,
not the auditable endpoint the sender relies on.
"""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PanelUser, Role
from app.panel.auth import hash_password
from tests.panel_test_client import panel_client


async def _admin(session: AsyncSession) -> None:
    role = (await session.execute(select(Role).where(Role.name == "admin"))).scalar_one_or_none()
    if role is None:
        role = Role(name="admin", description="admin")
        session.add(role)
        await session.flush()
    session.add(PanelUser(username="route-admin", password_hash=hash_password("secret123"),
                          role_id=role.id, is_active=True))
    await session.commit()


@pytest.mark.asyncio
async def test_the_page_is_removed_and_redirects(session: AsyncSession):
    await _admin(session)
    async with panel_client() as client:
        await client.post("/login", data={"username": "route-admin", "password": "secret123"},
                          follow_redirects=False)
        response = await client.get("/routing", follow_redirects=False)
        assert response.status_code == 301
        assert response.headers["location"] == "/channels"
        # Following it lands on a real page, not an error.
        landed = await client.get(response.headers["location"])
        assert landed.status_code == 200


def test_no_link_to_the_page_remains():
    """A dead sidebar entry is worse than no entry at all."""
    from pathlib import Path

    for template in Path("app/panel/templates").glob("*.html"):
        html = template.read_text(encoding="utf-8")
        assert 'href="/routing"' not in html, f"{template.name} still links to the removed page"
    assert not Path("app/panel/templates/routing.html").exists()


def test_the_route_is_not_registered_as_a_page():
    from pathlib import Path

    from app.panel.app import panel_app

    paths = {getattr(r, "path", None) for r in panel_app.routes}
    assert "/routing" in paths, "the redirect must still be registered"
    # Only the redirect remains: no template is rendered any more.
    assert not Path("app/panel/templates/routing.html").exists()


def test_the_machine_api_endpoint_survives():
    """The owner asked for the PAGE to go; the auditable API endpoint stays."""
    from app.api.routes import router

    assert "/routing/preview" in {getattr(r, "path", None) for r in router.routes}
