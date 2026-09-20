import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

limiter = Limiter(key_func=get_remote_address)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


class AuthMiddleware(BaseHTTPMiddleware):
    """Exige sessão autenticada para todas as rotas, exceto /login e /static."""

    PUBLIC_PATHS = {"/login"}

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path.startswith("/static") or path in self.PUBLIC_PATHS:
            return await call_next(request)
        if not request.session.get("user"):
            return RedirectResponse("/login")
        return await call_next(request)


@router.get("/login")
def login_form(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/")
    return templates.TemplateResponse(request, "login.html")


@router.post("/login")
@limiter.limit("5/5minutes")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    usuario_ok = secrets.compare_digest(username, settings.app_user)
    senha_ok = secrets.compare_digest(password, settings.app_password)
    if usuario_ok and senha_ok:
        request.session["user"] = username
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {"erro": "Usuário ou senha inválidos"},
        status_code=401,
    )


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
