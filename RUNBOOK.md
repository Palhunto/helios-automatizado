# RUNBOOK.md

## Operação final desejada

1. Criar projeto.
2. Informar disciplina, estrutura e materiais.
3. Executar/continuar questionário e planejamento acadêmico.
4. Produzir as 18 partes textuais.
5. Reconciliar referências e consolidar.
6. Planejar recursos visuais.
7. Produzir/recuperar imagens.
8. Formatar Google Docs.
9. Rodar QA final.
10. Abrir documento final.

## CLI incremental

M0:
```bash
ebook project create <config>
ebook project list
ebook project show <project_id>
ebook project validate <project_id>
ebook project recover <project_id>
ebook project runtime show <project_id>
ebook project runtime set <project_id> --browser-automation-enabled
```

M1:
```bash
ebook academic questionnaire import <project_id> <questionario.txt>
ebook academic questionnaire show <project_id> --raw
ebook academic questionnaire confirm <project_id> --raw-version 1
ebook academic answers import <project_id> <respostas.txt>
ebook academic answers show <project_id>
ebook academic plan authorize <project_id>
ebook academic plan import <project_id> <plano.md>
ebook academic plan show <project_id>
ebook academic status <project_id>
ebook academic validate <project_id>
```

M2:
```bash
ebook writing context create <project_id> --contract-id omega_writing_production --contract-version 1
ebook writing context show <project_id>
ebook writing context acknowledge <project_id> <acknowledgement.txt>
ebook writing context confirm <project_id> --raw-version 1
ebook writing unit prepare <project_id> INTRO
ebook writing unit import <project_id> INTRO <intro.txt>
ebook writing unit show <project_id> INTRO --raw
ebook writing unit confirm <project_id> INTRO --raw-version 1
ebook writing status <project_id>
ebook writing validate <project_id>
ebook writing consolidate <project_id>
ebook writing consolidated show <project_id>
```

M3:
```powershell
ebook project runtime set <project_id> --browser-automation-enabled
ebook --browser-channel chrome `
  --browser-profile-dir "$env:LOCALAPPDATA\HeliosEbookAutomation\chrome-profile" `
  --no-browser-headless browser setup --wait-seconds 300
ebook browser capture-spike --sample-kind acknowledgement
ebook browser capture-spike --sample-kind long_unit
ebook --browser-channel chrome `
  --browser-profile-dir <profile_dedicado> `
  browser composer-spike <project_id> <request_artifact_id>
ebook browser context-run <project_id>
ebook writing context confirm <project_id> --raw-version 1
ebook browser unit-run <project_id> INTRO
ebook browser continue <project_id>
ebook browser status <project_id>
ebook browser validate <project_id>
ebook browser recover <project_id>
ebook browser reconcile <project_id> <interaction_id>
ebook browser interaction show <project_id> [interaction_id]
ebook browser interaction abandon <project_id> <interaction_id> --operator <nome> --reason <motivo>
```

Habilite o browser pelo comando versionado acima; não edite o `project.yaml` congelado. O comando
preserva a configuração histórica e cria `config/runtime/vNNNN.json` com Artifact, StageRun e
cadeia SHA. `project runtime show` audita a versão efetiva e
`project runtime set <id> --no-browser-automation-enabled` cria uma nova versão para desabilitar.
O channel padrão é o Google Chrome instalado (`chrome`); `chromium` continua disponível e requer
`python -m playwright install chromium`. O profile persistente é dedicado e deve ficar fora do
repositório. Nunca use o profile pessoal/default e nunca deixe Chrome manual e Playwright abrirem
simultaneamente o mesmo profile. O adapter mantém lock exclusivo e também falha fechado quando o
próprio Chrome reporta o diretório em uso. `browser setup` apenas abre a sessão legítima para login
manual ou para probe de uma sessão já autenticada; desafios, CAPTCHA e 2FA nunca são contornados.
`capture-spike` deve ser executado uma vez
sobre um acknowledgement curto e uma vez sobre uma unidade longa. Métodos de captura não são
misturados silenciosamente. O comando não captura conversa vazia: cria uma conversa de teste,
envia um prompt sintético com marcador único, prova o user turn, aguarda o assistant turn terminar
e somente então captura rendered e Copy. Para diagnóstico headed, use `--log-level DEBUG` antes de
`browser` e `--error-hold-seconds N` após `capture-spike`; em erro, o Chrome permanece aberto por N
segundos. Nenhum WritingContext é usado nesse fluxo.

