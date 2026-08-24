# DATA_MODEL.md

## M0 — Core

### Project
- `id: UUID`
- `name`
- `slug`
- `config_path`
- `artifact_root`
- `created_at`
- `updated_at`

`status` não é duplicado em Project no M0: o estado operacional pertence aos runs.
`current_milestone` é metadado de desenvolvimento em `PROJECT_STATE.md`, não estado do ebook.

### StageRun
- `id`
- `project_id`
- `stage_id`
- `unit_id`
- `status`
- `input_hash`
- `idempotency_key`
- `version`
- `attempt`
- `max_attempts`
- `started_at`
- `finished_at`
- `created_at`
- `updated_at`
- `supersedes_run_id`

Estágios sem subunidade usam o próprio `stage_id` como `unit_id`.
Erros não são duplicados no run; ficam em `ErrorRecord`.

Artifact é a evidência padrão de um run `done`. Uma operação database-only precisa de regra
explícita de evidência. Para `writing_context_load/context:confirm:<ack_id>`, o validator exige:
acknowledgement confirmado, raw Artifact íntegro, run de importação `done` e `input_hash`
recalculado a partir de `acknowledgement_id + raw_sha256`.

### Artifact
- `id`
- `project_id`
- `stage_run_id`
- `artifact_type`
- `relative_path`
- `sha256`
- `byte_size`
- `version`
- `created_at`

### ErrorRecord
- `id`
- `project_id`
- `stage_run_id`
- `code`
- `message`
- `recoverable`
- `evidence_json`
- `created_at`
- `resolved_at`
- `resolution`

### SchemaMigration
- `version`
- `name`
- `sha256`
- `applied_at`

## Checkpoint no M0

Não existe tabela redundante de checkpoint. A conclusão semântica ocorre quando:

1. o arquivo final foi escrito e validado;
2. o Artifact foi registrado;
3. o StageRun foi marcado `done` na mesma transação do registro.

---

## M1 — Academic planning

### AcademicDocument

Uma tabela comum representa `questionnaire`, `consolidated_answers` e `academic_plan`:

- `id`, `project_id`, `stage_run_id`;
- `document_kind`, `acceptance_status`;
- `raw_version`, `accepted_version`;
- `raw_sha256`, `raw_artifact_id`, `accepted_artifact_id`;
- `prompt_id`, `prompt_version`, `prompt_sha256`;
- `upstream_document_id`, `authorization_id`;
- `created_at`, `accepted_at`.

Cada bruto recebe versão monotônica por tipo. Somente documentos aceitos recebem
`accepted_version`. A identidade idempotente permanece no `StageRun`; mudança de bytes, prompt,
upstream ou autorização produz outra identidade. Artefatos brutos e aceitos ficam no filesystem e
são referenciados por `Artifact`.

O questionário aceito contém, para cada posição 1→5:

- `canonical_question_number`;
- `canonical_text` vindo do snapshot versionado;
- `observed_text` preservado da resposta;
- `wording_status`: `normalized_match` ou `review_confirmed`.

Antes de confirmação, uma divergência estruturalmente válida fica como documento
`review_required`, sem artefato aceito.

As respostas aceitas contêm cinco itens ordenados, cada um com blocos de `status`, `text` e
`source_order`. Os status editoriais permitidos são `CONFIRMADO`, `COMPLEMENTO_PROPOSTO`,
`PREMISSA_EDITORIAL`, `PENDENTE` e `CONFLITO`; eles não são estados operacionais.

### AcademicReviewDecision

- `id`, `project_id`;
- `questionnaire_document_id`, `stage_run_id`;
- `decision = confirm_observed_questionnaire`;
- `created_at`.

A decisão é única por questionário e não reescreve o observado.

### AcademicPlanAuthorization

- `id`, `project_id`, `answers_document_id`;
- `conflict_count = 0`;
- `authorized_at`.

A autorização é única para a versão aceita das respostas. A constraint impede persistir uma
autorização com conflitos; não existe override em M1.

O plano é preservado como bytes UTF-8 não vazios. Seu `AcademicDocument` referencia tanto as
respostas quanto a autorização usadas. Quando novas respostas são aceitas, planos antigos
continuam armazenados, porém deixam de ser o plano compatível atual.

---

## M2 — Text production

### WritingContext
- identidade, projeto, StageRun, versão e `input_hash`;
- IDs/artifacts/hashes das accepted Consolidated Answers e do Academic Plan compatível;
- ID/versão/hash do writing prompt, Writing Production Contract e unit request prompt;
- hashes e artifacts do context package e manifest.

O contexto é imutável. `current` é derivado por comparação com a API pública do M1 e nunca
persistido. A criação relê o snapshot M1 sob o lock de escrita antes do commit.

### WritingAcknowledgement
- contexto, raw version/hash/artifact e StageRun;
- `confirmed_at` somente após ação explícita do operador.

Texto livre no acknowledgement não é usado para inferir semanticamente compreensão.

