# Lançar a despesa no Tricount ao concluir a lista

## Context

Hoje `POST /listas/{id}/concluir` só muda `Lista.status` para `concluida`; o entregável do app
termina na tela de rateio (foi assim que ficou quando a integração com Splitwise foi descartada,
por exigir Splitwise Pro). O objetivo agora é fechar o ciclo: ao concluir a lista, a despesa é
lançada automaticamente num tricount (grupo) específico, com os valores exatos por pessoa que a
tela de rateio já mostra, sem ninguém redigitar nada no app do Tricount.

**O que a pesquisa achou da API do Tricount:**

- **Não existe API oficial.** O Tricount é do bunq e não publica API pública nem portal de
  desenvolvedor. O que existe é a API privada do app Android, mapeada por engenharia reversa.
- Usaremos a lib Python não-oficial **`tricount-api==0.1.2`** (PyPI, MIT, `requests` +
  `cryptography`, Python ≥3.10) — é a base dos outros projetos da comunidade (`go-tricount`,
  `tricount-mcp`). Alternativa descartada: escrever nosso próprio cliente dos 2–3 endpoints —
  ganha zero dependência, custa a manutenção da engenharia reversa e da assinatura RSA.
- **Sem login.** O cliente registra um "device" anônimo (UUID + chave RSA) via
  `POST /v1/session-registry-installation` e recebe um token de sessão. As credenciais são um JSON
  reutilizável. **Correção pós-leitura do fonte:** a lib 0.1.2 **não** re-registra sozinha em 401 —
  não há tratamento de 401 em `client.py`, só `raise_for_status()`. Como o processo do Cloud Run
  dura horas e o token de sessão vive no header da `Session`, a expiração deixaria a integração
  quebrada até o próximo deploy. O `real.py` implementa a retentativa: em 401, descarta o client,
  re-autentica e refaz a operação **uma única vez** (401 = rejeitado antes de qualquer efeito, logo
  seguro até para escrita); falhando de novo, `TricountIndisponivel`.
- **O grupo é identificado pelo sharing token** `tXXXXX` do link `tricount.com/tXXXXX`. Esse token
  dá **leitura e escrita**, **não é revogável**, e qualquer um com ele edita tudo → é segredo.
- **Sim, é obrigatório vincular pessoa↔membro.** Membros têm `id`, `uuid`, `display_name`,
  `status`. Casar por nome lança a despesa na pessoa errada quando os nomes divergem (o
  `tricount-mcp` se recusa explicitamente a adivinhar). O vínculo será explícito, por dropdown.
- **Criar despesa com valores exatos:** `create_transaction_custom_split(tricount, description,
  amount, payer=<Member>, allocations=[(member, valor), ...])`. Valores passados **positivos** (a
  lib aplica o sinal; no servidor despesa é negativa). Erros vêm como `requests.HTTPError`.
- **Escrever exige o grupo sincronizado no device — `join_tricount` é obrigatório.** *Achado de
  campo (2026-11-10), que corrige o que este plano afirmava:* `get_tricount(token)` acha o grupo
  pelo token público e a **leitura** funciona, mas a escrita responde
  `HTTP 404 "Registry with id X not found"` enquanto o registry não estiver sincronizado na conta
  do device (`GET /v1/user/{uid}/registry` sem params volta vazio num device novo). Com
  `join_tricount(token)` antes, a escrita funciona — join é **obrigatório e suficiente**. O
  `real.py` faz esse join **só reagindo ao 404**, no caminho de escrita (ver §2.5 e §9).
- **Riscos aceitos:** API não documentada, pode quebrar sem aviso; escrita só é aceita com
  User-Agent do app Android (a lib já manda); o uso provavelmente está fora dos Termos do
  Tricount (grupo familiar próprio).

**Decisões tomadas pelo usuário:**

| Tema | Decisão |
|---|---|
| Pagador | fixo por conta — a `Pessoa` com `is_dono=True`. Hoje há um login só; quando existirem várias contas, cada conta tem seu dono padrão e o `is_dono` passa a ser escopado por conta sem mexer nesta integração. **Nada de env var para pagador.** |
| Vínculo pessoa↔membro | dropdown na tela de Pessoas, membros buscados ao vivo do tricount |
| Falha ao enviar | **fail-open**: a lista conclui normalmente, o erro é persistido, a tela mostra aviso + botão "Reenviar ao Tricount" |
| Credenciais do device | JSON no **Secret Manager** → env var → materializado em `/tmp` no primeiro uso |

---

## 1. Schema — duas tabelas novas (`app/models.py`)

```python
class PessoaTricount(SQLModel, table=True):
    pessoa_id: int = Field(foreign_key="pessoa.id", primary_key=True)
    membro_uuid: str
    membro_nome: str                        # cache p/ exibir sem bater na API
    vinculado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ListaTricount(SQLModel, table=True):
    lista_id: int = Field(foreign_key="lista.id", primary_key=True)
    status: str                             # "enviando" | "ok" | "erro"
    transaction_id: Optional[str] = None
    tricount_token: Optional[str] = None    # qual grupo recebeu (auditoria)
    amount_centavos: Optional[int] = None
    rateio_hash: Optional[str] = None       # sha256 das alocações enviadas
    erro: Optional[str] = None              # str(exc)[:500]
    tentativas: int = 0
    atualizado_em: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
```

**Tabela nova, não coluna.** `init_db()` em `app/db.py` só roda
`SQLModel.metadata.create_all(engine)`, que cria tabelas faltantes e **nunca** faz
`ALTER TABLE ADD COLUMN`. Uma coluna `tricount_tx_id` em `lista` funcionaria só em banco novo e
derrubaria a produção (Turso) com `no such column` no primeiro SELECT do ORM. Foi exatamente por
isso que `ListaPessoa` entrou como tabela. Bônus: PK = `lista_id` dá idempotência 1:1 de graça.
Custo: um `session.get()` extra em vez de ler um campo já carregado — irrelevante aqui.

Sem backfill (estado inicial vazio é o correto; adivinhar vínculo é proibido). `app/db.py` não
muda — os modelos novos entram pelo `from app import models` que `init_db` já faz.

**Limpeza referencial** (o projeto deleta à mão, sem cascade): `excluir_lista` em
`app/routers/listas.py` passa a deletar o `ListaTricount` da lista; `excluir` em
`app/routers/pessoas.py` passa a deletar o `PessoaTricount` da pessoa (mesmo padrão já usado para
`ListaPessoa`).

## 2. Camada desacoplada — novo pacote `app/tricount/`

Espelha `app/llm/` (ABC + impl real + fake + factory com `lru_cache`), padrão já usado duas vezes
no projeto.

