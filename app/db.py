from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

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
    _backfill_lista_pessoa()


def _backfill_lista_pessoa() -> None:
    from app.models import Item, ItemParticipante, Lista, ListaPessoa, Pessoa

    with Session(engine) as session:
        listas = session.exec(select(Lista)).all()
        for lista in listas:
            ja_tem = session.exec(
                select(ListaPessoa).where(ListaPessoa.lista_id == lista.id)
            ).first()
            if ja_tem:
                continue

            item_ids = session.exec(select(Item.id).where(Item.lista_id == lista.id)).all()
            pessoa_ids = set()
            if item_ids:
                participantes = session.exec(
                    select(ItemParticipante.pessoa_id).where(
                        ItemParticipante.item_id.in_(item_ids)
                    )
                ).all()
                pessoa_ids = set(participantes)

            if not pessoa_ids:
                pessoa_ids = set(session.exec(select(Pessoa.id)).all())

            for pessoa_id in pessoa_ids:
                session.add(ListaPessoa(lista_id=lista.id, pessoa_id=pessoa_id))

        session.commit()


def get_session():
    with Session(engine) as session:
        yield session
