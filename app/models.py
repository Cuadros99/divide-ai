from datetime import datetime
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
    criado_em: datetime = Field(default_factory=datetime.utcnow)
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
