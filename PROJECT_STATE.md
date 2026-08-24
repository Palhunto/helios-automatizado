# PROJECT_STATE.md

CURRENT_MILESTONE: M4
STATUS: READY_TO_START
SPEC_REVISION: OMEGABRAIN_REAL_PROMPTS_V1
LAST_REVIEW: 2026-08-24

## Estado

O M0 — Fundação foi implementado e verificado:

- pacote Python 3.12 e CLI `ebook`;
- configuração estrita de aplicação, projeto e pipeline;
- SQLite com migration versionada e detecção de drift;
- Project, StageRun, Artifact e ErrorRecord;
- máquina de estados, retries limitados e reprocessamento versionado;
- filesystem seguro, escrita atômica, SHA-256 e no-clobber;
- logging contextual com redaction;
- criação idempotente, validação e recovery de `running` órfão;
- testes unitários e integrados;
- hardening M0.12 de falha segura, idempotência, integridade, concorrência,
  recovery e rollback.

Verificação de fechamento:

- `78 passed`;
- cobertura de linhas de `96,9%`, branches de `89,8%` e combinada de `95,9%`;
- Ruff sem erros;
- mypy estrito sem erros;
- wheel construído com entry point e migration incluídos.

O M1 — Planejamento Acadêmico foi implementado e verificado:

- registry de prompts estrito com resolução exata de `prompt_id + version + SHA-256`;
- migration `0002_m1_academic_planning.sql`, preservando `0001_m0_core.sql` byte a byte;
- documentos acadêmicos, autorizações e decisões de revisão persistentes;
- bruto e aceito versionados separadamente, com proveniência e snapshots de prompt congelados;
- questionário estrutural com texto observado preservado, comparação normalizada e confirmação
  explícita de divergência sem substituir a redação observada;
- fallback posicional estrito para o caso real de clipboard com exatamente cinco perguntas não
  numeradas em blocos inequívocos, preservando o texto observado e o fluxo de revisão;
- parser determinístico de respostas com cabeçalho opcional, âncoras por número/título e cinco
  respostas únicas em ordem;
- `[CONFLITO]` bloqueia autorização normal; `[PENDENTE]` não bloqueia;
- Academic Plan exige autorização das respostas atuais e planos antigos permanecem preservados;
- API de leitura para respostas atuais e plano compatível, sem antecipar M2;
- validação somente leitura, recovery acadêmico e CLI completa do M1.

Verificação de fechamento do M1:

- `143 passed` em Python 3.12.13;
- cobertura de statements de `95,8%`, branches de `87,4%` e combinada de `94,4%`;
- módulos críticos: service 91%, repositories 99%, parsers 96%, validators 100%, recovery
  100% e prompt registry 100%;
- Ruff sem erros;
- mypy estrito sem erros em `src` e `tests`;
- wheel construído, contendo `0001` e `0002`, e instalado em ambiente virtual limpo;
- fluxo CLI completo validado com processos independentes e restart persistente;
- hash SHA-256 de `0001_m0_core.sql` preservado:
  `4cc7cdf4763d87c7ccf2a35355d55a551a46d68ed4f5fb74a5a7671487f781c8`;
- os quatro snapshots canônicos permanecem iguais aos hashes do registry.

O M1 permanece completo. O M2 — Produção Textual foi iniciado por autorização explícita do
usuário e concluído após os gates abaixo.

Baseline imutável registrado antes do M2:

- `0001_m0_core.sql`: `4cc7cdf4763d87c7ccf2a35355d55a551a46d68ed4f5fb74a5a7671487f781c8`;
- `0002_m1_academic_planning.sql`: `018196346e6441b8916253a9291123ddf4b4c224594227b71e276b86c69af149`;
- `academic_planning_v1.txt`: `15568476f949a1d09769c6ef9906aaf1c0c6cd3a85c258efe84c3bc9e50aeb95`;
- `writing_v1.txt`: `9cb06a7ae45e2df1304a724f209ac0f7aabc6a87dbdc4b07c793d0223dba2524`;
- `visual_planning_v1.txt`: `4492eadfb29795bc4374b7d685634dd6775c9b3d89f882b55a5d38eccf186708`;
- `image_global_style_v1.txt`: `45252129c7bae3e46a954723e79e2d43d689376cc404b01a140eedb3d3f2e2b1`.

O M2 — Produção Textual foi implementado e verificado:

- Writing Production Contract registry com resolução exata por ID/versão/hash;
- contract canônico com 18 unidades e contracts sintéticos independentes da engine;
- WritingContext imutável ligado a Answers/Plan atuais e aos snapshots de prompt/contract;
- releitura concorrente do snapshot M1 sob lock antes do commit do contexto;
- renderização determinística de Answers + Plan e context package congelado;
- acknowledgement bruto e confirmação explícita antes de qualquer unit request;
- prompt `omega_writing@1` somente no pacote inicial e request prompt operacional separado;
- preparações idempotentes/reprocessáveis com proveniência exata de `continuity_from`;
- raw/rejected/review/accepted versionados, bytes aceitos idênticos ao raw e histórico preservado;
- hard errors objetivos e warnings linguísticos conservadores;
- Citation Ledger de ocorrências autor-data, sem Reference Reconciliation;
- Production Set topológico com `current_compatible`, `missing_accepted` e
  `historical_incompatible`, incluindo propagação downstream por proveniência;
- consolidação textual determinística por ordem/separator do contract;
- WritingRecovery por operação e CLI manual completa;
- migration única `0003_m2_text_production.sql`, com FKs compostas de proveniência.

Verificação de fechamento do M2:

- `193 passed` em Python 3.12.13;
- statements globais `95,98%` e branches globais `87,35%`;
- módulos críticos de writing entre `96,30%–100%` de statements e `86,11%–100%` de branches;
- Ruff sem erros e mypy estrito sem erros em 73 arquivos;
- oito schemas JSON válidos em Draft 2020-12, oito YAMLs parseados e configs/prompts/contracts
  resolvidos pelos validadores estritos de runtime;
- wheel construído e instalado em venv limpo, expondo `ebook --help` e contendo `0001`–`0003`;
- fluxo CLI manual completo com contract reduzido, restart, validate e consolidação aprovado;
- hashes de `0001`, `0002` e dos quatro snapshots canônicos permanecem iguais ao baseline;
- hash final de `0003_m2_text_production.sql`:
  `7070935e2da6833aa6df5198d9f6a020ca91d9d22d4664f3850128647f143275`.

Hardening pós-teste manual do M2:

- `writing unit show ... --raw` passou a retornar o validation report imutável ligado à raw version
  solicitada, com findings e contagens derivadas;
- a leitura verifica o Artifact ID, hash do artifact, hash de proveniência e coerência estrutural do
  report antes de expor dados;
- ausência, artifact ausente e bytes divergentes falham fechados, sem alterar disposição ou
  confirmar a submissão;
- testes cobrem `review_required`, `rejected`, `accepted`, restart e os três cenários de
  integridade acima.

Hardening de contract do M2:

- `omega_writing_production@2` separa a faixa `target` (9.000–10.000) da faixa `accepted`
  (7.500–11.500) para todas as 18 unidades acadêmicas;
- o renderer operacional usa somente `target`; o validator usa somente `accepted`, sem warning
  por valores dentro da margem válida;
- V1, seu arquivo e SHA permanecem imutáveis; contratos sintéticos confirmam que a engine não
  contém limites ou IDs acadêmicos hardcoded.

## Bloqueios

Nenhum bloqueio técnico conhecido no início do M3.

## M3 — ChatGPT Plus Browser Automation

O M3 foi iniciado por autorização explícita do usuário em 2026-08-21 e concluído em 2026-08-24
após os gates automatizados e a validação em browser real. Os baselines de M0–M2 permaneceram
preservados durante o milestone.

Implementação automatizada concluída até M3.12:

