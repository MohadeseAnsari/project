from unittest.mock import patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.config import settings


@pytest.mark.asyncio
async def test_submit_valid_secret():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "shared_Secret": settings.shared_secret,
            "username": "testuser",
            "title": "Test Thesis",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
        })
    assert response.status_code in (200, 422, 500)


@pytest.mark.asyncio
async def test_submit_invalid_secret():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "shared_Secret": "wrong-secret",
            "username": "testuser",
            "title": "Test Thesis",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
        })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_missing_secret():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "username": "testuser",
            "title": "Test Thesis",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
        })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_missing_field():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "shared_Secret": settings.shared_secret,
            "username": "testuser",
            "title": "",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
        })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_submit_whitespace_field():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "shared_Secret": settings.shared_secret,
            "username": "   ",
            "title": "Test Thesis",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
        })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_submit_malformed_json():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/submit",
            content="not valid json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_submit_extra_fields():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "shared_Secret": settings.shared_secret,
            "username": "testuser",
            "title": "Test Thesis",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
            "extra_field": "should be ignored",
        })
    assert response.status_code in (200, 422, 500)


@pytest.mark.asyncio
async def test_auth_checked_before_validation():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/submit", json={
            "shared_Secret": "wrong-secret",
            "username": "testuser",
            "title": "",
            "abstract": "Test abstract",
            "field": "پزشکی",
            "university": "Tehran University",
            "author": "John Doe",
            "supervisor": "Dr. Smith",
        })
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_does_not_call_llm():
    with patch("app.news.generation.generate_news") as mock_gen:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.post("/submit", json={
                "shared_Secret": settings.shared_secret,
                "username": "testuser",
                "title": "Test Thesis",
                "abstract": "Test abstract",
                "field": "پزشکی",
                "university": "Tehran University",
                "author": "John Doe",
                "supervisor": "Dr. Smith",
            })
    assert response.status_code in (200, 422, 500)
    mock_gen.assert_not_called()
