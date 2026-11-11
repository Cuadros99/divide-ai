#!/usr/bin/env python3
"""Probe manual da API não-oficial do Tricount (bunq).

ATENÇÃO — leia antes de rodar:

* A `tricount-api` é **engenharia reversa** do app Android do Tricount (bunq). Não há
  contrato, não há documentação oficial e a própria docstring da lib se contradiz sobre a
  unidade monetária (`Amount` diz "full currency units, NOT cents", mas o exemplo do README
  usa `amount=5000` para um jantar). Este script existe para transformar essa ambiguidade em
  um fato observado — e para a unidade ela já virou fato (ver "Perguntas", item 1).
* O default é rodar **SÓ contra um tricount descartável**: crie um grupo novo no app, com 3
  membros fictícios, e use o `tXXXXX` dele. O script faz escritas reais na API.
* O sharing token dá leitura **e escrita** e **não é revogável**. Por isso o probe aborta se o
  `--token` for igual ao `TRICOUNT_TOKEN` de produção (env ou `.env`).
* `--grupo-real` é o escape **explícito** dessa guarda: em vez de abortar, o probe imprime um
  aviso e segue. Só use sabendo que (a) o alvo é o grupo real da família, (b) toda escrita
  aparece para TODOS os membros, com notificação no app, e (c) `--join` fica **proibido**
  nesse modo: medido em campo, o **servidor** vincula este device ao membro de menor id no
  `registry-synchronization` (a lib não chama `link_to_member`, o servidor vincula sozinho).
  O vínculo não é exclusivo — o celular do dono do grupo continua sendo o mesmo membro —, mas
  é estado remoto no grupo da família, e um script de descoberta não cria isso lá. A guarda do
  **path de credenciais de produção** continua abortando sempre, inclusive com `--grupo-real`
  — `--grupo-real` não a afeta.
* Este arquivo é um utilitário **manual**: nunca é importado nem chamado pelo app. Ele não
  depende de `app.config` de propósito, para continuar rodando sozinho.
* Depois de terminar: `limpar --confirmar` e apague o tricount descartável pelo app.

Perguntas do probe — estado em **2026-11-10**, depois da rodada de campo num tricount
descartável ("Teste", BRL, 3 membros fictícios). As respostas estão fixadas em
`_CENTAVOS_POR_UNIDADE_API` e no `app/tricount/real.py`:

1. **RESPONDIDA — unidade do `amount`.** Escrito `amount=1234` (int cru); o app Android mostrou
   **R$ 1.234,00** e o servidor devolveu a transação como `-1234.00 BRL`. Logo a API fala
   **unidades de moeda**, não centavos ⇒ `_CENTAVOS_POR_UNIDADE_API = 100` e o
   `Decimal(centavos)/100` do `real.py` estão certos.
2. **RESPONDIDA — a escrita exige `join_tricount()`.** Só com `get_tricount(token)` a escrita
   volta **HTTP 404 "Registry with id X not found"** (o token acha o grupo, mas o registry não
   está sincronizado na conta do device); com `join_tricount(token)` antes, a escrita funciona.
   Join é **obrigatório e suficiente** para escrever. Na lib 0.1.2 o join é só um
   `POST /v1/user/{uid}/registry-synchronization` e **não** chama `link_to_member` — mas o
   **servidor** auto-vincula o device ao membro de **menor id** nesse sync (medido:
   `membership_uuid_active` era `None` e passou a apontar para o membro de menor id). O vínculo
   **não é exclusivo**: o celular do dono continuou sendo o mesmo membro, com a despesa como
   "paid by me". Por isso o `real.py` faz o join **só reagindo a 404**, no caminho de escrita, e
   nunca chama `link_to_member`/`leave_tricount`.
3. **RESPONDIDA — `create_transaction_custom_split` aceita `category`:** sim (e
   `category_custom` também). O probe manda `Category.GROCERIES` por default
   (`--sem-categoria` para comparar).
4. **RESPONDIDA — tipo do `tx_id`:** `int` (ex.: `2142412623`); guardamos como `str` de todo
   jeito.
5. **RESPONDIDA — a `requests.Session` interna é acessível:** `client.session` é uma
   `requests.Session` ⇒ o wrapper de `timeout` do `real.py` está confirmado; o plano B
   (`ThreadPoolExecutor` + `future.result(timeout=...)`) fica descartado.

Achado colateral: **token inválido não dá 404** (ao contrário do que o README da lib diz) — dá
`HTTP 200` com `Response: []`, e a lib levanta `RuntimeError("No tricount found")`. O `real.py`
traduz isso em `TricountConfigError`.

**O QUE AINDA FALTA CONFIRMAR: decimais.** Todas as escritas de campo usaram inteiros, e o que o
`real.py` realmente manda é `"12.34"` / `"3.33"`. Por isso `--amount` e `--split` são `Decimal`
(nunca `float`: a lib serializa `str(-amount)` cru, e `float` geraria `"-0.30000000000000004"`).
Resta ver se o servidor aceita os centavos sem truncar nem re-arredondar.

Subcomandos (cada um independente, explícito e não-destrutivo por default):

    inspect        só leitura: membros, vars(client), vars(tricount), assinaturas
    unidade        UMA escrita diagnóstica (amount/split decimais configuráveis)
    centavo-impar  UMA escrita de 3.33/3.33/3.34 = 10.00 entre 3 membros (centavo ímpar) e
                   releitura do que o servidor guardou
    limpar         apaga transações (exige --confirmar). Por default alveja SÓ as que o
                   próprio probe criou — `description` começando com DESCRICAO_PROBE,
                   comparando sem case-sensitivity. `--ids` alveja ids exatos; `--todas`
                   volta a alvejar o grupo inteiro e só apaga junto com `--confirmar`.

Exemplos:

    python3 scripts/tricount_probe.py inspect --token tXXXXX
    python3 scripts/tricount_probe.py unidade --token tXXXXX --join --amount 12.34 --split 6.17,6.17
    python3 scripts/tricount_probe.py unidade --token tXXXXX --join --amount 0.03 --split 0.01,0.01,0.01
    python3 scripts/tricount_probe.py centavo-impar --token tXXXXX --join
    python3 scripts/tricount_probe.py limpar --token tXXXXX --join --confirmar
    python3 scripts/tricount_probe.py limpar --token tXXXXX --ids 123,456 --confirmar
    python3 scripts/tricount_probe.py limpar --token tXXXXX --todas --confirmar   # perigoso
    python3 scripts/tricount_probe.py inspect --token tREAL --grupo-real
"""