- APIs públicas M2 por `context_id` e `preparation_id` exatos, com compatibilidade manual;
- contracts `omega_writing_production@3` congelado e `@4` corrente, com request prompt
  `helios_writing_unit_request@2`, preservando V1/V2/V3;
- extração acadêmica determinística eager para todas as unidades, artifacts e manifest congelados;
- selectors V4 orientados por heading + nível: bookends por contexto global e pares A/B pelo mesmo
  capítulo desenvolvido, sem alterar o Academic Plan nem codificar IDs na engine;
- política tipada de evidência para StageRun `done`: Artifact por padrão e decisão SQLite somente
  quando entidade, source Artifact, source run e input hash são integralmente comprovados;
- runtime mutável em snapshots `config/runtime/vNNNN.json`, encadeados por SHA e registrados por
  StageRun/Artifact, sem editar a configuração histórica do projeto;
- migration `0004_m3_browser_automation.sql` com projeções, conversations, interactions e events;
- Playwright 1.62, channels `chrome|chromium`, profile persistente dedicado, lock exclusivo,
  sessão/challenge fail-closed e seletores isolados;
- uma conversa por WritingContext, prompt limpo, confirmação humana e mesma conversa por unidade;
- autoridade externa em BrowserInteraction e envelope genérico em StageRun;
- fronteira estrita UTF-8 bytes/texto, SHA de artifact separado de transport fingerprint;
- gate de captura com amostra curta e longa para um único `capture_method_version`;
- capture spike ativo com prompt sintético isolado, fingerprint, checkpoints DEBUG e erros por
  fronteira; nunca captura conversa vazia nem usa WritingContext;
- response artifact antes do import M2, dispositions preservadas e repair explícito por whitelist;
- recovery normal e bootstrap por fingerprint + baseline pré-send, sem reenvio ambíguo;
- CLI setup/status/validate/context-run/unit-run/continue/retry/recover e stdout UTF-8 seguro;
- matriz automatizada de crash/recovery, restart, idempotência e integridade.
- resolução explícita append-only de BrowserInteraction bloqueada, com operador/motivo/timestamp,
  audit trail, encerramento comprovável de provisioning sem path e sucessão de Interaction/StageRun;
- `browser status` expõe IDs bloqueados e `browser interaction show|abandon` separa abandono de
  `retry`, sem inferir que o request externo não foi enviado;
- Ctrl+C retorna exit 130 e o cleanup tolera fechamento concorrente do Playwright sem cascata de
  `TargetClosedError`, mantendo recovery fail-closed.
- `conversation_path` deixou de promover envio: `sending → sent` agora exige user turn do
  fingerprint exato observado após polling pelo timeout configurado;
- recovery reinspeciona `blocked/BROWSER_SEND_NOT_PROVABLE` com path/fingerprint íntegros e sem
  abandono, aguarda streaming/resposta e captura/importa sem reenviar;
- relatório de recovery distingue `examined`, `recovered` e `skipped` com motivo auditável.
- send normal usa o `data-testid=send-button` preferencial, valida visible/enabled/actionability,
  registra checkpoints antes/depois do clique e exige limpeza observada do composer;
- recovery não chama composer/fill/send para interaction `prepared`; reporta
  `not_started_requires_explicit_run` e permanece estritamente observacional.
- prova de primeiro envio agora é atômica: somente um user turn DOM com fingerprint observado
  idêntico pode promover `sending → sent`, preencher `sent_at` e vincular a conversa;
- `conversation_path` vem exclusivamente de `page.url` HTTPS no host `chatgpt.com`, em formato
  canônico `/c/<uuid>`; IDs DOM, message IDs e valores `WEB:*` são rejeitados;
- o fingerprint pré-send permanece apenas como expectativa em `BrowserInteraction`; o
  `first_turn_fingerprint` da conversa registra exclusivamente a observação DOM comprovada;
- recovery diagnostica paths históricos inválidos sem abrir a URL, reenviar, alterar status ou
  acrescentar events à interação afetada.
- migration `0006_m3_browser_conversation_invalidations.sql` preserva conversations/interactions/
  events/resolutions, permite gerações de conversa por contexto e adiciona ledger append-only;
- conversa `ready` com path inválido é derivada como `reusable=false`; após todas as interactions
  estarem abandonadas, o próximo context-run registra `legacy_invalid_conversation_path`, cria uma
  conversa `provisioning` substituta e nunca reescreve a conversa histórica.
- `browser interaction show` sem ID prioriza uma única interaction ativa/não resolvida, ignora
  abandonadas e falha com candidatas quando há ambiguidade; `browser status` lista IDs ativos;
- boundaries persistentes agora distinguem `pre_send`, `send_attempt_started` e `send_observed`:
  falha anterior ao callback imediatamente pré-clique permanece `prepared/pending` e retry-safe.
- resolução do composer agora seleciona exatamente um editor visível/editável, audita metadados DOM
  sem conteúdo editorial, exige foco confirmado e relê `value` para `textarea`/`input` ou texto para
  `contenteditable`; somente duas observações estáveis do fingerprint integral liberam
  `composer_filled` e a resolução do botão Send.
- `browser composer-spike PROJECT REQUEST_ARTIFACT` adiciona um probe Chrome headed/no-send: SQLite
  `mode=ro/query_only`, Artifact ID/SHA/bytes verificados, quatro payloads ordenados, limpeza
  comprovada antes/depois e relatório restrito a comprimentos, fingerprints e metadados DOM;
  o caminho não acessa Send/Enter nem cria interaction, StageRun ou event.

Verificação automatizada do M3 após o hardening de proveniência da prova de envio:

- `294 passed` em Python 3.12.13;
- Ruff sem erros e mypy estrito sem erros em 103 arquivos;
- cobertura dirigida M3: state machine/fingerprints 100%, projections 92,38%, persistence 90,18%,
  repositories 90,16%, recovery 84,91%, service 78,36% e adapter real 79,40% de linhas;
- wheel construído com `0001`–`0004` e pacote browser, instalado em venv limpa com dependências;
- Playwright 1.62, Chromium gerenciado e suporte ao Google Chrome instalado;
- hashes históricos `0001`–`0003`, prompts canônicos, request V1 e contracts V1/V2 preservados;
- hash de `0004`: `96f189dfaa32d33f3f81e1dfde0b2f12d2bd8a6a8e5a2e83b7a610b3d36f10a9`;
- hash de `0005`: `be2d09a4df430e59e4eceb7cf9ad4ea8de7ecfd1cbd3513788e7e17de7866cb0`;
- hash de `0006`: `12d3587f0cdcc273b618912198a7c293dc7283b28e1856212bb26a5f2e6b3580`;
- hash do request V2: `8007c31b2793631d08d335d9f74bcf74220f2dad052b86a4275ec16921513b02`;
- hash do contract V3: `a48cd37881e93deef21cc0802bb29db6495d7c92b440067d4505db9abf7a3e1b`.
- hash do contract V4: `2c3ebe9870a910f62e65397d075274ccfe0bd6af7bc8a5b6613419052612c9be`.

Evidências do aceite manual M3.13 e da validação em browser real:

- o bootstrap manual por Google Chrome autenticou e persistiu a sessão exclusivamente no profile
  dedicado `C:\Users\Palha\AppData\Local\HeliosEbookAutomation\chrome-profile`;
- um probe headed do Playwright com `channel=chrome` reutilizou exatamente esse profile e retornou
  `ready` em 2026-08-22;
- os spikes headed reais curto e longo enviaram somente prompts sintéticos, comprovaram user e
  assistant turns, capturaram rendered + Copy e fixaram `rendered_text_v1`; nenhum prompt editorial
  ou WritingContext foi enviado e nenhum challenge foi contornado;
