from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class MembroTricount:
    """Membro de um tricount, já reduzido ao que o app precisa.

    Identificamos o membro pelo `uuid` (e não pelo nome) porque casar por nome
    lança a despesa na pessoa errada quando os nomes divergem entre o app e o
    Tricount. `ativo` é False para membros removidos do grupo.
    """

    uuid: str
    nome: str
    ativo: bool


@dataclass(frozen=True)
class Alocacao:
    """Quanto um membro deve numa despesa.

    O valor é sempre **centavos inteiros**: float não fecha a conta de um split
    e a unidade de moeda que a API espera é problema da implementação real.
    """

    membro_uuid: str
    valor_centavos: int


@dataclass(frozen=True)
class DespesaCriada:
    """Resultado de um lançamento bem-sucedido, para auditoria/idempotência."""

    transaction_id: str
    amount_centavos: int


class TricountError(Exception):
    """Base de todos os erros da integração, para o service capturar de uma vez."""


class TricountConfigError(TricountError):
    """Falta de configuração ou de vínculo: token ausente, pessoa/dono sem membro.

    É erro nosso, não do Tricount — repetir a chamada não resolve.
    """


class TricountIndisponivel(TricountError):
    """Falha de comunicação: HTTPError, timeout, autenticação.

    É transitório em princípio, então a UI oferece reenvio.
    """


class TricountBackend(ABC):
    """Interface para lançar despesas num tricount (grupo) do Tricount.

    O domínio fala apenas **centavos `int`** e **uuid de membro**. Sharing token,
    credenciais do device, o objeto `Member` da lib e a unidade de moeda que a API
    espera ficam inteiramente dentro da implementação real — o router e o service
    nunca veem nada disso. Trocar de provedor (ou usar o fake) é só escrever outra
    implementação desta interface e registrá-la em app/tricount/factory.py.
    """

    @abstractmethod
    def listar_membros(self) -> list[MembroTricount]:
        """Devolve os membros do tricount configurado, para o vínculo pessoa↔membro."""
        ...

    @abstractmethod
    def criar_despesa(
        self,
        *,
        descricao: str,
        pagador_uuid: str,
        alocacoes: list[Alocacao],
    ) -> DespesaCriada:
        """Cria uma despesa com split customizado, com os valores exatos por membro.

        Semântica do Tricount: o pagador paga o total inteiro e a cota dele própria
        está dentro de `alocacoes`. O total da despesa é a soma das alocações.
        """
        ...
