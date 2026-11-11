# DivideAI

App pessoal/familiar (3-5 pessoas) para dividir compras de mercado: tire uma foto da lista/nota fiscal, deixe um LLM extrair os itens e preços, marque quem participa do rateio de cada item e acompanhe o total que cada pessoa deve pagar em tempo real.

Uso majoritariamente pelo celular (mobile-first).

## Contexto

Objetivo: (1) tirar uma foto da lista de compras/nota fiscal, (2) extrair itens+preços via LLM com visão, (3) marcar manualmente quem participa do rateio de cada item, (4) ver o total por pessoa atualizando em tempo real como resumo final da compra.

> Integração com Splitwise foi descartada: a API de terceiros do Splitwise passou a exigir assinatura Splitwise Pro ativa, então o entregável do app passou a ser o próprio resumo de quanto cada pessoa deve pagar. O envio automático voltou depois por outro caminho — o **Tricount**, que não tem API oficial nem paywall (ver [Integração com o Tricount](#integração-com-o-tricount-api-não-oficial)).

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

### Integração com o Tricount (API não-oficial)

Ao concluir a lista, a despesa é lançada automaticamente num tricount (grupo) específico, com os **valores exatos do rateio** que a tela já mostra — ninguém redigita nada no app do Tricount.

- **Não existe API oficial.** O Tricount (bunq) não publica API pública nem portal de desenvolvedor; o que existe é a API privada do app Android, mapeada por engenharia reversa. Usamos a lib não-oficial `tricount-api==0.1.2` (pin exato — engenharia reversa não tem contrato de compatibilidade, então subir versão é sempre aposta). Alternativa descartada: escrever nosso próprio cliente dos 2-3 endpoints — ganharia zero dependência, custaria manter a engenharia reversa e a assinatura RSA.
- **Pagador**: a `Pessoa` com `is_dono=True`, não uma env var. Hoje existe um login só; quando houver várias contas, `is_dono` passa a ser escopado por conta sem mexer nesta integração.
- **O vínculo pessoa↔membro do Tricount é explícito**, por dropdown na tela de Pessoas (membros buscados ao vivo do grupo, uma chamada à API por page load). Casar por nome lançaria a despesa **na pessoa errada** quando os nomes divergem ("Ana" vs "Ana Paula"), e um vínculo errado só aparece dias depois, no acerto de contas — então adivinhar é proibido.
- **Falha é fail-open**: a lista conclui normalmente, o erro é persistido e a tela de rateio mostra o aviso + botão "Reenviar ao Tricount". Fail-closed acoplaria o fluxo principal de um app pessoal a uma API de engenharia reversa (Tricount instável = ninguém consegue fechar a compra); `BackgroundTasks` seria pior, porque o Cloud Run sem CPU-always-on estrangula a CPU depois da resposta. O que se perde no fail-open: dá pra esquecer de clicar em "Reenviar" (uma fila de verdade, tipo Cloud Tasks, seria o correto em escala; para 3-5 usuários a linha no banco + botão é a fila do pobre).
- **Unidade da API: resolvida e confirmada em campo.** O payload é `{"value": str(-amount), ...}` e a docstring de `Amount` na `tricount-api` 0.1.2 diz `"1500" means 1500 JPY or 15.00 EUR, NOT cents`, logo `amount` está em **unidades de moeda com decimais** e `_CENTAVOS_POR_UNIDADE_API = 100` (fato de protocolo, não configuração). A doc da lib se contradiz (o exemplo usa `amount=5000` para um jantar), então o `scripts/tricount_probe.py unidade` foi rodado num tricount descartável em 2026-11-10: escreveu `amount=1234` e o app Android mostrou **R$ 1.234,00**, com o servidor devolvendo `-1234.00 BRL` — **unidades de moeda, não centavos**. Como o payload usa `str(-amount)` cru, passa-se `Decimal(centavos)/100` e nunca `float` (que geraria `"-0.30000000000000004"`). Falta só a confirmação com valor *decimal* (`12.34` / `3.33`), que é o que o app manda de verdade — por isso `--amount`/`--split` do probe são `Decimal`.
- **Escrever exige `join_tricount`; ler, não** (achado de campo, 2026-11-10). O sharing token acha o grupo e a **leitura** funciona com `get_tricount(token)`, mas a escrita volta `HTTP 404 "Registry with id X not found"` enquanto o grupo não estiver sincronizado na conta do device. Então o `real.py` faz o join **lazy e reativo**: tenta escrever, e **só** em 404 chama `join_tricount(token)` e refaz a escrita **uma única vez** (uma flag de instância evita repetir o POST nos envios seguintes do mesmo processo). O caminho de leitura — `listar_membros`, que o dropdown de Pessoas chama a cada page load — nunca faz esse POST. **Por que reativo e não um join proativo no setup:** com credencial estável o sync fica guardado no servidor, então depois do primeiro join nenhum outro é necessário (nem após cold start ou deploy) e o caminho comum fica em 1 chamada; o custo é que a **primeira** escrita de um device novo paga write + join + write. Um join no boot pagaria um POST a mais em toda instância nova e amarraria o cold start ao Tricount.
- **O que o join faz, e por que é aceitável:** na lib ele é só um `POST /registry-synchronization` e não chama `link_to_member`, mas o **servidor** vincula o device ao membro de **menor id** nesse sync (medido em campo: `membership_uuid_active` ia de `None` para o uuid daquele membro). O vínculo **não é exclusivo** — o celular do dono continua identificado como o mesmo membro, com a despesa aparecendo como "paid by me" para ele —, então ninguém perde identidade. Risco residual: no grupo real, o device passa a "ser" o membro de menor id do ponto de vista da API; não afeta a escrita (o pagador vai explícito no payload) nem os outros membros, mas é estado remoto que o app cria. `link_to_member()` (o que de fato troca a identidade de um membro) e `leave_tricount()` **nunca** são chamados em runtime.
- **Retentativas, uma de cada causa:** a lib não trata 401, então o `real.py` descarta o client, re-autentica e refaz a operação uma vez; o 404 tem seu próprio orçamento, com o join no meio. As duas são *one-shot* e independentes — no pior caso 3 tentativas, sem compor retry sobre retry e sem risco de loop. Repetir é seguro porque 401 e 404 são rejeições **antes** de qualquer efeito, logo não duplicam despesa.

Dois flags, de propósitos deliberadamente distintos:

- `TRICOUNT_ENABLED` (default `false`) — **kill switch**: uma API não-oficial pode quebrar sem aviso, e com o flag em `false` a integração e toda a UI dela desaparecem **sem deploy de código** (basta trocar a env var do serviço).
- `TRICOUNT_PROVIDER` (`fake` | `real`, default `fake`) — escolhe a implementação, no mesmo padrão das camadas de LLM e de storage. Dev local roda `TRICOUNT_ENABLED=true` + `TRICOUNT_PROVIDER=fake` e exercita o fluxo **inteiro**, inclusive as linhas gravadas no banco, sem tocar em nenhum grupo real.

`TRICOUNT_TOKEN` (o `tXXXXX` do link `tricount.com/tXXXXX`, que identifica o grupo) vai para o **Secret Manager**, nunca como `--set-env-vars`: o sharing token dá **leitura e escrita** no grupo e **não é revogável** — se vazar, a única saída é recriar o tricount. Esse é segredo, não config.

`TRICOUNT_CREDENTIALS_JSON` **não é segredo pelo conteúdo** (conferido no fonte da `tricount-api` 0.1.2): `Credentials.generate()` gera o par RSA, **descarta a chave privada** e o que `creds.save()` grava é só `app_id` (um uuid4) e `public_key_pem` — a chave **pública**. Nada ali autoriza nada; quem tiver o arquivo não lê nem escreve no grupo (para isso precisaria do token acima). O motivo de persistir é **operacional**, e vale enumerar porque é exatamente o que se perde ao deixar o app gerar credencial nova a cada boot:

1. **O join sairia a cada cold start.** O `user_id` da API é derivado da identidade do device (`POST /v1/session-registry-installation` manda `app_installation_uuid = app_id` e a resposta devolve o `user_id`; todo o resto da API é `/v1/user/{user_id}/...`), e o `registry-synchronization` — o "join", obrigatório para **escrever** — fica guardado no servidor amarrado a esse `user_id`. Com credencial estável, o join acontece **uma vez na vida** e o caminho comum fica em uma chamada; sem ela, cada cold start do Cloud Run é um device novo e não sincronizado, e a primeira escrita de **cada instância** paga write → 404 → join → write.
2. **Pior, e irreversível: lixo acumulado no grupo real.** Cada device novo é auto-vinculado pelo servidor ao membro de **menor id** do grupo (ver acima). Credencial descartável significaria um número **ilimitado** de contas apontando para uma pessoa real da família — estado remoto que o app não tem como limpar depois.
3. Evita registrar uma **instalação nova a cada cold start** numa API de engenharia reversa, sem limites publicados e sem ninguém a quem recorrer no dia em que um deles for atingido.
4. Evita pagar `rsa.generate_private_key(2048)` **dentro da requisição do usuário**, em cima da latência de cold start e da própria chamada ao Tricount.
5. Devolve ao WARN **"device Tricount novo registrado"** o papel de canário: com credencial estável, ele só aparece quando a configuração está errada; com credencial efêmera apareceria sempre e não significaria nada.

Ele vai para o Secret Manager **de todo jeito** (decisão consciente), mas pelo motivo certo: **disciplina e formato**, não sigilo — um lugar só de onde sai o que é injetado por `--set-secrets`, e o `public_key_pem` tem `\n` literais, frágeis dentro da string de `--set-env-vars`. O custo: um secret a mais que precisa existir antes do deploy, e a aparência de que o arquivo é sigiloso quando o único sigilo real é o do `TRICOUNT_TOKEN`. A alternativa (passar o JSON como env var normal) economizaria um secret e jogaria o escape do PEM para o YAML do workflow — não compensa.

Bootstrap das credenciais do device (uma vez, na máquina local): `Credentials.generate()` → `creds.save(path)` → `gcloud secrets create tricount-credentials-json --data-file=<path>`. Em produção o JSON chega pela env var e é materializado em `/tmp/tricount_credentials.json` (tmpfs no Cloud Run: ~1 KB de memória, e some com a instância — efêmero é o comportamento desejado). Se os logs mostrarem o WARN **"device Tricount novo registrado"**, o secret **não** foi lido e o app registrou um device novo — é o alarme de configuração errada, não um aviso cosmético.

**Riscos e limitações conhecidos:**

- O uso provavelmente está **fora dos Termos do Tricount** — risco aceito consciente, escopo de grupo familiar próprio.
- A API não é documentada e pode **quebrar sem aviso** (mitigado pelo kill switch, pelo fail-open e pelo pin de versão).
- A credencial de device é de **vida longa e sem rotação** automática — impacto baixo, porque ela não carrega autorização nenhuma (ver acima); o que de fato não tem rotação é o sharing token.
- O arredondamento assume **BRL com 2 decimais** (a moeda é definida na criação do tricount e é imutável).
- **Fora de escopo** (decisão explícita): atualizar/apagar a despesa quando a divisão muda depois do envio (`transaction_id` e `rateio_hash` ficam guardados justamente pra habilitar isso numa fase 2), retry automático, múltiplos pagadores e moedas além de BRL.

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
  tricount/
    base.py            # ABC TricountBackend + dataclasses do domínio (centavos, uuid de membro)
    rateio.py          # arredondamento largest-remainder reais -> centavos (puro, sem dependências)
    fake.py            # backend fake (membros fixos, nenhuma chamada de rede)
    real.py            # backend real sobre tricount-api (lib não-oficial)
    factory.py         # escolhe fake/real via TRICOUNT_PROVIDER
    service.py         # orquestra o envio ao concluir a lista (nunca levanta: fail-open)
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
scripts/
  tricount_probe.py     # probe manual contra um tricount descartável (valores decimais, join, membros, timeout); nunca chamado pelo app
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
- **Secrets**: segredos da aplicação (`APP_PASSWORD`, `SECRET_KEY`, `OPENAI_API_KEY`, `TURSO_AUTH_TOKEN`, `TRICOUNT_TOKEN` etc.) ficam no **Google Secret Manager** e são conectados ao serviço do Cloud Run via `--set-secrets`, não como `--set-env-vars` puro — evita que fiquem visíveis na lista de env vars do serviço/no histórico do comando de deploy. Config não sensível (`DATABASE_URL` do host, `STORAGE_BACKEND`, `GCS_BUCKET_NAME`, `TRICOUNT_ENABLED`, `TRICOUNT_PROVIDER`) vai como env var normal — os dois flags do Tricount ficam de fora do Secret Manager de propósito, porque o kill switch tem que ser virável com um `gcloud run services update`. `TRICOUNT_CREDENTIALS_JSON` é o caso do meio: não é segredo por conteúdo (só `app_id` + chave pública), mas vai pelo `--set-secrets` por disciplina e por causa dos `\n` do PEM — ver [Integração com o Tricount](#integração-com-o-tricount-api-não-oficial).
- **Cloudflare** (domínio + HTTPS): plano free, com CNAME apontando pro hostname `*.run.app` do serviço, proxy ativado (nuvem laranja), modo SSL/TLS "Full" — ver seção [Proteção de acesso](#proteção-de-acesso) acima pro motivo. É a configuração planejada/preparada: até o momento nenhum domínio próprio foi comprado (o Cloudflare não dá domínio de graça, só registro a preço de custo via Cloudflare Registrar), então o app hoje só é acessível pela URL padrão atribuída pelo Cloud Run.

## Rodando localmente

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # preencher as variáveis
uvicorn app.main:app --reload
```

Durante o desenvolvimento, defina `LLM_PROVIDER=fake` no `.env` para testar o fluxo de UI sem gastar créditos de API. Para exercitar o envio ao Tricount sem tocar num grupo real, use `TRICOUNT_ENABLED=true` + `TRICOUNT_PROVIDER=fake` (o fluxo inteiro roda, inclusive as linhas no banco).

## Verificação

- Rodar localmente e testar o fluxo completo (upload de foto de teste → conferir JSON extraído → marcar participantes → conferir totais por pessoa → concluir lista).
- Testar em um celular real (via `ngrok`/rede local) para validar layout mobile, tamanho de toque dos checkboxes e comportamento do HTMX sem reload.
- Com `TRICOUNT_PROVIDER=fake`, cobrir também: vínculo de pessoas, conclusão gerando `status="ok"`, reabrir→concluir **sem** reenviar, participante sem vínculo (aviso + "Reenviar ao Tricount") e `TRICOUNT_FAKE_FALHA=1`. Nunca contra `data/listadivide.db` — copie o banco antes e aponte `DATABASE_URL` para a cópia.
- **Unidade da API do Tricount**: deduzida do fonte da lib e **confirmada em campo** em 2026-11-10 (`amount=1234` → `R$ 1.234,00` ⇒ `_CENTAVOS_POR_UNIDADE_API = 100`). O que falta é a escrita **decimal**, que é o que o app manda: num tricount descartável, `scripts/tricount_probe.py unidade --token tXXXXX --join --amount 12.34 --split 6.17,6.17`, depois `centavo-impar --token tXXXXX --join` (`10.00` em `3.33/3.33/3.34`) e `unidade --token tXXXXX --join --amount 0.03 --split 0.01,0.01,0.01`; por fim `limpar --token tXXXXX --join --confirmar`. Nunca contra o grupo real: o probe aborta se o `--token` for o `TRICOUNT_TOKEN` de produção, e `--join` é proibido com `--grupo-real`.

### Verificação em produção (ordem importa)

1. Deploy com `TRICOUNT_ENABLED=false`. Conferir no Turso que as tabelas `pessoatricount` e `listatricount` foram criadas (`turso db shell <db> ".schema listatricount"`) e que nada mais mudou — `init_db()` só faz `create_all`, nunca `ALTER TABLE`. App saudável e UI sem traço da feature.
2. Virar `TRICOUNT_ENABLED=true` + `TRICOUNT_PROVIDER=real`, vincular as pessoas aos membros e conferir nos logs a **ausência** do WARN "device Tricount novo registrado" (é a prova de que o secret das credenciais foi lido).
3. Primeira escrita real controlada: lista de teste com um item de **R$ 0,03 entre 3 pessoas** → conferir valor e split no app do Tricount (é o caso que expõe erro de unidade e de arredondamento ao mesmo tempo) → apagar a despesa pelo app → apagar a lista. Nos logs, essa primeira escrita deve trazer **uma** linha `grupo Tricount sincronizado neste device (join_tricount) após 404` — é o join lazy acontecendo uma vez; se ela reaparecer em **todo** envio, o sync não está sendo aceito e vale investigar antes de confiar na integração.
4. Nos logs do Cloud Run: latência da conclusão em cold start (o `authenticate` custa um round trip por instância, e a primeira escrita do device paga write + join + write) e nenhuma conclusão estourando o timeout.