- a comparação persistida registrou primeira divergência, comprimentos e conteúdo substantivo:
  Copy acrescentou somente `CRLF` terminal (curto: 68/68 rendered versus 70/70 Copy; longo:
  5.120 caracteres/5.280 bytes rendered versus 5.122 caracteres/5.282 bytes Copy), sem tokens
  substantivos exclusivos; classificação `whitespace`, mantendo `rendered_text_v1` canônico;
- o primeiro create real com V4 resolveu e congelou os 18 recortes do Academic Plan histórico em
  WritingContext V2; os oito pares A/B possuem hashes idênticos por capítulo, `writing validate`
  retornou válido e nenhuma BrowserConversation/BrowserInteraction foi criada;
- `project validate` do projeto real passou após comprovar formalmente a confirmação artifactless;
  `DONE_RUN_WITHOUT_ARTIFACT` continua ativo para todas as operações não declaradas;
- browser automation foi habilitada oficialmente no projeto real por runtime snapshot V1
  (`11e407db17774e28acbdc2366ce739762c74b1b11b0349f19f761e714cdbcc37`), mantendo
  `config/project.yaml` no SHA `693ad1e1a6ca6b02ecb898e81fd422ded0e9bf1ed6dc0850f501f2da01e09e28`;
- a observação visual comprovou que o primeiro context-run real nunca saiu do composer de Novo
  chat. A interaction histórica `d01d8203-3241-4fc1-83f0-507146299787` preserva a evidência do bug:
  `/c/WEB:...`, fingerprint pré-send e `sent_at` foram gravados sem user turn real;
- a auditoria read-only confirmou que o evento `sending → sent` contém somente o path inválido e
  compartilha timestamp com `sent_at`/`ready_at`; não existe fingerprint DOM observado no evento;
- a interaction já possuía uma resolução histórica `operator_abandoned` posterior ao bloqueio. Este
  hardening não a abandonou, reenviou, recuperou nem reescreveu; o runtime corrigido recusa o path
  inválido e exige prova DOM + `page.url` real para novos envios;
- a nova interaction real `b2bd03db-d077-4428-9e3b-841861dbcc53` foi auditada read-only: SQLite
  continha somente events `prepared → sending`, `sent_at=null` e StageRun `running`; o log continha
  `composer_found` seguido de `BROWSER_COMPOSER_FILL_FAILED`, sem `send_trigger_started`. O registro
  histórico não foi alterado; o novo boundary impede essa promoção prematura em execuções futuras;
- a releitura anterior usava `inner_text()` sem distinguir `textarea`/`input`; o hardening agora
  audita e resolve o editor ativo, relê pelo mecanismo DOM correto e bloqueia antes de Send até a
  coincidência integral estabilizada. Nenhum browser/context-run real foi executado para validar
  este patch; o reteste headed permanece pendente de comando explícito;
- o composer spike no-send foi executado contra o Chrome/profile real e Artifact
  `a072b541-26ce-43d8-9ffd-c676f02a6f15`: curto 27/27 e multiline/Unicode 88/88 passaram; o caso
  grande passou por fingerprint integral (20.480 esperado, 20.481 observado por newline terminal
  normalizado). Todos foram limpos depois da prova, sem Send/Enter;
- o Artifact real possui 70.625 bytes/67.665 caracteres. Os probes revelaram e corrigiram troca
  `textarea → contenteditable`, separadores extras de `innerText`, `fill("")` ineficaz e perda por
  inserção sem backpressure. O código atual desvia >32 KiB para `keyboard.insert_text` em blocos,
  exige o fingerprint do prefixo após cada bloco e compara deterministicamente representação por
  blocos e `textContent`;
- o último reteste headed do código final foi impedido antes de abrir o Chrome pelo limite externo
  de aprovação/uso. O SQLite real permaneceu byte a byte no SHA
  `8d8850a804c1377bd583c5380e12e57fec183809a283d780c884b9991a019ee0`, com 28 StageRuns,
  2 conversations, 3 interactions e 12 events antes/depois. Nenhum `context-run` foi iniciado;
- a observação humana posterior confirmou que clique nativo e Ctrl+V inseriam visualmente o payload
  correto, mas o ChatGPT representa esse big paste por um botão semântico cujo `aria-label` começa
  por `Abrir anexo de texto colado`, mantendo o editor vazio. O gate conta exclusivamente esse
  controle dentro do formulário do composer antes/depois e considera `composer_filled` por duas
  leituras textuais integrais consecutivas ou por exatamente um novo pasted-text attachment
  (`after = before + 1`). Attachment preexistente, delta zero ou múltiplo não prova a operação;
  conteúdo do attachment nunca é lido ou logado. A limpeza do composer spike resolve o `role=group`
  do controle novo, aciona seu único botão semântico `Remover ficheiro` e comprova retorno ao
  baseline. Os 17 testes focados passaram, Ruff e mypy estrito passaram nos três arquivos tocados e
  nenhum `context-run`, spike ou browser real foi iniciado;
- o primeiro context-run posterior comprovou `composer_filled` e `send_button_found`, mas o botão
  permaneceu temporariamente disabled enquanto o ChatGPT processava o pasted-text attachment; não
  houve `send_trigger_started`. O adapter agora reobtém o botão a cada poll por até 30 segundos,
  tolera desaparecimento/rehidratação transitórios e só registra `send_button_enabled` após duas
  observações consecutivas visible+enabled. Timeout gera
  `BROWSER_SEND_BUTTON_ENABLE_TIMEOUT` ainda na fronteira pré-send, sem clique ou limpeza do
  composer. Os 9 testes focados passaram, Ruff e mypy estrito passaram nos dois arquivos tocados e
  nenhum browser real, spike ou `context-run` foi executado para este patch;
- como a interaction oficial permaneceu `prepared` e nenhum `send_trigger_started` ocorreu, o
  profile persistente pode conservar o pasted-text attachment dessa tentativa pré-send. Antes de
  inserir um request não-prefilled, o adapter agora reobtém o composer e observa somente
  `composer_text_length` e `pasted_text_attachment_count`; baseline `0/0` prossegue sem limpeza.
  Qualquer draft pré-send reutiliza a rotina comprovada do composer spike com baseline zero e exige
  nova observação `0/0` antes de liberar a inserção. Falha produz
  `BROWSER_COMPOSER_STALE_DRAFT_CLEAR_FAILED`, ainda antes de paste, Send ou callback de efeito
  externo. Os 6 testes focados passaram, Ruff e mypy estrito passaram nos dois arquivos tocados e
  nenhum browser real ou `context-run` foi executado;
- foi adicionado `ebook browser reconcile PROJECT INTERACTION_ID` exclusivamente para uma
  interaction `blocked`, não abandonada e com último erro `BROWSER_SEND_NOT_PROVABLE` cuja conversa
  real ainda não foi vinculada. O fluxo não preenche composer, não usa Send/Enter, não cria
  interaction, não incrementa attempt e não reenvia: prova primeiro a conversa restaurada ou um
  único candidato recente por fingerprint integral do primeiro turno, inclusive lendo de forma
  observacional um pasted-text attachment quando necessário. Somente após resposta completa e
  estável vincula `/c/<uuid>`, persiste o mesmo envio, captura Artifact/SHA por `rendered_text_v1`,
  importa o acknowledgement M2 pelo `context_id` original e conclui a mesma interaction e StageRun.
  Zero/múltiplos candidatos, divergência, attachment inacessível ou resposta incompleta preservam o
  bloqueio sem mutação. Os 12 testes focados passaram, Ruff e mypy estrito passaram nos nove
  arquivos verificados e nenhum browser, reconcile real, `context-run` ou Send foi executado;
