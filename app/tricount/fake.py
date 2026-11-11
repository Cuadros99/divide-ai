"""Backend fake do Tricount: exercita o fluxo inteiro sem tocar na API real."""

import logging
import os
from uuid import uuid4

from app.tricount.base import (
    Alocacao,
    DespesaCriada,
    MembroTricount,
    TricountBackend,
    TricountIndisponivel,
)

logger = logging.getLogger(__name__)

# Membros fixos. Os uuids têm prefixo "fake-" de propósito: se um uuid desses
# aparecer num `PessoaTricount` de produção, fica óbvio que o vínculo foi feito
# contra o fake e precisa ser refeito.
_MEMBROS = [
    MembroTricount(uuid="fake-ana", nome="Ana Prado", ativo=True),
    MembroTricount(uuid="fake-bruno", nome="Bruno Carvalho", ativo=True),
    MembroTricount(uuid="fake-carla", nome="Carla Menezes", ativo=True),
    MembroTricount(uuid="fake-diego", nome="Diego Fontes", ativo=True),
    # 5º membro **inativo**, de propósito: o `real.py` filtra `status == DELETED`
    # e o dropdown de Pessoas só deve oferecer membros ativos. Com ele aqui, o
    # filtro é exercitado em desenvolvimento — se alguém esquecer o `if m.ativo`
    # no template, "Elisa Rocha (removida)" aparece na tela e denuncia o bug.
    # O plano pede 4 membros; este 5º é adicional e opcional, e o item 1 da §7
    # ("dropdown lista os 4 membros fake") continua valendo exatamente porque
    # ele não deve aparecer.
    MembroTricount(uuid="fake-elisa", nome="Elisa Rocha (removida)", ativo=False),
]


class FakeTricountBackend(TricountBackend):
    """Backend sem rede, usado com TRICOUNT_PROVIDER=fake.

    Permite rodar o fluxo completo em desenvolvimento (inclusive gravando as
    linhas de `PessoaTricount`/`ListaTricount` no banco) sem registrar device,
    sem sharing token e sem lançar despesa em grupo nenhum.
    """

    def __init__(self) -> None:
        # Contador público de chamadas efetivas a `criar_despesa`. É o que prova
        # a não-duplicação do ciclo reabrir→concluir (item 3 da §7): se o service
        # respeitar o `status == "ok"` guardado, concluir de novo não incrementa.
        #
        # Vale entre requests porque `get_tricount()` é `@lru_cache`: existe uma
        # única instância por processo e o worker do uvicorn a reaproveita. Em
        # contrapartida, o contador zera a cada reinício do processo e não é
        # compartilhado entre instâncias — é instrumento de verificação manual,
        # não métrica.
        self.chamadas_criar_despesa = 0

    def listar_membros(self) -> list[MembroTricount]:
        return list(_MEMBROS)

    def criar_despesa(
        self,
        *,
        descricao: str,
        pagador_uuid: str,
        alocacoes: list[Alocacao],
    ) -> DespesaCriada:
        # Lido de os.environ **na hora da chamada**, não no import: assim dá para
        # ligar a falha, concluir a lista (vira status "erro"), desligar e clicar
        # em "Reenviar ao Tricount" — que é exatamente o item 5 da §7 — sem
        # reiniciar o processo e perder o contador acima.
        if os.environ.get("TRICOUNT_FAKE_FALHA") == "1":
            logger.warning(
                "FakeTricountBackend: falha simulada (TRICOUNT_FAKE_FALHA=1) "
                "descricao=%r pagador_uuid=%r alocacoes=%r",
                descricao,
                pagador_uuid,
                alocacoes,
            )
            raise TricountIndisponivel("Falha simulada do Tricount (TRICOUNT_FAKE_FALHA=1)")

        self.chamadas_criar_despesa += 1
        total = sum(a.valor_centavos for a in alocacoes)
        # Payload inteiro no log: é o substituto do app do Tricount em
        # desenvolvimento — é aqui que se confere se o rateio chegou certo.
        logger.info(
            "FakeTricountBackend.criar_despesa #%d descricao=%r pagador_uuid=%r "
            "total_centavos=%d alocacoes=%r",
            self.chamadas_criar_despesa,
            descricao,
            pagador_uuid,
            total,
            alocacoes,
        )
        return DespesaCriada(
            transaction_id=f"fake-{uuid4().hex[:8]}",
            amount_centavos=total,
        )
