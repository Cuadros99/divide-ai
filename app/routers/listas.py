import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, insert, select

from app.constants import CORES
from app.db import get_session
from app.llm.factory import get_parser
from app.models import Foto, Item, ItemParticipante, Lista, ListaPessoa, Pessoa
from app.storage import get_storage

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["foto_url"] = lambda key: get_storage().url_for(key)

_DATA_BR_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _totais_por_pessoa(session: Session, itens: list[Item], pessoas: list[Pessoa]) -> list[tuple[Pessoa, float]]:
    item_ids = [i.id for i in itens]

    participantes = (
        session.exec(select(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids))).all()
        if item_ids
        else []
    )
    participantes_por_item: dict[int, set[int]] = {}
    for p in participantes:
        participantes_por_item.setdefault(p.item_id, set()).add(p.pessoa_id)

    totais = {pessoa.id: 0.0 for pessoa in pessoas}
    for item in itens:
        pessoas_do_item = participantes_por_item.get(item.id, set())
        if not pessoas_do_item:
            continue
        valor_por_pessoa = item.preco / len(pessoas_do_item)
        for pessoa_id in pessoas_do_item:
            if pessoa_id in totais:
                totais[pessoa_id] += valor_por_pessoa

    return [(pessoa, totais[pessoa.id]) for pessoa in pessoas]


def _rateio_context(lista_id: int, session: Session) -> dict:
    lista = session.get(Lista, lista_id)
    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    pessoas = session.exec(
        select(Pessoa)
        .join(ListaPessoa, ListaPessoa.pessoa_id == Pessoa.id)
        .where(ListaPessoa.lista_id == lista_id)
        .order_by(Pessoa.id)
    ).all()
    todas_pessoas = session.exec(select(Pessoa).order_by(Pessoa.id)).all()
    membros_ids = {p.id for p in pessoas}
    item_ids = [i.id for i in itens]

    participantes = (
        session.exec(select(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids))).all()
        if item_ids
        else []
    )
    marcados = {(p.item_id, p.pessoa_id) for p in participantes}
    todos_marcados = bool(itens) and bool(pessoas) and len(marcados) == len(itens) * len(pessoas)

    totais = _totais_por_pessoa(session, itens, pessoas)
    total_geral = sum(v for _, v in totais)
    total_compra = sum(i.preco for i in itens)

    return {
        "lista": lista,
        "itens": itens,
        "pessoas": pessoas,
        "todas_pessoas": todas_pessoas,
        "membros_ids": membros_ids,
        "marcados": marcados,
        "todos_marcados": todos_marcados,
        "totais": totais,
        "total_geral": total_geral,
        "total_compra": total_compra,
        "cores": CORES,
    }


@router.get("/fotos/{key:path}")
def servir_foto(key: str):
    try:
        conteudo = get_storage().read(key)
    except FileNotFoundError:
        raise HTTPException(status_code=404)
    return Response(content=conteudo, media_type="image/jpeg")


@router.get("/")
def home(request: Request, session: Session = Depends(get_session)):
    listas = session.exec(select(Lista).order_by(Lista.criado_em.desc())).all()
    resumo = []
    for lista in listas:
        itens = session.exec(select(Item).where(Item.lista_id == lista.id)).all()
        resumo.append({"lista": lista, "n_itens": len(itens), "total": sum(i.preco for i in itens)})
    return templates.TemplateResponse(request, "home.html", {"listas": resumo})


@router.post("/listas/{lista_id}/excluir")
def excluir_lista(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    if lista:
        itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
        item_ids = [i.id for i in itens]
        if item_ids:
            participantes = session.exec(
                select(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids))
            ).all()
            for participante in participantes:
                session.delete(participante)
        for item in itens:
            session.delete(item)
        fotos = session.exec(select(Foto).where(Foto.lista_id == lista_id)).all()
        for foto in fotos:
            get_storage().delete(foto.path)
            session.delete(foto)
        session.delete(lista)
        session.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/listas/nova")
def nova_lista_form(request: Request):
    return templates.TemplateResponse(request, "new_list.html")


@router.post("/listas/processar")
async def processar_lista(fotos: list[UploadFile] = File(...), session: Session = Depends(get_session)):
    conteudos = [await foto.read() for foto in fotos]

    lista = Lista(nome="Compras")
    session.add(lista)
    session.commit()
    session.refresh(lista)

    lista_anterior = session.exec(
        select(Lista).where(Lista.id != lista.id).order_by(Lista.criado_em.desc())
    ).first()
    pessoa_ids = set()
    if lista_anterior:
        pessoa_ids = set(
            session.exec(
                select(ListaPessoa.pessoa_id).where(ListaPessoa.lista_id == lista_anterior.id)
            ).all()
        )
    if not pessoa_ids:
        pessoa_ids = set(session.exec(select(Pessoa.id)).all())
    for pessoa_id in pessoa_ids:
        session.add(ListaPessoa(lista_id=lista.id, pessoa_id=pessoa_id))
    session.commit()

    for i, conteudo in enumerate(conteudos):
        key = f"uploads/{lista.id}_{i}.jpg"
        get_storage().save(key, conteudo)
        session.add(Foto(lista_id=lista.id, path=key))
    session.commit()

    parser = get_parser()
    resultado = await parser.parse_images(conteudos)
    for item in resultado.itens:
        session.add(Item(lista_id=lista.id, nome=item.nome, preco=item.preco))
    lista.total_nota_extraido = resultado.total_nota

    if resultado.data_compra and _DATA_BR_RE.match(resultado.data_compra):
        lista.nome = f"Compras {resultado.data_compra}"
    else:
        lista.nome = f"Compras {datetime.now().strftime('%d/%m/%Y')}"

    session.add(lista)
    session.commit()

    return RedirectResponse(f"/listas/{lista.id}/revisar", status_code=303)


