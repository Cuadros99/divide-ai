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

    # Kill switch da integração: a API do Tricount é não-oficial, então dá pra
    # desligar integração + UI sem deploy de código.
    tricount_enabled: bool = False
    tricount_provider: str = "fake"  # "fake" | "real"
    tricount_token: Optional[str] = None  # tXXXXX do link tricount.com/tXXXXX
    tricount_credentials_json: Optional[str] = None
    tricount_timeout_seconds: float = 8.0


settings = Settings()
