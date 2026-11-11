import os
import re
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, delete, insert, select

from app.config import settings
from app.constants import CORES
from app.db import get_session
from app.llm.factory import get_parser
from app.models import (
    Foto,
    Item,
    ItemParticipante,
    Lista,
    ListaPessoa,
    ListaTricount,
    Pessoa,
)
from app.storage import get_storage
from app.tricount.service import (
    alocacoes_atuais,
    enviar_despesa,
    envio_em_voo,
    envio_pendente,
    hash_rateio,
)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
templates.env.globals["foto_url"] = lambda key: get_storage().url_for(key)
templates.env.globals["asset_v"] = int(os.path.getmtime("app/static/style.css"))

_DATA_BR_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")


def _totais_por_pessoa(
    itens: list[Item], pessoas: list[Pessoa], marcados: set[tuple[int, int]]
) -> list[tuple[Pessoa, float]]:
    participantes_por_item: dict[int, set[int]] = {}
    for item_id, pessoa_id in marcados:
        participantes_por_item.setdefault(item_id, set()).add(pessoa_id)

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


def _tem_rateio(totais: list[tuple[Pessoa, float]]) -> bool:
    """Existe alguém com valor > 0, ou seja: há de fato algo para dividir?

    É o critério único de "a lista pode ser concluída", usado pelo guard do servidor em
    `concluir_lista` e pelo `pode_concluir` que desabilita o botão na tela. Deliberadamente
    "valor > 0" e não "existe ItemParticipante": uma lista marcada só em itens de preço 0
    tem marcações e ainda assim não tem nada a dividir.

    A regra é de produto ("concluir uma lista sem rateio não significa nada"), NÃO da
    integração com o Tricount: vale sempre, independente de `settings.tricount_enabled`.
    Amarrá-la ao kill switch faria o app mudar de comportamento ao desligar o Tricount.
    """
    return any(valor > 0 for _, valor in totais)


def _ids_membros(lista_id: int, session: Session) -> list[int]:
    # só os ids (e não os objetos Pessoa): a sessão usa expire_on_commit=True, então objeto
    # ORM carregado antes de um commit é expirado e cobra um SELECT de refresh por linha depois.
    return session.exec(
        select(Pessoa.id)
        .join(ListaPessoa, ListaPessoa.pessoa_id == Pessoa.id)
        .where(ListaPessoa.lista_id == lista_id)
        .order_by(Pessoa.id)
    ).all()


def _marcados_dos_itens(item_ids: list[int], session: Session) -> set[tuple[int, int]]:
    if not item_ids:
        return set()
    participantes = session.exec(
        select(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids))
    ).all()
    return {(p.item_id, p.pessoa_id) for p in participantes}


def _totais_context(lista_id: int, session: Session) -> dict:
    # contexto mínimo de _totais.html (que só usa totais e cores)
    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    pessoas = session.exec(
        select(Pessoa)
        .join(ListaPessoa, ListaPessoa.pessoa_id == Pessoa.id)
        .where(ListaPessoa.lista_id == lista_id)
        .order_by(Pessoa.id)
    ).all()
    marcados = _marcados_dos_itens([i.id for i in itens], session)
    totais = _totais_por_pessoa(itens, pessoas, marcados)
    # lista_id entra no contexto porque o botão "Concluir lista" agora mora em
    # _totais.html (ver o comentário do template) e precisa montar o action do form.
    return {
        "totais": totais,
        "cores": CORES,
        "lista_id": lista_id,
        "pode_concluir": _tem_rateio(totais),
    }


def _envio_tricount_divergente(
    session: Session, envio: ListaTricount | None, totais: list[tuple[Pessoa, float]]
) -> bool:
    """True quando a divisão na tela já não é a que foi lançada no Tricount.

    Caminho frio por construção: só roda com lançamento `ok` e hash gravado.
    """
    if envio is None or envio.status != "ok" or not envio.rateio_hash:
        return False
    alocacoes = alocacoes_atuais(session, totais)
    if alocacoes is None:  # sem vínculo: não dá para comparar, então não avisamos nada
        return False
    return hash_rateio(alocacoes) != envio.rateio_hash