### `app/tricount/base.py`
```python
@dataclass(frozen=True)
class MembroTricount:   uuid: str; nome: str; ativo: bool

@dataclass(frozen=True)
class Alocacao:         membro_uuid: str; valor_centavos: int

@dataclass(frozen=True)
class DespesaCriada:    transaction_id: str; amount_centavos: int

class TricountError(Exception): ...
class TricountConfigError(TricountError): ...    # token ausente, pessoa/dono sem vínculo
class TricountIndisponivel(TricountError): ...   # HTTPError, timeout, auth

class TricountBackend(ABC):
    @abstractmethod
    def listar_membros(self) -> list[MembroTricount]: ...
    @abstractmethod
    def criar_despesa(self, *, descricao: str, pagador_uuid: str,
                      alocacoes: list[Alocacao]) -> DespesaCriada: ...
```

O domínio fala **centavos `int`** e **uuid de membro**. Sharing token, credenciais, o `Member` da
lib e a unidade de moeda ficam inteiramente dentro da implementação real — o router nunca vê isso.

### `app/tricount/rateio.py` — arredondamento (puro, sem dependências)

Vive aqui, não no router nem no backend: é regra do split, tem de valer igual para fake e real, e
é a única parte testável isoladamente.

```python
def ratear_centavos(valores: list[float]) -> tuple[int, list[int]]:
    """Largest remainder. Devolve (total_centavos, centavos_por_posicao)
    garantindo sum(centavos) == total_centavos."""
    total_cents = int(Decimal(str(sum(valores))).scaleb(2)
                      .quantize(Decimal(1), rounding=ROUND_HALF_UP))
    exatos = [Decimal(str(v)).scaleb(2) for v in valores]
    pisos = [int(e.to_integral_value(rounding=ROUND_FLOOR)) for e in exatos]
    resto = total_cents - sum(pisos)
    ordem = sorted(range(len(exatos)), key=lambda i: (-(exatos[i] - pisos[i]), i))
    for i in ordem[:resto] if resto > 0 else []:
        pisos[i] += 1
    for i in reversed(ordem[resto:]) if resto < 0 else []:   # defensivo contra erro de float
        pisos[i] -= 1
    return total_cents, pisos
```

- `total_cents` sai do arredondamento **da soma**, não da soma dos arredondados → o `amount`
  casa com o total que a tela de rateio mostra.
- `Decimal(str(v))` ancora no decimal que o float imprime, evitando que `3.3333333333333335`
  caia num piso errado.
- Empate de fração resolvido pelo índice, e `pessoas` já vem ordenada por `Pessoa.id` → resultado
  determinístico entre execuções.
- Alocações de 0 centavo são descartadas depois (descartar zero não quebra a invariante).

### `app/tricount/fake.py`
`FakeTricountBackend`: 4 membros fixos (`uuid="fake-<slug>"`), `criar_despesa` loga o payload e
devolve `transaction_id=f"fake-{uuid4().hex[:8]}"`. Honra `TRICOUNT_FAKE_FALHA=1` levantando
`TricountIndisponivel`, e conta chamadas num atributo (para provar a não-duplicação no
reabrir→concluir).

### `app/tricount/real.py`
`TricountAPIBackend`, import lazy de `tricount` no `__init__` (padrão do `GCSStorageBackend`).
**Correção pós-pesquisa:** o pacote PyPI se chama `tricount-api` mas o módulo importável é
`tricount` — `import tricount_api` levanta `ModuleNotFoundError`. Use sempre imports absolutos
(`from tricount import TricountAPI, Credentials, Category, MemberStatus`), porque dentro de
`app/tricount/` um import relativo implícito resolveria para o nosso próprio pacote:

1. `_credenciais()` → materializa as credenciais (§5) e devolve `Credentials`.
2. `_cliente()` → lazy e memoizado na instância: `TricountAPI(creds)`, `authenticate()` uma vez
   por processo, `get_tricount(settings.tricount_token)`. **Nada disso no boot** — cold start não
   pode depender do Tricount.
3. **Timeout — resolvido, plano A:** a lib não expõe `timeout`, mas `TricountAPI.__init__` faz
   `self.session = requests.Session()` (atributo público, confirmado no fonte da 0.1.2). Envolver
   `client.session.request` injetando `kw.setdefault("timeout", settings.tricount_timeout_seconds)`.
   O `ThreadPoolExecutor` com `future.result(timeout=...)` era o plano B e **não é necessário** —
   fica descartado (deixava thread pendurada).
4. `listar_membros()` → `tricount.members` → `MembroTricount`, filtrando `status == DELETED`,
   com cache TTL de 60s na instância (o dropdown de Pessoas não deve martelar a API).
5. `criar_despesa()` → resolve o `Member` por `uuid` em `tricount.members` (as `allocations` são
   tuplas `(Member, valor)`, objeto e não uuid), chama `create_transaction_custom_split(...)` com
   valores **positivos** convertidos por `_CENTAVOS_POR_UNIDADE_API` (constante de módulo — unidade
   é fato de protocolo, não configuração). **Unidade, lida no fonte da 0.1.2:** o payload é
   `{"value": str(-amount), "currency": tricount.currency}` e a docstring de `Amount` diz
   `"1500" means 1500 JPY or 15.00 EUR, NOT cents` ⇒ `amount` está em **unidades de moeda com
   decimais**, logo `_CENTAVOS_POR_UNIDADE_API = 100`. Como o payload é `str(-amount)` cru, passar
   `float` geraria `"-0.30000000000000004"`: passa-se `Decimal(centavos) / 100`.
   **Confirmado em campo (2026-11-10):** `amount=1234` saiu como `R$ 1.234,00` no app e o servidor
   devolveu `-1234.00 BRL` ⇒ unidades de moeda, `_CENTAVOS_POR_UNIDADE_API = 100` certo.
   `category` **é aceita** (confirmado na assinatura e em campo, junto com `category_custom`) e o
   enum tem `Category.GROCERIES` — o passo 5 do probe está respondido. Traduz
   `requests.HTTPError`/`RequestException`/timeout em `TricountIndisponivel` (preservando
   `status_code` na mensagem) e membro inexistente em `TricountConfigError`.
6. **`join_tricount` lazy na escrita, reagindo a 404** (decisão do usuário, 2026-11-10). A escrita
   exige o registry sincronizado no device (ver §Context). `_chamar(..., join_em_404=True)` só no
   `criar_despesa`: em `HTTPError` 404, `_sincronizar_grupo` chama `join_tricount(token)`, troca o
   objeto `Tricount` em cache pelo que o join devolve e **refaz a escrita uma única vez**; falhando
   de novo, `TricountIndisponivel` com o status na mensagem. Uma flag de instância (`_join_feito`)
   evita repetir o POST a cada envio do mesmo processo — e **não** é resetada pelo descarte do
   client, porque o sync é estado do device no servidor, não da sessão HTTP.
   - **Controle de retry:** as duas causas (401 e 404) têm orçamentos *one-shot* independentes, num
     laço — no máximo **uma retentativa de cada** por chamada (pior caso: 3 tentativas), sem
     compor retry sobre retry e sem possibilidade de loop.
   - **Leitura continua sem join:** `listar_membros` (chamado a cada render do dropdown de Pessoas)
     não faz nenhum POST de sincronização.
   - **Trade-off (404-reativo vs join proativo no setup):** com credencial estável, o sync fica
     guardado no servidor — depois do primeiro join nenhum outro é necessário, nem após cold start
     ou deploy, então o caminho comum fica em **1 chamada**. O custo é que a **primeira** escrita
     de um device novo paga write(404) + join + write (~2 round trips extras, uma vez na vida do
     device). Join proativo no boot/`_cliente()` pagaria um POST a mais em toda instância nova e
     amarraria o cold start a mais uma chamada ao Tricount — exatamente o que a decisão 1 do
     `real.py` existe para evitar.
   - **`link_to_member` e `leave_tricount` seguem proibidos em runtime** (ver §9).

