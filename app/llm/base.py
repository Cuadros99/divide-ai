from abc import ABC, abstractmethod
from typing import Optional

from pydantic import BaseModel


class ItemExtraido(BaseModel):
    nome: str
    preco: float


class ResultadoExtracao(BaseModel):
    itens: list[ItemExtraido]
    total_nota: Optional[float] = None
    # Data da compra impressa na nota/lista, no formato "DD/MM/AAAA" (padrão
    # brasileiro usado em todo o app). None quando não foi possível identificar.
    data_compra: Optional[str] = None


class ListaParser(ABC):
    """Interface para extrair itens e preços de uma foto de lista/nota fiscal.

    Trocar de provedor de LLM significa apenas escrever uma nova
    implementação desta interface e registrá-la em app/llm/factory.py.
    """

    @abstractmethod
    async def parse_images(self, images: list[bytes]) -> ResultadoExtracao:
        """Extrai os itens a partir de uma ou mais fotos da mesma lista/nota fiscal.

        Quando há mais de uma foto, a implementação deve evitar itens duplicados
        apenas quando o mesmo produto aparece em mais de uma imagem por sobreposição
        entre as fotos — itens repetidos dentro da mesma foto (ex: 2 unidades do
        mesmo produto em linhas consecutivas) devem ser mantidos como itens separados.
        """
        ...
