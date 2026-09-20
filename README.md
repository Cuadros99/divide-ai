# DivideAI

App pessoal/familiar (3-5 pessoas) para dividir compras de mercado: tire uma foto da lista/nota fiscal, deixe um LLM extrair os itens e preços, marque quem participa do rateio de cada item e acompanhe o total que cada pessoa deve pagar em tempo real.

Uso majoritariamente pelo celular (mobile-first).

## Contexto

Objetivo: (1) tirar uma foto da lista de compras/nota fiscal, (2) extrair itens+preços via LLM com visão, (3) marcar manualmente quem participa do rateio de cada item, (4) ver o total por pessoa atualizando em tempo real como resumo final da compra.

> Integração com Splitwise foi descartada: a API de terceiros do Splitwise passou a exigir assinatura Splitwise Pro ativa, então o entregável do app é o próprio resumo de quanto cada pessoa deve pagar (sem envio automático para nenhum serviço externo).

Decisões de arquitetura:
- **Input**: foto da lista (câmera ou galeria), processada por um LLM com visão (não texto digitado).
- **Stack**: FastAPI + HTMX/Alpine — um único serviço, sem frontend separado em React/Streamlit.
- **Persistência**: banco de dados (pessoas cadastradas + histórico de listas permanecem entre sessões).
- **LLM**: começa com GPT-4o, mas desacoplado atrás de uma interface para trocar de provedor facilmente depois.
- **Deploy**: Google Cloud Run, deploy direto do `Dockerfile` já existente (sem rewrite). Diferente do Fly.io usado antes (`min_machines_running=1`, máquina sempre quente), o free tier do Cloud Run é scale-to-zero — cold start ocasional na primeira requisição após ociosidade é um trade-off aceito (não resolvido) pra continuar dentro do free tier perpétuo.
- **Autenticação**: login por formulário + sessão, com rate limiting contra força bruta.

## Mockups de referência

Os mockups visuais em [`screens-mocks/`](./screens-mocks) são a base fiel para a implementação das telas (layout, textos, ícones, cores, hierarquia visual):

| Tela | Arquivo |
|---|---|
| Login | `screens-mocks/login.png` |
| Home | `screens-mocks/home.png` |
| Nova lista | `screens-mocks/new_list.png` |
| Revisar lista | `screens-mocks/list-review.png` |
| Rateio da lista | `screens-mocks/split.png` |
| Pessoas | `screens-mocks/settings.png` |

### Diretriz visual

- Mobile-first, otimizado para iPhone.
- Interface limpa e minimalista, fundo claro.
- Verde como cor principal (CTAs, ícones, destaques), com tons de cinza/azul-marinho para texto.
- Cards com bordas bem arredondadas.
- Botões grandes, largura total, fáceis de tocar.
- Tipografia simples, hierarquia clara (título grande + subtítulo cinza).
- Poucos elementos por tela, navegação intuitiva (bottom nav com "Listas" e "Configurações").
- Sem funcionalidades desnecessárias no MVP (ex: sem cadastro público de novos logins).

## Arquitetura

Um único serviço FastAPI (sem frontend separado — evita CORS, 2 deploys, build step de JS):

- **Jinja2** para renderizar HTML server-side.
- **HTMX** para atualizações parciais (ex: marcar checkbox de participante → recalcula totais no servidor → devolve só o fragmento HTML dos totais via *out-of-band swap*, sem reload de página). Dá a sensação de "tempo real".
- **Alpine.js** (via CDN) só para micro-interações client-side (ex: loading spinner ao enviar foto).
- **Pico.css** (classless CSS framework) como base para a UI mobile-first, customizado com o tema verde dos mocks.

### Modelo de dados (SQLModel/SQLAlchemy)

- `Pessoa`: id, nome, email (opcional, só exibição).
- `Lista`: id, criado_em, status (`rascunho` / `concluida`).
- `Item`: id, lista_id, nome, preco (editável manualmente após extração do LLM, pois OCR/visão pode errar).
- `ItemParticipante`: tabela de associação (item_id, pessoa_id) — quem participa do rateio daquele item.