### `app/tricount/factory.py`
```python
@lru_cache
def get_tricount() -> TricountBackend:
    if settings.tricount_provider == "real":
        from app.tricount.real import TricountAPIBackend
        return TricountAPIBackend()
    from app.tricount.fake import FakeTricountBackend
    return FakeTricountBackend()
```

## 3. Orquestração — `app/tricount/service.py`

```python
def enviar_despesa(session: Session, lista: Lista,
                   totais: list[tuple[Pessoa, float]]) -> ListaTricount | None
```

Recebe os `totais` já calculados (evita import circular com `app.routers.listas` e reusa
`_totais_por_pessoa` sem repetir query). **Nunca levanta** — devolve a linha para a UI.

1. `settings.tricount_enabled` falso → `return None`.
2. `atual = session.get(ListaTricount, lista.id)`. Se `status == "ok"` → devolve sem chamar nada
   (**anti-duplicata do reabrir→concluir**). Se `status == "enviando"` e `atualizado_em` < 5 min →
   devolve (request em voo / double-tap no mobile).
3. Filtra `valor > 0`, chama `ratear_centavos`, descarta alocações de 0 centavo.
4. Resolve vínculos: `session.get(PessoaTricount, pessoa.id)` por participante. Faltando →
   `status="erro"`, `erro="Sem vínculo no Tricount: Ana, Bruno"`, retorna.
5. Resolve o pagador: `select(Pessoa).where(Pessoa.is_dono == True)` → seu `PessoaTricount`.
   Faltando → mesmo tratamento. Semântica do Tricount: o pagador paga o `amount` inteiro e a
   própria cota dele está dentro das `allocations`.
6. **Reivindica o slot antes da chamada:** upsert `status="enviando"`, `tentativas += 1`,
   `rateio_hash=...` + `commit`. Protege contra duplo clique e contra crash no meio da chamada
   (fica `enviando`, a UI oferece reenvio depois do grace period). `IntegrityError` na corrida
   entre instâncias → tratar como "já em envio".
7. `get_tricount().criar_despesa(descricao=lista.nome, pagador_uuid=..., alocacoes=...)` dentro de
   `try/except TricountError` + `except Exception`.
8. Sucesso → `status="ok"` + `transaction_id` + `amount_centavos` + `tricount_token`.
   Falha → `status="erro"`, `erro=str(exc)[:500]`, `logger.exception`. `commit`, retorna.

## 4. Encaixe no fluxo — `app/routers/listas.py`

```python
@router.post("/listas/{lista_id}/concluir")
def concluir_lista(lista_id: int, session: Session = Depends(get_session)):
    lista = session.get(Lista, lista_id)
    lista.status = "concluida"
    session.add(lista)
    session.commit()                    # a conclusão NUNCA depende do Tricount

    if settings.tricount_enabled:
        ctx = _totais_context(lista_id, session)   # reuso; 2 queries, não o _rateio_context todo
        enviar_despesa(session, lista, ctx["totais"])

    return RedirectResponse(f"/listas/{lista_id}/rateio?concluida=1", status_code=303)
```

O handler é `def` (sync), então a chamada bloqueante roda no threadpool do Starlette e não trava o
event loop — não precisa de `async`/`asyncio.to_thread`.

Rota nova ao lado de `reabrir_lista`: `POST /listas/{lista_id}/tricount/reenviar` — só age se a
lista está concluída e o envio **não** está `ok`; chama o mesmo `enviar_despesa`; redirect 303 para
`/listas/{id}/rateio` (igual a concluir/reabrir, sem HTMX).

`_rateio_context` ganha a chave, **sem custo no caminho quente**:

```python
envio_tricount = (
    session.get(ListaTricount, lista_id)
    if settings.tricount_enabled and lista and lista.status == "concluida"
    else None
)
```

Os swaps HTMX de toggle só ocorrem em lista `rascunho` (checkboxes ficam `disabled` quando
concluída), então isso não adiciona RTT ao Turso nos partials — respeita a otimização de RTTs que
está no working tree.

`reabrir_lista` **não** mexe no `ListaTricount` — é justamente essa linha que impede o
relançamento.

## 5. Credenciais do device no Cloud Run

`real.py`: se `settings.tricount_credentials_json` tem conteúdo e
`/tmp/tricount_credentials.json` não existe, escreve o arquivo (`0600`) e `Credentials.load(path)`.
Senão, se o path existe, carrega. Senão, `Credentials.generate()` + `save(path)` +
`logger.warning("device Tricount novo registrado; grave o JSON no Secret Manager")` —
conveniência em dev, alarme em produção.

Bootstrap, uma vez, na máquina local: `Credentials.generate()` → `creds.save(path)` →
`gcloud secrets create tricount-credentials-json --data-file=...`.

`/tmp` no Cloud Run é tmpfs (conta como memória) — ~1 KB é irrelevante, e por ser efêmero some com
a instância, o que é desejável.

**Por que persistir a credencial, se ela não é segredo.** O JSON salvo tem só `app_id` (uuid4) e
`public_key_pem` — `Credentials.generate()` descarta a chave privada —, então nada ali autoriza
nada: o sigilo que importa é o do `tricount-token`. O motivo é **operacional**:
- o `user_id` vem da identidade do device (`app_installation_uuid = app_id`) e o
  `registry-synchronization` fica guardado no servidor amarrado a esse `user_id`: credencial
  estável ⇒ join **uma vez na vida**; credencial nova a cada cold start ⇒ a primeira escrita de
  cada instância paga write → 404 → join → write;
- cada device novo é auto-vinculado ao membro de **menor id** ⇒ contas ilimitadas apontando para
  uma pessoa real da família, sem como limpar pelo app;
- evita uma instalação nova por cold start numa API de engenharia reversa sem limites conhecidos;
- evita `rsa.generate_private_key(2048)` dentro da requisição do usuário no cold start;
- mantém o WARN "device Tricount novo registrado" como **canário** de configuração errada (com
  credencial efêmera ele apareceria sempre e não diria nada).

## 6. Config, deploy e UI

### `app/config.py`
```python
tricount_enabled: bool = False
tricount_provider: str = "fake"           # "fake" | "real"
tricount_token: Optional[str] = None      # tXXXXX do link tricount.com/tXXXXX
tricount_credentials_json: Optional[str] = None
tricount_timeout_seconds: float = 8.0
```
Dois flags com propósitos distintos: `TRICOUNT_ENABLED` é o **kill switch** (a API não-oficial
quebra ⇒ desliga integração e UI sem deploy de código); `TRICOUNT_PROVIDER` escolhe fake/real (dev
local roda `enabled=true` + `provider=fake` e exercita o fluxo inteiro, inclusive as linhas no
banco). `extra="ignore"` no `Settings` significa que env var sem campo é descartada em silêncio —
os campos acima são obrigatórios.