- uma `context_load` terminal com resolução append-only `operator_abandoned` agora encerra sua
  linhagem de attempts quando não existe interaction ativa. O próximo preparo cria nova conversation
  e novo StageRun, preserva todos os registros anteriores e inicia uma BrowserInteraction
  `prepared` com attempt 1, `max_attempts` normal e `supersedes_interaction_id` para a última
  abandonada; a entrada em `sending` não conta esse primeiro attempt duas vezes. Blocked sem
  resolução e interaction ambígua/ativa continuam fail-closed. Os 3 testes focados passaram, Ruff
  passou nos cinco arquivos tocados e mypy estrito passou nos oito arquivos necessários; nenhum
  browser ou `context-run` real foi executado;
- a prova pós-Send do happy path agora possui dois caminhos integrais: texto normal preserva a
  comparação existente; pasted-text attachment no próprio user turn ignora card/título/preview,
  abre somente esse controle, lê o conteúdo integral, normaliza pelo transporte e exige fingerprint
  exato. Falha de abertura mantém polling limitado com reacquire; conteúdo divergente e candidatos
  ambíguos falham fechado. Mesmo após a prova, somente `/c/<uuid>` canônico libera persistência e
  captura da resposta. Logs contêm apenas contagens, comprimentos, flags, hashes e path. Os 8 testes
  focados passaram, Ruff passou nos três arquivos tocados e mypy estrito passou nos seis arquivos
  necessários; nenhum browser, `context-run` ou Send real foi executado;
- a prova pós-Send de `context-run`/`unit-run` normal agora é estritamente estrutural e vinculada ao
  baseline pré-Send mantido no processo: após `composer_cleared`, exige URL `/c/<uuid>` canônica,
  exatamente um novo user turn, exatamente um attachment nesse novo turno quando o composer usou
  big-paste e duas leituras estruturais estáveis. Esse caminho não abre attachment/modal, não relê
  o payload e não recalcula seu fingerprint; o `transport_fingerprint` permanece como proveniência.
  A prova integral anterior continua disponível e inalterada para reconcile/recovery. Retomada de
  `send_attempt_started` sem conversation path canônico recuperável falha pré-resend com
  `BROWSER_SEND_REQUIRES_RECONCILE`. Os 38 testes de integração do browser e 11 testes unitários
  focados passaram; Ruff passou nos seis arquivos de código/teste verificados e mypy estrito passou
  nos 107 arquivos. Nenhum browser real, `context-run` ou Send foi executado;
- o foco inicial do composer em conversa reutilizada agora usa polling curto com reacquire a cada
  leitura, prefere `#prompt-textarea[contenteditable]`/`[contenteditable][role=textbox]`, aceita o
  fallback `textarea`/`input` somente quando visível, habilitado e editável e exige duas leituras
  semânticas estáveis antes de `click`/`focus`. Cada inspeção/actionability e ação usa timeout
  explícito de no máximo 500 ms; o sucesso exige `document.hasFocus()`, activeElement no composer e
  auditoria DOM focada. Ausência, detach ou reidratação sem editor focável termina pré-Send com
  `BROWSER_COMPOSER_FOCUS_TIMEOUT`. Os 13 testes focados passaram, Ruff passou nos dois arquivos
  tocados e mypy estrito passou nos 107 arquivos; nenhum browser real foi executado;
- a verificação pós-inserção de texto normal no composer agora é um gate próprio, separado das
  rotinas de big-paste e limpeza: reacquire o editor hidratado em cada poll, lê `textarea` ou
  `contenteditable` com timeout explícito de 500 ms e limita a convergência total a 5 s. Como as
  representações DOM não são canônicas para reconstrução byte-exata, o happy path normal não exige
  mais comprimento ou fingerprint do DOM: após o baseline vazio comprovado, exige texto não vazio
  em duas leituras consecutivas e delta zero de pasted-text attachments. Attachment inesperado
  falha fechado; o caminho big-paste continua exigindo delta exato `N -> N+1`. A integridade forte
  permanece anterior ao paste pelo SHA do Artifact/transport contra o SHA integral do clipboard.
  O timeout vazio falha pré-Send com `BROWSER_COMPOSER_INSERT_VERIFY_TIMEOUT`, e o
  `composer-text-spike` permanece somente diagnóstico. Os 108 testes unitários do adapter passaram,
  Ruff passou nos dois arquivos de código/teste e mypy estrito passou nos 108 arquivos; nenhum
  browser real ou Send foi executado;
- foi adicionada a rota diagnóstica read-only `browser composer-text-spike PROJECT INTERACTION_ID`
  para investigar divergências do editor já preenchido sem alterar o gate de produção. A interação
  vincula e valida o Artifact esperado e a `conversation_path` em SQLite read-only; navega para a
  URL canônica somente quando necessário e aguarda por até 30 s a hidratação do composer, com
  reacquire por poll, preferência por `contenteditable`, tolerância ao `textarea` transitório e duas
  leituras estáveis do único editor visível/editável. Sem expor conteúdo, a inspeção calcula
  `raw_length`, `normalized_length`, `normalized_sha256` e match para `textarea.value`,
  `textContent`, `innerText` e reconstrução de nós com blocos/`<br>`, além do índice da primeira
  divergência, prefixo/sufixo comuns e delta de comprimento. A rota não clica, não foca, não
  preenche, não limpa e não possui caminho de Send. O JavaScript exato passado a `evaluate()` foi
  extraído para uma função constante sem escapes `\n`/`\r`, backticks ou template/multiline
  literals; separadores são produzidos por `String.fromCharCode(10)` e um teste executa essa mesma
  fonte no runtime Node distribuído pelo Playwright. Os 8 testes focados passaram, Ruff passou nos
  seis arquivos verificados e mypy estrito passou nos 108 arquivos; nenhum spike/browser real ou
  Send foi executado;
- o baseline estrutural pré-Send agora é capturado em memória imediatamente após
  `send_button_enabled` e antes de `send_attempt_started`/`send_trigger_started`. O adapter exige
  um único root de conversa hidratado e duas leituras estáveis de `pre_send_user_turn_count`; se a
  cardinalidade não puder ser provada, falha pré-Send com
  `BROWSER_PRE_SEND_USER_TURN_BASELINE_UNAVAILABLE`, sem callback ou clique. A prova pós-Send exige
  `current_user_turn_count == pre_send_user_turn_count + 1` e seleciona o turno no ordinal do
  baseline. No happy path em processo, o serviço não renavega a conversa persistida logo após o
  Send, preservando o baseline recém-capturado; fluxos retomados continuam abrindo a conversa pelo
  caminho existente. Isso cobre igualmente `context_load` em conversa nova e `unit_request` em
  conversa reutilizada. Os 112 testes unitários do adapter e 39 testes de integração do browser
  passaram; Ruff passou nos cinco arquivos verificados e mypy estrito passou nos 108 arquivos.
  Nenhum browser real ou Send foi executado;
- falhas `BROWSER_SEND_REQUIRES_RECONCILE` lançadas depois de
  `send_attempt_started` agora bloqueiam atomicamente a mesma `BrowserInteraction` e seu
  `StageRun` antes de retornar ao CLI: a interação passa de `sending` para `blocked`, o run termina
  em `blocked` com `finished_at`, o evento append-only preserva `error_code` e o erro operacional é
  registrado sem remover eventos anteriores. Foi adicionado o comando estritamente local
  `browser interaction block-orphaned-send PROJECT INTERACTION_ID`, que promove um órfão somente
  com evento de fronteira presente, ausência completa de evidência sent/captured/imported e
  StageRun ainda `running`; chamadas repetidas são idempotentes. O reconcile explícito mantém suas
  provas integrais e agora reconhece também esse erro pós-Send equivalente. A interação histórica
  `2c56fe09-7cbf-4213-b040-8abefe9f1895` foi validada e promovida por esse comando: interação e run
  estão `blocked`, `sent_at`/`response_artifact_id` permanecem nulos e o novo evento preserva a
  linhagem `prepared -> sending -> blocked`. Os 45 testes de integração da automação do browser e
  os testes focados de CLI passaram; Ruff passou nos quatro arquivos verificados e mypy estrito
  passou nos 108 arquivos. Nenhum browser real ou Send foi executado;
