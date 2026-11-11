import os
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select

from app.config import settings
from app.constants import CORES
from app.db import get_session
from app.models import ItemParticipante, ListaPessoa, Pessoa, PessoaTricount
from app.tricount.base import MembroTricount, TricountError

router = APIRouter(prefix="/pessoas", tags=["pessoas"])
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["asset_v"] = int(os.path.getmtime("app/static/style.css"))

# Mensagem para exceção não prevista: `str(exc)` de um erro cru pode vazar
# detalhe interno (URL, token) para a tela, então só o esperado é exibido.
_ERRO_GENERICO = "Não foi possível falar com o Tricount agora."


def _vinculos(session: Session) -> dict[int, PessoaTricount]:
    """Todos os vínculos pessoa↔membro em UM select, indexados por pessoa_id."""
    return {v.pessoa_id: v for v in session.exec(select(PessoaTricount)).all()}


def _membros_ativos() -> list[MembroTricount]:
    """Membros ativos do tricount configurado.

    Import tardio da factory: o módulo da pessoa não deve depender do pacote
    tricount no boot (nem pagar o import quando a feature está desligada).
    """
    from app.tricount.factory import get_tricount

    return [m for m in get_tricount().listar_membros() if m.ativo]


def _mensagem(exc: TricountError) -> str:
    return (str(exc) or _ERRO_GENERICO)[:200]


def _resposta_vinculos(
    request: Request,
    session: Session,
    *,
    membros: list[MembroTricount] | None = None,
    erro: str | None = None,
):
    """Renderiza o container de vínculos (sucesso ou erro).

    Sempre 200: o htmx não faz swap em resposta de erro, e um 500 aqui
    deixaria a tela de Pessoas com o bloco em branco e sem saída.
    """
    return templates.TemplateResponse(
        request,
        "_tricount_vinculos.html",
        {
            "pessoas": session.exec(select(Pessoa).order_by(Pessoa.id)).all(),
            "vinculos": _vinculos(session),
            "membros": membros or [],
            "erro": erro,
        },
    )


def _vinculos_ou_erro(request: Request, session: Session):
    """Busca os membros e cai no estado de erro em vez de estourar 500."""
    try:
        membros = _membros_ativos()
    except TricountError as exc:
        return _resposta_vinculos(request, session, erro=_mensagem(exc))
    except Exception:
        return _resposta_vinculos(request, session, erro=_ERRO_GENERICO)
    return _resposta_vinculos(request, session, membros=membros)


@router.get("")
def listar(request: Request, session: Session = Depends(get_session)):
    pessoas = session.exec(select(Pessoa)).all()
    # Só paga o select dos vínculos quando a feature está ligada.
    vinculos: dict[int, PessoaTricount] = _vinculos(session) if settings.tricount_enabled else {}
    return templates.TemplateResponse(
        request,
        "pessoas.html",
        {
            "pessoas": pessoas,
            "cores": CORES,
            "tricount_enabled": settings.tricount_enabled,
            "vinculos": vinculos,
        },
    )


# ATENÇÃO À ORDEM: esta rota é estática e vem antes de qualquer rota com path
# param. Hoje nenhuma `/{pessoa_id}/...` casaria com ela ("vinculos" != os
# sufixos existentes, e `pessoa_id` é int), mas declarar antes torna o
# roteamento imune a uma rota `/{pessoa_id}/{algo}` futura.
@router.get("/tricount/vinculos")
def tricount_vinculos(request: Request, session: Session = Depends(get_session)):
    # Kill switch: 404 em vez de bater na API não-oficial. O template nem
    # renderiza o bloco quando desligado, então 404 aqui só acontece se
    # alguém chamar a URL na mão.
    if not settings.tricount_enabled:
        raise HTTPException(status_code=404)
    return _vinculos_ou_erro(request, session)


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


@router.post("/{pessoa_id}/tricount")
def vincular_tricount(
    request: Request,
    pessoa_id: int,
    membro_uuid: str = Form(""),
    session: Session = Depends(get_session),
):
    if not settings.tricount_enabled:
        raise HTTPException(status_code=404)

    uuid = membro_uuid.strip()

    if not uuid:
        atual = session.get(PessoaTricount, pessoa_id)
        if atual:
            session.delete(atual)
            session.commit()
        return _vinculos_ou_erro(request, session)

    # O nome é resolvido no servidor: o label que o cliente mandou não é
    # confiável, e `membro_nome` é cache exibido como verdade na tela.
    try:
        membros = _membros_ativos()
    except TricountError as exc:
        return _resposta_vinculos(request, session, erro=_mensagem(exc))
    except Exception:
        return _resposta_vinculos(request, session, erro=_ERRO_GENERICO)

    membro = next((m for m in membros if m.uuid == uuid), None)
    if membro is None:
        # uuid fora dos membros ativos = estado inconsistente (membro removido
        # do grupo, outro tricount configurado): erro de config, não grava.
        return _resposta_vinculos(
            request,
            session,
            erro="Esse membro não está mais no tricount configurado. Recarregue a lista.",
        )

    pessoa = session.get(Pessoa, pessoa_id)
    if pessoa is None:
        # Pessoa excluída em outra aba: só devolve a lista atualizada.
        return _resposta_vinculos(request, session, membros=membros)

    atual = session.get(PessoaTricount, pessoa_id)
    if atual:
        atual.membro_uuid = membro.uuid
        atual.membro_nome = membro.nome
        atual.vinculado_em = datetime.now(timezone.utc)
        session.add(atual)
    else:
        session.add(
            PessoaTricount(
                pessoa_id=pessoa_id,
                membro_uuid=membro.uuid,
                membro_nome=membro.nome,
            )
        )
    session.commit()
    return _resposta_vinculos(request, session, membros=membros)


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
        # Vale mesmo com tricount_enabled falso: dado órfão é dado órfão.
        vinculo = session.get(PessoaTricount, pessoa_id)
        if vinculo:
            session.delete(vinculo)
        session.delete(pessoa)
        session.commit()
    return RedirectResponse("/pessoas", status_code=303)
