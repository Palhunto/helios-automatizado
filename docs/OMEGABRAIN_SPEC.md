# OMEGABRAIN_SPEC.md — Contrato funcional derivado dos prompts reais

## Objetivo

Registrar as regras de produto que devem sobreviver à migração de automação por interface para pipeline híbrido.

Este arquivo não substitui os snapshots de `prompts/omega_brain/`. Em conflito textual, consultar o snapshot e registrar decisão.

## A. Academic Planning

### A1. Questionnaire
- cinco perguntas fixas;
- primeira execução entrega apenas as perguntas;
- não planejar ainda.

### A2. Consolidated Answers
- responder todos os cinco campos;
- preservar marcadores editoriais;
- não produzir planejamento na mesma etapa.

### A3. Academic Plan
Pré-condições:
- cinco respostas disponíveis;
- autorização explícita.

Propriedades:
- estrutura adaptável à disciplina;
- rigor acadêmico;
- alinhamento objetivos/conteúdo/atividade/evidência;
- sem invenção de fonte institucional;
- pendências secundárias não devem necessariamente bloquear.

## B. Writing

### B1. Context load
Input:
- consolidated answers;
- academic plan.

Expected first output:
- quatro linhas de resumo;
- confirmação de contexto avaliado.

### B2. Sections
Exactly 18:
- 1 intro;
- 16 chapter halves;
- 1 conclusion.

### B3. Deterministic constraints
- 9k–10k chars;
- no list syntax;
- exercises only in B halves;
- no exercises in intro/conclusion;
- no operational "part" labels;
- paragraph opening variation.

### B4. Semantic constraints
- academic university textbook;
- theory + application;
- examples/cases when useful;
- explicit author-date for studies;
- recent literature prioritized;
- no simplification.

## C. Visual planning

Select concepts only with real pedagogical/communication gain.

Required editorial fields:
- number/name;
- page;
- section;
- exact position;
- main concept;
- conceptual synthesis;
- short justification;
- image objective;
- image type;
- complexity;
- independent prompt.

No fixed number of figures.

## D. Image style

Global:
- background `#FFFFFF`;
- deep dark blue structural;
- graphite text/lines;
- medium/light greys;
- soft petroleum green as sparse accent;
- clean sans serif;
- clear hierarchy;
- generous spacing;
- no heavy texture/noise;
- controlled density.

## E. Operational normalization

The software may add fields that do not alter editorial meaning:
- IDs;
- hashes;
- timestamps;
- versions;
- statuses;
- anchor text;
- renderer;
- file paths.

The software must not silently:
- rewrite academic content;
- turn premise into confirmed;
- change planned visual concept;
- invent bibliography;
- create extra figures to satisfy quota.