from __future__ import annotations

import argparse
import inspect as inspect_mod
import json
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

# Path de scratch, deliberadamente DIFERENTE do path de produção
# (`/tmp/tricount_credentials.json`, usado pelo `real.py`): o probe registra um device
# próprio e descartável, e não pode contaminar nem reaproveitar a credencial real.
CREDS_SCRATCH_PADRAO = Path("/tmp/tricount-probe-scratch/credentials.json")
CREDS_PRODUCAO = Path("/tmp/tricount_credentials.json")

DESCRICAO_PROBE = "PROBE - apagar"

# Defaults das escritas diagnósticas, em **unidades de moeda** (unidade já confirmada em campo).
# `unidade`: 12.34 = 6.17 + 6.17 — o caso que mostra se o servidor preserva os centavos.
AMOUNT_PADRAO_UNIDADE = "12.34"
SPLIT_PADRAO_UNIDADE = "6.17,6.17"
# `centavo-impar`: 10.00 entre 3 ⇒ 3.33 + 3.33 + 3.34, resto de 1 centavo no último.
AMOUNT_PADRAO_IMPAR = "10.00"
SPLIT_PADRAO_IMPAR = "3.33,3.33,3.34"


# ---------------------------------------------------------------------------
# Import da lib (amigável se faltar)
# ---------------------------------------------------------------------------


def importar_lib():
    """Importa a `tricount-api` (módulo `tricount`) com mensagem amigável se faltar."""
    try:
        import tricount  # noqa: PLC0415
    except ImportError:
        sys.exit(
            "ERRO: a lib `tricount-api` não está instalada neste interpretador.\n"
            f"       (interpretador atual: {sys.executable})\n"
            "       Instale com: .venv/bin/pip install 'tricount-api==0.1.2'\n"
            "       Obs.: o pacote no PyPI é `tricount-api`, mas o módulo importável\n"
            "       chama-se `tricount` (sem sufixo)."
        )
    return tricount


# ---------------------------------------------------------------------------
# Guarda de segurança: nunca rodar contra o tricount de produção
# ---------------------------------------------------------------------------


def _token_producao(raiz_projeto: Path) -> str | None:
    """Lê `TRICOUNT_TOKEN` do ambiente ou, se não houver, do `.env` do projeto.

    Parse propositalmente ingênuo: o probe não importa `app.config` para não depender do app.
    """
    do_ambiente = (os.environ.get("TRICOUNT_TOKEN") or "").strip()
    if do_ambiente:
        return do_ambiente

    caminho_env = raiz_projeto / ".env"
    try:
        linhas = caminho_env.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    for linha in linhas:
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        if chave.strip().removeprefix("export").strip() != "TRICOUNT_TOKEN":
            continue
        valor = valor.strip().strip("'\"").strip()
        return valor or None
    return None