- `requirements.txt`: `tricount-api==0.1.2` (pin exato — engenharia reversa, sem contrato de
  compatibilidade). Puxa `cryptography`; lembrar do `--platform linux/amd64` no build local.
- `.env.example`: as 5 vars, com `TRICOUNT_ENABLED=false` e `TRICOUNT_PROVIDER=fake`.
- `.github/workflows/deploy.yml`: `--set-env-vars` += `TRICOUNT_ENABLED=true,TRICOUNT_PROVIDER=real`;
  `--set-secrets` += `TRICOUNT_TOKEN=tricount-token:latest,TRICOUNT_CREDENTIALS_JSON=tricount-credentials-json:latest`.
- **Vai pro Secret Manager:** `tricount-token` (dá escrita e não é revogável — é segredo, não
  config) e `tricount-credentials-json` — este **não** é segredo por conteúdo (só `app_id` +
  chave **pública**); vai pro Secret Manager por disciplina/formato (os `\n` do PEM) e, sobretudo,
  para a credencial ser **estável** (ver §5).
- `README.md`: seção da integração + o resultado da verificação de unidade.

### `app/templates/pessoas.html` + `app/routers/pessoas.py` (vínculo)
- `listar` passa `vinculos: dict[int, PessoaTricount]` (um SELECT) e `tricount_enabled`; o card da
  pessoa ganha badge "Tricount: {nome}" (`badge-success`) ou "Sem vínculo" (`badge-pending`).
- Bloco novo no fim da página, carregado ao vivo — **uma** chamada à API por page load, não uma
  por pessoa:
  ```html
  <div id="tricount-vinculos" hx-get="/pessoas/tricount/vinculos" hx-trigger="load" hx-swap="outerHTML"></div>
  ```
- `GET /pessoas/tricount/vinculos` → novo `app/templates/_tricount_vinculos.html`: uma linha por
  pessoa com `<select name="membro_uuid">` (opção vazia "— sem vínculo —" + membros ativos, uuid
  atual pré-selecionado) e `hx-post="/pessoas/{{ p.id }}/tricount" hx-trigger="change"
  hx-target="#tricount-vinculos" hx-swap="outerHTML"`. Em `TricountError`, renderiza o mesmo
  container com `alert-triangle` + mensagem + "Tentar de novo" (`hx-get` no mesmo endpoint) —
  nunca 500 na tela de Pessoas.
- `POST /pessoas/{pessoa_id}/tricount` (Form `membro_uuid`): vazio → deleta o vínculo; preenchido →
  resolve o **nome no servidor** via `listar_membros()` (não confia no label do cliente) e faz
  upsert. Devolve `_tricount_vinculos.html`.
- `tricount_enabled` falso → nada disso é renderizado.

### `app/templates/_rateio.html` (status + reenvio)
Dentro do `{% if lista.status == 'concluida' %}`, acima do "Editar divisão" — de preferência
extraído para `app/templates/_tricount_status.html` para manter `_rateio.html` legível:
- `status == "ok"` → linha discreta `{{ icons.icon('check') }} Lançado no Tricount` + o valor de
  `amount_centavos`; se `rateio_hash` ≠ hash atual, aviso "a divisão mudou depois do lançamento".
- `erro` (ou `enviando` velho) → `info-box` em tom de alerta com `alert-triangle`, a mensagem, e
  `<form method="post" action="/listas/{{ lista.id }}/tricount/reenviar">` com
  `btn btn-outline btn-block` "Reenviar ao Tricount".
- `envio_tricount` nulo com `tricount_enabled` → nada (lista concluída antes da feature).
- `app/static/style.css`: variante de alerta para `info-box` (já existem `--danger` e
  `badge-pending`).

## 7. Verificação end-to-end

### Local, com o fake (não toca em nada real)
Nunca contra `data/listadivide.db`: `cp data/listadivide.db <scratchpad>/teste.db` e rodar com
`DATABASE_URL=sqlite:///<scratchpad>/teste.db TRICOUNT_ENABLED=true TRICOUNT_PROVIDER=fake`.

1. `/pessoas` → dropdown lista os 4 membros fake; vincular 2 pessoas; recarregar e conferir
   persistência + badges.
2. Concluir uma lista → `ListaTricount` com `status="ok"`, `amount_centavos == sum(alocações)`,
   tx id visível na tela de rateio.
3. Reabrir → concluir de novo → **nenhuma** chamada nova (contador do fake), nenhuma linha nova.
4. Participante sem vínculo → lista conclui, aviso + botão de reenvio; vincular e reenviar → `ok`.
5. `TRICOUNT_FAKE_FALHA=1` → conclusão passa, `status="erro"`; desligar e reenviar → `ok`.
6. Double-tap em "Concluir lista" → um envio só (slot `enviando`).
7. Arredondamento no REPL (não há suíte de testes no projeto): `[10/3]*3`,
   `[33.333, 33.333, 33.334]`, `[0.01/3]*3`, `[0.005, 0.005]` e algumas centenas de vetores
   aleatórios de `preco/n`; asserções `sum(cents) == total_cents` e
   `total_cents == round(sum(valores)*100)`.

### Tricount **descartável** — resolve a ambiguidade de unidade
Script manual `scripts/tricount_probe.py`, **nunca** chamado pelo app, exige `--token` explícito e
aborta se o token for igual ao de produção.

1. Criar um tricount novo no app Android com 3 membros fictícios; copiar o `tXXXXX`.
2. `Credentials.generate()` em path de scratch → `authenticate()` → `get_tricount(token)` →
   imprimir `members` (`id`, `uuid`, `display_name`, `status`) e `vars(client)` (para achar a
   `requests.Session` do timeout).
3. **Unidade — RESPONDIDA em campo (2026-11-10):** escrito `amount=1234`, o app mostrou
   **R$ 1.234,00** e o servidor devolveu `-1234.00 BRL` ⇒ **unidades de moeda**, não centavos;
   `_CENTAVOS_POR_UNIDADE_API = 100` confirmado. **O que falta** é a escrita com valor *decimal*
   (`12.34` / `3.33`), que é o que o `real.py` manda de verdade: `--amount`/`--split` do probe
   passaram a ser `Decimal` por isso.
4. **Escrita exige `join_tricount`? — RESPONDIDA: SIM.** Só com `get_tricount` a escrita volta
   `404 "Registry with id X not found"`; com `join_tricount(token)` antes, funciona. Efeito
   colateral medido: o **servidor** auto-vincula o device ao membro de **menor id** no
   `registry-synchronization` (`membership_uuid_active` era `None` e passou a apontar para ele) —
   **não** é a lib que vincula (ela nunca chama `link_to_member`), é o servidor no sync. O vínculo
   **não é exclusivo**: o celular do dono continuou identificado como o mesmo membro, com a despesa
   como "paid by me". Por isso o `real.py` faz o join em 404 (§2.6) e `--join` do probe segue
   proibido com `--grupo-real`.
