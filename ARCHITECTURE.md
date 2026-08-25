# ARCHITECTURE.md

## Visão

```text
Project Manager
      ↓
Workflow Engine
 ├─ State Store (SQLite)
 ├─ Artifact Store (filesystem)
 ├─ Prompt Registry
 ├─ Validators
 ├─ Academic Planning Domain [M1]
 ├─ Text Production Domain   [M2]
 ├─ Browser Adapter          [M3]
 ├─ Visual Planning Domain   [M4]
 ├─ Image Manager            [M5]
 ├─ Image Production Adapter [M6]
 ├─ Google Docs Adapter      [M7]
 ├─ Final QA                 [M8]
 └─ UI                       [M9]
```

## Princípio central

O workflow editorial já existe no OmegaBrain. O software não deve reinventá-lo.
O objetivo é converter controle operacional frágil em estado, contratos, validações e adapters.

> LLM produz significado; código garante invariantes.

## Pipeline final canônico

```text
CREATE_PROJECT
    ↓
ACADEMIC_QUESTIONNAIRE
    ↓
CONSOLIDATED_ANSWERS
    ↓
ACADEMIC_PLAN
    ↓
WRITING_CONTEXT_LOAD
    ↓
TEXT_GENERATION
    ├─ INTRO
    ├─ CH01_A
    ├─ CH01_B
    ├─ ...
    ├─ CH08_A
    ├─ CH08_B
    └─ CONCLUSION
    ↓
REFERENCE_RECONCILIATION
    ↓
TEXT_CONSOLIDATION
    ↓
VISUAL_PAGINATION
    ↓
VISUAL_PLANNING
    ↓
VISUAL_ANCHOR_ENRICHMENT
    ↓
VISUAL_VALIDATION
    ↓
IMAGE_LIFECYCLE_PREPARATION
    ↓
IMAGE_PRODUCTION
    ↓
DOCS_FORMATTING
    ↓
FINAL_QA
    ↓
COMPLETE
```

## Por que existe `VISUAL_ANCHOR_ENRICHMENT`

O prompt visual canônico exige "Posição exata no texto", mas não exige sempre um trecho literal único.
Para automação confiável do Google Docs, o sistema precisa de uma âncora verificável.

A proposta editorial original é preservada e, depois, uma subetapa operacional obtém:
- `anchor_text` literal;
- `position_relative_to_anchor`;
- opcionalmente contexto anterior/posterior.

Python aceita a âncora apenas se ela existir no texto consolidado e for suficientemente discriminante.
Além disso, o offset literal precisa pertencer aos intervalos da mesma `page_key` e unidade tipada
da figura no `VisualPaginationSnapshot` compatível.
Unicidade é avaliada somente dentro desse source span de página + unidade; repetição do mesmo
literal em outra página do ebook não invalida a âncora.

## Por que existe `VISUAL_PAGINATION`

M4 exige exatamente uma figura para cada página elegível dos capítulos 1–8. O conjunto de páginas
não pode ser inferido silenciosamente por contagem de caracteres. Um `VisualPaginationSnapshot`
imutável referencia a `TextConsolidation` exata e congela a ordem, `page_key`, capítulo, unidades e
intervalos verificáveis no texto consolidado. Introdução, conclusão, referências e páginas externas
aos capítulos 1–8 ficam fora de `eligible_pages`.

M4.0 produz esse snapshot localmente por `helios_pagination_layout@1`. Chromium mede o layout real
em folhas DOM A4 explícitas e depois exporta o mesmo conjunto para PDF; não decide páginas por
estimativa de caracteres. Cada página elegível já reserva seu slot visual. Boundary de capítulo é
estrutural e não injeta heading: somente headings existentes no source e declarados pelo layout
editorial alteram a composição visível. Font assets e licenças são congelados por SHA sem fallback
do sistema. O manifest JSON é a fonte operacional; HTML e PDF são artifacts verificáveis.

## Responsabilidades

### Project Manager
Cria ID, valida config, cria diretórios e impede mistura entre projetos.

### Workflow Engine
Controla dependências, próxima unidade executável, transições, pausa, retomada e retries limitados. Não contém lógica editorial.

### State Store
SQLite guarda estado operacional, hashes, tentativas, referências a artefatos e eventos essenciais.

### Artifact Store
Filesystem guarda prompts congelados por projeto, respostas brutas, outputs aceitos, textos, planos, imagens, exports e logs.

### Prompt Registry
Resolve `prompt_id + version → arquivo + sha256`. Nunca retorna "última versão" implicitamente para um projeto já iniciado.

### Academic Planning Domain
Representa:
- perguntas padronizadas;
- respostas consolidadas;
- marcadores de origem/status;
- autorização para planejar;
- planejamento acadêmico aceito.