- o reconcile explícito agora preserva o caminho existente de `context_load` e também aceita
  `unit_request` bloqueada, sem preparar ou enviar outra unidade. O serviço valida a conversa
  canônica persistida, o mesmo WritingContext e o `preparation_id` exato contra projeto, contexto,
  unidade, Artifact e SHA do request; o ordinal recuperável é derivado somente das interações
  anteriores `sent`/`captured`/`imported` comprovadas na mesma conversa. Qualquer turno extra,
  cardinalidade instável, linhagem/timestamp ambíguo ou binding divergente falha fechado. Com duas
  leituras estruturais estáveis, o adapter seleciona o user turn ordinal exato e exige a resposta
  assistant imediatamente posterior, completa; a captura continua usando `rendered_text_v1`, gera
  o response Artifact e importa pelo fluxo M2 existente usando exclusivamente o `preparation_id`
  persistido. O sucesso percorre `blocked -> sent/reconciled -> captured -> imported` e conclui o
  StageRun, aceitando as disposições M2 existentes. Os 115 testes unitários completos do adapter e
  os 47 testes completos de integração do browser passaram. Nenhum browser real, reconcile real ou
  Send foi executado;
- a alocação canônica de raw submissions M2 agora usa o namespace físico real dos artifacts,
  `project_id + unit_id`, em vez de reiniciar a versão por WritingContext. Assim, happy path e
  browser reconcile continuam chamando a mesma `import_unit_by_preparation_id`, preservam um
  `text/raw/<unit>/v0001.txt` histórico e alocam deterministicamente a próxima raw version. Os
  artifacts derivados accepted/references usam o mesmo escopo de projeto/unidade para não apenas
  deslocar o conflito para a materialização seguinte. A idempotência por `preparation_id +
  raw_sha256` permanece anterior à alocação. Um estado legado estrito criado pela falha observada
  — submission `processing`, StageRun `running`, hashes/contagens idênticos, nenhum artifact
  materializado e raw version comprovadamente ocupada por outra submission — pode ser realocado
  para a próxima versão e concluído; qualquer outra interrupção continua fail-closed com recovery.
  Os 38 testes focados do M2 e 3 testes browser dos consumidores compartilhados passaram; Ruff e
  mypy estrito passaram. Nenhum browser real ou Send foi executado;
- foi adicionada a operação explícita e local `browser interaction resume-import PROJECT
  INTERACTION_ID` para concluir somente o import M2 de uma `unit_request` já `captured`. A rota
  aceita `captured` ou seu resultado terminal `imported` apenas para retorno idempotente, valida
  projeto, StageRun, response Artifact, owner, tipo, tamanho/SHA, capture method e o
  `preparation_id` exato contra contexto, unidade e request persistidos. Em `captured`, relê os
  bytes registrados e chama a mesma `import_unit_by_preparation_id` do happy path; depois persiste
  a identidade M2, transiciona `captured -> imported` e conclui o StageRun com `finished_at`. Uma
  segunda execução valida e retorna a submission existente, sem criar Artifact ou submission.
  Estados, kinds, owners e bindings divergentes falham fechado. Os 2 testes focados de retomada,
  o teste de parser CLI, os 4 testes da matriz de crash/import e os 4 testes do allocator M2
  passaram; Ruff e mypy estrito passaram. Nenhum browser real ou Send foi executado;
- `rendered_text_v1` agora extrai somente o container markdown folha dentro do assistant turn já
  provado, em vez de usar `inner_text()` no wrapper que também contém toolbar e ações. A
  cardinalidade exige exatamente um container visível e falha fechado em zero ou múltiplos; não
  há filtro por palavra, idioma ou regex, e `copy_text_v1` permanece inalterado. A rota read-only
  `browser assistant-response-spike` aguarda a conversa/ordinal hidratados por duas leituras,
  captura somente metadados e não clica nem persiste. Na conversa real, o writing block continha
  um markdown externo com ações e um `.ProseMirror.markdown.prose` interno; o selector folha
  isolou 9.933 caracteres, SHA
  `05cdc78a17a3c0d518202e91ff7f62357f0177a8d382d100210c2f364d014762` e confirmou
  `starts_with_expected_prefix=true` para `Em ambientes empresariais`. Os 120 testes unitários do
  adapter e 17 testes focados de integração passaram; Ruff e mypy estrito passaram. O browser foi
  aberto apenas para o spike read-only da conversa existente, sem Send ou mutação SQLite;
- foi adicionada a operação explícita `browser interaction recapture-response PROJECT
  INTERACTION_ID` somente para `unit_request` `captured`/`imported` com conversa, preparation,
  response Artifact e ordinal `persisted_unit_ordinal_v1` integralmente vinculados. A operação
  abre apenas a conversa canônica, relê de forma estável o assistant turn ordinal com o
  `rendered_text_v1` corrigido e não possui caminho de composer, preparação ou Send. SHA idêntico
  reutiliza a captura/import existentes sem criar Artifact; SHA diferente importa primeiro pelo
  mesmo `import_unit_by_preparation_id` idempotente e só então troca atomicamente o ponteiro
  corrente da interação para um novo response Artifact, preservando Artifact/submission/raw
  históricos e registrando IDs/hashes antigos e novos em evento append-only. O allocator M2
  fornece a próxima raw version monotônica; o teste dirigido preservou a raw v2 contaminada,
  materializou a limpa como v3 e comprovou que a repetição não cria nova raw ou Artifact. As 122
  provas unitárias do adapter, 13 do parser e 52 de integração do browser passaram (187 no total);
  Ruff e mypy estrito passaram. Nenhum browser real ou Send foi executado;
- o allocator canônico de `WritingUnitPreparation` agora reserva `version` no namespace físico
  real dos artifacts, `project_id + unit_id`, sob a mesma transação `BEGIN IMMEDIATE` que cria o
  StageRun e a preparation. A identidade idempotente completa por contexto/projeção/dependências/
  request permanece anterior à alocação, portanto uma repetição exata reutiliza a preparation
  existente. Uma preparation diferente em outro WritingContext recebe a próxima versão global e
  request/manifest compartilham o mesmo diretório `text/preparations/<unit>/vNNNN`, sem clobber. O
  teste dirigido preservou `CH01_A/v0001`, criou `v0002` após a mudança de contexto, reutilizou
  `v0002` na repetição e comprovou que o manifest manteve o binding `continuity_from: INTRO`. Os 3
  testes focados de projeção e 29 testes de regressão M2 passaram; Ruff passou, assim como mypy
  estrito nos 65 arquivos de produção e no teste alterado. Nenhum browser real ou Send foi
  executado;
- a recuperação M2 de preparation agora reconhece exclusivamente o legado estrito do allocator
  antigo: outro WritingContext possui a mesma `project_id + unit_id + version` concluída e com
  request/manifest registrados e íntegros, enquanto a preparation posterior permanece `running`,
  sem `finished_at`, sem artifacts próprios, sem sucessora e com os mesmos bindings ainda capazes
  de reproduzir exatamente `input_hash`, `request_sha256` e `manifest_sha256`. Nesse único caso, o
  mesmo registro incompleto é realocado para a próxima preparation version global e materializa
  request/manifest pelo renderer canônico compartilhado; não cria preparation paralela, não
  altera o StageRun histórico nem sobrescreve a versão ocupada. A repetição retorna a mesma
  preparation concluída. A auditoria read-only do projeto real confirmou a preparation
  `20f6b4a1-50c5-4efa-9fe6-2042263e5359` nesse formato, com v1 histórica de outro contexto, zero
  artifacts próprios, nenhuma sucessora, dependência `INTRO` aceita v2 e projeção acadêmica v2;
  nenhuma recuperação real foi executada. Os 28 testes focados de recovery/allocator/bindings e 6
  regressões M2 adicionais passaram; Ruff completo, mypy nos 65 arquivos de produção e nos dois
  testes alterados passaram. Nenhum browser real ou Send foi executado;