Cálculo do total por pessoa: soma, para cada item onde a pessoa está marcada, de `preco / nº de participantes do item`.

Em produção, `DATABASE_URL` aponta para um banco **Turso** (libSQL, compatível com o dialeto SQLite) via `sqlalchemy-libsql` — escolhido por exigir mudança mínima de código (mesma família de dialeto SQLite, não uma reescrita pra Postgres) e ter free tier perpétuo generoso. Localmente continua tudo igual, sem config: `sqlite:///./data/listadivide.db`. Dois detalhes de conexão não óbvios (bugs reais encontrados na configuração, documentados aqui pra economizar tempo de quem ler depois):
- A URL do Turso precisa de uma `/` antes da query string (`sqlite+libsql://<host>/?secure=true`) — sem essa barra, o parser de URL do SQLAlchemy engole a query string dentro do host silenciosamente.
- O token de autenticação tem que ir via `connect_args={"auth_token": ...}` no `create_engine()`, **não** embutido na URL como query param — o dialeto `sqlalchemy-libsql` não lê a chave `authToken` da URL (só reconhece `secure`, `uri`, `timeout` etc.), então o token precisa passar como connect arg cru, fora da allowlist de parsing da URL.

### Camada de LLM (desacoplada)

- `app/llm/base.py`: interface `ListaParser` com `parse_image(bytes) -> list[ItemExtraido]` (Pydantic).
- `app/llm/openai_parser.py`: implementação usando GPT-4o (structured outputs para garantir `[{nome, preco}]` bem formado).
- `app/llm/fake_parser.py`: implementação fake para desenvolvimento/testes, sem gastar créditos de API.
- `app/llm/factory.py`: escolhe a implementação via variável de ambiente `LLM_PROVIDER` — permite adicionar `gemini_parser.py` ou `anthropic_parser.py` depois sem tocar no resto do app.

### Camada de storage (desacoplada)

Mesmo padrão da camada de LLM, aplicado ao armazenamento das fotos de recibo (`app/storage.py`):

- `StorageBackend` (ABC): `save(key, content, content_type)`, `url_for(key)`, `read(key)`, `delete(key)`.
- `LocalStorageBackend`: arquivos em `app/static/`, usado no dev local (padrão, zero config).
- `GCSStorageBackend`: Google Cloud Storage via `google-cloud-storage`, usando Application Default Credentials (a service account de runtime do próprio Cloud Run em produção; um arquivo de chave de service account só se for testar contra um bucket real localmente).
- `get_storage()`: factory que escolhe o backend pela variável de ambiente `STORAGE_BACKEND` (`local` padrão, ou `gcs`).
- Em ambos os backends, `url_for()` devolve a mesma coisa: a rota `/fotos/{key}` da própria aplicação — então a foto é sempre servida de forma autenticada, independente do backend (ver seção seguinte).

### Proteção de acesso

App fica exposto na internet pública (hospedagem gratuita) e consome créditos pagos (OpenAI), então precisa de proteção contra força bruta, não só uma senha:

- **Login por formulário + cookie de sessão assinado** (em vez de HTTP Basic Auth puro): usuário/senha compartilhados entre os 3-5 usuários, validados uma vez em `POST /login`; depois uma sessão autentica as próximas requisições. Concentra a superfície de ataque em um único endpoint.
- **Rate limiting no endpoint de login** com `slowapi`: máx. 5 tentativas por IP a cada 5 minutos, com bloqueio incremental. Limitação conhecida: o contador é em memória, e o Cloud Run pode rodar mais de uma instância concorrente — então o rate limit não é uma garantia perfeitamente aplicada entre instâncias, só uma primeira barreira. Aceitável pra escala do app (3-5 usuários), mas não é uma propriedade resolvida como era com uma única máquina persistente no Fly.io.
- **Cloudflare (free plan) como camada real de domínio/HTTPS** na frente do Cloud Run — não é mais opcional nem fica na frente de um domínio do Fly.io. Motivo: o domain mapping gratuito do próprio Cloud Run não está disponível na região `southamerica-east1` (São Paulo); em vez de pagar por um HTTPS Load Balancer do GCP (~US$18/mês) só pra contornar isso, o plano free do Cloudflare cuida do domínio customizado + terminação TLS, com um registro CNAME apontando pro hostname `*.run.app` do serviço, modo SSL/TLS "Full" (não "Flexible" — isso criaria um loop de redirecionamento, já que o Cloud Run já força HTTPS).
- **Controle de acesso das fotos**: o plano original era um bucket GCS público de leitura, mas foi trocado por bucket **privado** + a rota autenticada `GET /fotos/{key:path}` (já coberta pelo `AuthMiddleware` global, como qualquer outra rota exceto `/login`). Motivo: fotos de recibo podem conter dados sensíveis (CPF), e as chaves de `Foto.path` são sequenciais/previsíveis (`uploads/{lista.id}_{i}.jpg`) — um bucket público deixaria qualquer um enumerar fotos de recibo de outras pessoas sem precisar logar. Isso também fechou, de quebra, uma brecha que já existia: fotos em dev local servidas pelo mount `/static` passavam batido pelo auth; agora os dois backends de storage servem fotos pela mesma rota autenticada.

