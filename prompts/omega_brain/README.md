# OmegaBrain — prompts canônicos

Esta pasta contém snapshots versionados dos prompts reais usados pelo fluxo OmegaBrain.

## Regra de preservação

Arquivos `*_vN.txt` são **fontes históricas imutáveis**. Não editar um snapshot já usado em produção.
Para alterar um prompt:

1. criar nova versão;
2. atualizar `prompts/registry.yaml`;
3. registrar o motivo em `DECISIONS.md` quando houver impacto de contrato;
4. preservar a versão anterior.

## Prompts

- `academic_planning_v1.txt`: perguntas padronizadas → respostas consolidadas → planejamento acadêmico.
- `writing_v1.txt`: carregamento do planejamento + produção das 18 partes.
- `visual_planning_v1.txt`: snapshot histórico de seleção livre e planejamento editorial das figuras.
- `visual_planning_v2.txt`: cobertura obrigatória de exatamente uma proposta para cada página
  elegível dos capítulos 1–8, preservando os critérios qualitativos da V1.
- `image_global_style_v1.txt`: identidade visual global aplicada às imagens geradas.

## Importante

Os prompts canônicos não devem ser modificados pelo parser nem pelo browser adapter.
Wrappers operacionais futuros podem acrescentar instruções fora do snapshot, desde que:
- não alterem significado editorial;
- sejam versionados separadamente;
- tenham objetivo técnico explícito;
- não sejam usados para esconder mudança de requisito.