### WritingUnitPreparation
- contexto, unit ID, versão, input hash e StageRun;
- artifacts/hashes do request operacional e continuity manifest.

`WritingPreparationDependency` preserva, para cada `continuity_from`, submission ID, accepted
version, artifact ID e SHA-256. A chave/foreign key composta impede misturar projeto, contexto ou
unidade incompatível.

### TextUnitSubmission
- contexto, preparação, unit ID e StageRun;
- `raw_version` monotônica para toda tentativa;
- `accepted_version` monotônica somente para versões aceitas;
- disposição `processing|accepted|review_required|rejected`;
- raw, validation report, accepted e Citation Ledger por artifact/hash;
- character/warning counts e timestamps.

`StageRun.done` significa que a importação/validação terminou mecanicamente; não significa que o
texto foi aceito. O artifact accepted contém exatamente os bytes do raw aprovado.

### Character count no Writing Production Contract

O formato V2 declara `character_count.target.{min,max}` e
`character_count.accepted.{min,max}` por unidade. A faixa `target` integra apenas o request
operacional ao LLM; a validação objetiva usa apenas `accepted`. O contract precisa satisfazer
`accepted.min <= target.min <= target.max <= accepted.max`. O V1 permanece um formato legado
compatível, no qual seus limites único são usados tanto como target quanto como accepted.

### CitationOccurrence
- projeto, submission e unit ID;
- ordinal, raw citation text, autor observado/normalizado;
- year text, parsed year opcional, offsets e extraction rule version.

O Citation Ledger não contém título, editora, DOI, URL ou correspondência bibliográfica.
`Citation Ledger != Reference Reconciliation`.

### TextProductionSet (visão derivada)

O resolver percorre o DAG do contract em ordem topológica. Cada unidade fica em
`current_compatible`, `missing_accepted` ou `historical_incompatible`. Uma accepted submission só
é compatível se a preparação referenciar exatamente as seleções atuais de todas as dependências.
O set só é `current` quando completo e quando seu WritingContext é current para o M1.

### TextConsolidation
- contexto, StageRun, versão e `production_set_hash`;
- separator, hashes/artifacts do texto e manifest;
- membros imutáveis com ordem, submission, accepted version, artifact e hash.

Consolidação concatena bytes aceitos na ordem do contract; não chama LLM nem exige Reference
Reconciliation no M2. Uma seleção nova produz consolidação nova e preserva a anterior.

---

## M3 — Browser automation

### ProjectRuntimeSnapshot

Configuração mutável não altera `config/project.yaml`. Cada versão em
`config/runtime/vNNNN.json` contém projeto, versão, SHA/path da configuração histórica, SHA do
snapshot anterior e o objeto runtime completo. O arquivo é um Artifact de um StageRun
`project_runtime_config/runtime:update`; versões precisam ser contíguas e toda a cadeia é validada
antes do uso.

### Academic projections do WritingContext

`writing_context_projection_manifests` liga um manifest congelado ao WritingContext.
`writing_unit_academic_projections` preserva, por unidade, selector JSON, IDs/hash do Academic Plan,
projection version, artifact e SHA-256 do recorte. A extração de todas as unidades ocorre eager;
ausência ou ambiguidade impede criar um contexto apto à automação.

O selector pode restringir `heading_level` de 1 a 6, além de `heading_pattern` e ancestral. Essa
cardinalidade vem do Writing Production Contract: a engine não conhece `INTRO`, capítulos ou
conclusão. Em `omega_writing_production@4`, os bookends usam seções globais existentes e cada par
`CHnn_A/B` aponta para o mesmo capítulo desenvolvido, distinguindo-o de entradas resumidas em
níveis inferiores.

### BrowserConversation

Uma conversa ativa `chatgpt_web` por WritingContext, com estado `provisioning|ready|blocked`, path
canônico `/c/<uuid>`, fingerprint observado do primeiro turno, timestamp e baseline de paths
observado antes do send. O histórico pode conter várias gerações de conversa para o mesmo contexto;
no máximo uma pode permanecer não invalidada.
Nenhum cookie, token ou browser storage entra no SQLite.

O path só pode ser extraído do `page.url` observado em HTTPS no host `chatgpt.com`, depois de um
user turn comprovado. IDs DOM, message IDs, valores `WEB:*` e URLs ainda em `/` não constituem uma
conversa. O fingerprint calculado do request antes do envio é expectativa da BrowserInteraction;
não pode preencher `first_turn_fingerprint`.

### BrowserInteraction

Autoridade do transporte/efeito externo. Liga conversa, contexto, preparação exata, request
artifact/SHA, fingerprint, tentativa, response artifact/SHA, capture method, resultado M2 e estados
`prepared|sending|sent|streaming|captured|imported|failed|blocked`.

`captured` exige response artifact íntegro e SHA comprovado. `imported` exige que os mesmos bytes
tenham sido entregues pela API M2 de `context_id`/`preparation_id` exatos e que a identidade
resultante esteja persistida. `StageRun` permanece somente o envelope operacional genérico.

