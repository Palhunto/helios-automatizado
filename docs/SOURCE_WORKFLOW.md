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
- seleciona apenas conceitos que realmente ganham com síntese visual;
- evita decoração e quota artificial;
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
Não existe regra "uma imagem por página".
Quantidade menor de imagens fortes é preferível a muitas superficiais.

---

## 5. Enriquecimento de âncoras

A posição editorial precisa ser convertida em localização operacional.

Após o plano:
- obter `anchor_text` literal;
- obter `before|after`;
- validar no texto consolidado;
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

Renderer futuro:
- SVG/Python quando o recurso for estrutural e determinístico;
- IA quando a síntese exigir composição pictórica/editorial.

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