5. `Category.GROCERIES`: **RESPONDIDA — aceita** (e `category_custom` também).
6. Soma com centavo ímpar — com a unidade resolvida, o caso é `3.33/3.33/3.34 = 10.00` (o antigo
   `333/333/334 = 1000` testava *real* ímpar): confirmar que o servidor aceita e não re-arredonda.
7. Tipo do `tx_id`: **RESPONDIDA — `int`** (ex.: `2142412623`); guardamos como `str` de todo jeito.
8. `delete_transaction` em tudo, e apagar o tricount descartável.

### Produção
1. Deploy com `TRICOUNT_ENABLED=false`. Confirmar no Turso que `pessoatricount` e `listatricount`
   foram criadas (`turso db shell ... ".schema listatricount"`) e que nada mais mudou; app
   saudável, UI sem traço da feature.
2. Virar `TRICOUNT_ENABLED=true` + `provider=real`, vincular as pessoas, e conferir que os logs
   **não** têm o WARN de "device novo" — prova que o secret foi lido.
3. Primeira escrita real: lista de teste com um item de R$ 0,03 entre 3 pessoas → conferir valor e
   split no app Tricount → apagar a despesa pelo app → apagar a lista.
4. Nos logs do Cloud Run: latência da conclusão no cold start (o `authenticate` custa um round
   trip por instância) e nenhuma conclusão estourando o timeout.

## 8. Ordem de execução

1. `app/models.py` (as duas tabelas) → subir local e confirmar o `create_all`.
2. `app/config.py`, `.env.example`, `requirements.txt`.
3. `app/tricount/`: `base.py` → `rateio.py` → `fake.py` → `factory.py` → `service.py`.
4. Rotas e templates de Pessoas (o vínculo é pré-requisito do envio).
5. `concluir_lista` + `/tricount/reenviar` + `_rateio_context` + `_tricount_status.html` + CSS.
6. Limpezas referenciais em `excluir_lista` e no `excluir` de pessoa.
7. Verificação local com o fake (§7).
8. `real.py` + probe no tricount descartável; fixar unidade, timeout e a questão do `join`.
9. Secret Manager + `deploy.yml` + README; deploy com `ENABLED=false`; vincular; primeira escrita
   real controlada.

## 9. Riscos e o que fica de fora

**Riscos**
- API não-oficial pode quebrar sem aviso → mitigado por `TRICOUNT_ENABLED`, fail-open e pin de
  versão.
- O sharing token dá escrita e **não é revogável**: se vazar, só resta recriar o tricount. Por isso
  vai para o Secret Manager, não para `--set-env-vars`.
- `join_tricount` (obrigatório para escrever) faz o **servidor** vincular este device ao membro de
  **menor id** do grupo — não é a lib que vincula, é o sync. Medido em campo e **mitigado em
  campo**: o vínculo **não é exclusivo** (o celular do dono continua sendo o mesmo membro, a
  despesa aparece como "paid by me" para ele), então o join não rouba a identidade de ninguém.
  Risco residual aceito: no grupo real, o device passa a "ser" o membro de menor id do ponto de
  vista da API — não afeta a escrita (o pagador vai explícito em `payer` e nas alocações) nem os
  outros membros, mas é estado remoto que o app cria. `link_to_member` e `leave_tricount` seguem
  **proibidos em runtime**: o primeiro é o que de fato troca a identidade de um membro, e o
  segundo desfaria o sync e devolveria o 404 na escrita seguinte.
- Credencial de device de vida longa, sem rotação automática.
- Arredondamento assume BRL com 2 decimais (a moeda é definida na criação do tricount e imutável).
- O slot `enviando` cobre duplo clique e crash, mas não é lock distribuído — duas instâncias
  concluindo a mesma lista no mesmo instante colidem na PK; tratar o `IntegrityError` como "já em
  envio".
- `total_geral` ≠ `total_compra` quando há item sem participante: a despesa vai com a soma dos
  rateios (correto, mas vale o aviso).

**Fora de escopo (decisão explícita)**
Atualizar/apagar a despesa quando a divisão muda depois do envio — `transaction_id` e
`rateio_hash` ficam guardados exatamente para habilitar isso numa fase 2; retry automático/cron;
escolha de tricount por lista (o token é global); categorias por item; múltiplos pagadores;
reconciliação via `get_balances`; moedas além de BRL; suíte de testes automatizada.

**Por que fail-open, e o que se perde:** fail-closed acoplaria o fluxo principal de um app pessoal
a uma API de engenharia reversa — uma indisponibilidade do Tricount impediria concluir listas.
`BackgroundTasks` é pior: no Cloud Run sem CPU-always-on a CPU é estrangulada depois da resposta,
então a chamada pode arrastar ou nem terminar antes do shutdown, sem retry nem visibilidade.
Síncrono com timeout curto é melhor: é uma request, o usuário já está esperando o redirect, e o
erro fica persistido e visível. O que se perde: a conclusão passa a esperar até ~8s de rede no pior
caso, e dá para esquecer de clicar "Reenviar" (uma fila de verdade, Cloud Tasks, seria o correto em
escala; para 2–5 usuários a linha no banco + botão é a fila do pobre).

---

## 10. Progresso da execução

Atualizado em 2026-11-10. Legenda: `[ ]` pendente · `[~]` em andamento · `[x]` feito · `[!]` bloqueado (depende de ação manual do usuário).