def checar_guardas(
    token: str,
    caminho_creds: Path,
    grupo_real: bool = False,
    join: bool = False,
) -> None:
    """Aborta se o token for o de produção ou se o path de credenciais for o de produção.

    `grupo_real` (flag `--grupo-real`) é o escape **explícito** da guarda do token, e só dela:
    em vez de abortar, imprime um aviso em várias linhas e segue. A guarda do path de
    credenciais de produção não tem escape — reaproveitar aquele arquivo misturaria o device
    do app de verdade com o device descartável do probe.
    """
    raiz_projeto = Path(__file__).resolve().parent.parent
    producao = _token_producao(raiz_projeto)

    # --join é incompatível com --grupo-real: medido em campo, o SERVIDOR vincula este device
    # ao membro de menor id no `registry-synchronization` (a lib não chama `link_to_member`).
    # O vínculo não é exclusivo, mas é estado remoto no grupo da família — e um script de
    # descoberta não cria isso lá. Quem precisa escrever no grupo real é o app, que faz o join
    # sozinho ao receber 404 (ver `_sincronizar_grupo` em app/tricount/real.py).
    if join and grupo_real:
        sys.exit(
            "ABORTADO: --join é incompatível com --grupo-real.\n"
            "          O servidor auto-vincula este device ao membro de MENOR ID do grupo no\n"
            "          registry-synchronization (medido em campo; não é a lib que vincula).\n"
            "          O vínculo não é exclusivo — ninguém perde a identidade —, mas é estado\n"
            "          remoto, e criar isso no grupo da família a partir de um probe não é\n"
            "          necessário: o app faz o join sozinho em 404, no caminho de escrita.\n"
            "          Exercite o join num tricount DESCARTÁVEL."
        )

    # Esta guarda aborta SEMPRE, inclusive com --grupo-real.
    if caminho_creds.resolve() == CREDS_PRODUCAO.resolve():
        sys.exit(
            f"ABORTADO: {CREDS_PRODUCAO} é o path de credenciais de PRODUÇÃO (ver real.py).\n"
            f"          Use o default de scratch ({CREDS_SCRATCH_PADRAO}) ou outro path.\n"
            "          --grupo-real NÃO dispensa esta guarda."
        )

    if producao and token.strip() == producao:
        if not grupo_real:
            sys.exit(
                "ABORTADO: o --token passado é igual ao TRICOUNT_TOKEN de produção\n"
                f"          (lido de {'TRICOUNT_TOKEN no ambiente' if os.environ.get('TRICOUNT_TOKEN') else raiz_projeto / '.env'}).\n"
                "          Este probe FAZ ESCRITAS REAIS. Crie um tricount descartável no app\n"
                "          Android, com membros fictícios, e use o tXXXXX dele.\n"
                "          Se o alvo é MESMO o grupo real da família, passe --grupo-real."
            )
        print()
        print("=" * 78)
        print("AVISO — --grupo-real: a guarda do token de produção foi DISPENSADA de propósito.")
        print("  (a) O alvo é o GRUPO REAL da família, não um tricount descartável.")
        print("  (b) Toda escrita (criar / editar / apagar despesa) aparece para TODOS os")
        print("      membros do grupo, com notificação no app. Não existe modo rascunho e o")
        print("      sharing token não é revogável.")
        print("  (c) --join continua PROIBIDO neste cenário: o servidor auto-vincula o device")
        print("      ao membro de menor id no sync (vínculo não exclusivo, mas estado remoto).")
        print("=" * 78)
        return

    if grupo_real:
        # Passou --grupo-real, mas o token nem é o de produção: nada foi dispensado.
        print("[guarda] --grupo-real passado, mas o --token não é o TRICOUNT_TOKEN de produção")
        print("         — nenhuma guarda precisou ser dispensada.")

    print(f"[guarda] token de produção {'configurado e diferente do passado' if producao else 'não configurado'} — ok")


# ---------------------------------------------------------------------------
# Erros de rede: status + corpo, sem stacktrace cru
# ---------------------------------------------------------------------------


def _formatar_corpo(resp) -> str:
    try:
        return json.dumps(resp.json(), indent=2, ensure_ascii=False)[:4000]
    except Exception:
        return (resp.text or "")[:4000]


def rodar_protegido(rotulo: str, func, *args, **kwargs):
    """Executa `func`, traduzindo erros HTTP/rede em mensagem legível (sem stacktrace)."""
    import requests  # noqa: PLC0415

    try:
        return func(*args, **kwargs)
    except requests.HTTPError as exc:
        resp = exc.response
        status = getattr(resp, "status_code", "?")
        print(f"\nFALHA HTTP em {rotulo}: status {status}", file=sys.stderr)
        if resp is not None:
            print(f"URL: {getattr(resp.request, 'url', '?')}", file=sys.stderr)
            print("Corpo da resposta:", file=sys.stderr)
            print(_formatar_corpo(resp), file=sys.stderr)
        if status in (401, 403, 404):
            print(
                "\nDica: numa escrita, 404 'Registry with id X not found' (e também 401/403) é "
                "o sintoma\n      conhecido de 'o registry não está sincronizado neste device'. "
                "Repita o comando com\n      --join (pergunta 2 do probe, já respondida: join é "
                "obrigatório para escrever) —\n      mas SÓ num tricount descartável: --join é "
                "proibido junto com --grupo-real.",
                file=sys.stderr,
            )
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"\nFALHA DE REDE em {rotulo}: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Setup: credenciais de scratch + client autenticado + tricount
# ---------------------------------------------------------------------------


