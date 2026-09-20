from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

Path("data").mkdir(exist_ok=True)


def _build_database_url() -> str:
    url = settings.database_url
    if url.startswith("sqlite+libsql") and settings.turso_auth_token:
        if "?" in url:
            url = f"{url}&secure=true"
        else:
            base = url if url.endswith("/") else f"{url}/"
            url = f"{base}?secure=true"
    return url


database_url = _build_database_url()
if database_url.startswith("sqlite+libsql"):
    connect_args = {"check_same_thread": False}
    if settings.turso_auth_token:
        # the libsql dialect only reads a fixed allowlist of query keys from the URL
        # (not authToken), so the token must go through connect_args instead
        connect_args["auth_token"] = settings.turso_auth_token
elif database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    connect_args = {}
engine = create_engine(database_url, connect_args=connect_args)


def init_db() -> None:
    from app import models  # noqa: F401 (garante que os modelos sejam registrados)

    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session
