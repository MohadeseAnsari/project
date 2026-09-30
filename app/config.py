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

    api_host: str = "0.0.0.0"
    api_port: int = 8000


settings = Settings()