Antes do `context-run`, execute `composer-spike` com o `request_artifact_id` exato. Esse comando
sempre força Chrome headed, exige channel `chrome`, abre o banco em `mode=ro/query_only` e verifica
tipo, vínculo ao WritingContext, SHA e bytes do context package. Ele limpa e comprova vazio no
início e após cada payload. O método
não chama `send_message`, não resolve/clica Send, não usa Enter e não cria interação ou StageRun.
Cada caso informa somente `expected_length`, `observed_length`, fingerprints esperado/observado,
`stable_read_1`, `stable_read_2` e metadados do editor. Feche antes qualquer Chrome manual que use
o mesmo profile dedicado.

No probe real de 2026-08-22, Copy acrescentou somente `CRLF` terminal nas amostras curta e longa;
não houve token substantivo exclusivo em nenhum método. A divergência foi classificada como
`whitespace`, e `rendered_text_v1` permaneceu canônico porque preservou integralmente o conteúdo
textual necessário ao M2. O método selecionado nunca é trocado automaticamente pela comparação.

Cada WritingContext usa uma conversa própria. O primeiro response importado continua exigindo
`writing context confirm`; não existe confirmação automática. `browser continue` avança no máximo
uma operação e pausa em confirmation, `review_required` ou `rejected`. Retry de rejeição usa
`browser retry <project> <interaction_id>`, preserva a raw anterior e aplica apenas repair
determinístico por findings permitidos.

O context de automação atual usa `omega_writing_production@4` com
`helios_writing_unit_request@2`. A V4 resolve eager todas as projeções antes de persistir o context:
`INTRO` e `CONCLUSION` vêm de seções globais já existentes; `CH01_A/B` até `CH08_A/B` compartilham
o recorte de nível 1 do respectivo capítulo. V3 permanece congelada e não deve ser alterada.

Para o contract V2 de redação acadêmica, crie o contexto explicitamente com
`--contract-version 2`. O request operacional continua instruindo 9.000–10.000 caracteres; a
aceitação objetiva usa 7.500–11.500, sem warning apenas por estar fora da faixa-alvo.

Use `-` no lugar do arquivo para ler bytes do stdin. O `confirm` só é necessário para um
questionário `review_required`; ele registra a decisão e preserva tanto a redação observada quanto
a referência canônica. A autorização do plano é recusada enquanto as respostas atuais contiverem
`[CONFLITO]`; `[PENDENTE]` permanece evidência editorial, mas não bloqueia tecnicamente.

No M2, envie o output de `writing context show` uma vez ao OmegaBrain. Importe o acknowledgement e
confirme explicitamente antes de preparar unidades. `unit prepare` gera um request para a mesma
conversa e inclui continuidade exatamente conforme `continuity_from`. Use `--reprocess` somente
para criar uma nova preparação preservando a anterior. Uma resposta `review_required` precisa de
`unit confirm`; uma `rejected` não é confirmável. O status distingue ausência de accepted de
accepted histórica incompatível.

Antes de `unit confirm`, execute `ebook writing unit show <project_id> <unit_id> --version <N>
--raw`. A saída contém o raw e o validation report imutável da mesma versão, incluindo findings e
contagens derivadas. Se o artifact do report estiver ausente ou divergente, o comando falha fechado;
ele não confirma nem altera a disposição.