## Estrutura de arquivos

```
app/
  main.py              # FastAPI app, monta routers
  config.py            # pydantic-settings: LLM_PROVIDER, OPENAI_API_KEY, DATABASE_URL, STORAGE_BACKEND, APP_USER/APP_PASSWORD...
  db.py                # engine/session SQLModel (SQLite local ou Turso via sqlalchemy-libsql)
  models.py            # Pessoa, Lista, Item, ItemParticipante, Foto
  auth.py              # login, sessão, rate limiting
  storage.py           # StorageBackend: Local (app/static) ou GCS, via /fotos/{key} autenticado
  llm/
    base.py
    openai_parser.py
    fake_parser.py
    factory.py
  routers/
    pessoas.py         # CRUD de pessoas cadastradas
    listas.py          # upload foto -> parse -> confirmar -> ver -> toggle participante -> concluir
  templates/
    base.html
    login.html
    home.html
    new_list.html
    list_review.html
    split.html
    pessoas.html
    _icons.html          # macro de ícones SVG, importado pelas outras templates
    _itens_lista.html    # partial (lista de itens na revisão)
    _itens_lista_oob.html # partial (HTMX oob swap ao adicionar/remover item)
    _rateio.html         # partial (tabela item x pessoa)
    _totais.html         # partial (HTMX oob swap dos totais por pessoa)
    _total_compra.html   # partial (HTMX oob swap do total da compra)
  static/
screens-mocks/          # mocks visuais (base para as telas)
requirements.txt
Dockerfile
.env.example
```

