# REVISION_NOTES.md — Revisão OmegaBrain V1

Data: 2026-08-21

## Motivo

O starter pack original foi revisto depois do recebimento dos prompts reais usados pelo OmegaBrain.

## Mudanças principais

1. Planejamento acadêmico separado em:
   - questionário;
   - respostas consolidadas;
   - planejamento.

2. Redação definida formalmente como:
   - carregamento do contexto;
   - 18 partes canônicas;
   - validação determinística;
   - reference ledger;
   - consolidação.

3. Planejamento visual atualizado para os campos reais do prompt.

4. Criada subetapa de `anchor enrichment` para converter "posição exata" em trecho literal verificável sem alterar o prompt visual original.

5. Identidade visual global transformada em prompt versionado independente.

6. Milestones renumerados:
   - M0 Fundação
   - M1 Planejamento acadêmico
   - M2 Produção textual
   - M3 Automação ChatGPT Plus
   - M4 Planejamento visual
   - M5 Image Manager
   - M6 Renderer híbrido
   - M7 Google Docs
   - M8 QA
   - M9 UI
   - M10 Hardening

7. Novos schemas:
   - consolidated answers;
   - text section;
   - reference ledger;
   - visual plan revisado;
   - image manifest revisado.

8. Prompts canônicos adicionados com SHA-256 no registry.

## O que não mudou

- Python;
- SQLite;
- local-first;
- ChatGPT Plus como motor principal;
- idempotência;
- recovery;
- M0 sem integrações externas;
- API paga não é requisito.