Consolidação normal exige Production Set completo, dependency-compatible e WritingContext current.
Ela concatena os bytes accepted na ordem/separator do contract. O Citation Ledger é somente
evidência de menções autor-data: `Citation Ledger != Reference Reconciliation`.

Comandos futuros só quando o milestone exigir:
```bash
ebook run <project_id>
ebook resume <project_id>
ebook retry <project_id> <unit_id>
ebook inspect <project_id>
```

## Semântica

- `run`: executa próxima unidade permitida.
- `resume`: reconcilia estado e continua do primeiro item realmente pendente.
- `retry`: reprocessa unidade específica com registro de versão/tentativa.
- `inspect`: somente leitura.

## Recovery

Após interrupção:
1. reiniciar app;
2. executar `ebook project validate <project_id>`;
3. executar `ebook project recover <project_id>` quando existir `running` órfão;
4. verificar o status retornado;
5. repetir a criação quando o resultado for `pending_retry`;
6. não prosseguir automaticamente quando o resultado for `blocked`.

Nunca recompor estado somente olhando "qual foi a última linha do log".

No M3, use `ebook browser recover`: o `StageRun` não prova envio externo. Se o primeiro send cair
antes da prova atômica, recovery compara o fingerprint observado do primeiro user turn e exclui
toda conversa presente no baseline pré-send. Exatamente um candidato com path real `/c/<uuid>`
derivado de `page.url` pode ser vinculado; zero ou vários resultam em `blocked`, sem criar ou
reenviar conversa. `project recover` deliberadamente não converte runs do browser em retry.

Quando recovery terminar em `blocked` porque o efeito externo continua não comprovável, consulte o
`interaction_id` em `browser status` e o histórico completo em `browser interaction show`. Se o
operador decidir descartar essa tentativa ambígua, use `browser interaction abandon` com identidade
e motivo explícitos. O comando preserva todos os registros e não declara que o request não foi
enviado; uma execução posterior cria uma interação/StageRun sucessores. Não use `browser retry`
para esse caso, e `browser continue` não atravessa um bloqueio sem a resolução registrada.

Depois de `operator_abandoned`, confirme em `browser status` que `active_interactions` está vazio.
O próximo `browser context-run` inicia uma linhagem limpa mesmo se a anterior esgotou
`max_attempts`: cria nova conversation, novo StageRun e interaction em attempt 1, ligada à última
abandonada por `supersedes_interaction_id`. A conversation, StageRun, events, resolution e Artifacts
anteriores permanecem imutáveis. Não use esse caminho enquanto existir blocked sem resolução ou
interaction em envio/observação.

Antes de abandonar, execute novamente `ebook browser recover <project_id>`. Interações bloqueadas
por `BROWSER_SEND_NOT_PROVABLE` são automaticamente elegíveis a reprobe somente quando possuem
conversa `ready`, path, fingerprint coincidente e nenhuma resolução do operador. O comando abre a
conversa existente e polla o DOM; se encontrar o user turn, aguarda/captura/importa a resposta sem
reenviar. Se não encontrar, mantém `blocked`. Leia `examined`, `recovered` e `skipped[].reason` na
saída para distinguir ausência de candidato, resolução humana e abandono terminal.

Se a conversa real e a resposta já existem, mas a interação bloqueada não recebeu um path
comprovado, execute `ebook browser reconcile <project_id> <interaction_id>`. Esse comando é
read-only no browser: não preenche composer, não aciona Send/Enter, não cria tentativa, não abandona
e não reenvia. Ele exige `blocked`, ausência de abandono e último erro
`BROWSER_SEND_NOT_PROVABLE`; tenta primeiro a conversa restaurada pelo profile e depois conversas
recentes posteriores a `send_attempt_started`. Nunca aceita `/c/WEB:*` nem seleciona por título ou
data: o primeiro turno precisa coincidir integralmente com o `transport_fingerprint`, inclusive por
leitura observacional do pasted-text attachment quando aplicável. Só após uma resposta completa e
estável o comando vincula `/c/<uuid>`, captura `rendered_text_v1`, registra Artifact/SHA, importa o
acknowledgement pelo `context_id` original e conclui a mesma interaction e o mesmo StageRun. Qualquer
candidato zero/ambíguo, divergência de fingerprint, attachment inacessível ou resposta ausente
mantém o registro `blocked` sem mutação.

