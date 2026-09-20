from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_user: str = "admin"
    app_password: str = "changeme"
    secret_key: str = "dev-secret-change-me"

    llm_provider: str = "fake"
    openai_api_key: Optional[str] = None
    openai_model: str = "gpt-4o"

    database_url: str = "sqlite:///./data/listadivide.db"
    turso_auth_token: Optional[str] = None
    storage_backend: str = "local"  # "local" | "gcs"
    gcs_bucket_name: Optional[str] = None


settings = Settings()
