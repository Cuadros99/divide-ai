"""Orquestração do lançamento da despesa de uma lista no Tricount.

Esta camada é a única que conhece ao mesmo tempo o banco (`ListaTricount`,
`PessoaTricount`) e o backend do Tricount. O router só chama `enviar_despesa` e
renderiza o que voltar.

Duas regras valem para tudo aqui:

* **Fail-open.** A conclusão da lista nunca pode falhar por causa de uma API
  não-oficial, então `enviar_despesa` **nunca levanta**: todo erro vira uma linha
  `ListaTricount` com `status="erro"` e a UI oferece "Reenviar ao Tricount".
* **Idempotência pela PK.** `ListaTricount.lista_id` é PK, então existe no máximo
  um envio por lista. É isso que impede o relançamento no ciclo
  reabrir → concluir.
"""

import hashlib
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import settings
from app.models import Lista, ListaTricount, Pessoa, PessoaTricount
from app.tricount.base import Alocacao, TricountError
from app.tricount.factory import get_tricount
from app.tricount.rateio import ratear_centavos

logger = logging.getLogger(__name__)

# Quanto tempo um envio pode ficar "enviando" antes de a gente considerar que o
# processo morreu no meio da chamada. Abaixo disso tratamos como request em voo
# (duplo clique no mobile) e não disparamos um segundo lançamento; acima disso a
# UI libera o reenvio manual. 5 min é folgado em relação ao timeout de ~8s da
# chamada, mas curto o bastante para não travar o botão por muito tempo.
GRACE_ENVIANDO = timedelta(minutes=5)


def hash_rateio(alocacoes: list[Alocacao]) -> str:
    """sha256 determinístico das alocações, para detectar divisão alterada.

    Determinístico de propósito: o template compara o hash do rateio atual da tela
    com o `rateio_hash` gravado no envio para avisar "a divisão mudou depois do
    lançamento". Por isso ordenamos por `membro_uuid` antes de serializar (a ordem
    em que as alocações foram montadas não pode mudar o hash) e incluímos o total,
    para que o hash também mude se o conjunto de membros mudar mantendo os valores.
    """
    pares = sorted((a.membro_uuid, a.valor_centavos) for a in alocacoes)
    total = sum(centavos for _, centavos in pares)
    payload = "|".join(f"{uuid}:{centavos}" for uuid, centavos in pares) + f"#total:{total}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _agora() -> datetime:
    return datetime.now(timezone.utc)


def _como_utc(valor: datetime | None) -> datetime | None:
    """Normaliza `atualizado_em` para tz-aware UTC antes de qualquer comparação.

    O SQLite/libsql guarda DATETIME como texto sem offset, então um valor gravado
    tz-aware volta do banco **naive** — e comparar naive com
    `datetime.now(timezone.utc)` levanta TypeError. Foi exatamente esse detalhe que
    derrubou a produção antes (commit 02bfd51). Como só gravamos UTC, assumir UTC
    no valor naive é correto.
    """
    if valor is None:
        return None
    if valor.tzinfo is None:
        return valor.replace(tzinfo=timezone.utc)
    return valor.astimezone(timezone.utc)


def envio_em_voo(envio: ListaTricount | None) -> bool:
    """True quando outro request parece estar no meio da chamada de rede agora.

    É a pergunta do passo 2 do envio: "devo desistir de reivindicar o slot?".
    `enviando` recente é duplo clique no mobile (ou duas instâncias); passado o
    grace period assumimos que o processo anterior morreu e o slot pode ser
    retomado. `atualizado_em` ausente conta como *não* em voo: na dúvida é melhor
    reivindicar (e deixar a PK resolver o empate) do que travar o lançamento.

    Não é o complemento de `envio_pendente`: status `ok` e status desconhecido são
    False nas duas, e cada uma responde uma pergunta diferente.
    """
    if envio is None or envio.status != "enviando":
        return False
    atualizado = _como_utc(envio.atualizado_em)
    if atualizado is None:
        return False
    return _agora() - atualizado < GRACE_ENVIANDO


