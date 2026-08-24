# QA.md — Invariantes e critérios de qualidade

## Invariantes globais

- unidade `done` não é repetida implicitamente;
- artefato válido não é sobrescrito silenciosamente;
- todo output crítico possui persistência;
- todo prompt usado possui `prompt_id`, versão e hash;
- falha de unidade não apaga unidades anteriores;
- parser não inventa campo ausente;
- DB e filesystem são reconciliáveis;
- segredo nunca entra em log/repositório.
- run `done` sem Artifact só é aceito por política database-only tipada que recomponha identidade
  e proveniência; qualquer operação não declarada continua inválida.

## QA — Fundação M0

- migration aplicada possui versão, nome e SHA-256;
- migration aplicada não pode ser alterada silenciosamente;
- slug e artifact root são únicos;
- paths persistidos são relativos e normalizados;
- `running` exige timestamp de início;
- `done`, `failed` e `skipped` exigem timestamp de término;
- tentativa nunca ultrapassa `max_attempts`;
- Artifact referencia o StageRun produtor;
- path de Artifact é único dentro do projeto;
- `validate` não altera estado;
- `recover` nunca marca `done` sem validar bytes e contrato;
- config inválida falha antes de criar projeto;
- logs JSONL possuem contexto e redaction.
- configuração histórica de projeto não é editada; snapshots runtime são versionados, encadeados
  por SHA, registrados como Artifact e resolvidos de forma fail-closed.

## QA — Browser M3

- interrupção durante `sending` nunca prova envio nem autoriza reenvio;
- conversation path isolado nunca promove `sending` para `sent`;
- `sent` exige user turn correspondente ao fingerprint observado no DOM após polling pelo timeout
  configurado;
- user turn textual preserva a prova integral existente; se o turno contém pasted-text attachment,
  preview, título, `aria-label`, existência do card, composer limpo e URL são insuficientes. O
  attachment do próprio turno precisa abrir, expor o conteúdo integral e produzir exatamente o
  `transport_fingerprint` esperado;
- attachment ainda carregando mantém o polling limitado e reacquire dos turnos; attachment
  inacessível, conteúdo divergente ou múltiplos candidatos não desambiguáveis falham fechado, sem
  captura da resposta;
- o fingerprint pré-send é somente expectativa; path `/c/<uuid>`, fingerprint observado e
  `sent_at` são persistidos atomicamente depois da prova DOM e o path vem de `page.url` em
  `https://chatgpt.com`;
- envio normal registra `composer_found`, `composer_filled`, `send_button_found`,
  `send_button_enabled`, `send_trigger_started`, `send_trigger_completed`, `composer_cleared` e
  `user_turn_observed` nas respectivas fronteiras observáveis;
- o locator do composer resolve exatamente um editor visível/editável; antes da inserção e depois
  do foco, DEBUG registra somente tag, `contenteditable`, role, id/data-testid,
  `isContentEditable`, foco e quantidade de descendentes editáveis, nunca o conteúdo editorial;
- no paste nativo, foco DOM não prova foco nativo: o bounding box do composer é convertido para o
  centro seguro em coordenadas de tela do HWND dedicado comprovado, `LEFTDOWN/LEFTUP` são emitidos
  por Win32 `SendInput` e o `activeElement` é reverificado antes de permitir Ctrl+V; falha em obter
  geometria ou concluir o clique bloqueia o Ctrl+V;
- `textarea`/`input` são relidos por `value` e `contenteditable` por texto do editor; somente duas
  observações estáveis cujo `transport_fingerprint` integral coincide com o request produzem
  `composer_filled` e permitem procurar o botão Send;
- após `paste_key_completed`, a verificação não lê imediatamente: reobtém o editor real em cada
  poll e aceita duas provas mutuamente suficientes — duas leituras textuais integrais consecutivas
  ou exatamente um novo pasted-text attachment, contado no formulário do composer pelo botão cujo
  `aria-label` começa por `Abrir anexo de texto colado`, sem leitura de conteúdo, demonstrado por
  baseline pré-Ctrl+V e `pasted_text_attachment_count_after =
  pasted_text_attachment_count_before + 1`; attachment preexistente, delta zero ou múltiplo não
  prova o paste. Timeout permanece pré-send e não limpa o composer;