| # | Etapa (§8) | Status | Notas |
|---|---|---|---|
| 1 | `app/models.py` — `PessoaTricount` + `ListaTricount`, confirmar `create_all` | [x] | `create_all` verificado numa **cópia** do banco: só adições, nenhum DDL das tabelas antigas mudou. `data/listadivide.db` intocado. |
| 2 | `app/config.py`, `.env.example`, `requirements.txt` | [x] | `tricount-api==0.1.2` instalado no venv (pure-python `py3-none-any` → sem o problema de arm64 do build). |
| 3a | `app/tricount/base.py` | [x] | Contrato: centavos `int` + uuid de membro; `criar_despesa` keyword-only. |
| 3b | `app/tricount/rateio.py` | [x] | Maior resto, módulo puro. **Verificado:** 4 casos-limite + 1421 vetores, 0 falhas de `sum(cents) == total_cents` e de `total_cents == ROUND_HALF_UP(soma*100)`. |
| 3c | `app/tricount/fake.py` + `factory.py` | [x] | 4 membros ativos + 1 inativo (exercita o filtro do dropdown). `TRICOUNT_FAKE_FALHA` lido **na chamada**, para ligar falha → concluir → desligar → reenviar sem restart. `chamadas_criar_despesa` só incrementa em sucesso. |
| 3d | `app/tricount/service.py` | [x] | Expõe `enviar_despesa`, `hash_rateio` e `GRACE_ENVIANDO`. Smoke isolado cobriu os 8 passos (sem vínculo, ok, reenvio pós-ok sem chamada, slot recente/velho, kill switch, `TRICOUNT_FAKE_FALHA` → erro → reenvio ok). |
| 4 | Rotas + templates de Pessoas (vínculo) | [x] | `GET /pessoas/tricount/vinculos` + `POST /pessoas/{id}/tricount`, declaradas antes das rotas com path param. Kill switch → 404. Estado de erro sempre **200** (htmx não faz swap em erro) e `Exception` genérica usa mensagem fixa para não vazar URL/token. Extras: opção "(fora do grupo)" quando o vínculo aponta para membro que saiu. |
| 5 | `concluir_lista` + `/tricount/reenviar` + `_rateio_context` + `_tricount_status.html` + CSS | [x] | 7 estados do `_tricount_status.html` renderizados + smoke de integração em banco isolado (concluir → `ok`, reabrir+concluir → 0 chamadas novas, divergência de hash, grace period, kill switch). Colisão de rotas testada no matcher do Starlette. |
| 6 | Limpezas referenciais (`excluir_lista`, `excluir` de pessoa) | [x] | Feito dentro de 4 e 5 (cada agente limpou o router que já era dono). Vale com `tricount_enabled` falso também. |
| 7 | Verificação local com o fake (§7) | [x] | **78/78 checagens passaram, 0 bugs.** Por HTTP real (`TestClient`, stack inteiro com auth+gzip) contra uma **cópia** do banco; `data/listadivide.db` com md5 e mtime idênticos antes e depois. Double-tap testado com **concorrência real** (2 threads + barrier): 1 chamada, `tentativas=1`. RTTs medidos por instrumentação do engine: **0 queries** de tricount nos partials HTMX; só o caminho frio paga as 2 extras. |
| 7b | Ajustes pós-verificação (bloquear conclusão sem marcação, linha "Enviando…", 404) | [x] | **104/104 checagens, 0 falhas** (56 `on` + 9 `off` + 15 `extra` + 24 da fase nova `bloqueio`). Banco real com md5 e mtime idênticos. |
| 8a | `app/tricount/real.py` | [x] | 51 asserções com dublê, todas passando; nenhuma chamada de rede real (o teste bloqueia `HTTPAdapter.send`). TTL de 60s aplicado ao próprio objeto `Tricount` (um relógio só). `INACTIVE` entra com `ativo=False` para o vínculo histórico não desaparecer do dropdown. **Revisado em 2026-11-10:** `join_tricount` passou a ser chamado **na escrita, reagindo a 404** (§2.6), com retry one-shot por causa (401 e 404) e flag `_join_feito`; 50 asserções novas com dublê (`HTTPAdapter.send` bloqueado) cobrindo 401, 404, 401+404 nas duas ordens, repetição, 500 sem retry e leitura sem join. `link_to_member`/`leave_tricount` continuam **nunca** chamados. |
| 8b | `scripts/tricount_probe.py` | [x] | Subcomandos `inspect` / `unidade` / `centavo-impar` / `limpar`. Guarda dupla: aborta se o token == `TRICOUNT_TOKEN` de produção, e recusa `--creds` no path de produção. `limpar` é dry-run por default. **Revisado em 2026-11-10:** `--amount`/`--split` são `Decimal` exato (nunca `float`, que a lib serializaria com `str()` cru), soma validada em `Decimal`; `centavo-impar` passou a `10.00 = 3.33+3.33+3.34`; ambos relêem a transação pelo servidor para mostrar os valores crus (`Amount.value` é `str`). |
| 8c | Rodar o probe no tricount descartável | [x] | **Feito pelo usuário em 2026-11-10**, no tricount "Teste" (BRL, 3 membros fictícios). Respostas em "Achados de campo" abaixo: unidade = unidades de moeda; escrita exige `join`; `category` aceita; `tx_id` `int`; `client.session` acessível. Sobra só a escrita **decimal** (`12.34` / `3.33`). |
| 9a | `deploy.yml` + README | [x] | 2 env vars + 2 secrets no workflow; README com a subseção da integração, estrutura de arquivos e verificação de produção. |
| 9b | Secret Manager + deploy + primeira escrita real | [!] | **Ação do usuário:** `gcloud secrets create tricount-token` / `tricount-credentials-json`, depois o deploy e a escrita controlada de R$ 0,03. |

### O que a leitura do fonte da lib já resolveu (não precisa mais do probe)

| Pergunta do plano | Resposta | Onde |
|---|---|---|
| Módulo a importar | **`tricount`**, não `tricount_api` — o plano estava errado e isso quebraria o `real.py` | §2 corrigida |
| Timeout acessível? | Sim: `TricountAPI.session` é uma `requests.Session` pública ⇒ plano A; `ThreadPoolExecutor` descartado | §2.3 corrigida |
| `category` aceita? | Sim, e existe `Category.GROCERIES` (melhor que `FOOD_AND_DRINK` aqui) | §2.5 corrigida |
| Unidade da API | Unidades de moeda com decimais (`str(-amount)`, docstring de `Amount`: *"NOT cents"*) ⇒ `_CENTAVOS_POR_UNIDADE_API = 100`, passar `Decimal(cents)/100` e nunca `float`. **Confirmado em campo em 2026-11-10** (`1234` → `R$ 1.234,00`) | §2.5 + achados de campo |
| Tipo do `tx_id` | `int` (a lib faz `int(...)` no `_extract_id`); guardamos como `str` de todo jeito | — |
| Re-registro em 401 | **Não existe** na lib — o plano afirmava que existia. O `real.py` faz a retentativa única | §Context corrigida |
| `Credentials.save()` | usa `open(path, "w")` ⇒ nasce 0644 pelo umask; o `real.py` faz `chmod 0600` imediato | — |
| `Credentials.generate()` | descarta a chave privada (guarda só `app_id` + `public_key_pem`); `authenticate()` só manda a pública | — |
| `tricount.__version__` | reporta `"0.1.0"` com dist-info `0.1.2` — não serve para pin/diagnóstico | — |
| `edit_transaction` | só aceita `split_among: list[Member]` (divisão **igual**), não alocações customizadas | ver fase 2 abaixo |

### Nota sobre o arredondamento (item 7 da §7, já verificado)

A asserção `total_cents == round(sum(valores)*100)` que o plano escreveu é **imprecisa** e foi
substituída por `ROUND_HALF_UP` na verificação: `round()` do Python é banker's rounding (desempata
para o par), e `sum*100` arredonda o **produto em float**, criando empate falso (ex.: `378.915`
imprime `378.91499999999996` mas `*100` cai exatamente em `.5`). Das 1421 amostras, 11 divergiram —
todas empates exatos de meio centavo, só em vetores heterogêneos adversariais. O critério da função
está certo: para BRL, meio centavo sobe; banker's é para estatística, não para dinheiro.