- a recovery canônica de preparation M2 foi exposta por `ebook writing unit recover PROJECT_ID
  UNIT_ID`. A CLI apenas chama `WritingService.recover_unit_preparation`, que seleciona a
  preparation da unidade no WritingContext atual e delega ao mesmo `WritingRecovery`; nenhum
  critério de reconstrução ou realocação foi duplicado no parser/handler. O resultado estruturado
  informa preparation/context/unit, versão anterior e atual, StageRun/status/`finished_at`, os
  dois Artifacts materializados e se houve recovery nesta chamada. Uma repetição sobre a
  preparation já concluída valida a evidência persistida e retorna o mesmo resultado sem criar
  versão ou Artifact. Estados não recuperáveis propagam o último erro M2 específico, ou
  `WRITING_RECOVERY_REQUIRED` quando não existe evidência mais precisa. Os 38 testes focados de
  CLI/recovery/allocator passaram; Ruff e mypy passaram nos 65 arquivos de produção e nos três
  testes verificados. O comando não foi executado sobre o SQLite real; nenhum browser ou Send foi
  executado;
- a aquisição canônica do composer reutilizado agora trata qualquer `textarea` como estado
  transitório de hidratação e só admite como editor operacional um `div` visível, habilitado,
  editável, `contenteditable` e `role=textbox`. O adapter reacquire em cada poll por até 30
  segundos, clica/foca somente o contenteditable atual e exige duas confirmações consecutivas de
  `document.hasFocus()`, active element e metadata focada. Desaparecimento, detach ou substituição
  do nó reinicia apenas a estabilização; nenhum insert/native paste ou lookup de Send ocorre antes
  da prova. Os 8 testes focados do adapter passaram, incluindo textarea persistente até timeout e
  substituição do contenteditable; Ruff e mypy passaram nos dois arquivos alterados. Nenhum
  browser real ou Send foi executado;
- foi adicionada a rota diagnóstica estritamente read-only `browser unit-response-spike PROJECT
  INTERACTION_ID`, que valida os bindings persistidos em conexão SQLite `query_only`, deriva o
  ordinal somente de interações anteriores comprovadamente enviadas e audita o assistant turn
  existente sem clicar nem registrar conteúdo. Para CH01_B, o histórico provou ordinal 3 e o
  probe real encontrou 4 user turns, 4 assistant turns, exatamente um `.markdown.prose` visível,
  9.780 caracteres semânticos com SHA
  `f0cee77b9777537dd599bb6f0f30a392e91e0cfc28f9ff26c304fddf7edf1393`, zero indicador de
  geração, zero Stop, três controles pós-resposta e duas leituras estáveis do mesmo turn. O
  wrapper legado também ficou estável nesta observação, porém tinha 9.788 caracteres: oito caracteres
  de UI além do conteúdo semântico, com os três controles no mesmo subtree. O detector canônico
  de completion do reconcile deixou de depender desse `inner_text()` incidental e agora exige um
  único container semântico visível e não vazio, SHA estável em duas leituras, ausência de
  indicador de geração/Stop visível e presença estável dos controles pós-resposta; ordinal e
  cardinalidade anteriores permanecem inalterados e qualquer ambiguidade continua fail-closed. O
  SQLite permaneceu `blocked`, com os mesmos três eventos e sem response Artifact/timestamps. Os
  7 testes focados do adapter/detector, o teste do parser e 2 regressões de reconcile passaram;
  Ruff e mypy passaram nos arquivos alterados. O profile dedicado foi aberto somente para esse
  probe observacional; nenhum Send ou reconcile mutável foi executado;
- a divergência entre `browser unit-response-spike` e o reconcile de CH01_B foi auditada sem
  alterar critérios: antes, o probe e `inspect_reconciliation_unit_turn` implementavam avaliações
  DOM separadas e o timeout do serviço descartava a última observação. Ambos agora chamam
  `_evaluate_unit_response_completion`, que reacquire e restringe todos os locators ao mesmo
  `main` da conversa; o serviço passa explicitamente o path canônico esperado ao adapter, preserva
  as duas leituras estruturais/semânticas exigidas e expõe somente
  contagens, índices, comprimento, igualdade de SHA e sinais de completion. O timeout
  `BROWSER_RECONCILE_UNIT_RESPONSE_INCOMPLETE` registra esse snapshot com `elapsed_ms` e
  `failure_reason`. A validação read-only real do helper compartilhado na mesma `/c/<uuid>`
  explicou a falha: durante 98 polls o DOM expôs 3 user turns e 3 assistant turns, não os 4/4 do
  carregamento anterior; para ordinal 3 não houve `selected_user_index`, portanto a razão foi
  `ordinal_user_turn_missing`, antes de qualquer avaliação do assistant/semântica. O SQLite
  permaneceu bloqueado e sem response Artifact; nenhum Send ou reconcile mutável foi executado;
- a alternância estrutural da conversa de CH01_B entre 4/4 e 3/3 foi auditada pela rota
  read-only `browser conversation-structure-spike PROJECT INTERACTION_ID`, que usa a conversa
  canônica persistida em SQLite `query_only` e registra somente URL, contagens, IDs estruturais,
  controles/índices de branch, sinais de lazy rendering e controles pós-resposta. O probe não
  clica, não foca, não preenche e não envia; suas únicas ações na página são navegação, scroll e
  uma recarga solicitada para comparar o mesmo estado. A rota foi endurecida para não aceitar o
  shell 0/0 como conversa hidratada. Na observação válida, a URL permaneceu canônica, os snapshots
  antes e depois do scroll foram 4 user/4 assistant, a recarga retornou novamente 4/4 com os mesmos
  oito `data-message-id`, zero controles/índices/atributos explícitos de branch, zero indicadores
  visíveis de carregamento e três controles pós-resposta no último assistant visível. Assim, B e C
  foram refutados: o quarto par está no estado persistido servido e a leitura histórica 3/3 foi
  uma representação DOM incompleta durante reidratação (A). Nenhum detector de produção foi
  alterado. A interação continuou `blocked`, com três eventos, `sent_at`, `imported_at` e response
  Artifact nulos. Os 6 testes focados do adapter e o teste do parser passaram; Ruff e mypy passaram
  nos três arquivos de produção alterados. Nenhum Send ou mutação SQLite foi executado;
- a estabilização ordinal compartilhada por `inspect_reconciliation_unit_turn` e
  `unit-response-spike` agora trata cardinalidade abaixo de `ordinal + 1` em qualquer lado como
  `turn_cardinality_hydrating`, inclusive nas sequências 0/0 e 3/3 observadas antes de 4/4. Root,
  locators de user/assistant e a lista ordenada de turns são reobtidos em cada poll; somente a
  cardinalidade exata, roles alternados e duas assinaturas consecutivas iguais liberam a seleção
  dos índices do ordinal. A assinatura inclui todos os `data-message-id`/IDs disponíveis, de modo
  que troca de root com as mesmas identidades converge e troca de identidade reinicia a
  estabilidade. User/assistant/turns extras e ordem ambígua continuam `AMBIGUOUS` fail-closed. O
  timeout do reconcile durante path/root/cardinalidade/estabilização agora retorna
  `BROWSER_RECONCILE_UNIT_TURN_HYDRATION_TIMEOUT` com as últimas contagens, quantidade de IDs e
  estabilidade estrutural; timeouts posteriores continuam usando o erro semântico existente. O
  detector semântico, Send, composer, M2 e persistence não foram alterados. Os 141 testes completos
  do adapter e 3 testes focados de unit reconcile passaram; Ruff passou nos quatro arquivos
  verificados e mypy passou nos 67 arquivos de produção. Nenhum browser real ou Send foi executado;