### Text Production Domain
É uma engine dirigida por Writing Production Contracts versionados. O contract define unidades,
ordem, DAG `continuity_from`, limites, headings, política de listas e separator. O domínio mantém
WritingContext imutável, preparação com proveniência exata, raw/accepted independentes, Citation
Ledger, Production Set derivado e consolidação byte a byte. O primeiro contract possui 18 partes;
a engine não conhece esse número nem IDs específicos.

O prompt canônico de redação é congelado e enviado apenas no pacote inicial do contexto. Depois do
acknowledgement e da confirmação explícita, cada unidade recebe um request operacional separado,
destinado à mesma conversa já inicializada.

### Browser Adapter
Opera ChatGPT Plus quando M3 estiver ativo. Não decide regra de negócio.

### Visual Planning Domain
Preserva o planejamento visual editorial, importa a paginação canônica, exige cobertura 1:1 das
páginas elegíveis, adiciona ligações operacionais de página/capítulo/unidade, valida âncoras e
finaliza explicitamente `helios_visual_manifest@1`.

### Image Manager
No M5, controla lifecycle, versões, batches, hashes, estados, tentativas e artifacts por `visual_id`,
sem executar a produção real.

### Image Production Adapter
No M6, produz imagens via GPT, captura/download, reconcilia por `visual_id`, repete somente
`missing|failed` e valida completude. Renderer determinístico pode existir apenas como fallback
excepcional explícito e não controla o workflow.

### Google Docs Adapter
Aplica operações determinísticas e idempotentes de formatação/inserção.

## Estados de unidade

- `pending`
- `running`
- `done`
- `failed`
- `pending_retry`
- `blocked`
- `skipped`

Transições inválidas geram erro.

```text
pending → running → done
pending → running → failed → pending_retry → running → done
pending → blocked
```

`done → running` somente via reprocessamento/versionamento explícito.

## Estados editoriais das respostas consolidadas

Estes não substituem estados operacionais:

- `CONFIRMADO`
- `COMPLEMENTO_PROPOSTO`
- `PREMISSA_EDITORIAL`
- `PENDENTE`
- `CONFLITO`

`CONFLITO` só bloqueia avanço quando o contrato editorial o classificar como impeditivo.

## Idempotency key conceitual

```text
SHA256(
  project_id
  + stage_id
  + unit_id
  + normalized_inputs
  + prompt_id
  + prompt_version
  + contract_or_adapter_version
)
```

## Runtime de cada ebook

```text
projects/<slug>-<project_id>/
├─ project.json
├─ config/
│  ├─ project.yaml
│  └─ runtime/
│     └─ vNNNN.json
├─ prompts/
│  └─ frozen/
├─ academic/
│  ├─ questionnaire/
│  ├─ consolidated-answers/
│  └─ plan/
├─ text/
│  ├─ context/
│  ├─ preparations/
│  ├─ raw/
│  ├─ accepted/
│  ├─ validation/
│  ├─ references/
│  └─ consolidated/
├─ visual-plan/
│  ├─ pagination/
│  ├─ raw/
│  ├─ accepted/
│  └─ anchors/
├─ images/
├─ exports/
├─ logs/
└─ tmp/
```

`tmp/` é descartável; os demais não.

## Escrita segura

Sempre que possível:
1. escrever em temporário;
2. flush/close;
3. validar;
4. renomear atomicamente;
5. registrar hash;
6. só então marcar DB como `done`.

## Startup/recovery

Ao iniciar:
1. abrir DB;
2. carregar milestone e projeto;
3. detectar unidades deixadas em `running`;
4. reconciliar com artefatos existentes;
5. marcar `done` somente se houver evidência válida;
6. caso contrário usar `pending_retry` ou `blocked`;
7. nunca repetir unidade `done` implicitamente.

## Consistência banco/filesystem

O DB é fonte de verdade operacional.
O filesystem é fonte de verdade do conteúdo pesado.

Uma unidade só é `done` quando:
- o DB registra conclusão;
- o artefato exigido existe, por padrão;
- seu hash corresponde ao registrado, quando aplicável.

Operações deliberadamente sem artifact precisam de política tipada por estágio/operação e de uma
entidade SQLite cuja identidade, estado e proveniência recomponham exatamente o `input_hash` do
run. A confirmação de acknowledgement usa esse modelo; ausência de regra continua sendo
`DONE_RUN_WITHOUT_ARTIFACT`.

`config/project.yaml` nunca é editado para alternar comportamento runtime. Overrides autorizados
formam uma cadeia imutável `config/runtime/vNNNN.json`, com SHA anterior, Artifact e StageRun. O
snapshot mais recente só é efetivo após validação integral da cadeia.
