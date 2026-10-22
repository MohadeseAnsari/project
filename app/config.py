"""Application settings loaded from environment variables and the optional .env file.

All secrets (shared secret, AvalAI API key, database credentials) come from
environment configuration only — nothing is hard-coded as a real credential.
"""

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed application configuration.

    Fields are populated from (in order of precedence): process environment
    variables, then the ``.env`` file in the working directory. Unknown
    variables are ignored so the same code runs in local and containerized
    environments.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/irandoc_news"
    shared_secret: str = "change-me-in-production"
    avalai_api_key: str = ""
    avalai_base_url: str = "https://api.avalai.ir/v1"
    llm_model: str = "gpt-5.6-luna"

    # Engineering decision (not required by the assignments): a hung LLM call
    # would otherwise block the single sequential worker forever, because the
    # stale-job lease can only recover jobs after the worker process is gone.
    # A bounded timeout lets the worker fail the job and continue.
    llm_timeout_seconds: float = 120.0

    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 30.0
    worker_lease_minutes: int = 5

    # Panel (console) live updates.
    #
    # The dashboard's counters and health card are re-fetched on this cadence
    # so an operator watching a generate→approve→publish run sees it move
    # without reloading. It is deliberately slower than the workers' own poll
    # loops: this is a human-facing readout, not a control loop, and the
    # figures are cheap `GROUP BY` counts.
    panel_dashboard_refresh_seconds: int = 15

    # Combined generation + ISCED-F 2013 classification.
    #
    # ONE LLM call per thesis returns the news AND the classification. The
    # model answers with up to ``top_n_categories`` ISCED-F 2013 categories,
    # each with a 0..1 relevance score; the worker stores every qualifying
    # match and routing fans the article out to each category's channels.
    top_n_categories: int = 3
    # A match only counts (for storage and routing) at or above this score.
    min_category_relevance: float = 0.5

    # Human gate for publishing. When True, newly queued deliveries start as
    # `awaiting_confirmation`; when False, news is auto-published to matched
    # channels. Default off (auto-publish mode) per the operator's request.
    require_human_confirmation: bool = False

    # What to do when an article has NO qualifying category match.
    # "review" (default, safest): stays in review, visible via filter.
    # "fallback_other": enqueue deliveries for the "other" category's channels.
    # "failed": move to failed with a clear error message.
    unmatched_category_behavior: str = "review"

    # Compact LLM output (v3-compact).
    skip_reason_above_score: int = 90
    store_llm_raw_output: bool = False

    # Outbound delivery (sender Worker).
    #
    # One token per platform, not per channel: a deployment publishes through
    # one bot per platform, and `channels.chat_id` selects the destination. If
    # several bots per platform are ever needed, the token moves onto the
    # channel row.
    #
    # Missing tokens are NOT treated as an error at import time — the sender
    # Worker only touches a platform whose token is configured, so adding
    # Telegram later does not require a Bale token and vice versa.
    bale_bot_token: str = ""
    telegram_bot_token: str = ""
    bale_api_base: str = "https://tapi.bale.ai"
    telegram_api_base: str = "https://api.telegram.org"

    # Delay between two consecutive sends. Applied after every send attempt so
    # the publisher stays well under both Bale's and Telegram's rate limits and
    # does not look like a burst to the channel.
    sender_send_delay_seconds: float = 5.0
    sender_poll_interval_seconds: float = 2.0

    # Bounded retry: `sender_max_attempts` tries with exponential backoff, then
    # the delivery is parked in `failed` with its error for manual inspection.
    # Unbounded retry would let one broken channel stall every article behind it.
    sender_max_attempts: int = 5
    sender_backoff_base_seconds: float = 10.0
    sender_request_timeout_seconds: float = 20.0

    # A send attempt is normally seconds; a `sending` row older than this means
    # the Worker died mid-call, so it is returned to `pending`.
    sender_lease_minutes: int = 5

    # Review panel (server-rendered Jinja2 + HTMX). Credentials come from env
    # only; the first admin is created at runtime, never seeded in migrations.
    # (The panel's listen address/port live in docker-compose.yml, not here:
    # a settings field the container never reads would be a lie.)
    panel_admin_username: str = "admin"
    panel_admin_password: str = ""
    panel_secret_key: str = "change-me-in-production"
    # Send the session cookie with the Secure flag only when the panel sits
    # behind a TLS-terminating reverse proxy; over plain HTTP browsers would
    # otherwise drop the cookie and nobody could log in.
    panel_cookie_secure: bool = False
    # Public-login guardrails: N failures from one IP inside the window locks
    # that IP out for the lockout duration. Process-local memory (single web
    # replica); restarts clear the counters, failures stay in the audit trail.
    panel_login_max_attempts: int = 10
    panel_login_window_minutes: int = 10
    panel_login_lockout_minutes: int = 15
    # Session lifetime in hours (cookie Max-Age and server-side expiry agree).
    panel_session_hours: int = 12
    # Extra trusted proxy peers/networks for X-Forwarded-* headers, beyond
    # loopback. Needed because inside Docker the host proxy arrives as the
    # bridge gateway, not 127.0.0.1 — set this to the compose bridge subnet
    # (e.g. "172.19.0.0/16"). Only the proxy may use these addresses: anyone
    # on them can set the visitor IP the lockout and audit trail record.
    panel_trusted_proxies: str = ""

    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Origins allowed to call the API from a browser, as a comma-separated list.
    #
    # Needed by the demo UI, which is served from a different port than the API
    # and is therefore a cross-origin request. Left as an explicit list rather
    # than "*" on purpose: this API authenticates with a shared secret, and a
    # wildcard would let any site a presenter visits make credentialed calls
    # against it.
    #
    # Set DEMO_CORS_ORIGINS when the demo is served from another host, e.g.
    # "http://example.com:8080". The defaults cover local development only.
    demo_cors_origins: str = "http://localhost:8080,http://127.0.0.1:8080"


settings = Settings()

if not settings.require_human_confirmation:
    logging.warning(
        "REQUIRE_HUMAN_CONFIRMATION=false: news will be auto-published to "
        "matched categories' channels without human review. "
        "Ensure auto-routing is verified (scripts/auto_route_channels.py)."
    )