(`fly.toml` ainda existe no repo, mas é vestigial — não é mais usado pro deploy; ver [Deploy gratuito](#deploy-gratuito).)

## Fluxo principal

1. Usuário faz login.
2. Na Home, toca em "Nova lista".
3. Tira uma foto da lista/nota fiscal ou escolhe uma imagem da galeria.
4. A imagem é enviada ao backend, que usa o LLM com visão para identificar itens e preços.
5. Na tela "Revisar lista", o usuário confere e corrige os dados extraídos (nome/preço editáveis, adicionar/remover itens).
6. Ao confirmar, a lista é salva.
7. Na tela "Rateio da lista", cada item tem um checkbox por pessoa cadastrada.
8. Cada alteração recalcula em tempo real (via HTMX, sem reload) o valor devido por cada pessoa — esse resumo por pessoa é o entregável final da compra.
9. Usuário toca em "Concluir lista" para marcar a compra como fechada.

## Telas

**1. Login** (`login.png`) — logo + nome do app, usuário/e-mail e senha, botão "Entrar", aviso "uso pessoal e seguro". Sem cadastro público no MVP.

**2. Home** (`home.png`) — header com nome do app e link "Pessoas"; card de destaque "Nova lista"; lista "Suas listas" com histórico (nome, data, nº de itens, valor total, status "Em andamento" / "Concluída"); bottom nav fixo com abas "Listas" e "Configurações".

**3. Nova lista** (`new_list.png`) — botão "Tirar foto" (câmera) ou "Escolher da galeria"; área de pré-visualização da imagem; botão "Processar lista" que dispara a extração via LLM (loading durante o processamento).

**4. Revisar lista** (`list-review.png`) — thumbnail da foto enviada + "Ver foto"; itens detectados, nome e preço editáveis, ícone de lixeira para remover, "Adicionar item"; total da compra; botão "Confirmar lista".

**5. Rateio da lista** (`split.png`) — cabeçalho da lista (nome, data, total); tabela item × pessoa com checkboxes (toggle "Todos"); painel "Totais por pessoa" atualizado a cada toggle (o resumo final de quanto cada um paga); botão "Concluir lista"; opção "Editar itens da lista".

**6. Pessoas** (`settings.png`) — lista de pessoas cadastradas (nome, e-mail, badge "Você" para o dono da conta); "Adicionar pessoa"; editar/remover.

## Deploy gratuito

Stack atual: **Cloud Run + Turso + GCS + Cloudflare**, escolhida sobre alternativas tipo AWS/Azure por ter os free tiers perpétuos (não crédito de trial) mais generosos pra escala minúscula deste app, e porque o `Dockerfile` já existente tornou o Cloud Run um alvo de baixo esforço (sem rewrite — diferente de, por exemplo, AWS Lambda, que exigiria empacotar o app com Mangum).

- **Cloud Run** (compute): deploy direto do código via `gcloud run deploy --source .`, usando o `Dockerfile` do repo, região `us-central1` (não São Paulo — o domain mapping gratuito do Cloud Run não existe em `southamerica-east1`, e como o Cloudflare já cuida do domínio na borda — ver abaixo — a escolha de região não precisa satisfazer essa restrição como precisaria se dependesse do domain mapping do próprio Cloud Run; `us-central1` foi escolhida só por ser uma região bem suportada). Sem `--min-instances` definido, ou seja, scale-to-zero — trade-off deliberado pra ficar dentro do free tier perpétuo do Cloud Run (2M requisições / 180.000 vCPU-s / 360.000 GB-s por mês), ao custo de cold start ocasional na primeira requisição após período ocioso.
- **Turso** (banco de dados): ver seção [Modelo de dados](#modelo-de-dados-sqlmodelsqlalchemy) acima — SQLite-compatível via `sqlalchemy-libsql`, free tier perpétuo generoso, mudança mínima de código.
- **Google Cloud Storage** (fotos): bucket privado, acessado só pela rota autenticada da própria aplicação — ver seção [Proteção de acesso](#proteção-de-acesso) acima.
- **Secrets**: segredos da aplicação (`APP_PASSWORD`, `SECRET_KEY`, `OPENAI_API_KEY`, `TURSO_AUTH_TOKEN` etc.) ficam no **Google Secret Manager** e são conectados ao serviço do Cloud Run via `--set-secrets`, não como `--set-env-vars` puro — evita que fiquem visíveis na lista de env vars do serviço/no histórico do comando de deploy. Config não sensível (`DATABASE_URL` do host, `STORAGE_BACKEND`, `GCS_BUCKET_NAME`) vai como env var normal.
- **Cloudflare** (domínio + HTTPS): plano free, com CNAME apontando pro hostname `*.run.app` do serviço, proxy ativado (nuvem laranja), modo SSL/TLS "Full" — ver seção [Proteção de acesso](#proteção-de-acesso) acima pro motivo. É a configuração planejada/preparada: até o momento nenhum domínio próprio foi comprado (o Cloudflare não dá domínio de graça, só registro a preço de custo via Cloudflare Registrar), então o app hoje só é acessível pela URL padrão atribuída pelo Cloud Run.

## Rodando localmente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # preencher as variáveis
uvicorn app.main:app --reload
```

Durante o desenvolvimento, defina `LLM_PROVIDER=fake` no `.env` para testar o fluxo de UI sem gastar créditos de API.

## Verificação

- Rodar localmente e testar o fluxo completo (upload de foto de teste → conferir JSON extraído → marcar participantes → conferir totais por pessoa → concluir lista).
- Testar em um celular real (via `ngrok`/rede local) para validar layout mobile, tamanho de toque dos checkboxes e comportamento do HTMX sem reload.
