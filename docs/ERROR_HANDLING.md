# ERROR_HANDLING.md

## Categorias

### ValidationError
Contrato determinístico inválido. Não avançar; reparar apenas unidade afetada.

Exemplos:
- texto fora da faixa `accepted` do Writing Production Contract;
- seção de exercícios em unidade errada;
- âncora ausente;
- schema inválido.

### SemanticRepairRequired
Output é estruturalmente processável, mas viola requisito semântico que não pode ser corrigido deterministicamente.

### IntegrationError
Serviço externo indisponível. Preservar estado; retry limitado; depois `blocked`/`pending_retry`.

### ArtifactError
Arquivo ausente/inválido. Nunca marcar `done` sem evidência.

`DONE_RUN_WITHOUT_ARTIFACT` continua sendo o default para qualquer operação concluída sem
Artifact. Uma operação database-only reconhecida mas com entidade, hash ou proveniência
divergente produz `DONE_RUN_DATABASE_EVIDENCE_INVALID`.

### ConflictError
Destino ocupado/estado incompatível. Nunca sobrescrever silenciosamente.

### AuthError
Sessão/credencial inválida. Bloquear somente integração afetada.

### EditorialConflict
Marcador `[CONFLITO]` ou incompatibilidade editorial real.
Nem todo `[PENDENTE]` é bloqueante.

### AcademicValidationError
Input acadêmico viola contrato mecânico (UTF-8, quantidade, ordem, âncora, marcador ou conteúdo
não vazio). O bruto é preservado quando a operação já possui identidade; o run é bloqueado sem
invalidar documentos aceitos anteriores. Divergência apenas de redação no questionário não entra
nesta categoria: produz `review_required`.

### WritingValidationError
Input textual viola uma invariante objetiva (UTF-8, vazio, limite, heading, lista proibida ou
estrutura final). Achados linguísticos conservadores produzem warning/`review_required`, não hard
error no contract V1. O raw é preservado e um run mecanicamente `done` pode corresponder a uma
submission `rejected`.

## Retry

- sempre limitado;
- sempre registrado;
- sem duplicar efeitos já confirmados;
- retry técnico usa mesmos inputs;
- mudança de prompt/input cria nova identidade/versão;
- nunca infinito.

## Recovery de `running`

Após crash:
1. verificar se existe artefato completo;
2. validar hash/contrato;
3. se válido, reconciliar para `done`;
4. se não, `pending_retry`;
5. nunca assumir sucesso apenas pela existência de arquivo parcial.

Nos milestones M0–M2, a operação é explícita:

- `ebook project validate <project_id>` apenas relata inconsistências;
- `ebook project recover <project_id>` reconcilia runs `running`;
- artefato comprovadamente íntegro permite `done`;
- artifact ausente ou temporário leva a `pending_retry`;
- arquivo final divergente leva a `blocked`.
- runs acadêmicos são reconciliados a partir de bruto, prompt congelado, proveniência e bytes
  esperados; confirmação de revisão também é recuperável.
- runs de writing recompõem contexto, acknowledgement, preparação, submissão, Citation Ledger,
  confirmação e consolidação somente quando arquivos, hashes, validação e proveniência conferem;
  ausência leva a `pending_retry` e divergência/corrupção a `blocked`.

No M3, `BrowserInteraction` — não `StageRun` — é a autoridade do efeito externo. Recovery usa
conversation path, request SHA, transport fingerprint, turno do usuário, response artifact e
identidade M2. Estado ambíguo, sessão expirada, login, challenge/CAPTCHA, conversa errada, response
vazia, timeout ou SHA divergente bloqueiam a interação. Na janela de bootstrap, somente um path
novo fora do baseline pré-send e com primeiro turno comprovado pode ser vinculado; nunca há reenvio
silencioso.

Configuração runtime inválida falha antes de abrir o browser. Gaps de versão, path divergente,
snapshot malformado, cadeia SHA quebrada, source config divergente ou run incompatível usam códigos
`PROJECT_RUNTIME_*`; nenhuma dessas falhas provoca fallback silencioso ao YAML histórico.

O capture spike diferencia: composer ausente (`BROWSER_SPIKE_COMPOSER_NOT_FOUND`), disparo não
efetivado (`BROWSER_SPIKE_PROMPT_NOT_SENT`), user turn não observado
(`BROWSER_SPIKE_USER_TURN_NOT_OBSERVED`), resposta não iniciada
(`BROWSER_SPIKE_RESPONSE_NOT_STARTED`), timeout de geração (`BROWSER_SPIKE_RESPONSE_TIMEOUT`),
assistant turn ausente/ambíguo (`BROWSER_SPIKE_ASSISTANT_TURN_NOT_FOUND`) e resposta vazia
(`BROWSER_SPIKE_RESPONSE_EMPTY`). Em DEBUG, checkpoints não editoriais mostram cada fronteira do
fluxo sem registrar prompt ou resposta.

O composer spike é diagnóstico read-only e nunca persiste ErrorRecord. Channel/headless inválido
usa `BROWSER_COMPOSER_SPIKE_REQUIRES_*`; falha de inserção ou limpeza usa
`BROWSER_COMPOSER_SPIKE_FILL_FAILED`/`BROWSER_COMPOSER_SPIKE_CLEAR_FAILED`. Editor ausente,
ambíguo, sem foco ou com fingerprint divergente reutiliza os erros fail-closed do composer normal.
Evidência contém somente comprimentos, fingerprints e metadados DOM, nunca payload.
Inserção segmentada que não comprova o prefixo integral usa
`BROWSER_COMPOSER_CHUNK_NOT_OBSERVED` e interrompe antes do próximo bloco.

Erros persistidos ficam em `ErrorRecord`. Falhas anteriores à criação do projeto são emitidas
somente no terminal/log, pois ainda não existe identidade persistível.

## Mensagem de erro mínima

- projeto;
- etapa;
- unidade;
- código;
- falha;
- recuperável ou não;
- evidência;
- próxima ação segura.
