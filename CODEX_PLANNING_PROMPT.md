# Prompt inicial de planejamento — Hélios Ebook Automation

Você está iniciando o projeto **Hélios Ebook Automation**.

Nesta execução, **não programe**. Sua tarefa é estudar o repositório e preparar um plano técnico executável exclusivamente para `M0 — Fundação`.

## Leia nesta ordem

1. `AGENTS.md`
2. `PROJECT_STATE.md`
3. `MILESTONES.md`
4. `ARCHITECTURE.md`
5. `DECISIONS.md`
6. `docs/SOURCE_WORKFLOW.md`
7. `docs/OMEGABRAIN_SPEC.md`
8. `docs/DATA_MODEL.md`
9. `QA.md`
10. `LLM.md`
11. `SECURITY.md`
12. `RUNBOOK.md`
13. `config/`
14. `schemas/`
15. `prompts/registry.yaml`
16. `prompts/omega_brain/`
17. `src/`, `tests/`, `tools/`, `pyproject.toml`

## Regra dura

Não implementar:
- M1 planejamento acadêmico;
- M2 redação;
- M3 Playwright;
- M4 planejamento visual;
- M5 imagens;
- M6 SVG;
- M7 Google Docs;
- M8 QA final;
- M9 UI;
- M10 hardening específico;
- OpenAI API.

Os prompts reais devem ser compreendidos apenas para garantir que a fundação não bloqueie os domínios futuros.

## O que planejar para M0

- Project;
- StageRun/Operation;
- Artifact;
- ErrorRecord;
- SQLite;
- migrations;
- state machine;
- configuração;
- filesystem;
- escrita atômica;
- SHA-256;
- logging;
- recovery;
- idempotência genérica;
- CLI mínima;
- testes.

## Princípios

- DB = fonte de verdade operacional.
- Filesystem = conteúdo pesado.
- Nenhuma etapa longa vive só em memória.
- `done` não volta a executar implicitamente.
- parser não inventa informação.
- dependência externa só se standard library não resolver bem.
- UI não vem antes do core.

## Entrega obrigatória

Produza exatamente:

### A. Entendimento do projeto
Problema, objetivo, arquitetura, papel do M0 e relação com milestones futuros.

### B. Estado do repositório
Arquivos existentes, lacunas, inconsistências e artefatos já preparados.

### C. Conflitos
Para cada conflito:
- arquivos;
- impacto;
- interpretação;
- ação.
Se não houver: `Nenhum conflito normativo bloqueante encontrado.`

### D. Decisões técnicas do M0
Python, SQLite, migration strategy, config, CLI, filesystem, hashing, logging e testes.

### E. Modelo de dados M0
Entidades, campos essenciais, relações e constraints.

### F. Máquina de estados
Estados, transições permitidas, inválidas, recovery de `running`, retry e reprocessamento explícito.

### G. Árvore esperada após M0
Somente arquivos realmente necessários.

### H. Plano de implementação
Use para cada tarefa:

`M0.x — Nome`
- Objetivo
- Arquivos
- Implementação
- Testes
- Critério de aceite
- Dependências

### I. Ordem de execução
Mostre dependências.

### J. Test plan
Unit, integration, persistence, recovery, idempotency e negative tests.

### K. Riscos
Tabela: risco, nível, impacto, mitigação.

### L. Definition of Done
Checklist binário observável.

### M. Fora do M0
Liste explicitamente tudo que não será implementado.

### N. Perguntas bloqueantes
Somente perguntas realmente impeditivas.
Se não houver: `Nenhuma pergunta bloqueante. O M0 pode ser implementado com a documentação atual.`

## Encerramento

Finalize após o planejamento.
Não altere código.
Não altere `CURRENT_MILESTONE`.
Não comece implementação automaticamente.
