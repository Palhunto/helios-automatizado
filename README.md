# Hélios Ebook Automation

Ferramenta local para tornar o fluxo OmegaBrain de produção de ebooks mais confiável, recuperável e menos dependente de controle inteligente do computador.

## Princípio

> ChatGPT Plus/OmegaBrain executa trabalho intelectual. Python executa trabalho operacional.

## Fluxo editorial canônico

```text
Questionário acadêmico
→ Respostas consolidadas
→ Planejamento acadêmico
→ Carregamento do contexto
→ 18 partes textuais
→ Reconciliação de referências
→ Consolidação
→ Planejamento visual
→ Enriquecimento/validação de âncoras
→ Produção de imagens
→ Google Docs
→ QA final
```

## Por que existe

O fluxo legado já produz ebooks, mas depende de:
- contexto de conversa;
- copiar/colar;
- controle de UI;
- verificações pelo próprio LLM;
- salvamento sequencial;
- retomada frágil;
- formatação por interface.

Este projeto mantém os prompts e decisões editoriais, mas transforma operação em:
- SQLite;
- filesystem;
- hashes;
- schemas;
- validators;
- adapters;
- checkpoints.

## Prompts reais

Snapshots em:

`prompts/omega_brain/`

Eles são fonte de verdade versionada para o comportamento editorial atual.

## Estado do desenvolvimento

M0–M4 estão completos. O M4 entrega paginação canônica local, importação do planejamento visual,
âncoras literais versionadas e finalização explícita em `helios_visual_manifest@1`.
As evidências de fechamento estão em `PROJECT_STATE.md` e a revisão em `docs/M4_REVIEW.md`.
M5–M10 permanecem posteriores: lifecycle de imagens, produção via GPT, Google Docs, QA final,
interface do operador e hardening de produção.

Leia:
1. `AGENTS.md`
2. `PROJECT_STATE.md`
3. `MILESTONES.md`
4. `ARCHITECTURE.md`
5. `CODEX_PLANNING_PROMPT.md`

Não há integrações externas em M1.

## Uso local

Requer Python 3.12.

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"
# Necessário somente para o channel chromium gerenciado:
.venv/Scripts/python -m playwright install chromium

ebook project create config/project.example.yaml
ebook project list
ebook project show <project_id>
ebook project validate <project_id>
ebook project recover <project_id>
ebook project runtime show <project_id>
ebook project runtime set <project_id> --browser-automation-enabled

ebook academic questionnaire import <project_id> <arquivo-ou-->
ebook academic questionnaire show <project_id> [--version N] [--raw]
ebook academic questionnaire confirm <project_id> --raw-version N
ebook academic answers import <project_id> <arquivo-ou-->
ebook academic answers show <project_id> [--version N] [--raw]
ebook academic plan authorize <project_id>
ebook academic plan import <project_id> <arquivo-ou-->
ebook academic plan show <project_id> [--version N] [--raw]
ebook academic status <project_id>
ebook academic validate <project_id>

ebook writing context create <project_id> [--contract-id ID --contract-version N]
ebook writing context show <project_id> [--version N]
ebook writing context acknowledge <project_id> <arquivo-ou-->
ebook writing context confirm <project_id> [--raw-version N]
ebook writing unit prepare <project_id> <unit_id> [--reprocess]
ebook writing unit import <project_id> <unit_id> <arquivo-ou-->
ebook writing unit show <project_id> <unit_id> [--version N] [--raw]
ebook writing unit confirm <project_id> <unit_id> --raw-version N
ebook writing status <project_id>
ebook writing validate <project_id>
ebook writing consolidate <project_id>
ebook writing consolidated show <project_id> [--version N]