def envio_pendente(envio: ListaTricount | None) -> bool:
    """True quando o lançamento falhou ou ficou pendurado em `enviando`.

    É a pergunta da UI: "devo oferecer o botão Reenviar ao Tricount?". Diferente de
    `envio_em_voo`, que o envio usa para decidir se abandona a tentativa: `erro` é
    pendente para a UI mas não impede reivindicar o slot, e um `enviando` recente
    não é pendente justamente porque ainda está em voo.
    """
    if envio is None or envio.status == "ok":
        return False
    if envio.status == "erro":
        return True
    if envio.status != "enviando":
        return False
    atualizado = _como_utc(envio.atualizado_em)
    if atualizado is None:
        return True
    return _agora() - atualizado > GRACE_ENVIANDO


def _devedores(totais: list[tuple[Pessoa, float]]) -> list[tuple[Pessoa, float]]:
    """Só quem deve algo entra na despesa: `valor > 0` tira de cena o participante
    da lista que não ficou com nenhum item."""
    return [(pessoa, valor) for pessoa, valor in totais if valor > 0]


def _alocacoes(
    session: Session, devedores: list[tuple[Pessoa, float]]
) -> tuple[list[Alocacao], list[str], int]:
    """Rateia em centavos e resolve o vínculo pessoa↔membro.

    Devolve `(alocacoes, nomes_sem_vinculo, total_centavos)` em vez de já decidir o
    que fazer, porque os dois chamadores querem coisas diferentes do mesmo cálculo:
    `_enviar` precisa dos nomes para montar a mensagem de erro e do total para o log
    de divergência, e `alocacoes_atuais` só quer saber se dá para comparar hashes.

    Dois detalhes deliberados:

    * **Um SELECT com `IN`** em vez de um `session.get` por pessoa: cada get é um RTT
      contra o Turso, e o caminho de reconstrução do hash roda a cada render de lista
      concluída.
    * **O vínculo é checado antes de descartar a cota de 0 centavo.** Fail-safe: uma
      pessoa sem vínculo cuja cota zerou no arredondamento ainda assim bloqueia o
      envio, em vez de sair silenciosamente da despesa.
    """
    total_centavos, centavos = ratear_centavos([valor for _, valor in devedores])
    vinculos = {
        vinculo.pessoa_id: vinculo
        for vinculo in session.exec(
            select(PessoaTricount).where(
                PessoaTricount.pessoa_id.in_([pessoa.id for pessoa, _ in devedores])
            )
        ).all()
    }

    sem_vinculo: list[str] = []
    alocacoes: list[Alocacao] = []
    for (pessoa, _valor), valor_centavos in zip(devedores, centavos):
        vinculo = vinculos.get(pessoa.id)
        if vinculo is None:
            sem_vinculo.append(pessoa.nome)
            continue
        if valor_centavos == 0:
            # Descartar alocação de 0 centavo não quebra a invariante do rateio
            # (a soma das demais continua igual ao total), e o Tricount não tem o
            # que fazer com uma cota zerada.
            continue
        alocacoes.append(Alocacao(membro_uuid=vinculo.membro_uuid, valor_centavos=valor_centavos))
    return alocacoes, sem_vinculo, total_centavos


def alocacoes_atuais(
    session: Session, totais: list[tuple[Pessoa, float]]
) -> list[Alocacao] | None:
    """As alocações que um envio feito agora produziria — para comparar hashes.

    Mesmo cálculo que `_enviar` usa, para que a regra do split viva num lugar só.
    Devolve `None` quando falta vínculo para alguém com cota: aí não dá para dizer se
    a divisão divergiu do que foi lançado.
    """
    devedores = _devedores(totais)
    if not devedores:
        return []
    alocacoes, sem_vinculo, _total_centavos = _alocacoes(session, devedores)
    if sem_vinculo:
        return None
    return alocacoes