@router.get("/listas/{lista_id}/revisar")
def revisar_lista(lista_id: int, request: Request, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    fotos = session.exec(select(Foto).where(Foto.lista_id == lista_id)).all()
    total = sum(i.preco for i in itens)

    total_diverge = (
        lista.total_nota_extraido is not None
        and abs(lista.total_nota_extraido - total) > 0.01
    )

    return templates.TemplateResponse(
        request,
        "list_review.html",
        {
            "lista": lista,
            "itens": itens,
            "fotos": fotos,
            "total": total,
            "total_diverge": total_diverge,
        },
    )


@router.post("/itens/{item_id}/atualizar")
def atualizar_item(
    item_id: int, request: Request, nome: str = Form(...), preco: float = Form(...), session: Session = Depends(get_session)
):
    item = session.get(Item, item_id)
    item.nome = nome
    item.preco = preco
    session.add(item)
    session.commit()

    itens = session.exec(select(Item).where(Item.lista_id == item.lista_id)).all()
    total = sum(i.preco for i in itens)
    return templates.TemplateResponse(request, "_total_compra.html", {"total": total})


@router.post("/listas/{lista_id}/itens")
def adicionar_item(
    lista_id: int, request: Request, nome: str = Form("Novo item"), preco: float = Form(0), session: Session = Depends(get_session)
):
    session.add(Item(lista_id=lista_id, nome=nome, preco=preco))
    session.commit()

    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    total = sum(i.preco for i in itens)
    return templates.TemplateResponse(
        request, "_itens_lista_oob.html", {"itens": itens, "total": total}
    )


@router.post("/itens/{item_id}/excluir")
def excluir_item(item_id: int, request: Request, session: Session = Depends(get_session)):
    item = session.get(Item, item_id)
    lista_id = item.lista_id
    session.delete(item)
    session.commit()

    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    total = sum(i.preco for i in itens)
    return templates.TemplateResponse(
        request, "_itens_lista_oob.html", {"itens": itens, "total": total}
    )


@router.get("/listas/{lista_id}/rateio")
def rateio_lista(lista_id: int, request: Request, session: Session = Depends(get_session)):
    ctx = _rateio_context(lista_id, session)
    return templates.TemplateResponse(request, "split.html", ctx)


@router.post("/itens/{item_id}/participantes/{pessoa_id}/toggle")
def toggle_participante(item_id: int, pessoa_id: int, request: Request, session: Session = Depends(get_session)):
    existente = session.get(ItemParticipante, (item_id, pessoa_id))
    if existente:
        session.delete(existente)
    else:
        session.add(ItemParticipante(item_id=item_id, pessoa_id=pessoa_id))
    session.commit()

    item = session.get(Item, item_id)
    ctx = _rateio_context(item.lista_id, session)
    return templates.TemplateResponse(request, "_totais.html", ctx)


@router.post("/listas/{lista_id}/participantes/alternar-todos")
def alternar_todos(lista_id: int, request: Request, session: Session = Depends(get_session)):
    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    pessoas = session.exec(
        select(Pessoa)
        .join(ListaPessoa, ListaPessoa.pessoa_id == Pessoa.id)
        .where(ListaPessoa.lista_id == lista_id)
        .order_by(Pessoa.id)
    ).all()
    item_ids = [i.id for i in itens]

    existentes = (
        session.exec(select(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids))).all()
        if item_ids
        else []
    )
    todos_marcados = bool(itens) and bool(pessoas) and len(existentes) == len(itens) * len(pessoas)

    if item_ids:
        session.exec(delete(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids)))

    if not todos_marcados and itens and pessoas:
        rows = [{"item_id": item.id, "pessoa_id": pessoa.id} for item in itens for pessoa in pessoas]
        session.exec(insert(ItemParticipante).values(rows))

    session.commit()

    ctx = _rateio_context(lista_id, session)
    return templates.TemplateResponse(request, "_rateio.html", ctx)


@router.post("/listas/{lista_id}/participantes/{pessoa_id}/toggle")
def toggle_participante_lista(
    lista_id: int, pessoa_id: int, request: Request, session: Session = Depends(get_session)
):
    lista = session.get(Lista, lista_id)
    if not lista:
        raise HTTPException(status_code=404)
    if lista.status == "concluida":
        raise HTTPException(status_code=400)

    existente = session.get(ListaPessoa, (lista_id, pessoa_id))
    if existente:
        session.delete(existente)
        itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
        item_ids = [i.id for i in itens]
        if item_ids:
            session.exec(
                delete(ItemParticipante).where(
                    ItemParticipante.item_id.in_(item_ids),
                    ItemParticipante.pessoa_id == pessoa_id,
                )
            )
    else:
        session.add(ListaPessoa(lista_id=lista_id, pessoa_id=pessoa_id))
    session.commit()

    ctx = _rateio_context(lista_id, session)
    return templates.TemplateResponse(request, "_rateio.html", ctx)


@router.post("/listas/{lista_id}/concluir")
def concluir_lista(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    lista.status = "concluida"
    session.add(lista)
    session.commit()
    return RedirectResponse(f"/listas/{lista_id}/rateio?concluida=1", status_code=303)


@router.post("/listas/{lista_id}/reabrir")
def reabrir_lista(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    lista.status = "rascunho"
    session.add(lista)
    session.commit()
    return RedirectResponse(f"/listas/{lista_id}/rateio", status_code=303)