- o reconcile de `unit_request` agora detecta exclusivamente a hidratação incompleta não-zero que
  permanece com a mesma assinatura de contagens por cinco segundos. Nesse caso realiza no máximo
  uma re-navigation read-only para o mesmo `/c/<uuid>` canônico através de `open_conversation`,
  descartando toda prova/locator anterior e reiniciando a aquisição estrutural. O shell 0/0 não
  inicia a janela; evolução espontânea 3/3 → 4/4 não recarrega; 4/5 continua ambíguo e encerra antes
  de reload. Se a única recarga continuar em 3/3 até o deadline global, permanece o erro
  `BROWSER_RECONCILE_UNIT_TURN_HYDRATION_TIMEOUT`. Logs, resultado de sucesso e evidência de erro
  expõem somente `hydration_reload_triggered`, `hydration_reload_count`, `pre_reload_counts` e
  `post_reload_counts`; nada disso é persistido. Os 14 testes estruturais focados do adapter e 7
  testes focados do unit reconcile passaram, incluindo 0/0 → 3/3 persistente → reload → 4/4,
  convergência sem reload, timeout após a única recarga e ambiguidade sem recarga. Ruff passou nos
  arquivos alterados e mypy passou nos 67 arquivos de produção. Nenhum browser real ou Send foi
  executado;
- a primitiva de recuperação de hidratação foi unificada com a rota diagnóstica comprovada. Antes,
  `conversation-structure-spike` mantinha a mesma Playwright `Page` e chamava
  `page.reload(wait_until="domcontentloaded")`, enquanto o reconcile reutilizava
  `open_conversation`, que fazia `page.goto(base_url + conversation_path,
  wait_until="domcontentloaded")`. Agora ambos chamam `reload_conversation`: o helper valida o
  `/c/<uuid>` atual, mantém a mesma `Page`, limpa somente provas/locators em memória, executa
  `page.reload(wait_until="domcontentloaded", timeout=self.timeout_ms)` e valida novamente o path;
  nenhum wait adicional ou nova Page é criado, e cada caller reacquire root/turns no poll seguinte.
  O limite de uma recarga automática no reconcile permanece inalterado. O probe read-only real
  pós-patch observou 3/3 antes do scroll, 3/3 depois do scroll e 4/4 após o helper compartilhado,
  com oito IDs estruturais, duas leituras estáveis e a mesma URL canônica. A interaction real
  permaneceu `blocked`, com três events, response Artifact/`sent_at`/`imported_at` nulos e o mesmo
  `updated_at`. Os 3 testes focados do helper/probe e 4 cenários de hydration reload passaram; Ruff
  passou nos seis arquivos verificados e mypy passou nos 67 arquivos de produção. Nenhum reconcile
  real, Send ou mutação SQLite foi executado;
- a prova estrutural pós-Send do happy path deixou de manter um avaliador próprio de user-count,
  busca ordinal e estabilidade. `_evaluate_unit_turn_structure` agora é o único helper canônico
  usado por `inspect_sent_turn_structure` e pelo completion compartilhado de probe/reconcile: em
  ambos reacquire a conversation root local, user/assistant turns e turns ordenados a cada poll;
  trata cardinalidade insuficiente como hidratação; recusa cardinalidade extra/roles ambíguos;
  estabiliza por duas assinaturas consecutivas contendo path, contagens, roles e IDs; e somente
  então expõe os índices `ordinal * 2` e `+1`. No happy path, `ordinal` continua sendo exatamente o
  baseline capturado antes de `send_attempt_started`; a contagem de pasted-text attachment é apenas
  uma extensão da mesma assinatura canônica, preservando duas leituras e a cardinalidade específica
  do big-paste. O detector/capture posterior da resposta, composer, paste, Send, M2 e persistence
  não foram alterados; incerteza continua bloqueando pelo fluxo existente. Os 146 testes completos
  do adapter e 11 regressões focadas de envio/reconcile passaram; Ruff passou nos três arquivos
  verificados e mypy passou nos 67 arquivos de produção. Nenhum browser real ou Send foi executado;
- foi adicionado o runner sequencial M3 `ebook browser run-writing PROJECT_ID`. O comando mantém
  uma única instância de `ChatGPTWebAdapter` — e, portanto, a mesma BrowserSession/Page — durante
  todo o batch. Antes de preparar qualquer unidade, o serviço procura `unit_request` bloqueada não
  abandonada e qualquer interação ativa; efeitos externos com `send_attempt_started` passam pelo
  reconcile observacional existente na mesma sessão e nunca por resend. Cada resultado importado
  força nova resolução do Production Set; somente a próxima `missing_accepted` com todas as
  dependências `current_compatible` é preparada. `accepted` continua o loop; um import M2
  `review_required` ligado à interaction corrente só é auto-confirmado pelo próprio serviço M2
  quando o validation report íntegro possui `error_count=0` e warnings positivos, usando exatamente
  a `raw_version` importada e recalculando o Production Set antes da próxima unidade. `rejected`,
  qualquer error count positivo, binding/integridade divergente e falha na confirmação encerram
  fail-closed sem preparar a próxima unidade. A disposição persistida e os comandos manuais M2 não
  mudaram. O resumo expõe `processed_units`, `recovered_interactions`, `accepted_units`, total de
  warnings e agregação por unidade/códigos, além de `blocked_interaction` e `stop_reason`; warnings
  idênticos aparecem uma única vez na lista de códigos. Os 8 testes focados do runner passaram,
  cobrindo duas unidades na mesma sessão/conversa, recálculo entre unidades, recovery anterior à
  próxima preparação, reconcile pós-Send sem reenvio, auto-confirmação warning-only, warnings
  consecutivos acumulados, rejeição sem confirmação e falha de auto-confirmação sem avanço. Ruff e
  o parser CLI passaram em uma seleção focada de 9 testes; as 8 regressões integrais de fluxo e
  validation report do M2 também passaram. Ruff passou em `src tests` e mypy estrito passou nos 67
  arquivos de produção. Nenhum browser real ou Send adicional foi executado;
- o binding estrutural de `unit_request` pós-Send/reconcile deixou de usar ordinal/cardinalidade
  global como autoridade. Antes de `send_attempt_started`, o adapter agora estabiliza em duas
  leituras a cauda local montada (`user` + `assistant`), exigindo IDs estruturais não vazios e
  persistindo path, roles, IDs e contagens apenas telemétricas no evento append-only da interação.
  `_evaluate_local_turn_successor` é o binder canônico compartilhado pelo happy path e pelo
  reconcile: reacquire root/turns em cada leitura, reencontra exatamente a âncora persistida e
  aceita somente o primeiro `user` seguido pelo primeiro `assistant`, ambos com IDs únicos e duas
  assinaturas locais estáveis. Turns históricos podem estar desmontados e as contagens globais
  podem diminuir sem afetar a prova; âncora ausente, IDs ausentes ou múltiplos sucessores continuam
  fail-closed. O reconcile não deriva mais o binding de interações/ordinais anteriores; o número
  pré-Send permanece somente como telemetria e compatibilidade de evidência. A mudança usa o JSON
  de eventos existente, sem migration/schema novo, e não altera composer, paste, Send, captura
  semântica ou M2. Os 20 testes focados do adapter e 15 testes focados do serviço passaram,
  incluindo histórico completo/parcial, cardinalidade decrescente, ambiguidade, desaparecimento
  da âncora, persistência pré-efeito, ausência de resend e compartilhamento do binder. Nenhum
  browser real ou Send foi executado;