- antes de inserir request não-prefilled de uma interaction ainda `prepared`, o adapter reobtém o
  composer e exige `composer_text_length = 0` e `pasted_text_attachment_count = 0`. Draft textual
  ou pasted-text attachment sobrevivente é limpo pela mesma rotina comprovada do composer spike e
  precisa retornar ao baseline `0/0`; falha gera `BROWSER_COMPOSER_STALE_DRAFT_CLEAR_FAILED` antes
  de qualquer paste, resolução de Send ou `send_attempt_started`;
- após `send_button_found`, o adapter reobtém o botão em cada poll por no máximo 30 segundos;
  existência disabled, invisibilidade, desaparecimento e troca transitória do nó não disparam
  clique nem limpeza e reiniciam a estabilidade. Somente duas observações consecutivas visible e
  enabled produzem `send_button_enabled`; timeout gera
  `BROWSER_SEND_BUTTON_ENABLE_TIMEOUT` estritamente pré-send. Falha do clique já autorizado e
  composer não limpo continuam produzindo erros distintos;
- recovery nunca preenche composer nem chama `send_message`; interação `prepared` permanece intacta
  e requer execução explícita fora do recovery;
- recovery ambíguo termina em `blocked` e expõe o `interaction_id` ao operador;
- uma `unit_request` bloqueada com prova `post_send_local_successor_v1`, âncora persistida no
  boundary anterior `send_attempt_started`, user ID local real, sem response Artifact e sem
  abandono permanece elegível à captura/reconcile pelo binder local, mesmo quando `send_observed`
  não chegou a ser persistido; `request-placeholder-*` no assistant é identidade unresolved e
  exige late binding de um successor real;
- essa retomada não chama Send nem a prova legada por fingerprint e só reabre o StageRun depois de
  o mesmo user e um único assistant successor real serem provados; ausência do boundary/âncora/user
  ID, divergência do user, assistant ausente/ambíguo, captura já existente ou abandono continuam
  fail-closed;
- depois da primeira prova estrutural de um assistant successor real, o recovery registra seu ID em
  evento append-only antes da captura e passa a estabilizar o conteúdo por assistant message ID +
  semantic SHA; ausência temporária do turno no DOM não confirma, invalida nem incrementa a
  estabilidade, e a captura final resolve o assistant pelo ID latched sem exigir o user montado;
- reaparecimento do mesmo assistant com SHA diferente reinicia a estabilidade sem aceitar conteúdo;
  um assistant real diferente para o mesmo user permanece conflito fail-closed, e identidade
  `request-placeholder-*` nunca é latched como real;
- `continue` não ultrapassa interação bloqueada sem resolução explícita;
- abandono só aceita interação `blocked`, preserva interação, StageRun, events, artifacts e hashes;
- resolução registra operador, motivo e timestamp sem afirmar `not_sent`;
- nova tentativa cria BrowserInteraction e StageRun sucessores, sem reutilizar `retry`;
- quando não há interaction ativa e a última `context_load` bloqueada foi explicitamente
  `operator_abandoned`, uma nova execução inicia outra linhagem com nova conversation, novo
  StageRun e interaction `prepared` em `attempt = 1`, mantendo o `max_attempts` configurado e
  apontando `supersedes_interaction_id` para a abandonada;
- a nova linhagem não altera attempt, StageRun, events, resolution, conversation ou Artifacts
  históricos; blocked sem resolução e estados `sending|sent|streaming|captured` continuam impedindo
  a criação;
- cleanup após Ctrl+C libera profile/Playwright sem mascarar o estado persistido com cascata de
  `TargetClosedError`.
- `BROWSER_SEND_NOT_PROVABLE` bloqueado, com path/fingerprint íntegros e sem abandono, é elegível
  para reprobe explícito; resposta encontrada é capturada/importada sem chamar `send_message`.
- recovery informa itens examinados, recuperados e ignorados, incluindo o motivo de cada skip.
- recovery recusa `/c/WEB:*`, IDs DOM/message e outros paths não canônicos sem navegar, reenviar ou
  alterar o histórico persistido.
