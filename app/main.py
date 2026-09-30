"""FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.api.limits import limiter
from app.api.routes import router
from app.config import settings
from app.db.connection import engine
from app.schemas.news import ServiceInfo, ThesisInput


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Configure logging on startup and dispose the engine on shutdown."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    yield
    await engine.dispose()


app = FastAPI(
    title="IranDoc News Generation",
    description="Automatic journalistic news generation from IranDoc thesis metadata.",
    version="1.0.0",
    lifespan=lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
# Allows the browser-based demo UI (served separately on :8080) to call the API
# from :8000. Configurable via DEMO_CORS_ORIGINS; defaults to local development
# origins. An explicit list, not "*", because the API is authenticated with a
# shared secret.
_DEMO_CORS_ORIGINS = [
    origin.strip()
    for origin in settings.demo_cors_origins.split(",")
    if origin.strip()
]
if _DEMO_CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_DEMO_CORS_ORIGINS,
        allow_methods=["*"],
        allow_headers=["*"],
    )
app.include_router(router)


@app.get("/", response_model=ServiceInfo)
async def root():
    """Service identity endpoint."""
    return {"service": "IranDoc News Generation", "version": "1.0.0"}


def custom_openapi():
    """Add the ``POST /submit`` request body to the OpenAPI schema.

    The endpoint reads the raw request body manually (to preserve the
    400/401-before-422 order required by the assignment), so FastAPI does not
    auto-generate a ``requestBody`` for it. This injects the request body
    explicitly using the existing ``ThesisInput`` model, without changing the
    endpoint's runtime behavior.
    """
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    submit_schema = ThesisInput.model_json_schema(ref_template="#/components/schemas/{model}")
    schema["paths"]["/submit"]["post"]["requestBody"] = {
        "required": True,
        "content": {
            "application/json": {
                "schema": submit_schema,
                "example": {
                    "shared_Secret": "REPLACE_WITH_REAL_SECRET",
                    "username": "test_user",
                    "title": "Sample Thesis Title",
                    "abstract": "This is a sample abstract used for API testing.",
                    "field": "پزشکی",
                    "university": "Tehran University",
                    "table_of_contents": "1. Introduction\n2. Methods\n3. Results",
                    "author": "John Doe",
                    "supervisor": "Dr. Smith",
                },
            }
        },
    }
    app.openapi_schema = schema
    return schema


app.openapi = custom_openapi