def preparar_cliente(args):
    """Devolve `(tricount_mod, client, tricount)` — credenciais de scratch, authenticate, get_tricount."""
    tricount_mod = importar_lib()
    caminho = Path(args.creds).expanduser()
    checar_guardas(
        args.token,
        caminho,
        grupo_real=getattr(args, "grupo_real", False),
        join=getattr(args, "join", False),
    )

    caminho.parent.mkdir(parents=True, exist_ok=True)
    if caminho.exists():
        creds = tricount_mod.Credentials.load(caminho)
        print(f"[creds] carregadas de {caminho}")
    else:
        creds = tricount_mod.Credentials.generate()
        creds.save(caminho)
        try:
            caminho.chmod(0o600)
        except OSError:
            pass
        print(f"[creds] GERADAS e salvas em {caminho} (device novo, descartável)")
    print(f"[creds] app_id={creds.app_id}")

    client = tricount_mod.TricountAPI(creds)
    user_id = rodar_protegido("authenticate()", client.authenticate)
    print(f"[auth] ok — user_id={user_id!r} ({type(user_id).__name__})")

    if getattr(args, "join", False):
        tc = rodar_protegido("join_tricount()", client.join_tricount, args.token)
        print("[join] join_tricount() executado — é o que habilita a ESCRITA (sem ele a API")
        print("       responde 404 'Registry with id X not found'). Esperado: o servidor")
        print("       auto-vincula este device ao membro de MENOR ID (vínculo não exclusivo):")
        print(f"       membership_uuid_active={tc.membership_uuid_active!r}")
        vinculado = tc.linked_member
        print(f"       linked_member={vinculado.display_name if vinculado else None!r}")
        print("       (obs.: este objeto vem do get_tricount_by_id interno do join; um")
        print("        get_tricount(token) depois disso ainda devolve membership_uuid_active=None)")
    else:
        tc = rodar_protegido("get_tricount()", client.get_tricount, args.token)

    print(f"[tricount] id={tc.id!r} título={tc.title!r} moeda={tc.currency!r} status={tc.status!r}")
    return tricount_mod, client, tc


def exigir_membros(tc, minimo: int):
    ativos = [m for m in tc.members if (m.status or "").upper() != "DELETED"]
    if len(ativos) < minimo:
        sys.exit(
            f"ABORTADO: o tricount tem {len(ativos)} membro(s) ativo(s), e este passo precisa "
            f"de {minimo}.\n          Adicione membros fictícios no app Android e rode de novo."
        )
    return ativos


def _categoria(tricount_mod, args):
    """Pergunta 3: `create_transaction_custom_split` aceita `category`?"""
    if getattr(args, "sem_categoria", False):
        print("[categoria] enviando SEM category (--sem-categoria)")
        return None
    print("[categoria] enviando category=Category.GROCERIES (pergunta 3)")
    return tricount_mod.Category.GROCERIES


def _relatar_tx_id(tx_id) -> None:
    """Pergunta 4: tipo do tx_id."""
    print(f"\n>>> tx_id = {tx_id!r}  (tipo Python: {type(tx_id).__name__})")
    print("    O `real.py` guarda isso como str de todo jeito.")


# ---------------------------------------------------------------------------
# Valores monetários: Decimal exato, NUNCA float
# ---------------------------------------------------------------------------


def tipo_decimal(texto: str) -> Decimal:
    """`type=` do argparse para valores monetários: `Decimal`, nunca `float`.

    A lib serializa o valor com `str(-amount)` cru, então `float` vazaria ruído binário no
    payload (`str(-0.3)` pode sair `"-0.30000000000000004"`). `Decimal` é exato sob `-` e
    `str()`, e é exatamente o que o `real.py` manda (`Decimal(centavos)/100`).

    Aceita `12.34` e `1234` — ambos são entradas válidas em unidades de moeda.
    """
    bruto = texto.strip()
    try:
        valor = Decimal(bruto)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(
            f"{texto!r} não é um decimal válido (ex.: 12.34 ou 1234)"
        ) from exc
    if not valor.is_finite():
        raise argparse.ArgumentTypeError(f"{texto!r} não é um número finito")
    return valor


def _parsear_split(bruto: str) -> list[Decimal]:
    """`--split` → lista de `Decimal` (mesma regra do `tipo_decimal`, erro via sys.exit)."""
    partes = [p.strip() for p in bruto.split(",") if p.strip()]
    if not partes:
        sys.exit("ABORTADO: --split está vazio (ex.: 6.17,6.17).")
    valores: list[Decimal] = []
    for parte in partes:
        try:
            valores.append(tipo_decimal(parte))
        except argparse.ArgumentTypeError as exc:
            sys.exit(f"ABORTADO: --split: {exc}")
    return valores


