import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.constants import CORES
from app.db import get_session
from app.models import ItemParticipante, ListaPessoa, Pessoa

router = APIRouter(prefix="/pessoas", tags=["pessoas"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["asset_v"] = int(os.path.getmtime("app/static/style.css"))


@router.get("")
def listar(request: Request, session: Session = Depends(get_session)):
    pessoas = session.exec(select(Pessoa)).all()
    return templates.TemplateResponse(
        request, "pessoas.html", {"pessoas": pessoas, "cores": CORES}
    )


@router.post("")
def criar(
    nome: str = Form(...),
    email: str = Form(""),
    session: Session = Depends(get_session),
):
    ja_tem_dono = session.exec(select(Pessoa).where(Pessoa.is_dono == True)).first()  # noqa: E712
    pessoa = Pessoa(
        nome=nome,
        email=email or None,
        is_dono=not ja_tem_dono,
    )
    session.add(pessoa)
    session.commit()
    return RedirectResponse("/pessoas", status_code=303)


@router.post("/{pessoa_id}/editar")
def editar(
    pessoa_id: int,
    nome: str = Form(...),
    email: str = Form(""),
    session: Session = Depends(get_session),
):
    pessoa = session.get(Pessoa, pessoa_id)
    if pessoa:
        pessoa.nome = nome
        pessoa.email = email or None
        session.add(pessoa)
        session.commit()
    return RedirectResponse("/pessoas", status_code=303)


@router.post("/{pessoa_id}/excluir")
def excluir(pessoa_id: int, session: Session = Depends(get_session)):
    pessoa = session.get(Pessoa, pessoa_id)
    if pessoa:
        participantes = session.exec(
            select(ItemParticipante).where(ItemParticipante.pessoa_id == pessoa_id)
        ).all()
        for participante in participantes:
            session.delete(participante)
        lista_pessoas = session.exec(
            select(ListaPessoa).where(ListaPessoa.pessoa_id == pessoa_id)
        ).all()
        for lista_pessoa in lista_pessoas:
            session.delete(lista_pessoa)
        session.delete(pessoa)
        session.commit()
    return RedirectResponse("/pessoas", status_code=303)
