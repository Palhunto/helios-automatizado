# CODEX_BOOTSTRAP.md

Você está assumindo o projeto **Hélios Ebook Automation**.

A especificação foi revisada usando os prompts reais do OmegaBrain. Antes de escrever código, leia:

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
12. `prompts/registry.yaml`

Para compreender o produto final, leia os snapshots em `prompts/omega_brain/`, mas **não implemente seus milestones ainda**.

Implemente somente **M0 — Fundação**.

Entregue:
- estrutura Python organizada;
- SQLite + migrations;
- modelo de projeto;
- máquina básica de estados;
- criação/carregamento;
- diretórios independentes;
- hashing determinístico;
- logging contextual;
- escrita segura de artefatos;
- recuperação básica de unidades `running` órfãs;
- testes dos invariantes M0.

Não implemente ainda:
- domínio acadêmico M1;
- produção textual M2;
- Playwright M3;
- planejamento visual M4;
- geração de imagem M5;
- SVG M6;
- Google Docs M7;
- UI M9;
- OpenAI API paga.

Antes de encerrar:
1. rode testes;
2. corrija falhas;
3. atualize `PROJECT_STATE.md`;
4. liste pendências reais;
5. não avance para M1 automaticamente.

Objetivo: fundação difícil de quebrar, não quantidade de features.
