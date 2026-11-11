"""Backend real: fala com a API não-oficial do Tricount (bunq) via `tricount-api`.

Tudo que é detalhe do provedor mora aqui — credenciais de device, sharing token,
o objeto `Member` da lib, a unidade de moeda do protocolo e o timeout de rede.
O resto do app só conhece `MembroTricount`/`Alocacao`/centavos `int`, conforme
`app/tricount/base.py`.

Três decisões estruturais, porque são as que doem se forem esquecidas:

1. **Nada de rede no `__init__`.** O backend é construído no `lru_cache` da
   factory, que roda no primeiro request depois do cold start do Cloud Run. Se o
   construtor autenticasse, uma indisponibilidade do Tricount derrubaria rotas
   que não têm nada a ver com a integração. Autenticação e `get_tricount`
   acontecem no primeiro uso (`_cliente`), memoizados na instância.

2. **Timeout é obrigação nossa.** A lib não expõe `timeout`, então envolvemos a
   `requests.Session` do client (atributo público `TricountAPI.session`). Sem
   isso, uma request pendurada prenderia o worker até o timeout do Cloud Run,
   e o fluxo de concluir lista é síncrono (ver §9 do plano).

3. **`join_tricount` só no caminho de escrita, e só reagindo a um 404.**
   `link_to_member` e `leave_tricount` **nunca** são chamados em runtime — ver
   o comentário destacado em `criar_despesa` e a `_sincronizar_grupo`.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Iterator

from app.config import settings
from app.tricount.base import (
    Alocacao,
    DespesaCriada,
    MembroTricount,
    TricountBackend,
    TricountConfigError,
    TricountError,
    TricountIndisponivel,
)

logger = logging.getLogger(__name__)

# Onde o JSON do device vive em produção. `/tmp` no Cloud Run é tmpfs (conta como
# memória, ~1 KB é irrelevante) e some com a instância — efêmero é desejável: a
# fonte da verdade é o Secret Manager, via TRICOUNT_CREDENTIALS_JSON (§5).
CAMINHO_CREDENCIAIS_PADRAO = Path("/tmp/tricount_credentials.json")

# A API recebe o valor em **unidades de moeda com decimais**, não em centavos:
# `create_transaction_custom_split` serializa `{"value": str(-amount), "currency":
# tricount.currency}` (observado no fonte da lib instalada, tricount-api 0.1.2),
# **Confirmado em campo (2026-11-10):** `scripts/tricount_probe.py unidade`
# escreveu `amount=1234` num tricount descartável e o app Android mostrou
# `R$ 1.234,00`; o servidor devolveu a transação como `-1234.00 BRL` (duas
# casas). Unidade é fato de protocolo, não configuração — por isso constante de
# módulo e não `Settings`.
_CENTAVOS_POR_UNIDADE_API = 100

# Como a serialização é `str(-amount)` crua, **não** passamos float: `str(-0.3)`
# pode sair "-0.30000000000000004" e o valor chega errado no Tricount. `Decimal`
# é exato sob `-` e `str()`, então convertemos centavos→unidades em Decimal.
_QUANTIZACAO = Decimal("0.01")

# O dropdown de Pessoas chama `listar_membros` a cada render; sem cache isso
# martela uma API de engenharia reversa.
_TTL_CACHE_SEGUNDOS = 60.0

# Quanto do corpo da resposta de erro entra na mensagem. O suficiente para
# diagnosticar, pouco o bastante para não vazar um payload inteiro no log.
_MAX_CORPO_ERRO = 300


class TricountAPIBackend(TricountBackend):
    """Implementação de `TricountBackend` sobre a lib `tricount` (pacote `tricount-api`).

    O import da lib é lazy (mesmo padrão do `GCSStorageBackend` em
    `app/storage.py`): quem usa o fake nunca paga o import nem precisa da
    dependência resolvida. Os nomes importados ficam guardados na instância
    porque os métodos precisam deles (`Category`, `MemberStatus`) e reimportar
    em cada chamada seria ruído.
    """

    def __init__(self, caminho_credenciais: Path | str | None = None) -> None:
        # Import lazy: nenhuma rede, só resolução de módulo.
        from tricount import Category, Credentials, MemberStatus, TricountAPI

        # `requests` entra por aqui também (é dependência transitiva da lib, não
        # nossa) para que importar este módulo não exija nada de terceiros.
        import requests

        self._Category = Category
        self._Credentials = Credentials
        self._MemberStatus = MemberStatus
        self._TricountAPI = TricountAPI
        self._requests = requests

        self._caminho_credenciais = Path(
            caminho_credenciais if caminho_credenciais is not None else CAMINHO_CREDENCIAIS_PADRAO
        )

        # Estado memoizado na instância (a factory é `lru_cache`, então na
        # prática é "uma vez por processo"): client autenticado + o `Tricount`.
        self._client: Any | None = None
        self._tricount: Any | None = None
        self._tricount_buscado_em: float = 0.0

        # O grupo já foi sincronizado neste device (`join_tricount`) durante a
        # vida deste processo? É um POST idempotente, mas repeti-lo a cada envio
        # seria um round trip inútil — e, se o 404 voltar mesmo depois do join,
        # insistir não ajudaria. Deliberadamente **não** é resetado por
        # `_descartar_cliente`: o sync é estado do device no servidor, não da
        # sessão HTTP, então re-autenticar não o desfaz.
        self._join_feito = False

    # ------------------------------------------------------------------
    # Credenciais do device (§5)
    # ------------------------------------------------------------------

    def _credenciais(self) -> Any:
        """Materializa as credenciais do device e devolve um `Credentials`.

        Ordem de precedência:

        1. `TRICOUNT_CREDENTIALS_JSON` com conteúdo e o arquivo ainda não
           existe → grava o arquivo (0600) e carrega dele. Carregar do arquivo
           e não do texto mantém um caminho só, igual ao do passo 2.
        2. Arquivo já existe → carrega (o secret não muda no meio da vida da
           instância, então reescrever não traria nada).
        3. Nada → `generate()` + `save()` + WARNING. É conveniência em dev
           (funciona sem bootstrap) e alarme em produção: um device novo a cada
           cold start significa credencial não persistida.
        """
        caminho = self._caminho_credenciais
        bruto = (settings.tricount_credentials_json or "").strip()

        if bruto and not caminho.exists():
            self._gravar_credenciais(caminho, bruto)
            return self._carregar(caminho)

        if caminho.exists():
            return self._carregar(caminho)

        creds = self._Credentials.generate()
        caminho.parent.mkdir(parents=True, exist_ok=True)
        creds.save(caminho)
        # `Credentials.save` usa `open(path, "w")`, que nasce 0644 pelo umask —
        # chmod imediato para a credencial não ficar legível por todo mundo.
        os.chmod(caminho, 0o600)
        logger.warning(
            "device Tricount novo registrado em %s; grave o JSON no Secret Manager "
            "(TRICOUNT_CREDENTIALS_JSON), senão cada instância registra um device novo",
            caminho,
        )
        return creds

    def _carregar(self, caminho: Path) -> Any:
        """`Credentials.load` com o erro traduzido: JSON corrompido é erro nosso."""
        try:
            return self._Credentials.load(caminho)
        except (OSError, ValueError, KeyError) as exc:
            raise TricountConfigError(
                f"credenciais do device Tricount ilegíveis em {caminho}: {exc}"
            ) from exc

    def _gravar_credenciais(self, caminho: Path, bruto: str) -> None:
        """Grava o JSON do secret com 0600, de forma atômica-ish.

        `os.open` com modo 0600 em vez de `open()` + `chmod`: assim o arquivo
        nunca existe world-readable, nem por uma janela. A gravação vai num
        temporário e só depois `os.replace` — um crash no meio não deixa um
        arquivo truncado que o próximo boot carregaria como válido.
        """
        try:
            dados = json.loads(bruto)
        except ValueError as exc:
            raise TricountConfigError(
                f"TRICOUNT_CREDENTIALS_JSON não é JSON válido: {exc}"
            ) from exc
        if not isinstance(dados, dict) or not {"app_id", "public_key_pem"} <= dados.keys():
            raise TricountConfigError(
                "TRICOUNT_CREDENTIALS_JSON precisa ter as chaves 'app_id' e 'public_key_pem'"
            )

        caminho.parent.mkdir(parents=True, exist_ok=True)
        temporario = caminho.with_name(f"{caminho.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(temporario, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as arquivo:
                json.dump(dados, arquivo, indent=2)
            os.replace(temporario, caminho)
        except OSError as exc:
            # Dois workers subindo juntos podem correr aqui; `os.replace` é
            # atômico, então o perdedor só reescreve o mesmo conteúdo.
            temporario.unlink(missing_ok=True)
            raise TricountConfigError(
                f"não foi possível gravar as credenciais do device em {caminho}: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Client (lazy, memoizado) e timeout
    # ------------------------------------------------------------------

    def _aplicar_timeout(self, client: Any) -> None:
        """Injeta `timeout` em toda request do client.

        A lib não aceita `timeout` em nenhum método, mas guarda uma
        `requests.Session` pública em `TricountAPI.session` e usa
        `session.get/post/...`, que delegam a `session.request`. Envolver
        `session.request` numa função que faz `setdefault("timeout", ...)`
        cobre todas as chamadas de uma vez (plano A da §2.3 — o plano B era um
        `ThreadPoolExecutor`, que deixa thread pendurada e foi descartado).

        O wrap é defensivo: se uma versão futura da lib não expuser a Session,
        logamos WARNING e seguimos sem timeout em vez de explodir no boot.
        Perder o timeout degrada; falhar aqui derrubaria a integração inteira.
        """
        session = getattr(client, "session", None)
        original: Callable[..., Any] | None = getattr(session, "request", None)
        if session is None or not callable(original):
            logger.warning(
                "TricountAPI sem `session.request` acessível: seguindo SEM timeout de rede "
                "(uma chamada pendurada pode prender o worker). Revisar a versão da lib."
            )
            return

        timeout = settings.tricount_timeout_seconds

        def request_com_timeout(*args: Any, **kwargs: Any) -> Any:
            kwargs.setdefault("timeout", timeout)
            return original(*args, **kwargs)

        session.request = request_com_timeout  # type: ignore[method-assign]

    def _token(self) -> str:
        token = (settings.tricount_token or "").strip()
        if not token:
            raise TricountConfigError(
                "TRICOUNT_TOKEN ausente: configure o token do link tricount.com/tXXXXX"
            )
        return token

    def _cliente(self) -> tuple[Any, Any]:
        """Devolve `(client autenticado, Tricount)`, construindo na primeira vez.

        `authenticate()` roda **uma vez por processo** (o client fica memoizado);
        o `Tricount` também é memoizado, mas com validade de
        `_TTL_CACHE_SEGUNDOS` — ver `_tricount_fresco`.
        """
        token = self._token()

        if self._client is None:
            client = self._TricountAPI(self._credenciais())
            self._aplicar_timeout(client)
            with self._erros_traduzidos("autenticar no Tricount"):
                try:
                    client.authenticate()
                except RuntimeError as exc:
                    # A lib levanta RuntimeError quando a resposta não traz
                    # token/user_id — do nosso lado é indisponibilidade.
                    raise TricountIndisponivel(f"autenticação no Tricount falhou: {exc}") from exc
            self._client = client

        if self._tricount is None:
            self._buscar_tricount(token)

        return self._client, self._tricount

    def _buscar_tricount(self, token: str) -> Any:
        """`get_tricount(token)` com erros traduzidos, atualizando o cache."""
        with self._erros_traduzidos("buscar o tricount"):
            try:
                tricount = self._client.get_tricount(token)
            except RuntimeError as exc:
                # "No tricount found": o token não resolve. Repetir não ajuda —
                # é configuração, não indisponibilidade.
                raise TricountConfigError(
                    f"token Tricount não resolve nenhum tricount ({token[:8]}…): {exc}"
                ) from exc
        self._tricount = tricount
        self._tricount_buscado_em = monotonic()
        return tricount

    def _tricount_fresco(self) -> tuple[Any, Any]:
        """Como `_cliente`, mas re-busca o `Tricount` quando o TTL expirou.

        Decisão de invalidação: o `Tricount` carrega a lista de membros, e
        `listar_membros` precisa de dados frescos (alguém entrou/saiu do grupo e
        o dropdown de Pessoas tem de mostrar). Em vez de um cache separado de
        membros, o TTL de 60s vale para o próprio `Tricount` — um cache só,
        sem dois relógios para desencontrar, e `criar_despesa` ganha de graça
        uma lista de `Member` no máximo 60s velha para resolver os uuids.
        """
        client, tricount = self._cliente()
        if monotonic() - self._tricount_buscado_em >= _TTL_CACHE_SEGUNDOS:
            tricount = self._buscar_tricount(self._token())
        return client, tricount

    def _descartar_cliente(self) -> None:
        """Esquece client e tricount para que a próxima chamada re-autentique."""
        self._client = None
        self._tricount = None
        self._tricount_buscado_em = 0.0

    # ------------------------------------------------------------------
    # Tradução de erros
    # ------------------------------------------------------------------

    @contextmanager
    def _erros_traduzidos(self, operacao: str) -> Iterator[None]:
        """Converte erros de rede da lib nos erros do domínio (`base.py`).

        `TricountError` nosso passa reto (já está traduzido). `HTTPError`
        preserva o `status_code` e um pedaço do corpo na mensagem, porque é o
        que permite distinguir "token morreu" de "payload inválido" lendo
        apenas o log/a linha de erro persistida.
        """
        try:
            yield
        except TricountError:
            raise
        except self._requests.HTTPError as exc:
            raise TricountIndisponivel(
                f"{operacao}: Tricount respondeu {self._descrever_http(exc)}"
            ) from exc
        except self._requests.RequestException as exc:
            # Cobre Timeout, ConnectionError, SSLError… todos transitórios em
            # princípio, então a UI oferece reenvio.
            raise TricountIndisponivel(
                f"{operacao}: falha de rede com o Tricount ({type(exc).__name__}: {exc})"
            ) from exc

    def _descrever_http(self, exc: Any) -> str:
        resposta = getattr(exc, "response", None)
        status = getattr(resposta, "status_code", None)
        corpo = ""
        texto = getattr(resposta, "text", None)
        if texto:
            corpo = f" — corpo: {texto[:_MAX_CORPO_ERRO]}"
        if status is None:
            return f"erro HTTP ({exc}){corpo}"
        return f"HTTP {status}{corpo}"

    def _status_http(self, exc: Any) -> int | None:
        return getattr(getattr(exc, "response", None), "status_code", None)

    def _chamar(
        self,
        operacao: str,
        fn: Callable[[Any, Any], Any],
        *,
        join_em_404: bool = False,
    ) -> Any:
        """Executa `fn(client, tricount)` com **no máximo uma** retentativa por causa.

        Duas causas, cada uma com seu "orçamento" de uma única retentativa, e
        por isso elas não se multiplicam nem podem virar loop (no pior caso são
        3 tentativas: a original, a de 401 e a de 404 — em qualquer ordem):

        **401 — sessão expirada.** *Divergência verificada no fonte:* a lib
        0.1.2 *não* re-registra o device em 401 — ela só faz
        `raise_for_status()`. Como o token de sessão é guardado no header da
        Session e o processo vive horas no Cloud Run, um 401 por sessão expirada
        tornaria a integração permanentemente quebrada até o próximo deploy.
        Então descartamos o client e tentamos de novo uma única vez: um 401
        significa que a request foi rejeitada *antes* de qualquer efeito, logo
        repetir é seguro mesmo para uma escrita.

        **404 — grupo não sincronizado neste device** (só com `join_em_404`,
        isto é, só no caminho de escrita). Achado de campo (2026-11-10): com
        apenas `get_tricount(token)`, a escrita responde
        `404 "Registry with id X not found"`; depois de um `join_tricount(token)`
        ela funciona. O 404 também é "rejeitado antes de qualquer efeito", logo
        repetir não duplica despesa.

        Se falhar de novo, é `TricountIndisponivel` com o status na mensagem e a
        UI oferece reenvio.
        """
        rotulo = operacao
        # Orçamentos independentes e one-shot: é o que garante "no máximo uma
        # retentativa de cada causa" sem compor retry sobre retry.
        pode_reautenticar = True
        pode_sincronizar = join_em_404 and not self._join_feito

        while True:
            client, tricount = self._tricount_fresco()
            try:
                with self._erros_traduzidos(rotulo):
                    return fn(client, tricount)
            except TricountIndisponivel as exc:
                status = self._status_http(exc.__cause__)

                if status == 401 and pode_reautenticar:
                    pode_reautenticar = False
                    logger.info("sessão Tricount expirada (401) em '%s'; re-autenticando", operacao)
                    self._descartar_cliente()
                    rotulo = f"{operacao} (após re-autenticar)"
                    continue

                if status == 404 and pode_sincronizar:
                    pode_sincronizar = False
                    self._sincronizar_grupo(client, operacao)
                    rotulo = f"{operacao} (após sincronizar o grupo no device)"
                    continue

                raise

    def _sincronizar_grupo(self, client: Any, operacao: str) -> None:
        """`join_tricount(token)`: registra o grupo na conta deste device.

        **Por que reativo (só em 404) e não proativo no setup?** Com uma
        credencial estável (o `TRICOUNT_CREDENTIALS_JSON` do Secret Manager) o
        sync fica guardado **no servidor**: depois do primeiro join nenhum outro
        é necessário, nem mesmo após cold start ou deploy. Um join proativo
        pagaria um POST a mais em *toda* instância nova para um estado que já
        existe; o caminho comum fica em 1 chamada. O custo: a **primeira**
        escrita de um device novo paga write(404) + join + write — ~2 round
        trips extras, uma vez na vida do device, dentro de um fluxo em que o
        usuário já está esperando o redirect. A alternativa descartada era join
        no boot/`_cliente()`, que amarraria o cold start a mais uma chamada ao
        Tricount (e o §1 da docstring do módulo existe justamente para não fazer
        isso).

        **Efeito colateral real, medido em campo (2026-11-10).** No fonte da
        0.1.2 `join_tricount` é só um
        `POST /v1/user/{uid}/registry-synchronization` e não toca em
        `membership_uuid_active` — mas o **servidor** vincula o device ao membro
        de **menor id** nesse sync: no tricount descartável,
        `membership_uuid_active` era `None` antes do join e passou a apontar
        para o membro de menor id depois. Ou seja: o vínculo acontece sem que a
        lib chame `link_to_member()` (que este arquivo nunca chama, e nem
        poderia — escolher membro é decisão de pessoa, não de runtime).

        Por que isso é aceitável: o auto-vínculo **não é exclusivo**. Depois do
        join do device do probe, o celular do usuário continuou identificado
        como o mesmo membro no mesmo grupo (a despesa aparece como "paid by me"
        no app dele) — duas contas podem apontar para o mesmo membro, então o
        join não rouba a identidade de ninguém. Risco residual registrado: no
        grupo real, este device passa a "ser" o membro de menor id do ponto de
        vista da API. Não afeta a escrita (o pagador vai explícito em `payer` e
        nas alocações) nem os outros membros, mas é estado remoto que o app
        cria.

        `leave_tricount()` (desfazer o sync) também não é chamado em runtime:
        desfazer na saída reintroduziria o 404 na escrita seguinte.

        **Não use `membership_uuid_active` como sinal de "já dei join":** depois
        do join, `get_tricount(token)` (lookup pelo token público) continua
        devolvendo `None` nesse campo, enquanto `get_tricount_by_id` — usado por
        dentro do `join_tricount(fetch_full=True)` — devolve o uuid vinculado. O
        sinal confiável é a flag de instância `_join_feito`.
        """
        token = self._token()
        with self._erros_traduzidos(f"{operacao}: sincronizar o grupo neste device"):
            try:
                tricount = client.join_tricount(token)
            except RuntimeError as exc:
                # Mesmo padrão de `_buscar_tricount`: a lib sinaliza "não achei o
                # grupo" com RuntimeError, não com HTTPError.
                raise TricountIndisponivel(
                    f"{operacao}: sincronizar o grupo neste device não devolveu o tricount ({exc})"
                ) from exc

        self._join_feito = True
        # O join devolve o `Tricount` completo (`get_tricount_by_id` por dentro),
        # agora com `membership_uuid_active` — então aproveitamos como cache em
        # vez de pagar outro `get_tricount`. Os `Member` são instâncias novas, e
        # `executar` re-resolve os uuids a cada tentativa, por isso trocar o
        # objeto no meio do retry é seguro.
        self._tricount = tricount
        self._tricount_buscado_em = monotonic()
        logger.info(
            "grupo Tricount sincronizado neste device (join_tricount) após 404 em '%s'", operacao
        )

    # ------------------------------------------------------------------
    # Interface pública
    # ------------------------------------------------------------------

    def listar_membros(self) -> list[MembroTricount]:
        """Membros do tricount configurado, para o vínculo pessoa↔membro.

        Membros `DELETED` ficam de fora: já não participam de rateio e
        poluiriam o dropdown. `INACTIVE` entra com `ativo=False` — a pessoa
        pode ter vínculo histórico e sumir da lista esconderia o problema.

        A comparação de status é defensiva: `Member.status` é `str` (vem cru do
        JSON) e `MemberStatus` é um enum `str`-based, então normalizamos os dois
        lados em vez de confiar em `is`/igualdade de enum.
        """
        _, tricount = self._tricount_fresco()
        deletado = self._normalizar_status(self._MemberStatus.DELETED)
        ativo_valor = self._normalizar_status(self._MemberStatus.ACTIVE)

        membros: list[MembroTricount] = []
        for membro in getattr(tricount, "members", []) or []:
            status = self._normalizar_status(getattr(membro, "status", ""))
            if status == deletado:
                continue
            membros.append(
                MembroTricount(
                    uuid=str(getattr(membro, "uuid", "") or ""),
                    nome=str(getattr(membro, "display_name", "") or ""),
                    ativo=status == ativo_valor,
                )
            )
        return membros

    @staticmethod
    def _normalizar_status(status: Any) -> str:
        valor = getattr(status, "value", status)
        return str(valor or "").strip().upper()

    def criar_despesa(
        self,
        *,
        descricao: str,
        pagador_uuid: str,
        alocacoes: list[Alocacao],
    ) -> DespesaCriada:
        """Lança a despesa com split customizado, nos valores exatos por membro.

        Semântica do Tricount: o pagador paga o total e a cota dele está dentro
        de `alocacoes`; o total é a soma das alocações. Os valores vão
        **positivos** — a lib aplica o sinal negativo na serialização.

        ---------------------------------------------------------------------
        `join_tricount` SÓ REAGINDO A 404. `link_to_member` e
        `leave_tricount`, NUNCA — em nenhuma circunstância, em runtime.
        ---------------------------------------------------------------------
        A API **exige** que o grupo esteja sincronizado na conta deste device
        para aceitar escrita: só com `get_tricount(token)` ela responde
        `404 "Registry with id X not found"` (achado de campo, 2026-11-10). Daí
        o `join_em_404=True`: em 404, `_sincronizar_grupo` faz o join e a
        escrita é refeita **uma única vez** (ver `_chamar` para o controle de
        retry e `_sincronizar_grupo` para o trade-off de ser reativo).

        O join tem um efeito colateral, medido em campo: o **servidor** vincula
        este device ao membro de menor id do grupo (ainda que a lib nunca chame
        `link_to_member`). É aceitável porque o vínculo **não é exclusivo** — o
        celular do usuário continua sendo o mesmo membro — e porque não altera
        quem paga: `payer` e as alocações vão explícitos no payload. Os detalhes
        e o risco residual estão em `_sincronizar_grupo`.

        `link_to_member()` continua **proibido** aqui: escolher membro é decisão
        de pessoa (dropdown de Pessoas), e um efeito colateral destrutivo e
        silencioso não pode disparar de um clique em "Concluir".
        `leave_tricount()` também não — desfazer o sync devolveria o 404.
        """
        if not alocacoes:
            raise TricountConfigError("despesa sem alocações: nada a lançar no Tricount")

        total_centavos = sum(a.valor_centavos for a in alocacoes)
        if any(a.valor_centavos < 0 for a in alocacoes) or total_centavos <= 0:
            # Erro nosso (rateio inconsistente), não do Tricount: repetir não resolve.
            raise TricountConfigError(
                f"alocações inválidas para o Tricount: total={total_centavos} centavos"
            )

        def executar(client: Any, tricount: Any) -> int:
            pagador = self._resolver_membro(tricount, pagador_uuid, papel="pagador")
            pares = [
                (
                    self._resolver_membro(tricount, a.membro_uuid, papel="participante"),
                    self._em_unidades(a.valor_centavos),
                )
                for a in alocacoes
            ]
            return client.create_transaction_custom_split(
                tricount=tricount,
                description=descricao,
                amount=self._em_unidades(total_centavos),
                payer=pagador,
                allocations=pares,
                # GROCERIES ("🛒 Groceries") em vez de FOOD_AND_DRINK, que no
                # Tricount é "🍔 Restaurants": o app é de rateio de compra de
                # mercado, então essa é a categoria que bate com a realidade.
                category=self._Category.GROCERIES,
            )

        # `join_em_404=True` só aqui: o caminho de LEITURA (`listar_membros`,
        # chamado a cada render do dropdown de Pessoas) continua sem nenhum POST
        # de sincronização — leitura funciona com `get_tricount` puro.
        transaction_id = self._chamar("criar despesa no Tricount", executar, join_em_404=True)
        logger.info(
            "despesa lançada no Tricount: transaction_id=%s total_centavos=%s participantes=%s",
            transaction_id,
            total_centavos,
            len(alocacoes),
        )
        # A lib devolve `int`; o domínio guarda `str` (id opaco para auditoria).
        return DespesaCriada(transaction_id=str(transaction_id), amount_centavos=total_centavos)

    def _resolver_membro(self, tricount: Any, uuid: str, *, papel: str) -> Any:
        """Acha o `Member` pelo uuid — nunca pelo nome.

        Casar por nome lança a despesa na pessoa errada quando os nomes
        divergem entre o app e o Tricount (ver `MembroTricount` em `base.py`).
        Usa `Tricount.get_member_by_uuid` quando existe, com varredura manual de
        fallback para não depender de um helper que pode sair numa versão nova.
        """
        if not uuid:
            raise TricountConfigError(f"{papel} sem membro Tricount vinculado")

        membro = None
        getter = getattr(tricount, "get_member_by_uuid", None)
        if callable(getter):
            membro = getter(uuid)
        if membro is None:
            membro = next(
                (m for m in getattr(tricount, "members", []) or [] if getattr(m, "uuid", None) == uuid),
                None,
            )
        if membro is None:
            # Vínculo quebrado (membro removido do grupo, por exemplo): é
            # configuração — alguém tem de refazer o vínculo em Pessoas.
            raise TricountConfigError(
                f"{papel} aponta para o membro Tricount {uuid}, que não existe mais no grupo"
            )
        return membro

    @staticmethod
    def _em_unidades(centavos: int) -> Decimal:
        """Centavos `int` → unidades de moeda como `Decimal` exato.

        `Decimal` e não `float` porque a lib serializa com `str(-amount)` cru
        (ver `_CENTAVOS_POR_UNIDADE_API`). O `quantize` deixa o valor no formato
        canônico de 2 casas ("0.30" em vez de "0.3"), que é o que o app Android
        envia.
        """
        return (Decimal(int(centavos)) / _CENTAVOS_POR_UNIDADE_API).quantize(_QUANTIZACAO)
