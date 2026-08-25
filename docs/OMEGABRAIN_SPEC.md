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

The operational OmegaBrain workflow requires exactly one visual proposal for each eligible canonical
page in chapters 1–8. Within every required page, select the concept with the strongest real
pedagogical/communication gain. Mandatory coverage does not authorize decorative or semantically
empty figures.

Eligible pages come exclusively from a versioned `VisualPaginationSnapshot` bound by exact ID and
hash provenance to one `TextConsolidation`. Every eligible page has a stable `page_key`, global
order, typed chapter/unit mapping, and verifiable text ranges. Character-count page estimation is
forbidden. Introduction, conclusion, references, and pages outside chapters 1–8 are excluded.

The canonical M4.0 producer uses `helios_pagination_layout@1`, pinned licensed fonts and real
Chromium measurement over explicit A4 DOM pages. Chapter boundaries cause page breaks but never
inject a visible heading. A heading affects layout only when present in the source and explicitly
declared by the editorial layout. Every eligible page reserves its visual slot before image
production. The JSON manifest is operationally canonical; the PDF is visual evidence and is byte
deterministic only within the same exact input and `renderer_fingerprint`.

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

Operational fields include canonical `page_key`, typed chapter/unit links, and a separate versioned
anchor. The editorial `page` and `section` values are preserved as observed but do not establish
referential integrity.

Anchor uniqueness is local to the assigned page's source span and selected typed unit span. The
same literal may occur elsewhere in the ebook without invalidating that anchor.

For an accepted plan:

```text
figure_count == eligible_page_count
set(figure.page_key) == set(eligible_page.page_key)
count(figures where page_key == P) == 1 for every eligible page P
figure.number == eligible_page.order == 1..N
```

Zero figures are valid only when the compatible pagination snapshot contains zero eligible pages.
Promotion is an explicit finalize operation and produces `helios_visual_manifest@1`.

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
- canonical page/chapter/unit links;
- file paths.

The software must not silently:
- rewrite academic content;
- turn premise into confirmed;
- change planned visual concept;
- invent bibliography;
- estimate pagination from character count;
- omit an eligible page or create duplicate/out-of-scope page coverage;
- use required coverage as justification for a decorative or semantically empty figure.