def _alocacoes_validadas(args, membros) -> list[tuple[Any, Decimal]]:
    """Casa `--split` com os membros ativos e valida a soma **em Decimal exato**.

    A comparação é `sum(valores) != args.amount` em `Decimal`: comparação numérica exata, sem
    tolerância e sem passar por `float`. `Decimal("10.00") == Decimal("10")` é verdadeiro — o
    que importa é o valor, não a grafia.
    """
    valores = _parsear_split(args.split)

    if len(valores) > len(membros):
        sys.exit(f"ABORTADO: --split tem {len(valores)} valores, mas só há {len(membros)} membros ativos.")
    if any(v < 0 for v in valores):
        sys.exit("ABORTADO: --split não aceita valores negativos (a lib aplica o sinal sozinha).")
    if args.amount <= 0:
        sys.exit(f"ABORTADO: --amount tem de ser positivo (recebi {args.amount}).")

    soma = sum(valores, Decimal(0))
    if soma != args.amount:
        sys.exit(f"ABORTADO: soma do --split ({soma}) != --amount ({args.amount}).")

    return list(zip(membros, valores))


def _brl(valor: Decimal) -> str:
    """Formata em pt-BR só para a conferência visual no app (R$ 1.234,00)."""
    texto = f"{valor:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _reler_transacao(client, args, tx_id) -> None:
    """Relê a transação pelo servidor e imprime os valores **crus** (`Amount.value` é `str`).

    É a evidência da pergunta que sobrou: se mandamos `3.33` e o servidor devolve `3.33`, ele
    não truncou nem re-arredondou os centavos.
    """
    print("\n[reler] relendo pelo servidor para ver o que ele guardou (teste dos decimais)...")
    relido = rodar_protegido("get_tricount()", client.get_tricount, args.token)
    for tx in relido.transactions:
        if str(tx.id) != str(tx_id):
            continue
        print(f"  total  = {getattr(tx.amount, 'value', tx.amount)!r} {getattr(tx.amount, 'currency', '')}")
        for alloc in tx.allocations:
            membro = relido.get_member_by_uuid(alloc.membership_uuid)
            nome = membro.display_name if membro else alloc.membership_uuid
            print(f"  alloc  = {getattr(alloc.amount, 'value', alloc.amount)!r}  -> {nome!r}")
        break
    else:
        print("  (transação não voltou no get_tricount — confira pelo app)")
    print(">>> Valores iguais aos enviados ⇒ a API aceita decimais sem truncar nem re-arredondar.")


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def cmd_inspect(args) -> None:
    tricount_mod, client, tc = preparar_cliente(args)

    print("\n=== MEMBROS ===")
    for m in tc.members:
        print(f"  id={m.id!r:>10}  uuid={m.uuid!r}  display_name={m.display_name!r}  status={m.status!r}")

    print("\n=== vars(client) — onde está a requests.Session do timeout (pergunta 5) ===")
    for chave, valor in vars(client).items():
        print(f"  {chave} = {type(valor).__module__}.{type(valor).__name__}  ->  {valor!r:.200}")
    sessao = getattr(client, "session", None)
    if sessao is not None:
        print(
            f"\n  client.session é {type(sessao).__module__}.{type(sessao).__name__} — "
            "acessível, então o real.py pode envolver `session.request` e injetar\n"
            "  kw.setdefault('timeout', ...). Plano B (ThreadPoolExecutor) não é necessário."
        )
        print(f"  headers: {dict(sessao.headers)}")
    else:
        print("\n  ATENÇÃO: não achei `client.session` — cair para o plano B do timeout.")

    print("\n=== vars(tricount) ===")
    for chave, valor in vars(tc).items():
        if chave in ("members", "transactions"):
            print(f"  {chave} = list[{len(valor)}]")
            continue
        print(f"  {chave} = {valor!r}")

    print("\n=== ASSINATURAS ===")
    for nome in (
        "create_transaction_custom_split",
        "create_transaction",
        "edit_transaction",
        "delete_transaction",
        "get_balances",
        "join_tricount",
        "link_to_member",
    ):
        metodo = getattr(client, nome, None)
        if metodo is None:
            print(f"  {nome}: AUSENTE nesta versão da lib")
            continue
        try:
            assinatura = inspect_mod.signature(metodo)
        except (TypeError, ValueError):
            print(f"  {nome}: assinatura indisponível")
            continue
        print(f"  {nome}{assinatura}")

    criar = getattr(client, "create_transaction_custom_split", None)
    if criar is not None:
        params = inspect_mod.signature(criar).parameters
        print(
            f"\n  category aceita por create_transaction_custom_split? "
            f"{'SIM' if 'category' in params else 'NÃO'}"
            f"   (category_custom? {'SIM' if 'category_custom' in params else 'NÃO'})"
        )

    print("\n=== TRANSAÇÕES EXISTENTES ===")
    if not tc.transactions:
        print("  (nenhuma)")
    for tx in tc.transactions:
        print(f"  id={tx.id!r} ({type(tx.id).__name__})  amount={tx.amount}  desc={tx.description!r}  status={tx.status!r}")

    print("\n[inspect] nada foi escrito.")