`BrowserInteractionEvent` é histórico append-only das transições e evidências não secretas.

Os boundaries persistentes de efeito externo são:

- `prepared` + event `effect_boundary=pre_send`: composer/click ainda não alcançaram a fronteira
  externa; falhas de fill são retry-safe e o StageRun permanece `pending`;
- `sending` + event `effect_boundary=send_attempt_started`: controles foram validados e o clique
  está prestes a ser disparado; ausência de prova posterior permanece fail-closed;
- `sent` + event `effect_boundary=send_observed`: user turn real foi comprovado no DOM.

`sent` exige observação do user turn com o `transport_fingerprint` exato no DOM. Para a primeira
mensagem, `conversation_path`, `first_turn_fingerprint`, a transição para `sent` e `sent_at` são
persistidos na mesma transação, somente depois dessa prova.

A prova do user turn aceita texto normal somente pelo conteúdo integral renderizado. Quando o
próprio turno contém a representação semântica de pasted-text attachment, título, preview, card,
`aria-label`, composer limpo e URL não provam o request: o adapter abre o attachment, lê sua
representação integral, aplica a normalização de transporte e exige o fingerprint exato. A URL
canônica `/c/<uuid>` continua sendo uma segunda condição obrigatória antes da persistência.

Uma interação bloqueada por `BROWSER_SEND_NOT_PROVABLE` continua elegível a reprobe quando a
conversa está `ready`, path e fingerprint coincidem e não existe resolução do operador. Recovery
pode registrar `blocked → sent` somente com nova prova DOM, preservando o timestamp legado e todo o
histórico de events; nenhum request é reenviado.

Paths históricos fora do formato canônico são inconsistências auditáveis, não candidatos a
recovery. Eles permanecem preservados no SQLite e são reportados por validação/recovery sem
navegação, reenvio ou reescrita de events.

### BrowserConversationInvalidation

Ledger append-only que retira uma conversa histórica da seleção ativa sem alterar sua linha.
Registra conversa substituída, conversa `provisioning` substituta, projeto, motivo fechado
`legacy_invalid_conversation_path`, evidência JSON e timestamp. Só pode ser criado quando todas as
interactions da conversa inválida estão `blocked` e possuem resolução explícita do operador.

Depois da invalidação, `browser status` expõe a conversa antiga com `reusable=false` e a decisão;
o path, `first_turn_fingerprint`, status e events históricos permanecem intactos. A validação de
integridade continua recusando paths inválidos não resolvidos, mas não promove um registro
invalidado a candidato ativo.

### BrowserInteractionResolution

Decisão append-only do operador para uma interação `blocked` cujo efeito externo não pôde ser
comprovado. Registra `operator_abandoned`, identidade explícita do operador, motivo, timestamp,
ação auditável sobre a tentativa de provisionamento e snapshot da conversa. Não registra
`not_sent`, não altera a interação ou o StageRun bloqueados e não remove events/artifacts.

Após a resolução, uma nova BrowserInteraction pode apontar para a anterior por
`supersedes_interaction_id`; seu StageRun também referencia o StageRun anterior. Uma conversa ainda
em `provisioning`, sem path/fingerprint e sem outra interação recuperável, tem sua tentativa de
provisionamento encerrada e pode ser reprovisionada somente pela nova interação explícita.

Uma linhagem de `context_load` cujo último efeito terminal foi explicitamente
`operator_abandoned` deixa de ser candidata ativa quando não existe nenhuma interação recuperável.
Uma nova execução cria outra BrowserConversation e outro StageRun, preserva integralmente a
linhagem anterior e inicia uma BrowserInteraction `prepared` com `attempt = 1`, `max_attempts` da
configuração corrente e `supersedes_interaction_id` apontando para a última abandonada. Esse attempt
já identifica o primeiro envio da nova linhagem e não é incrementado novamente ao entrar em
`sending`.

---

## M4 — Visual planning

### VisualFigure
Editorial fields:
- `figure_id`
- `order`
- `number`
- `name`
- `page`
- `section`
- `exact_position`
- `main_concept`
- `conceptual_synthesis`
- `justification`
- `objective`
- `visual_type`
- `complexity`
- `generation_prompt`

Operational enrichment:
- `anchor_text`
- `position_relative_to_anchor`
- `anchor_before`
- `anchor_after`
- `anchor_validation_status`
- `renderer`
- `status`

---

## M5 — Image

### ImageAsset
- `figure_id`
- `renderer`
- `prompt_hash`
- `style_prompt_hash`
- `file_path`
- `file_sha256`
- `status`
- `attempts`
- `created_at`

## Separação importante

`CONFIRMADO`/`PENDENTE` etc. são status editoriais de conteúdo.
`pending`/`running`/`done` etc. são status operacionais.
Nunca reutilizar o mesmo enum.
