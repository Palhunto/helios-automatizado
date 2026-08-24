# AGENTS.md — Regras obrigatórias para agentes de código

## Ordem de leitura antes de alterar código

1. `PROJECT_STATE.md`
2. `MILESTONES.md`
3. `ARCHITECTURE.md`
4. `DECISIONS.md`
5. `docs/SOURCE_WORKFLOW.md`
6. `docs/OMEGABRAIN_SPEC.md`
7. `docs/DATA_MODEL.md`
8. `QA.md`
9. `LLM.md`
10. `SECURITY.md`
11. `prompts/registry.yaml`

Quando o milestone tocar comportamento do OmegaBrain, leia também o snapshot de prompt correspondente em `prompts/omega_brain/`.

## Ordem de autoridade

1. Instrução explícita mais recente do usuário.
2. `PROJECT_STATE.md`.
3. `MILESTONES.md`.
4. `DECISIONS.md`.
5. `ARCHITECTURE.md`.
6. `docs/OMEGABRAIN_SPEC.md`.
7. `QA.md`.
8. Demais documentos.

Os snapshots em `prompts/omega_brain/` são fonte de verdade sobre o **comportamento editorial atualmente usado**. Não os "melhore" silenciosamente.

Não reconcilie conflitos silenciosamente.

## Regra dura de milestone

Implemente **somente** o milestone em `CURRENT_MILESTONE`.

É proibido antecipar:
- UI final;
- Playwright antes de M3;
- Google Docs antes de M7;
- geração de imagens antes de M5;
- renderer SVG antes de M6;
- OpenAI API paga sem decisão explícita;
- abstrações especulativas sem necessidade atual.

Infraestrutura mínima de milestone futuro só pode ser criada se for estritamente necessária ao milestone atual e deve ser registrada em `PROJECT_STATE.md`.

## Princípio de arquitetura

> LLM produz significado; código garante invariantes.

O sistema deve preservar a lógica editorial do OmegaBrain, mas retirar do LLM tarefas mecânicas, verificáveis e recuperáveis.

## Fluxo canônico

1. `ACADEMIC_QUESTIONNAIRE`
2. `CONSOLIDATED_ANSWERS`
3. `ACADEMIC_PLAN`
4. `WRITING_CONTEXT_LOAD`
5. `TEXT_GENERATION` — 18 partes
6. `REFERENCE_RECONCILIATION`
7. `TEXT_CONSOLIDATION`
8. `VISUAL_PLANNING`
9. `VISUAL_ANCHOR_ENRICHMENT`
10. `VISUAL_VALIDATION`
11. `IMAGE_ROUTING`
12. `IMAGE_PRODUCTION`
13. `DOCS_FORMATTING`
14. `FINAL_QA`

## Prioridades de engenharia

1. Persistência.
2. Idempotência.
3. Retomada após falha.
4. Integridade de artefatos.
5. Validação determinística.
6. Logs úteis.
7. Só depois ergonomia.

## Idempotência

Executar a mesma unidade com os mesmos inputs não pode:
- duplicar conteúdo;
- sobrescrever artefato válido;
- repetir etapa já confirmada;
- regenerar imagem concluída;
- reenviar operação externa já confirmada.

Quando aplicável, a identidade da execução deve considerar:
- `project_id`;
- `stage_id`;
- `unit_id`;
- `input_hash`;
- `prompt_id`;
- `prompt_version`;
- `artifact_version`.

## Checkpoints

Persistir após cada unidade semanticamente concluída, incluindo no futuro:
- perguntas apresentadas;
- respostas consolidadas;
- planejamento acadêmico;
- contexto de redação carregado;
- cada uma das 18 partes;
- reconciliação de referências;
- planejamento visual;
- cada figura;
- formatação.

Nenhuma etapa longa pode depender apenas da memória do processo.

## Dados editoriais x metadados operacionais

Dados editoriais vêm do OmegaBrain e devem ser preservados.

Metadados operacionais são produzidos pelo software, por exemplo:
- IDs;
- hashes;
- status;
- tentativas;
- paths;
- timestamps;
- renderer;
- validações.

Nunca peça ao LLM para inventar metadado operacional que o código pode calcular.

## Artefatos

Não sobrescrever output válido silenciosamente. Reparos geram nova versão ou reprocessamento explicitamente registrado.

Snapshots de prompts usados em produção são imutáveis.

## Falha segura

Em falha:
1. preservar outputs anteriores;
2. não reiniciar o projeto;
3. marcar somente a unidade afetada;
4. registrar motivo;
5. permitir retomada;
6. não avançar dependentes.

## Determinístico antes de LLM

Use Python para:
- contar caracteres;
- localizar trechos literais;
- testar unicidade;
- calcular hashes;
- ordenar;
- nomear arquivos;
- detectar duplicatas;
- validar schemas;
- controlar estado;
- validar presença/ausência de exercícios;
- identificar marcadores estruturais;
- decidir se uma unidade terminou.

Use LLM apenas para trabalho semântico. Veja `LLM.md`.

## ChatGPT Plus e API

ChatGPT Plus é o motor criativo primário.

Não migrar para API paga sem decisão explícita.

Browser automation jamais deve burlar limites, CAPTCHA, controles de acesso ou mecanismos anti-abuso.

## Browser automation — quando M3 chegar

- Preferir Playwright.
- Não usar `sleep()` fixo como sincronização principal.
- Aguardar estados observáveis.
- Falhar explicitamente se a UI mudou.
- Não usar visão/LLM para clicar quando seletor determinístico basta.
- Separar estado de negócio do adapter de browser.
- Persistir evidência suficiente para evitar reenvio duplicado.

## Segurança

Nunca versionar:
- cookies;
- API keys;
- OAuth tokens;
- senhas;
- perfil autenticado de navegador.

## Banco

SQLite é a fonte de verdade operacional inicial. Mudanças de schema devem preservar dados e ter migração.

## Logs

Logs devem incluir `project_id`, etapa, unidade, ação e resultado. Nunca registrar segredos.

## Antes de declarar tarefa concluída

- rodar testes relevantes;
- rodar lint/typecheck se configurados;
- conferir escopo do milestone;
- atualizar `PROJECT_STATE.md`;
- atualizar docs/schemas afetados;
- não avançar milestone automaticamente.

## Anti-padrões proibidos

- retries infinitos;
- exceptions engolidas;
- monólitos sem separação;
- estado global implícito;
- parsing "criativo" que inventa campos;
- output crítico só no console;
- operação destrutiva escondida;
- UI antes do core;
- reescrita editorial não solicitada;
- modificar snapshot de prompt em uso;
- transformar pendência editorial em bloqueio técnico sem regra explícita.