def _rateio_context(
    lista_id: int,
    session: Session,
    membros_ids: set[int] | None = None,
    marcados: set[tuple[int, int]] | None = None,
) -> dict:
    # membros_ids/marcados vêm preenchidos quando quem chamou já descobriu o estado final,
    # evitando repetir os mesmos SELECTs (cada um é um RTT contra o Turso).
    lista = session.get(Lista, lista_id)
    itens = session.exec(select(Item).where(Item.lista_id == lista_id)).all()
    todas_pessoas = session.exec(select(Pessoa).order_by(Pessoa.id)).all()

    if membros_ids is None:
        membros_ids = set(_ids_membros(lista_id, session))
    # todas_pessoas já vem ordenada por id, então filtrar preserva a ordem do antigo join
    pessoas = [p for p in todas_pessoas if p.id in membros_ids]
    membros_ids = {p.id for p in pessoas}

    if marcados is None:
        marcados = _marcados_dos_itens([i.id for i in itens], session)
    todos_marcados = bool(itens) and bool(pessoas) and len(marcados) == len(itens) * len(pessoas)

    totais = _totais_por_pessoa(itens, pessoas, marcados)
    total_geral = sum(v for _, v in totais)
    total_compra = sum(i.preco for i in itens)

    # Caminho quente intacto: só lista concluída (com a integração ligada) paga o
    # session.get extra. Os swaps HTMX de toggle acontecem apenas em lista `rascunho` — os
    # checkboxes ficam `disabled` quando concluída —, então os partials não ganham RTT
    # contra o Turso e a otimização de RTTs deste módulo continua valendo.
    envio_tricount = (
        session.get(ListaTricount, lista_id)
        if settings.tricount_enabled and lista and lista.status == "concluida"
        else None
    )

    return {
        "lista": lista,
        "lista_id": lista_id,
        "pode_concluir": _tem_rateio(totais),
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
        "tricount_enabled": settings.tricount_enabled,
        "envio_tricount": envio_tricount,
        # mesmo guard de caminho frio: `envio_tricount` só é não-nulo em lista concluída
        # com a integração ligada, e as duas funções devolvem False para None.
        "envio_tricount_em_voo": envio_em_voo(envio_tricount),
        "envio_tricount_pendente": envio_pendente(envio_tricount),
        "envio_tricount_divergente": _envio_tricount_divergente(
            session, envio_tricount, totais
        ),
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
        # limpeza referencial à mão (o projeto não usa cascade), independente de
        # tricount_enabled: a linha pode ter sobrado de quando a integração estava ligada
        envio_tricount = session.get(ListaTricount, lista_id)
        if envio_tricount:
            session.delete(envio_tricount)
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
def rateio_lista(
    lista_id: int,
    request: Request,
    erro: str | None = None,
    session: Session = Depends(get_session),
):
    # `erro` é o sinal da recusa do POST /concluir (redirect 303 com ?erro=sem_rateio).
    # Chega por query param, e não por 400 cru, porque o form é um POST normal (sem HTMX):
    # o usuário continua na tela de rateio, que é onde ele resolve o problema.
    ctx = _rateio_context(lista_id, session)
    ctx["erro"] = erro
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
    return templates.TemplateResponse(request, "_totais.html", _totais_context(item.lista_id, session))


@router.post("/listas/{lista_id}/participantes/alternar-todos")
def alternar_todos(lista_id: int, request: Request, session: Session = Depends(get_session)):
    item_ids = session.exec(select(Item.id).where(Item.lista_id == lista_id)).all()
    pessoa_ids = _ids_membros(lista_id, session)

    existentes = _marcados_dos_itens(item_ids, session)
    todos_marcados = (
        bool(item_ids) and bool(pessoa_ids) and len(existentes) == len(item_ids) * len(pessoa_ids)
    )

    if item_ids:
        session.exec(delete(ItemParticipante).where(ItemParticipante.item_id.in_(item_ids)))

    marcados: set[tuple[int, int]] = set()
    if not todos_marcados and item_ids and pessoa_ids:
        rows = [
            {"item_id": item_id, "pessoa_id": pessoa_id}
            for item_id in item_ids
            for pessoa_id in pessoa_ids
        ]
        session.exec(insert(ItemParticipante).values(rows))
        marcados = {(r["item_id"], r["pessoa_id"]) for r in rows}

    session.commit()

    # o estado final é determinístico (tudo marcado ou nada), então não relemos itemparticipante
    ctx = _rateio_context(lista_id, session, membros_ids=set(pessoa_ids), marcados=marcados)
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


# Handler sync (`def`) de propósito: o Starlette roda handler sync no threadpool, então a
# chamada de rede bloqueante do Tricount não trava o event loop — não precisa de `async`
# nem de `asyncio.to_thread`.
@router.post("/listas/{lista_id}/concluir")
def concluir_lista(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    if not lista:
        raise HTTPException(status_code=404)

    # Guard de servidor (a autoridade): o botão desabilitado é só conveniência, aqui a
    # recusa vale até para POST feito na mão. Vale independente de tricount_enabled —
    # ver _tem_rateio. Recusa = redirect 303 de volta à tela de rateio com o motivo, em
    # vez de 400 cru: o form é um POST normal, sem HTMX.
    ctx = _totais_context(lista_id, session)  # reuso; 3 queries, não o _rateio_context todo
    if not ctx["pode_concluir"]:
        return RedirectResponse(f"/listas/{lista_id}/rateio?erro=sem_rateio", status_code=303)

    lista.status = "concluida"
    session.add(lista)
    session.commit()  # commit primeiro: a conclusão NUNCA depende do Tricount (fail-open)

    if settings.tricount_enabled:
        # Reusa o mesmo `totais` que autorizou a conclusão (uma fonte de verdade só).
        # Custo: a sessão usa expire_on_commit=True, então cada Pessoa do rateio custa um
        # SELECT de refresh aqui — bem menos que recalcular o contexto inteiro, e este é
        # um caminho frio (uma conclusão de lista, atrás de uma chamada de rede de ~8s).
        enviar_despesa(session, lista, ctx["totais"])  # nunca levanta; persiste o erro

    return RedirectResponse(f"/listas/{lista_id}/rateio?concluida=1", status_code=303)


@router.post("/listas/{lista_id}/reabrir")
def reabrir_lista(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    if not lista:
        raise HTTPException(status_code=404)
    lista.status = "rascunho"
    session.add(lista)
    session.commit()
    # NÃO mexer no ListaTricount aqui: é justamente a linha `status == "ok"` preservada que
    # impede o relançamento duplicado no ciclo reabrir → concluir. "Limpar o envio ao
    # reabrir" parece arrumação, mas lança a despesa duas vezes no Tricount.
    return RedirectResponse(f"/listas/{lista_id}/rateio", status_code=303)


# Sem colisão de rota: o prefixo literal `tricount` difere de `participantes`/`concluir`/
# `reabrir` e `{lista_id}` não casa com `/`, então só esta rota atende este caminho.
@router.post("/listas/{lista_id}/tricount/reenviar")
def reenviar_tricount(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    if settings.tricount_enabled and lista and lista.status == "concluida":
        envio = session.get(ListaTricount, lista_id)
        if envio is None or envio.status != "ok":  # já lançado não se relança
            ctx = _totais_context(lista_id, session)
            enviar_despesa(session, lista, ctx["totais"])
    return RedirectResponse(f"/listas/{lista_id}/rateio", status_code=303)