Achado colateral: `_rateio.html` formata com `'%.2f'|format(valor)`, que é half-even sobre o valor
binário, então em empate exato de meio centavo a tela pode divergir 1 centavo do `amount_centavos`
enviado. Com preços de 2 decimais isso **nunca** ocorre (500 rodadas do cenário realista, 0
divergências); só apareceria com preço de 3 decimais no banco. Fica registrado, sem ação.

### Achados de campo — 2026-11-10 (tricount descartável "Teste", BRL, 3 membros fictícios)

1. **Escrever exige `join_tricount`; ler, não.** `get_tricount(token)` acha o grupo pelo token
   público e a leitura funciona, mas `create_transaction_custom_split` devolve
   **`HTTP 404 "Registry with id X not found"`** enquanto o registry não estiver sincronizado na
   conta do device (`GET /v1/user/{uid}/registry` sem params volta vazio num device novo). Com
   `join_tricount(token)` antes, a escrita **funciona**. Join é **obrigatório e suficiente**.
2. **O join vincula o device ao membro de menor id — pelo servidor, não pela lib.** No fonte da
   0.1.2, `join_tricount` é só um `POST /v1/user/{uid}/registry-synchronization` e não toca em
   `membership_uuid_active`; quem troca identidade é `link_to_member()` (um PUT separado), que o
   app **nunca** chama. Mas, medido em campo, depois do join o objeto voltou com
   `membership_uuid_active='578c7b4a-…'` / `linked_member='Daniel Cuadros'` (o membro de menor id,
   `528283283`), onde antes era `None` — ou seja, **o servidor vincula sozinho no sync**. A
   premissa original do plano estava certa no *efeito* e errada na *mecânica*.
   **Mitigação empírica:** o vínculo **não é exclusivo** — o celular do usuário continuou
   identificado como "Daniel Cuadros" no mesmo grupo depois do join do device do probe (a despesa
   aparece como "paid by me" no app dele). Duas contas podem apontar para o mesmo membro, então o
   join não rouba a identidade de ninguém — é isso que torna o risco aceitável.
   **Risco residual registrado:** no grupo real, o device passa a "ser" o membro de menor id do
   ponto de vista da API. Não afeta a escrita (o pagador vai explícito em `payer` e nas alocações)
   nem os outros membros, mas é estado remoto que o app cria.
   *Detalhe de implementação:* depois do join, `get_tricount(token)` (lookup pelo token público)
   **continua** devolvendo `membership_uuid_active=None`, enquanto `get_tricount_by_id` (usado por
   dentro do `join_tricount(fetch_full=True)`) devolve o uuid vinculado — então
   `membership_uuid_active` **não** serve como sinal de "já dei join"; o sinal é a flag de
   instância `_join_feito`.
3. **Unidade monetária confirmada: unidades de moeda, não centavos.** O probe escreveu
   `amount=1234` (int cru) e o app Android mostrou **R$ 1.234,00**; o servidor devolveu a
   transação como `amount=-1234.00 BRL` (duas casas decimais). Logo
   `_CENTAVOS_POR_UNIDADE_API = 100` e o `Decimal(centavos)/100` do `real.py` estão **certos** —
   essa lógica não se mexe.
4. **Token inválido não dá 404** (ao contrário do que o README da lib afirma): dá `HTTP 200` com
   `Response: []`, e a lib levanta `RuntimeError("No tricount found")`. O `real.py` já traduz isso
   em `TricountConfigError` — correto, sem mudança.
5. Confirmações menores: `category` **aceita** (e `category_custom` também); `tx_id` veio `int`
   (`2142412623`); `client.session` é uma `requests.Session` acessível ⇒ o wrapper de `timeout`
   do `real.py` está confirmado e o plano B (`ThreadPoolExecutor`) fica descartado.

### O que ainda falta (ação do usuário)

1. **Confirmar decimais em campo.** Todas as escritas de campo usaram inteiros; o `real.py` manda
   `"12.34"` / `"3.33"`. Rodar no descartável, com `--join`:
   `unidade --amount 12.34 --split 6.17,6.17`, `centavo-impar` (default `10.00` em
   `3.33/3.33/3.34`) e `unidade --amount 0.03 --split 0.01,0.01,0.01`; conferir no app e na
   releitura que o servidor não truncou nem re-arredondou. Depois `limpar --confirmar` e apagar o
   tricount descartável.
2. **Token do grupo REAL da família.** O `TRICOUNT_TOKEN` que está no `.env` hoje é o do
   descartável **"Teste"** — trocar pelo do grupo real antes de qualquer escrita de verdade.
3. **Secrets no GCP:** `gcloud secrets create tricount-token` / `tricount-credentials-json` (etapa
   9b).
4. **Deploy** com `TRICOUNT_ENABLED=false` → conferir schema no Turso → virar `true` + `real` →
   vincular pessoas → primeira escrita real controlada de R$ 0,03 entre 3 pessoas. A **primeira**
   escrita do device em produção vai pagar write(404) + join + write; é o esperado, e o log
   `"grupo Tricount sincronizado neste device (join_tricount) após 404"` é a prova de que
   aconteceu uma vez só.

### Achados que afetam a fase 2 (fora de escopo, mas bom saber)

`edit_transaction` só aceita `split_among: list[Member]` (divisão **igual**), não alocações customizadas. Então "atualizar a despesa quando a divisão muda" provavelmente vira *delete + recreate*, não *edit* — o `transaction_id` guardado continua sendo o que habilita isso.

### Dívida de duplicação paga (pós-paralelização)

Como os agentes trabalharam em arquivos disjuntos, o router reimplementou os passos 3–4 do
`_enviar`. Consolidado depois: o `service.py` passou a expor `alocacoes_atuais(session, totais)`,
`envio_em_voo(envio)` e `envio_pendente(envio)`, e o router perdeu ~60 linhas — não tem mais nenhuma
regra de split, e o grace period de 5 min vive num lugar só (`GRACE_ENVIANDO`).

`envio_pendente` **não** é a negação de `envio_em_voo`, e por isso são duas funções: com
`status == "erro"`, pendente é True (a UI oferece reenvio) mas em_voo é False (o `_enviar` segue e
reivindica o slot). Forçar uma função só exigiria um parâmetro de modo ou mudar o tratamento de
`erro` num dos lados — ou seja, mudança de comportamento. Os dois smokes deram saída idêntica linha
a linha antes e depois, incluindo o `rateio_hash`.

### Decisões tomadas pelo `service.py` onde a §3 era ambígua

| Ponto | Decisão | Trade-off |
|---|---|---|
| `tentativas` | só conta envio que reivindicou o slot (erro de vínculo/pagador não incrementa) | o contador vira indicador real de chamadas de rede; perde-se saber quantas vezes se tentou com config quebrada |
| Quem precisa de vínculo | só quem tem cota > 0 | participante sem nenhum item não bloqueia o envio (não entraria nas `allocations`); a leitura literal da §3 ("por participante") avisaria mais cedo, ao custo de bloquear envios legítimos. **Afeta o item 4 da §7:** o teste tem de usar um participante que de fato tem itens |
| Ordem da checagem | vínculo é checado **antes** de descartar alocações de 0 centavo | fail-safe barato: quem arredondou para 0 e não tem vínculo ainda aparece no aviso |
| Token | não validado no service — token ausente é `TricountConfigError` do backend real | não duplica regra de configuração do provider; o fake nem usa token |
| Total divergente | `logger.warning` quando o total alocado ≠ o confirmado pelo backend (caso do item sem participante da §9) | grava o que o backend confirmou e deixa rastro, em vez de falhar |