def _registrar_erro(session: Session, lista_id: int, mensagem: str) -> ListaTricount:
    """Persiste a falha na linha da lista (upsert) e devolve a linha para a UI.

    Não mexe em `tentativas`: só conta como tentativa o envio que chegou a
    reivindicar o slot (passo 6) — erro de vínculo/config nunca tocou a API.
    """
    linha = session.get(ListaTricount, lista_id)
    if linha is None:
        linha = ListaTricount(lista_id=lista_id, status="erro")
    linha.status = "erro"
    linha.erro = mensagem[:500]
    linha.atualizado_em = _agora()
    session.add(linha)
    session.commit()
    return linha


def enviar_despesa(
    session: Session, lista: Lista, totais: list[tuple[Pessoa, float]]
) -> ListaTricount | None:
    """Lança a despesa da lista no Tricount e devolve o estado do envio.

    `totais` já vem calculado pelo router (`_totais_por_pessoa`): evita o import
    circular com `app.routers.listas` e não repete as queries de itens/marcações.

    Devolve `None` quando a integração está desligada, ou a linha `ListaTricount`
    (`ok` | `enviando` | `erro`) em qualquer outro caso. **Nunca levanta.**
    """
    # Passo 1: kill switch. Com a integração desligada nem tocamos no banco, para
    # que a feature não deixe rastro (nem linhas, nem UI).
    if not settings.tricount_enabled:
        return None

    try:
        return _enviar(session, lista, totais)
    except Exception:
        # Rede de segurança do fail-open: qualquer bug nosso (ou falha de banco)
        # daqui não pode estourar no handler de concluir a lista.
        logger.exception("Falha inesperada ao enviar despesa ao Tricount (lista_id=%s)", lista.id)
        try:
            session.rollback()
            return session.get(ListaTricount, lista.id)
        except Exception:
            logger.exception("Não foi possível ler o estado do envio (lista_id=%s)", lista.id)
            return None