# ---------------------------------------------------------------------------
# unidade
# ---------------------------------------------------------------------------


def cmd_unidade(args) -> None:
    tricount_mod, client, tc = preparar_cliente(args)
    membros = exigir_membros(tc, 2)
    alocacoes = _alocacoes_validadas(args, membros)
    pagador = membros[0]

    print(f"\n[unidade] amount={args.amount} (Decimal; o payload leva str(-amount) = '-{args.amount}')")
    print(f"[unidade] pagador: {pagador.display_name!r}")
    for m, v in alocacoes:
        print(f"[unidade]   alocação {v} -> {m.display_name!r} ({m.uuid})")
    if not args.join:
        print("[unidade] escrevendo com APENAS get_tricount(): esperado 404 'Registry with id X")
        print("          not found' (pergunta 2, já respondida). Para escrever, repita com --join.")

    tx_id = rodar_protegido(
        "create_transaction_custom_split()",
        client.create_transaction_custom_split,
        tc,
        args.descricao,
        args.amount,
        pagador,
        alocacoes,
        category=_categoria(tricount_mod, args),
    )
    _relatar_tx_id(tx_id)
    _reler_transacao(client, args, tx_id)

    print("\n>>> AGORA ABRA O APP ANDROID no tricount descartável e leia o valor da despesa:")
    print(f"      esperado: {_brl(args.amount)}  (a API fala unidades de moeda — já confirmado em campo)")
    print("      se aparecer valor 100x maior/menor, ou com os centavos zerados, a API")
    print("      truncou/reinterpretou o decimal: ANOTE e não prossiga para o grupo real.")
    print("\n    Vale repetir com um valor de centavo baixo, onde o truncamento gritaria:")
    print(f"      {Path(sys.argv[0]).name} unidade --token {args.token} --join --amount 0.03 --split 0.01,0.01,0.01")
    print("\n    Depois: limpar --confirmar, e apague o tricount descartável pelo app.")


# ---------------------------------------------------------------------------
# centavo-impar
# ---------------------------------------------------------------------------


def cmd_centavo_impar(args) -> None:
    """Resto de 1 centavo entre 3 membros.

    Com a unidade confirmada como **unidades de moeda**, `333/333/334 = 1000` testava real
    ímpar, não centavo ímpar — o caso que interessa é `10.00` dividido em
    `3.33 / 3.33 / 3.34`, que é exatamente o que o `ratear_centavos` (maior resto) produz para
    R$ 10,00 entre três pessoas.
    """
    tricount_mod, client, tc = preparar_cliente(args)
    membros = exigir_membros(tc, 3)[:3]
    alocacoes = _alocacoes_validadas(args, membros)

    rotulo_split = "/".join(str(v) for _, v in alocacoes)
    print(f"\n[centavo-impar] amount={args.amount}, alocações {rotulo_split} (soma exata, resto no último)")
    for m, v in alocacoes:
        print(f"[centavo-impar]   {v} -> {m.display_name!r}")
    if not args.join:
        print("[centavo-impar] sem --join a escrita deve falhar com 404 (ver pergunta 2).")

    tx_id = rodar_protegido(
        "create_transaction_custom_split()",
        client.create_transaction_custom_split,
        tc,
        f"{args.descricao} {rotulo_split}",
        args.amount,
        membros[0],
        alocacoes,
        category=_categoria(tricount_mod, args),
    )
    _relatar_tx_id(tx_id)
    _reler_transacao(client, args, tx_id)
    print(f"    (no app, a despesa tem de aparecer como {_brl(args.amount)}, com o centavo")
    print(f"     extra no último membro: {alocacoes[-1][0].display_name!r})")


# ---------------------------------------------------------------------------
# limpar
# ---------------------------------------------------------------------------


class SelecaoAlvos(NamedTuple):
    """Resultado da seleção de alvos do `limpar` (vem de uma função pura, testável sem rede)."""

    alvos: list[str]       # ids que serão apagados
    ignoradas: list[str]   # ids descartados por não casarem com a descrição do probe
    modo: str              # "ids" | "todas" | "descricao"


def _e_do_probe(descricao: str | None) -> bool:
    """A `description` começa com `DESCRICAO_PROBE`? Sem case-sensitivity e tolerando None.

    A lib devolve `description` possivelmente `None` (transações criadas por outros clientes
    podem vir sem descrição), e o app Android normaliza caixa de forma imprevisível — por isso
    a comparação é feita em minúsculas e `None` nunca casa.
    """
    if not descricao:
        return False
    return descricao.strip().lower().startswith(DESCRICAO_PROBE.lower())