- `browser reconcile PROJECT INTERACTION_ID` aceita somente interaction `blocked`, não abandonada e
  com último erro `BROWSER_SEND_NOT_PROVABLE`; não preenche composer, não aciona Send/Enter, não
  cria interaction, não incrementa attempt e não usa o fluxo de abandono;
- reconcile observa primeiro a conversa restaurada e só então candidatos posteriores a
  `send_attempt_started`; todo `/c/<uuid>` candidato é validado pelo fingerprint integral do
  primeiro turno, nunca por título/data, e `/c/WEB:*` é recusado;
- pasted-text attachment pode ser aberto somente para recuperar a representação necessária à prova
  do fingerprint, sem editar, enviar ou registrar conteúdo; attachment inacessível falha fechado;
- reconcile persiste path/fingerprint/tempo, Artifact/SHA `rendered_text_v1` e acknowledgement M2
  apenas depois de resposta correspondente completa e estável; reutiliza a mesma interaction,
  attempt, `context_id` e StageRun;
- zero candidatos comprováveis, mais de um candidato não desambiguado, fingerprint divergente ou
  resposta ausente/incompleta preservam integralmente a interaction `blocked` pré-reconciliação;
- conversa `ready` com path inválido nunca é reutilizável; enquanto houver interaction sem resolução
  explícita, nova conversa é recusada fail-closed;
- após todas as interactions inválidas serem abandonadas, um ledger append-only registra
  `legacy_invalid_conversation_path`, preserva a conversa antiga e cria outra `provisioning` para o
  novo envio;
- status expõe a conversa histórica como `reusable=false`; path, fingerprint e events originais não
  são reescritos.
- falha de composer/fill anterior a `send_trigger_started` mantém interaction `prepared`, StageRun
  `pending` e somente boundary `pre_send`; repetir a mesma operação não duplica interaction;
- imediatamente antes do clique, `send_attempt_started` promove atomicamente para `sending`; somente
  prova DOM promove para `send_observed`/`sent`;
- `interaction show ID` é exato; sem ID seleciona uma única interaction ativa, ignora abandonadas e
  falha com IDs candidatos quando houver mais de uma ativa;
- `browser status` lista `active_interactions` com seus IDs, conversation IDs e estados.
- `browser composer-spike PROJECT REQUEST_ARTIFACT` é headed/Chrome-only, resolve o WritingContext
  proprietário do Artifact ID explícito por conexão SQLite `mode=ro/query_only` e testa, nessa
  ordem, payload curto, multiline/Unicode,
  grande e o request artifact exato;
- o composer spike limpa draft inicial e cada payload com prova de editor vazio e de retorno da
  contagem de pasted-text attachments ao baseline; a remoção fica restrita ao `role=group` do
  controle `Abrir anexo de texto colado` e ao seu único botão `Remover ficheiro`; não chama
  `send_message`, não resolve/clica Send, não usa Enter e não cria BrowserInteraction, StageRun ou
  event;
- cada caso do composer spike expõe somente comprimentos, fingerprints, duas leituras estáveis e
  metadados DOM; payload editorial não aparece em stdout nem logs.
- `browser run-writing` preserva a disposição M2 `review_required`, mas quando a submission exata
  importada pela BrowserInteraction possui validation report íntegro com `error_count = 0` e
  warnings positivos, registra os warnings, confirma diretamente essa `raw_version` pelo serviço
  M2, recalcula o Production Set e só então prepara a próxima unidade;
- o summary do runner acumula total de warnings, quantidade por unidade e códigos únicos por
  unidade; `review_required` causado somente por warnings não é `stop_reason`;
- `rejected`, qualquer `error_count > 0`, binding/integridade divergente ou falha durante a
  auto-confirmação nunca chama Send para a unidade seguinte e encerra o runner fail-closed;

## QA — Planejamento acadêmico

- exatamente cinco perguntas observadas, não vazias e ordenadas;
- numeração explícita 1→5 é aceita; na ausência total de numeração, exatamente cinco blocos
  inequívocos são mapeados por posição 1→5;