- a completion e a captura `rendered_text_v1` de `unit_request` agora permanecem estritamente
  dentro do assistant successor já provado pelo binder local. O helper canônico
  `_scoped_assistant_content_snapshot` recebe esse wrapper exato, procura somente sua raiz
  semântica `.markdown.prose` de nível superior, exclui toolbar/controles do texto e expõe apenas
  metadata segura: message ID, cardinalidade/visibilidade, hierarquia pai-filho, comprimento, SHA
  e sinais de geração. Regiões Markdown aninhadas pertencentes à mesma resposta são capturadas por
  uma única raiz canônica; duas raízes independentes no mesmo assistant turn continuam
  `BROWSER_RESPONSE_CONTENT_CONTAINER_AMBIGUOUS`. O happy path deixou de declarar completion pelo
  `inner_text` do wrapper inteiro e agora, assim como o reconcile, exige o mesmo conteúdo semântico
  com SHA estável por duas leituras, ausência de geração/Stop e controles pós-resposta. A captura
  reacquire o assistant pelos IDs vinculados, confirma novamente o ID e o SHA provados e nunca
  consulta `.markdown.prose` de outros turns. Os 17 testes focados do adapter e 23 testes focados
  de integração passaram, incluindo vários assistant turns simultaneamente montados, raiz com
  regiões aninhadas, roots independentes fail-closed e compartilhamento entre happy path e
  reconcile. Ruff e mypy passaram nos arquivos alterados. Nenhum browser real ou Send foi
  executado;
- o recovery de `unit_request` em `sent` agora reconhece explicitamente o evento persistido
  `proof_kind=post_send_local_successor_v1`. Nesse estado ele valida a preparation/conversa e a
  `pre_send_turn_anchor`, exige o `selected_user_id` persistido, abre somente a conversa canônica e
  chama diretamente o binder local `inspect_reconciliation_unit_turn` seguido da captura semântica
  escopada `capture_reconciled_unit_response`; `inspect_turn(expected_fingerprint)` e qualquer rota
  de Send não são chamados. `request-placeholder-*` deixou de ser identidade autoritativa: no
  happy path novo ele é persistido como `selected_assistant_id=null` com
  `assistant_identity_state=unresolved`; evidência histórica que ainda contém o placeholder recebe
  a mesma interpretação. O recovery exige então um assistant ID real do binder, ligado ao mesmo
  `selected_user_id`, e registra o late binding em evento append-only
  `assistant_successor_late_bound` assim que a identidade estrutural é provada, antes de estabilizar
  e registrar a resposta. Se o evento
  `sent` já contém um assistant ID real, qualquer ID real diferente continua fail-closed. A resposta
  é registrada no Artifact da mesma interação e importada pela rota M2 existente; `imported`
  finaliza o StageRun como `done`. Prova local sem âncora/identidade ou divergência durante
  captura/import bloqueia a interação e finaliza o StageRun como `blocked`. Interações históricas
  `sent` sem o marcador de prova local continuam usando exatamente o recovery legado. Os 5 testes
  novos e 5 regressões correlatas passaram, cobrindo placeholder → assistant real, identidade real
  A → real B fail-closed, sucesso sem fingerprint/Send, binding incompleto, preservação do legado,
  reconcile explícito e crash recovery. Ruff passou em `src tests` e mypy passou nos 67 arquivos
  de produção. Nenhum browser real ou Send foi executado;
- o dead-end posterior desse recovery local foi removido sem alterar composer, binder, capture, M2
  ou schema. Uma `unit_request` com `proof_kind=post_send_local_successor_v1`,
  `pre_send_turn_anchor` no boundary `send_attempt_started`, `selected_user_id`, nenhum response
  Artifact e nenhuma resolução `operator_abandoned` continua recuperável quando fica `blocked`,
  inclusive se `send_observed`/`sent_at` não chegaram a ser persistidos. O predicate único usado
  por `browser reconcile`, `browser recover` e pelo runner aceita a prova local tanto no evento
  `sent` integral quanto no evento `blocked` posterior ao início do efeito externo;
- `selected_assistant_id=request-placeholder-*` é tratado como unresolved. A retomada reutiliza a
  âncora e o user ID persistidos, chama exclusivamente o binder local e a captura semântica
  escopada, exige um assistant successor real e unívoco, faz late binding e só então reabre o mesmo
  StageRun para `captured -> imported`, sem Send, nova tentativa ou prova legada por fingerprint.
  Ausência de `send_attempt_started`, user divergente, assistant ausente/ambíguo, response já
  capturada ou abandono continuam fail-closed. As 9 regressões focadas passaram, cobrindo reconcile
  manual, BrowserRecovery, runner após unidade aceita, placeholder, assistant ausente, boundary
  ausente, abandono e compatibilidade com o recovery local anterior. Ruff passou em `src tests` e
  mypy estrito passou nos 67 arquivos de produção. Nenhum browser real ou Send adicional foi
  executado;
- a estabilidade posterior à primeira prova local do assistant deixou de depender de o par
  user→assistant permanecer simultaneamente montado em todos os polls. Assim que o binder prova um
  assistant real, o serviço persiste `assistant_successor_late_bound` append-only antes da captura;
  o adapter mantém esse ID latched e as leituras seguintes resolvem diretamente o assistant único.
  Uma leitura em que o turno está desmontado apenas aguarda e preserva o contador; o mesmo ID + SHA
  em duas observações completas libera a captura, enquanto SHA diferente reinicia em uma leitura e
  assistant real conflitante bloqueia. A captura final continua revalidando ID e SHA, mas não exige
  o user montado. Restart após o evento reutiliza a identidade persistida sem Send; placeholder
  continua unresolved. Onze testes focados do adapter e dez do serviço/runner passaram, cobrindo
  desmontagem/reaparecimento, mudança de SHA, conflito de identidade, checkpoint/restart e ausência
  de latch para placeholder. Ruff passou em `src tests` e mypy estrito passou nos 67 arquivos de
  produção. Nenhum browser real ou Send adicional foi executado;
- em 2026-08-24, o usuário confirmou explicitamente que o M3 foi validado em browser real além do
  gate antigo de context load + INTRO + CH01_A. Esse gate foi substituído pela evidência real mais
  recente e não deve ser repetido como condição de fechamento;
- o cleanup final partiu do baseline Git `d8e2082`, alterou somente testes defasados e documentação
  e não abriu browser real nem modificou código de produção;
- o teste de recaptura agora materializa explicitamente a prova histórica
  `persisted_unit_ordinal_v1`, sem confundi-la com o binding corrente
  `persisted_unit_local_successor_v1`;
- o teste de conversa nova reconhece baseline estrutural válido `0/0` sem predecessor local; a
  âncora `user → assistant` continua obrigatória para `unit_request` em conversa reutilizada.

Verificação final de fechamento do M3:

- `508 passed` em Python 3.12, sem browser real;
- Ruff sem erros em `src` e `tests`;
- mypy estrito sem erros em 112 arquivos de `src` e `tests`;
- árvore Git do baseline estava limpa antes do cleanup e nenhum arquivo de produção foi alterado.

O M3 está `COMPLETE`. O gate antigo não deve ser reexecutado.

## M4 — Planejamento Visual

O M4 foi autorizado e está preparado para início, mas nenhuma implementação visual foi criada
neste cleanup. As fontes de verdade já lidas são `MILESTONES.md`, `ARCHITECTURE.md`,
`docs/SOURCE_WORKFLOW.md`, `docs/OMEGABRAIN_SPEC.md`, `docs/DATA_MODEL.md`, `QA.md` e o snapshot
imutável `prompts/omega_brain/visual_planning_v1.txt`.

Próximo escopo permitido:

- importar/capturar o planejamento visual preservando o bruto;
- parsear somente os campos canônicos em `VisualPlan` e `VisualFigure`;
- enriquecer e validar âncoras literais contra o texto consolidado;
- implementar persistência, versionamento, idempotência, recovery, schemas e testes do M4.

Continuam fora do escopo do M4: geração de imagens, Image Manager, renderer SVG/Python, Google Docs
e UI.

## Regra

Não avançar `CURRENT_MILESTONE` sem:
1. critérios de aceite satisfeitos;
2. testes;
3. atualização deste arquivo;
4. comando explícito do usuário.
