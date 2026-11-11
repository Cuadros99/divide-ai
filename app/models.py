from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


class Pessoa(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    nome: str
    email: Optional[str] = None
    is_dono: bool = False


class Lista(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    nome: str
    criado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: str = "rascunho"  # rascunho | concluida
    total_nota_extraido: Optional[float] = None  # total impresso na nota, lido pelo LLM (para conferência)


class Foto(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lista_id: int = Field(foreign_key="lista.id")
    path: str


class Item(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    lista_id: int = Field(foreign_key="lista.id")
    nome: str
    preco: float


class ItemParticipante(SQLModel, table=True):
    item_id: int = Field(foreign_key="item.id", primary_key=True)
    pessoa_id: int = Field(foreign_key="pessoa.id", primary_key=True)


class ListaPessoa(SQLModel, table=True):
    lista_id: int = Field(foreign_key="lista.id", primary_key=True)
    pessoa_id: int = Field(foreign_key="pessoa.id", primary_key=True)


class PessoaTricount(SQLModel, table=True):
    pessoa_id: int = Field(foreign_key="pessoa.id", primary_key=True)
    membro_uuid: str
    membro_nome: str  # cache p/ exibir sem bater na API
    vinculado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ListaTricount(SQLModel, table=True):
    lista_id: int = Field(foreign_key="lista.id", primary_key=True)
    status: str  # enviando | ok | erro
    transaction_id: Optional[str] = None
    tricount_token: Optional[str] = None  # qual grupo recebeu (auditoria)
    amount_centavos: Optional[int] = None
    rateio_hash: Optional[str] = None  # sha256 das alocações enviadas
    erro: Optional[str] = None  # str(exc)[:500]
    tentativas: int = 0
    atualizado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