### Achados da verificação E2E — decididos pelo usuário em 2026-11-06

**1. Concluir lista sem nenhuma marcação vira alerta de falha do Tricount com botão inútil.**
Hoje as listas 1, 2 e 4 do banco estão nesse estado. Grava
`erro="Nada a lançar: nenhuma pessoa com valor nesta lista."` e a tela mostra `info-box--alerta`
"Não foi lançado no Tricount" + "Reenviar ao Tricount"; clicar só re-grava o mesmo erro. Está
conforme a §3 passo 3, mas lê como "o Tricount falhou" quando o fato é "não havia o que dividir".

| Opção | Ganha | Custa |
|---|---|---|
| (a) deixar como está | zero trabalho; estado visível e auditável | alerta falso-positivo no caso mais comum de uso descuidado; treina o usuário a ignorar o aviso, que é o que mata o valor do fail-open |
| (b) `return None` sem persistir quando não há devedor | tela limpa, nenhum falso alarme | perde o rastro de que a conclusão aconteceu sem lançamento; marcando os itens depois não há botão para lançar (só reabrir→concluir) |
| (c) persistir + campo `erro_tipo` (`nada_a_lancar` / `sem_vinculo` / `indisponivel`); o template suprime o botão e usa tom neutro no primeiro caso | distingue "não havia o que lançar" de "o Tricount falhou" preservando o rastro | uma coluna nova em `ListaTricount` (de graça agora, proibitiva depois: `create_all` nunca faz `ALTER TABLE`) |

**→ DECISÃO (usuário, 2026-11-06): nenhuma das três — _não permitir concluir lista sem itens
marcados_.** É estritamente melhor: elimina o estado na origem, então não há alerta falso, não há
coluna nova e não se perde rastro (o rastro que faltava era de um evento que deixa de existir).
Critério do bloqueio: **nenhuma pessoa com valor > 0** (não "nenhum `ItemParticipante`" — marcação
só em item de preço 0 dá no mesmo). Aplicado **independente de `tricount_enabled`**, porque a regra
é de produto ("concluir sem rateio não significa nada") e não da integração. O caminho
"Nada a lançar" do service fica como defesa inalcançável pela UI, protegendo a rota de reenvio.
O que se perde: deixa de ser possível concluir uma lista só para arquivá-la sem dividir nada.

**2. Entre o crash no meio da chamada e os 5 min de `GRACE_ENVIANDO`, a tela não diz nada.**
Com `status="enviando"` recente, `envio_pendente` é False e o ramo `ok` não vale ⇒
`_tricount_status.html` renderiza vazio. No caso normal ninguém vê (o envio dura ~8 s e o redirect
chega depois), mas quem concluiu durante um crash/deploy fica até 5 min sem ver nem "lançado" nem
"pendente". Opções: (a) deixar assim — zero ruído no caso comum, 5 min de silêncio ambíguo no caso
ruim; (b) linha discreta "Enviando ao Tricount…" sem botão — estado sempre explícito, mas aparece
quase só no caso ruim e, sem polling, não se atualiza sozinha.

**3. Inconsistência pré-existente, que o diff torna visível:** `POST /listas/999/concluir` e
`/reabrir` dão **500** (o handler desreferencia o `None` do `session.get`), enquanto a rota nova
`/tricount/reenviar` dá 303 porque checa `if ... and lista and ...`. A rota nova está mais correta
que as antigas. **→ DECISÃO: padronizar `HTTPException(404)` nas duas antigas** (2 linhas; aceita-se
a mistura com o commit da feature em troca de não deixar 500 virar ruído no log do Cloud Run).

### Como o bloqueio da conclusão ficou implementado

`_tem_rateio(totais)` = `any(valor > 0 ...)` em `app/routers/listas.py`; `pode_concluir` entra nos
dois contextos sem nenhuma query nova. A recusa é `303` para `/listas/{id}/rateio?erro=sem_rateio`
(mesmo padrão do `?concluida=1` já usado), e não 400, porque o form é POST normal sem HTMX — um 400
jogaria o usuário numa página de erro em vez de deixá-lo onde ele resolve o problema.

**O form "Concluir lista" mudou de `split.html` para dentro de `#totais-panel` (`_totais.html`).**
Era necessário: o botão ficava fora dos containers que o HTMX troca, então `disabled` ali ficaria
com **estado velho** — marcar o primeiro item não reabilitaria o botão sem recarregar a página.
`#totais-panel` é o alvo do toggle de item e o partial é incluído por `_rateio.html`, que é o que as
outras duas rotas de toggle devolvem, logo os três caminhos de swap ficam cobertos com zero
plumbing. A alternativa era `hx-swap-oob` a partir das 3 rotas: preservaria a posição visual exata,
mas um OOB esquecido numa rota futura reintroduziria o estado velho em silêncio — justamente o bug
que o ajuste elimina. **O que se perde:** a ordem visual muda (totais → botão → dica → info-box
"Tudo certo?"), e o texto do "Tudo certo?" passa a ler como legenda depois da ação, não antes.

### Observações sem ação

- `GET /listas/999/rateio` devolve 200 com página vazia (o Jinja silencia atributo de `None`), não
  500 — fora do escopo do ajuste 3, que pedia só os dois POSTs.
- `_tricount_status.html` não põe separador de milhar (`R$ 1234,56`), igual ao resto do app.
- `tricount_token` fica `NULL` com o fake, como esperado.

### Decisão operacional em aberto (para o usuário)

O `deploy.yml` foi com `TRICOUNT_ENABLED=true` (como manda a §6), mas o passo 1 da verificação de produção pede o **primeiro** deploy com `false` só para confirmar que as tabelas nascem no Turso. Opções:

| Opção | Ganha | Custa |
|---|---|---|
| **Manter `true` no YAML** e desligar à mão antes/depois da tag (`gcloud run services update --update-env-vars=TRICOUNT_ENABLED=false`) | o estado desejado fica versionado; deploys futuros não revertem o kill switch | uma intervenção manual no primeiro deploy |
| Pôr `false` no YAML e virar à mão depois | respeita a ordem da verificação por default | **todo** deploy futuro desligaria a integração — o kill switch viraria o default, o que é pior |
| Não verificar com `false` | zero fricção | perde a chance de ver as tabelas serem criadas sem a feature ligada; se o `create_all` fizesse algo inesperado, descobriríamos já com a integração ativa |

**Recomendação:** manter `true` (como está) e, no primeiro deploy, desligar por `gcloud run services update` antes de apontar a tag. O que se perde: um comando manual que ninguém vai lembrar na próxima vez — mitigado por já estar escrito na seção de verificação do README.