Um path legado como `/c/WEB:...` não é conversa recuperável. Recovery deve reportar
`invalid_conversation_path`, sem abrir o path e sem alterar a interaction ou seus events. Não use
esse registro como prova de envio e não edite o SQLite para corrigi-lo.

Quando todas as interactions ligadas a essa conversa já possuírem resolução explícita
`operator_abandoned`, o próximo `context-run` não reutiliza a linha `ready` inválida. Em uma única
transação ele cria uma nova BrowserConversation `provisioning` e registra
`legacy_invalid_conversation_path` no ledger de invalidações. A conversa antiga continua visível em
`browser status` com `reusable=false`; seus campos e events não mudam. Se alguma interaction ainda
não estiver resolvida, o comando falha com `BROWSER_INVALID_CONVERSATION_REQUIRES_RESOLUTION`.

Recovery nunca deve produzir uma mensagem nova. Se `examined` contiver interaction `prepared`, ela
será listada em `skipped` com `not_started_requires_explicit_run`; use `context-run`/`unit-run`
somente como decisão separada. Para diagnosticar envio normal em DEBUG, procure os checkpoints
`composer_found`, `composer_filled`, `send_button_found`, `send_button_enabled`,
`send_trigger_started`, `send_trigger_completed`, `composer_cleared` e `user_turn_observed`.

Para big paste, `composer_cleared` não prova envio. O checkpoint `user_turn_observed` só pode surgir
depois que o attachment semântico do próprio user turn for aberto e seu conteúdo integral produzir
o fingerprint esperado, seguido de URL `/c/<uuid>` válida. Em DEBUG,
`browser_user_turn_proof` registra somente contagens, comprimentos, flags, fingerprints e path;
nunca registra request, preview ou conteúdo do attachment.

Entre `composer_found` e `composer_filled`, procure `browser_composer_audit` nas fases `located` e
`focused`. O registro contém apenas metadados DOM (`tag_name`, `contenteditable`, role,
id/data-testid, `is_content_editable`, foco e contagem de descendentes editáveis). O adapter relê
`value` para `textarea`/`input` e texto para `contenteditable`; exige duas leituras estabilizadas com
fingerprint integral idêntico ao request. O conteúdo editorial nunca é incluído nesse log. Ausência
de foco ou divergência mantém a operação antes de Send.

Uma falha `BROWSER_COMPOSER_FILL_FAILED` antes de `send_trigger_started` é pré-envio comprovado: a
interaction deve continuar `prepared`, o StageRun `pending` e o event deve conter
`effect_boundary=pre_send`. Repetir o comando reutiliza a mesma interaction. Depois do boundary
`send_attempt_started`, qualquer falha continua fail-closed em `sending` até prova ou resolução.

Sem ID, `browser interaction show` mostra somente quando existe uma única interaction ativa ou uma
única bloqueada ainda não resolvida. Mais de uma candidata gera
`BROWSER_INTERACTION_SELECTION_AMBIGUOUS` com a lista de IDs; interactions abandonadas nunca vencem
uma ativa. Use o ID explícito para consultar história. `browser status` expõe todos os IDs em
`active_interactions` além das contagens.

O recovery do projeto também reconcilia runs acadêmicos interrompidos. Ele recalcula o artefato
aceito a partir do bruto e do snapshot de prompt congelado; ausência comprovada resulta em
`pending_retry` e divergência de hash/contrato resulta em `blocked`.

