from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.sessions import SessionMiddleware

from app.auth import AuthMiddleware, limiter
from app.auth import router as auth_router
from app.config import settings
from app.db import init_db
from app.routers.listas import router as listas_router
from app.routers.pessoas import router as pessoas_router

app = FastAPI(title="DivideAI")

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# A ordem importa: SessionMiddleware precisa rodar antes do AuthMiddleware
# para que request.session já esteja disponível quando o Auth checar login.
app.add_middleware(AuthMiddleware)
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key)

Path("app/static/uploads").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

app.include_router(auth_router)
app.include_router(pessoas_router)
app.include_router(listas_router)


@app.on_event("startup")
def on_startup() -> None:
    init_db()