ebook browser setup [--wait-seconds N]
ebook browser capture-spike --sample-kind acknowledgement
ebook browser capture-spike --sample-kind long_unit
ebook browser composer-spike <project_id> <request_artifact_id>
ebook browser context-run <project_id> [--context-id ID]
ebook browser unit-run <project_id> <unit_id> [--context-id ID]
ebook browser continue <project_id>
ebook browser retry <project_id> <interaction_id>
ebook browser status <project_id>
ebook browser validate <project_id>
ebook browser recover <project_id>
ebook browser reconcile <project_id> <interaction_id>
ebook browser interaction show <project_id> [interaction_id]
ebook browser interaction abandon <project_id> <interaction_id> --operator <nome> --reason <motivo>
```

O channel padrão é `chrome` e usa o Google Chrome instalado. O profile padrão é
`%LOCALAPPDATA%/HeliosEbookAutomation/chrome-profile`; seleções explícitas usam
`--browser-channel chrome|chromium` e `--browser-profile-dir <diretório>`, antes do subcomando
`browser`. O profile precisa ser dedicado: nunca use o profile pessoal/default e feche o Chrome
manual que estiver usando esse mesmo diretório antes de iniciar Playwright.

Configuração inválida não cria projeto. Repetir `project create` com o mesmo slug e inputs retorna
o projeto existente; inputs divergentes com slug ocupado geram conflito.

`project validate` é somente leitura. `project recover` é a operação explícita que reconcilia
unidades deixadas em `running`.

Não edite `config/project.yaml` para habilitar o browser. Use `project runtime set`; o comando cria
um snapshot imutável encadeado em `config/runtime/vNNNN.json`, com StageRun, Artifact e SHA. Para
desabilitar, use `--no-browser-automation-enabled`. `project runtime show` informa a fonte e versão
efetivas.

O questionário exige cinco itens numerados, ordenados e não vazios. Diferenças apenas tipográficas
ou de whitespace são aceitas como `normalized_match`; redação estruturalmente válida mas divergente
fica em `review_required` até `questionnaire confirm`. O texto observado nunca é substituído pelo
texto canônico. Nas respostas, `[CONFLITO]` impede `plan authorize`; `[PENDENTE]` não impede.

No writing, `omega_writing@1` é emitido uma única vez no context package. Depois do
acknowledgement e da confirmação explícita, cada `unit prepare` gera somente o pedido operacional
para a mesma conversa. Unidades, ordem, limites, headings, continuidade e separator vêm do Writing
Production Contract. `Citation Ledger != Reference Reconciliation`; o M2 não inventa bibliografia.
`writing unit show ... --raw` também expõe o validation report persistido para aquela raw version,
após verificar o artifact e o SHA; findings de `review_required` devem ser consultados antes de uma
confirmação explícita.

O contract `omega_writing_production@2` separa a faixa-alvo entregue ao modelo (9.000–10.000
caracteres) da faixa objetiva de aceitação (7.500–11.500). A seleção é explícita por
`--contract-version 2`; o V1 e seu hash permanecem preservados.

O M3 cria contexts de automação com `omega_writing_production@4` e
`helios_writing_unit_request@2`; V3 e seu SHA permanecem imutáveis. A V4 projeta `INTRO` e
`CONCLUSION` exclusivamente de seções globais existentes e seleciona cada capítulo desenvolvido
por heading + nível declarados no contract, sem IDs acadêmicos na engine. Antes de qualquer envio,
extrai e congela eager os 18 recortes; selector ausente ou ambíguo impede criar o contexto. O context package
continua sendo enviado uma única vez e a confirmação humana do acknowledgement continua
obrigatória. `review_required` e `rejected` pausam o avanço; repair de rejeição é explícito,
limitado e aceita somente findings objetivos da whitelist.

Hashes de artifacts são sempre hashes dos bytes UTF-8 reais. O fingerprint de transporte usa
apenas normalização mínima de DOM e nunca substitui SHA-256. Antes do adapter real, o operador deve
registrar os dois spikes de captura (acknowledgement curto e unidade longa) para um único
`capture_method_version`. Cada `capture-spike` cria uma conversa de teste, envia um prompt
sintético com marcador único, comprova o user turn e a resposta associada, aguarda conclusão e
compara rendered com o mecanismo Copy. Ele nunca usa WritingContext ou conteúdo editorial real.
O marker registra comprimentos, primeira divergência, classificação e presença de diferença
substantiva sem alterar automaticamente o método selecionado.

Antes do primeiro `context-run` real, `browser composer-spike` abre obrigatoriamente o Google Chrome
headed com o profile dedicado e testa inserção/limpeza do editor sem possuir caminho de envio. A
ordem é payload curto, multiline/Unicode, grande e bytes UTF-8 exatos do Artifact ID indicado,
validado como package do WritingContext proprietário. O SQLite é aberto em `mode=ro/query_only`;
nenhum StageRun,
BrowserInteraction ou event é criado. A saída registra somente comprimentos, fingerprints, duas
leituras estáveis e metadados DOM, nunca os payloads.

Se recovery não conseguir comprovar um envio externo, a interação permanece `blocked` e seu ID
aparece em `browser status`. Consulte o audit trail com `browser interaction show`. Somente uma
decisão explícita `interaction abandon`, identificada e justificada, permite criar uma tentativa
sucessora; a decisão significa abandono da tentativa não comprovável, jamais prova de que ela não
foi enviada. `browser retry` continua reservado ao repair determinístico de submissão rejeitada.
Quando não restar interaction ativa, uma `context_load` terminal explicitamente abandonada encerra
sua linhagem: o próximo `context-run` cria nova conversation, novo StageRun e interaction
`prepared` com attempt 1 e `supersedes_interaction_id`, sem consumir ou reescrever o limite e o
histórico anteriores. Bloqueio sem abandono e envio ambíguo continuam fail-closed.

O path `/c/...` é persistido assim que aparece, mas não prova envio. A interação permanece
`sending` enquanto o adapter polla o DOM pelo timeout configurado; `sent` e `sent_at` surgem apenas
quando o user turn com fingerprint exato é observado. `browser recover` também reinspeciona
`BROWSER_SEND_NOT_PROVABLE` bloqueado com path/fingerprint íntegros e sem abandono. A saída separa
`examined`, `recovered` e `skipped` com motivos; reprobe nunca chama `send_message`.

No happy path, um user turn de big paste pode conter apenas o pasted-text attachment. Nesse caso o
adapter ignora título e preview, abre o controle semântico dentro do próprio turno, lê o conteúdo
integral e exige o mesmo fingerprint do transporte. Somente depois dessa prova e de uma URL
canônica `/c/<uuid>` a resposta pode ser capturada; `/c/WEB:*` continua inválido.

Quando o envio real existe, mas a interação bloqueada ainda não possui `conversation_path`, use
explicitamente `browser reconcile PROJECT INTERACTION_ID`. O comando aceita somente uma interação
`blocked`, não abandonada e cujo último erro seja `BROWSER_SEND_NOT_PROVABLE`. Ele observa primeiro
a conversa restaurada pelo profile dedicado e, se necessário, candidatos recentes posteriores ao
boundary de envio; todo candidato precisa provar o fingerprint integral do primeiro turno. Um
pasted-text attachment pode ser aberto apenas para essa prova. Zero candidatos, ambiguidade,
fingerprint diferente, attachment inacessível ou resposta incompleta mantêm a interação bloqueada.
Depois da prova, a resposta existente é capturada por `rendered_text_v1`, importada pelo
acknowledgement M2 do mesmo `context_id`, e a mesma interação e o mesmo StageRun são concluídos sem
nova tentativa ou reenvio.

O envio usa prioritariamente `button[data-testid='send-button']`; Enter não é mecanismo primário.
Em DEBUG, os checkpoints distinguem composer encontrado/preenchido, botão encontrado/enabled,
início/conclusão do clique Playwright, limpeza do composer e observação do user turn. Botão
invisível, disabled, não-actionable ou composer que não limpa falham com códigos específicos.
Recovery não preenche nem envia um composer; uma interaction `prepared` aparece em `skipped` como
`not_started_requires_explicit_run`.

## Verificação

```bash
pytest --cov=ebook_pipeline --cov-branch
ruff check src tests
mypy src tests
python -m build --wheel
```