def _selecionar_alvos(transacoes, ids: str | None = None, todas: bool = False) -> SelecaoAlvos:
    """Decide QUAIS transações o `limpar` alveja — função pura, sem rede e sem cliente.

    Precedência: `--ids` manda sobre tudo; depois `--todas`; o default (nenhum dos dois) é o
    seguro: só as transações que o próprio probe criou. O default antigo era "todas", o que
    num grupo real apagaria o histórico da família.
    """
    if ids:
        return SelecaoAlvos([i.strip() for i in ids.split(",") if i.strip()], [], "ids")

    if todas:
        return SelecaoAlvos(
            [str(tx.id) for tx in transacoes if getattr(tx, "id", None) is not None], [], "todas"
        )

    alvos: list[str] = []
    ignoradas: list[str] = []
    for tx in transacoes:
        if getattr(tx, "id", None) is None:
            continue
        destino = alvos if _e_do_probe(getattr(tx, "description", None)) else ignoradas
        destino.append(str(tx.id))
    return SelecaoAlvos(alvos, ignoradas, "descricao")


def cmd_limpar(args) -> None:
    _, client, tc = preparar_cliente(args)

    selecao = _selecionar_alvos(tc.transactions, args.ids, args.todas)
    alvos = selecao.alvos

    if selecao.modo == "ids":
        print(f"\n[limpar] --ids: alvo exato de {len(alvos)} id(s) (ignora o filtro de descrição).")
    elif selecao.modo == "todas":
        print()
        print("!" * 78)
        print("!! --todas: o ALVO é TODAS as transações do tricount, INCLUSIVE as que o probe")
        print("!!          NÃO criou. Num grupo real isso apaga o histórico da família.")
        print("!" * 78)
        print(f"[limpar] {len(alvos)} transação(ões) no grupo.")
        if not args.confirmar:
            # --todas só apaga junto com --confirmar; sozinha ela continua dry run.
            print("[limpar] --todas sem --confirmar: continua DRY RUN, nada será apagado.")
    else:
        print(f"\n[limpar] default seguro: só transações cuja descrição começa com {DESCRICAO_PROBE!r}")
        print(f"[limpar] {len(alvos)} consideradas, {len(selecao.ignoradas)} ignoradas por não casarem.")

    if not alvos:
        if selecao.modo == "descricao":
            print(
                f"[limpar] nenhuma transação com a descrição do probe ({DESCRICAO_PROBE!r}) foi "
                "encontrada.\n"
                "         Para apagar ids específicos, use --ids 123,456.\n"
                "         Para alvejar TODAS as transações (inclusive as que o probe não criou),\n"
                "         use --todas --confirmar."
            )
        else:
            print("[limpar] nenhuma transação para apagar.")
        return

    print(f"\n[limpar] {len(alvos)} transação(ões) alvo:")
    for tx in tc.transactions:
        if str(tx.id) in alvos:
            print(f"  id={tx.id!r}  amount={tx.amount}  desc={tx.description!r}")

    if not args.confirmar:
        print("\n[limpar] DRY RUN — nada foi apagado. Repita com --confirmar para apagar de verdade.")
        return

    for alvo in alvos:
        try:
            identificador = int(alvo)
        except ValueError:
            print(f"  id {alvo!r} não é inteiro — pulando")
            continue
        rodar_protegido(f"delete_transaction({identificador})", client.delete_transaction, tc, identificador)
        print(f"  apagada: {identificador}")

    if getattr(args, "grupo_real", False):
        # No grupo real não há "apague o tricount depois": o grupo continua vivo.
        print("\n[limpar] pronto. O alvo era o GRUPO REAL — confira no app que só as despesas")
        print("         do probe desapareceram.")
    else:
        print("\n[limpar] pronto. Não esqueça de apagar o tricount descartável pelo app Android.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def construir_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tricount_probe.py",
        description=(
            "Probe manual da API não-oficial do Tricount. Por default, RODE SÓ CONTRA UM "
            "TRICOUNT DESCARTÁVEL: unidade/centavo-impar fazem escritas reais e limpar apaga. "
            "Para alvejar o grupo real da família, use --grupo-real (dispensa só a guarda do "
            "token, nunca a do path de credenciais, e proíbe --join)."
        ),
        epilog=(
            "Veja a docstring do módulo para as 5 perguntas do probe (todas respondidas em campo "
            "em 2026-11-10; o que falta é confirmar que a API aceita decimais sem truncar) e "
            "para o que --grupo-real libera."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    comum = argparse.ArgumentParser(add_help=False)
    comum.add_argument(
        "--token",
        required=True,
        help=(
            "o tXXXXX do tricount DESCARTÁVEL (o token de produção é recusado pela guarda, "
            "salvo com --grupo-real)."
        ),
    )
    # Flag global (vai para todos os subcomandos via `parents=[comum]`): o escape da guarda
    # do token tem de valer igual em inspect, unidade, centavo-impar e limpar.
    comum.add_argument(
        "--grupo-real",
        action="store_true",
        dest="grupo_real",
        help=(
            "escape EXPLÍCITO da guarda do token: em vez de abortar quando o --token é o "
            "TRICOUNT_TOKEN de produção, imprime um aviso e segue. Incompatível com --join; "
            "não afeta a guarda do path de credenciais de produção."
        ),
    )
    comum.add_argument(
        "--creds",
        default=str(CREDS_SCRATCH_PADRAO),
        help=f"JSON de credenciais de scratch (default: %(default)s; o path de produção {CREDS_PRODUCAO} é recusado).",
    )

    sub = parser.add_subparsers(dest="comando", required=True)

    p_inspect = sub.add_parser(
        "inspect",
        parents=[comum],
        help="só leitura: membros, vars(client)/vars(tricount), assinaturas, transações.",
    )
    p_inspect.add_argument(
        "--join",
        action="store_true",
        help="usar join_tricount() em vez de get_tricount() (proibido com --grupo-real).",
    )
    p_inspect.set_defaults(func=cmd_inspect)

    p_unidade = sub.add_parser(
        "unidade",
        parents=[comum],
        help="ESCREVE uma despesa diagnóstica com amount/split DECIMAIS exatos e relê o resultado.",
        description=(
            "Escreve uma despesa diagnóstica. A unidade já está resolvida (unidades de moeda); o "
            "que esta escrita confirma agora é que a API aceita DECIMAIS sem truncar — por isso "
            "--amount/--split são Decimal e o default é 12.34 = 6.17 + 6.17."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_unidade.add_argument(
        "--amount",
        type=tipo_decimal,
        default=Decimal(AMOUNT_PADRAO_UNIDADE),
        help=(
            "total em unidades de moeda, decimal exato (Decimal, nunca float). Aceita 12.34 e "
            "1234 (default: %(default)s)."
        ),
    )
    p_unidade.add_argument(
        "--split",
        default=SPLIT_PADRAO_UNIDADE,
        help=(
            "alocações decimais separadas por vírgula; a soma tem de ser EXATAMENTE igual a "
            "--amount (default: %(default)s)."
        ),
    )
    p_unidade.add_argument("--descricao", default=DESCRICAO_PROBE, help="descrição da despesa (default: %(default)s).")
    p_unidade.add_argument("--sem-categoria", action="store_true", dest="sem_categoria", help="não enviar `category`.")
    p_unidade.add_argument(
        "--join",
        action="store_true",
        help="fazer join_tricount() antes de escrever (pergunta 2; proibido com --grupo-real).",
    )
    p_unidade.set_defaults(func=cmd_unidade)

    p_impar = sub.add_parser(
        "centavo-impar",
        parents=[comum],
        help="ESCREVE 10.00 em 3.33/3.33/3.34 entre 3 membros e relê o que o servidor guardou.",
        description=(
            "Resto de 1 centavo entre 3 membros. Com a unidade confirmada em unidades de moeda, "
            "o caso interessante é 10.00 = 3.33 + 3.33 + 3.34 (o antigo 1000 = 333+333+334 "
            "testava real ímpar, não centavo ímpar)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_impar.add_argument(
        "--amount",
        type=tipo_decimal,
        default=Decimal(AMOUNT_PADRAO_IMPAR),
        help="total em unidades de moeda, decimal exato (default: %(default)s).",
    )
    p_impar.add_argument(
        "--split",
        default=SPLIT_PADRAO_IMPAR,
        help=(
            "alocações decimais separadas por vírgula, soma EXATAMENTE == --amount "
            "(default: %(default)s)."
        ),
    )
    p_impar.add_argument("--descricao", default=DESCRICAO_PROBE, help="descrição da despesa (default: %(default)s).")
    p_impar.add_argument("--sem-categoria", action="store_true", dest="sem_categoria", help="não enviar `category`.")
    p_impar.add_argument(
        "--join",
        action="store_true",
        help="fazer join_tricount() antes de escrever (proibido com --grupo-real).",
    )
    p_impar.set_defaults(func=cmd_centavo_impar)

    p_limpar = sub.add_parser(
        "limpar",
        parents=[comum],
        help=(
            "APAGA transações (dry run sem --confirmar). Default: só as criadas pelo probe."
        ),
        description=(
            "Apaga transações. Sem --ids e sem --todas, alveja SÓ as transações cuja descrição "
            f"começa com {DESCRICAO_PROBE!r} (sem case-sensitivity) — as que o próprio probe "
            "criou. Sempre dry run até passar --confirmar."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_limpar.add_argument(
        "--ids",
        help=(
            "ids separados por vírgula; alveja exatamente esses e tem precedência sobre "
            "--todas. Omitido: só as transações do probe."
        ),
    )
    p_limpar.add_argument(
        "--todas",
        action="store_true",
        help=(
            "alveja TODAS as transações do grupo, inclusive as que o probe NÃO criou "
            "(apaga histórico alheio). Só apaga junto com --confirmar."
        ),
    )
    p_limpar.add_argument("--confirmar", action="store_true", help="exigido para apagar de verdade.")
    p_limpar.add_argument(
        "--join",
        action="store_true",
        help="fazer join_tricount() antes de apagar (proibido com --grupo-real).",
    )
    p_limpar.set_defaults(func=cmd_limpar)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = construir_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