def _enviar(
    session: Session, lista: Lista, totais: list[tuple[Pessoa, float]]
) -> ListaTricount | None:
    lista_id = lista.id

    # Passo 2: idempotência e proteção contra request duplicado.
    atual = session.get(ListaTricount, lista_id)
    if atual is not None:
        if atual.status == "ok":
            # Anti-duplicata do ciclo reabrir → concluir: já lançado, nada a fazer.
            return atual
        if envio_em_voo(atual):
            # Outro request está no meio da chamada (ou o usuário deu double-tap).
            return atual
        # `enviando` fora do grace period cai aqui: o processo anterior provavelmente
        # morreu durante a chamada de rede, então reivindicamos o slot de novo.

    # Passo 3: rateio em centavos. Só quem deve algo entra na despesa; `valor > 0`
    # tira de cena participantes da lista que não ficaram com nenhum item.
    devedores = _devedores(totais)
    if not devedores:
        return _registrar_erro(
            session, lista_id, "Nada a lançar: nenhuma pessoa com valor nesta lista."
        )

    # Passo 4: vínculo pessoa↔membro. Adivinhar por nome lança na pessoa errada,
    # então sem vínculo explícito não há envio. O cálculo é o mesmo que
    # `alocacoes_atuais` expõe para a comparação de hash da tela.
    alocacoes, sem_vinculo, total_centavos = _alocacoes(session, devedores)

    if sem_vinculo:
        # Ordem determinística (alfabética) para a mensagem não dançar entre
        # recarregamentos da tela.
        nomes = ", ".join(sorted(sem_vinculo))
        return _registrar_erro(session, lista_id, f"Sem vínculo no Tricount: {nomes}")

    if not alocacoes:
        return _registrar_erro(
            session,
            lista_id,
            "Nada a lançar: todas as cotas ficaram em R$ 0,00 depois do arredondamento.",
        )

    # Passo 5: pagador fixo por conta — a pessoa marcada como dono. Nada de env var:
    # quando existirem várias contas, `is_dono` passa a ser escopado por conta e
    # esta integração não muda.
    dono = session.exec(select(Pessoa).where(Pessoa.is_dono == True)).first()  # noqa: E712
    if dono is None:
        return _registrar_erro(
            session,
            lista_id,
            "Nenhuma pessoa está marcada como dono da conta para pagar a despesa.",
        )
    vinculo_dono = session.get(PessoaTricount, dono.id)
    if vinculo_dono is None:
        return _registrar_erro(
            session, lista_id, f"Sem vínculo no Tricount: {dono.nome} (pagador)"
        )
    # O dono pode não ser participante desta lista: isso não é erro. Ele paga o
    # total e só aparece nas alocações se tiver cota — é a semântica do Tricount
    # (o pagador paga o `amount` inteiro e a cota dele está dentro do split).

    rateio_hash = hash_rateio(alocacoes)
    amount_centavos = sum(a.valor_centavos for a in alocacoes)

    # Passo 6: reivindica o slot e **commita antes** da chamada de rede. Assim um
    # segundo request (duplo clique) vê `enviando` no passo 2 e desiste, e um crash
    # no meio da chamada deixa rastro em vez de sumir.
    try:
        if atual is None:
            atual = ListaTricount(
                lista_id=lista_id,
                status="enviando",
                tentativas=1,
                rateio_hash=rateio_hash,
                atualizado_em=_agora(),
            )
        else:
            atual.status = "enviando"
            atual.tentativas = (atual.tentativas or 0) + 1
            atual.rateio_hash = rateio_hash
            atual.atualizado_em = _agora()
        session.add(atual)
        session.commit()
    except IntegrityError:
        # Corrida entre instâncias: as duas tentaram inserir a mesma PK. O slot não
        # é lock distribuído, mas a PK resolve o empate — quem perdeu trata como
        # "já em envio" e devolve o que estiver gravado.
        session.rollback()
        logger.warning(
            "Envio ao Tricount já reivindicado por outra instância (lista_id=%s)", lista_id
        )
        return session.get(ListaTricount, lista_id)

    # Passo 7: a chamada de rede, com os dois excepts separados — `TricountError` é
    # falha esperada da integração (config/indisponibilidade) e qualquer outra é bug
    # ou surpresa da lib não-oficial. Nos dois casos a lista já está concluída.
    try:
        despesa = get_tricount().criar_despesa(
            descricao=lista.nome,
            pagador_uuid=vinculo_dono.membro_uuid,
            alocacoes=alocacoes,
        )
    except TricountError as exc:
        logger.exception("Erro da integração ao lançar a despesa (lista_id=%s)", lista_id)
        return _registrar_erro(session, lista_id, str(exc) or exc.__class__.__name__)
    except Exception as exc:
        logger.exception("Erro inesperado ao lançar a despesa no Tricount (lista_id=%s)", lista_id)
        return _registrar_erro(session, lista_id, str(exc) or exc.__class__.__name__)

    # Passo 8: sucesso. Guardamos `transaction_id` e `rateio_hash` para auditoria e
    # para habilitar a fase 2 (atualizar a despesa quando a divisão mudar), e o
    # token para saber qual grupo recebeu o lançamento.
    transaction_id = str(despesa.transaction_id)
    atual.status = "ok"
    atual.transaction_id = transaction_id
    atual.amount_centavos = despesa.amount_centavos
    atual.tricount_token = settings.tricount_token
    atual.erro = None
    atual.atualizado_em = _agora()
    session.add(atual)
    session.commit()
    # logamos a partir das variáveis locais: a sessão usa expire_on_commit=True, então
    # reler atributos de `atual` aqui custaria um SELECT de refresh (RTT no Turso).
    logger.info(
        "Despesa lançada no Tricount (lista_id=%s, transaction_id=%s, amount_centavos=%s)",
        lista_id,
        transaction_id,
        despesa.amount_centavos,
    )
    if amount_centavos != despesa.amount_centavos or amount_centavos != total_centavos:
        # Não é erro (gravamos o que o backend confirmou), mas divergência entre o
        # total da tela e o que foi lançado merece rastro: acontece quando há item
        # sem participante ou cota zerada descartada.
        logger.warning(
            "Total lançado difere do rateio da tela (lista_id=%s, alocado=%s, "
            "confirmado=%s, total_rateio=%s)",
            lista_id,
            amount_centavos,
            despesa.amount_centavos,
            total_centavos,
        )
    return atual
