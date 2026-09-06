# SOURCE_WORKFLOW.md — Workflow canônico OmegaBrain

## Fonte

Este documento descreve o processo observado nos prompts reais armazenados em `prompts/omega_brain/`.

O software não deve substituir o método editorial; deve converter instruções operacionais frágeis em código e contratos.

## Macrofluxo real

1. Planejamento acadêmico.
2. Produção textual.
3. Reconciliação/consolidação.
4. Planejamento visual.
5. Produção das imagens.
6. Formatação no Google Docs.

---

## 1. Planejamento acadêmico

Fluxo obrigatório:

```text
PERGUNTAS PADRONIZADAS
→ RESPOSTAS CONSOLIDADAS
→ PLANEJAMENTO ACADÊMICO
```

### Perguntas
São cinco perguntas fixas e ordenadas.

### Respostas consolidadas
Distinguem origem/status editorial:

- `[CONFIRMADO]`
- `[COMPLEMENTO PROPOSTO]`
- `[PREMISSA EDITORIAL]`
- `[PENDENTE]`
- `[CONFLITO]`

### Planejamento
Só pode ocorrer quando:
- as cinco respostas existem;
- autorização para planejar foi dada;
- não existe conflito impeditivo sem tratamento.

---

## 2. Produção textual

O redator recebe respostas consolidadas + planejamento dentro de `<planejamento>`.

Primeiro checkpoint:
- resumo breve de quatro linhas;
- confirmação de que o contexto foi avaliado.

Depois são produzidas exatamente 18 partes:

```text
INTRO
CH01_A
CH01_B
CH02_A
CH02_B
CH03_A
CH03_B
CH04_A
CH04_B
CH05_A
CH05_B
CH06_A
CH06_B
CH07_A
CH07_B
CH08_A
CH08_B
CONCLUSION
```

Cada parte:
- 9.000–10.000 caracteres com espaços;
- texto contínuo;
- sem listas/enumerações;
- linguagem acadêmica;
- autor-data quando houver estudos;
- exercícios apenas na segunda metade de cada capítulo central;
- variedade nos inícios de parágrafos.

O software valida as condições objetivas; o LLM continua responsável pela qualidade semântica.

---

## 3. Referências e consolidação

A consolidação:
- preserva a ordem;
- não reescreve conteúdo aprovado;
- une as duas partes de cada capítulo;
- remove apenas resíduos operacionais quando necessário.

O sistema mantém ledger de citações para apoiar correspondência entre corpo e bibliografia.

---

## 4. Planejamento visual

O especialista visual:
- analisa o material inteiro;
- recebe um snapshot canônico das páginas elegíveis dos capítulos 1–8;
- produz exatamente uma proposta para cada página elegível;
- seleciona, dentro de cada página obrigatória, o conceito com maior ganho pedagógico/editorial;
- evita decoração e complexidade artificial sem omitir páginas;
- produz prompts independentes.

Campos canônicos:
- Imagem [número] — [nome]
- Página
- Seção
- Posição exata no texto
- Conceito principal
- Síntese conceitual
- Justificativa curta
- Objetivo
- Tipo
- Complexidade visual
- Prompt independente

Complexidade:
- Editorial direta
- Editorial estruturada
- Síntese conceitual

### Correção canônica
O workflow operacional OmegaBrain original prevalece: existe exatamente uma figura para cada página
elegível dos capítulos 1–8. Introdução, conclusão, referências e páginas externas a esses capítulos
não participam da cobertura. "Evitar excesso" regula qualidade, densidade e complexidade; não
autoriza deixar uma página elegível sem proposta.

A paginação é fornecida por `VisualPaginationSnapshot` versionado, ligado por ID e hash à
`TextConsolidation` exata. Cada página possui `page_key` estável, ordem, capítulo, unidades e
intervalos verificáveis no texto consolidado. É proibido estimar páginas silenciosamente por número
de caracteres.

Em M4.0, `helios_pagination_layout@1` fixa A4, margens, tipografia, page breaks e o slot visual das
páginas elegíveis antes de qualquer imagem. Capítulos CH01–CH08 iniciam nova página por boundary
estrutural; isso não autoriza injetar `Capítulo N` visível. Headings só alteram o layout quando já
existem no source e são declarados no layout editorial. Fontes e licenças são assets versionados e
obrigatórios, sem fallback do sistema. Chromium mede folhas DOM explícitas; o manifest JSON é a
fonte operacional e o PDF é evidência visual.

---

## 5. Enriquecimento de âncoras

A posição editorial precisa ser convertida em localização operacional.

Após o plano:
- obter `anchor_text` literal;
- obter `before|after`;
- no plano, preservar `page_unit_ids` exatamente dos unit spans da `page_key`, sem unidade dominante;
- validar literalidade dentro da `page_key` e contenção integral em exatamente uma dessas units;
- rejeitar boundary atravessado ou ocorrência ambígua entre units da mesma página;
- atribuir `anchor.unit_id` somente depois dessa prova;
- não alterar a proposta da figura.

---

## 6. Produção de imagens

Para cada figura:
- usar prompt independente integral;
- aplicar estilo global versionado;
- uma figura por operação;
- salvar com nome determinístico;
- manifest + hash;
- retry limitado;
- não regenerar figura `done` com mesmos inputs.

No M5, o Image Manager prepara lifecycle, versões, batches, hashes, estados e artifacts. No M6, a
produção real ocorre via GPT, com captura/download, reconciliação por `visual_id`, retry somente de
`missing|failed` e validação de completude. SVG/Python permanece fallback excepcional, não rota
primária.

---

## 7. Google Docs

Objetivo:
- preservar texto;
- inserir no local planejado;
- aplicar legenda e fonte;
- estilos consistentes;
- quebras nativas;
- cabeçalho/rodapé/paginação;
- sumário quando aplicável.

API/código primeiro; browser apenas como fallback para lacunas reais.

---

## Legado → sistema

| Legado | Sistema |
|---|---|
| estado no chat | SQLite |
| copiar planejamento | artefatos versionados |
| contar pelo modelo | Python |
| "checagem silenciosa" objetiva | validators |
| posição textual sem prova | anchor enrichment |
| nomear manualmente | função determinística |
| repetir após crash | checkpoint/resume |
| imagem refeita | manifest + hash |
| formatar por clique | Docs API/código |