- ordem 1→5 preservada;
- comparação com as cinco perguntas canônicas usa normalização tipográfica e de whitespace;
- divergência de redação estruturalmente válida gera `review_required`, não rejeição;
- confirmação explícita gera `review_confirmed` sem substituir a redação observada;
- cada item aceito preserva texto observado, texto canônico e vínculo do prompt;
- respostas consolidadas armazenadas separadamente;
- marcadores reconhecidos somente entre:
  - `CONFIRMADO`;
  - `COMPLEMENTO_PROPOSTO`;
  - `PREMISSA_EDITORIAL`;
  - `PENDENTE`;
  - `CONFLITO`;
- planejamento não pode ser marcado `done` sem:
  - respostas consolidadas;
  - autorização registrada;
- `[CONFLITO]` bloqueia autorização; `[PENDENTE]` não bloqueia;
- sugestão/premissa não pode ser convertida em confirmado pelo software;
- informação removida não deve reaparecer por merge automático.

## QA — Redação

Unidades, ordem, limites, continuidade e regras estruturais vêm exclusivamente do Writing
Production Contract resolvido por ID/versão/hash. O contract canônico V1 contém `INTRO`,
`CH01_A` ... `CH08_B` e `CONCLUSION`; contracts alternativos não exigem branches na engine. No
V2, `character_count.target` é a faixa enviada ao LLM e `character_count.accepted` é a faixa usada
para aceitação: a segunda deve conter a primeira e não há warning somente por o valor ficar entre
as duas fronteiras.

Para cada unidade aceita:
- contagem normalizada para medição dentro da faixa `accepted` declarada pelo contract;
- conteúdo não vazio;
- headings obrigatórios/proibidos e heading final obedecem ao contract;
- listas/enumerações de alta confiança são recusadas somente quando proibidas;
- meta-rótulos explicitamente proibidos pelo contract são recusados;
- preparação registra exatamente as accepted submissions/artifacts/hashes de `continuity_from`;
- aberturas de parágrafo geram warnings/review conservadores, nunca hard errors no contract V1;
- output bruto é preservado mesmo quando rejeitado.

O raw aceito e seu artifact accepted são byte a byte idênticos. `StageRun.done` não implica
`TextUnitSubmission.accepted`.

`writing unit show --raw` precisa expor o validation report persistido da mesma raw version após
verificar Artifact ID e SHA. Essa leitura é somente leitura e nunca confirma uma revisão.

O Production Set é derivado topologicamente e distingue `current_compatible`, `missing_accepted`
e `historical_incompatible`. Ele só é `current` se estiver completo e seu WritingContext ainda
apontar para Answers + Plan atuais do M1.

## QA — Citações e referências

- ledger por accepted submission com raw, autor observado/normalizado, ano, offsets, ordem e regra;
- citações detectadas não são descartadas;
- software não completa metadados bibliográficos por adivinhação.

`Citation Ledger != Reference Reconciliation`. A reconciliação bibliográfica completa permanece
pendente para milestone posterior.

## QA — Planejamento visual

Cada figura aceita deve preservar:
- número/nome;
- página quando informada;
- seção;
- posição exata;
- conceito principal;
- síntese conceitual;
- justificativa;
- objetivo;
- tipo;
- complexidade;
- prompt independente integral.

Valores canônicos de complexidade:
- `Editorial direta`;
- `Editorial estruturada`;
- `Síntese conceitual`.

Além disso, antes de geração:
- `anchor_text` operacional existe;
- anchor é validável no texto consolidado;
- `position_relative_to_anchor` é `before` ou `after`;
- não existe quota de figuras por página;
- figura decorativa não deve ser criada somente para preencher espaço.

## QA — Imagens

- `figure_id` único;
- prompt hash estável;
- nome de arquivo determinístico;
- arquivo válido;
- file hash registrado;
- rerun não gera novamente `done`;
- retry limitado;
- conflito não sobrescreve arquivo existente.

## QA — Docs

- texto aprovado não é reescrito;
- figura inserida no ponto planejado;
- nenhuma figura duplicada;
- estilos idempotentes;
- rerun não duplica legendas/cabeçalhos/quebras;
- erro parcial mantém estado recuperável.

## Severity futura

- `critical`: impede `COMPLETE`;
- `error`: unidade falhou/precisa reparo;
- `warning`: revisão recomendada sem impedir necessariamente;
- `info`: evidência operacional.
