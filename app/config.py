"""Application settings loaded from environment variables and the optional .env file.

All secrets (shared secret, AvalAI API key, database credentials) come from
environment configuration only — nothing is hard-coded as a real credential.
"""

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

    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