## Configuração local do M0

Precedência: flags da CLI → variáveis `HELIOS_*` → defaults.

```text
HELIOS_DATA_DIR=./data
HELIOS_PROJECTS_DIR=./projects
HELIOS_PIPELINE_CONFIG=./config/pipeline.yaml
HELIOS_PROMPT_REGISTRY=./prompts/registry.yaml
HELIOS_WRITING_CONTRACT_REGISTRY=./writing_contracts/registry.yaml
HELIOS_LOG_LEVEL=INFO
HELIOS_MAX_ATTEMPTS=3
HELIOS_BROWSER_CHANNEL=chrome
HELIOS_BROWSER_PROFILE_DIR=<profile-chrome-dedicado-fora-do-repositorio>
HELIOS_BROWSER_HEADLESS=false
HELIOS_BROWSER_TIMEOUT_SECONDS=180
HELIOS_BROWSER_CAPTURE_METHOD_VERSION=rendered_text_v1
```

O aplicativo não carrega `.env` automaticamente.

## Planejamento visual — M4

Parta de uma consolidação textual completa e atual. O plano editorial é produzido pelo OmegaBrain
com o prompt V2 e importado como UTF-8; as âncoras são um enriquecimento separado.

```powershell
ebook pagination create <project_id>
ebook pagination validate <project_id>
ebook visual plan import <project_id> plano-visual.txt
ebook visual figure list <project_id>
ebook visual anchor request <project_id> <figure_id>
ebook visual anchor import <project_id> <figure_id> ancora.json
ebook visual anchor show <project_id> <figure_id>
ebook visual plan finalize <project_id>
ebook visual plan manifest <project_id>
ebook visual plan validate <project_id>
ebook visual plan status <project_id>
```

`anchor request` exporta instrução, proposta e os trechos exatos da página para escolha semântica
da âncora. Não faz envio externo. O contrato `helios_visual_anchor_enrichment@1` recebe somente:

```json
{"anchor_text": "Trecho literal copiado da página.", "position_relative_to_anchor": "after"}
```

Use `before` ou `after`. Os campos opcionais `anchor_before` e `anchor_after` precisam ser contexto
literal imediatamente adjacente dentro da página; não desambiguam trechos repetidos. IDs, unidade,
offsets e hashes são calculados pelo programa. Não inclua cercas Markdown no arquivo JSON.

Importe uma âncora por figura. Uma tentativa inválida conserva bruto e relatório; corrija somente
o JSON daquela figura e importe novamente. Bytes diferentes geram versão nova e preservam a
proposta editorial. Reimportar bytes idênticos reutiliza a tentativa anterior e não a promove
automaticamente sobre uma tentativa posterior. Consulte versões com `anchor show --version N`.

`finalize` exige o plano atual, cobertura completa e a versão mais recente válida de cada âncora.
Falha de uma figura impede a finalização. Novos reparos deixam o manifest anterior preservado;
um novo `finalize` explícito congela outra versão, sem alterar a anterior. Use `plan manifest
--version N` para consultar histórico. `plan status` distingue validade estrutural e finalização.

Depois de interrupção, `anchor recover <project_id> <figure_id>` relê o bruto persistido e retoma
a mesma unidade. Para uma finalização interrompida, repita `plan finalize` com os mesmos inputs.
Os retries são limitados pela configuração. Fontes alteradas, hashes divergentes ou arquivos
conflitantes impedem promoção; preserve as evidências e consulte o erro antes de reparar.

`plan validate` verifica integridade de imports, anchors e manifests, incluindo o histórico.
Uma tentativa editorial rejeitada, mas íntegra, não é corrupção. `pagination validate` inclui
também a inspeção textual completa do PDF; as operações de âncora verificam seus hashes, tamanho,
geometria e toda a proveniência operacional sem repetir a extração textual do PDF por figura.
