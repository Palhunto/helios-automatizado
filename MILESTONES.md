# MILESTONES.md

## Regra geral

Cada milestone deve produzir um resultado testável e utilizável pelo milestone seguinte.
Não avançar automaticamente.
O milestone atual é definido em `PROJECT_STATE.md`.

---

# M0 — Fundação

## Objetivo
Criar infraestrutura local confiável antes de qualquer automação externa.

## Escopo
- estrutura Python;
- configuração;
- SQLite + migrations;
- Project;
- StageRun/Operation;
- Artifact;
- máquina de estados;
- filesystem seguro;
- hashing;
- logging;
- checkpoints;
- recovery básico;
- CLI mínima de projeto;
- testes.

## Explicitamente fora
- OmegaBrain real;
- Playwright;
- geração textual;
- planejamento visual;
- imagens;
- Google Docs;
- API paga;
- UI.

## Aceite
- criar projeto;
- reiniciar processo;
- recarregar mesmo projeto;
- persistir estado;
- reconciliar `running` órfão;
- escrita atômica;
- hashes estáveis;
- testes passando.

---

# M1 — Planejamento acadêmico

## Objetivo
Modelar e validar o fluxo canônico:
`PERGUNTAS → RESPOSTAS CONSOLIDADAS → PLANEJAMENTO`.

## Escopo
- entidades do domínio acadêmico;
- artefatos raw/accepted;
- cinco perguntas como contrato;
- parser dos marcadores editoriais;
- registro de autorização para planejar;
- bloqueio por conflito impeditivo;
- importação manual de respostas do OmegaBrain;
- schemas;
- testes.

## Importante
M1 não automatiza navegador. Entradas podem ser importadas/coladas para validar o domínio.

## Aceite
- armazenar as três etapas separadamente;
- impedir `ACADEMIC_PLAN` sem respostas consolidadas + autorização;
- preservar marcadores;
- retomar após restart;
- não reexecutar etapa `done`.

---

# M2 — Produção textual

## Objetivo
Representar a sessão de redação e as unidades de um Writing Production Contract como operações
idempotentes, versionadas e recuperáveis.

## Escopo
- `WRITING_CONTEXT_LOAD`;
- `WritingContext` imutável com acknowledgement e confirmação explícita;
- unidades definidas por contract (18 no formato canônico V1);
- preparação por unidade com proveniência de continuidade;
- armazenamento raw/accepted;
- limites e headings definidos por contract;
- validador de listas/enumerações;
- regra de exercícios do contract canônico;
- detecção de meta-referências a "partes";
- warnings conservadores de abertura de parágrafos;
- Citation Ledger sem Reference Reconciliation;
- Production Set dependency-compatible derivado topologicamente;
- consolidação ordenada;
- importação manual de outputs;
- testes.

## Aceite
- contract canônico com 18 unidades e contract sintético sem branches especiais;
- cada parte pode ser aceita/rejeitada isoladamente;
- rerun não duplica conteúdo;
- exercícios aparecem apenas onde permitido;
- regeneração upstream invalida deterministicamente seleções downstream incompatíveis;
- consolidação reproduz ordem e separator do contract com bytes aceitos intactos;
- citações detectadas são registradas como ocorrências para reconciliação futura.

---

# M3 — Automação do ChatGPT Plus

## Objetivo
Substituir importação manual por browser automation robusta sem mudar regras de domínio.

## Escopo
- Playwright;
- sessão legítima;
- criação/seleção de conversa;
- envio;
- detecção de término;
- captura;
- evidência de envio/resposta;
- recuperação;
- erros de UI;
- checkpoints;
- integração com M1/M2.

## Regras
- sem `sleep()` como sincronização principal;
- sem bypass de proteção;
- sem LLM clicando quando seletor basta;
- não duplicar envio confirmado.

## Aceite
Um fluxo acadêmico + textual de teste consegue avançar sem intervenção normal, preservando recovery.

---

# M4 — Planejamento visual

## Objetivo
Transformar o prompt visual canônico em artefatos estruturados e localizáveis.

## Escopo
- importação/captura do planejamento;
- parser dos campos canônicos;
- `VisualFigure`;
- `VisualPlan`;
- enriquecimento de âncora literal;
- validação de âncora no texto consolidado;
- sem quota de figuras;
- repair dirigido;
- testes.

## Aceite
- todas as figuras aceitas têm os campos editoriais obrigatórios;
- cada figura tem âncora operacional validada;
- o plano pode ter zero, uma ou várias figuras por capítulo;
- nenhum renderer é executado ainda.

---

# M5 — Image Manager

## Objetivo
Garantir que figura planejada tenha identidade, manifest, arquivo e recovery.

## Escopo
- manifest;
- nome determinístico;
- prompt hash;
- file hash;
- status/tentativas;
- um arquivo por figura;
- não sobrescrever;
- retry limitado;
- integração com geração de imagem disponível no fluxo Plus quando aplicável.

## Aceite
Rodar o mesmo projeto duas vezes não regenera figuras `done` com mesmo hash.

---

# M6 — Renderização híbrida

## Objetivo
Direcionar figuras adequadas para renderer determinístico e reduzir geração IA desnecessária.

## Escopo
- classificação de renderer;
- SVG/Python;
- componentes reutilizáveis;
- fluxos;
- comparações;
- ciclos;
- matrizes;
- sequências;
- export PNG quando necessário;
- manutenção da identidade visual.

## Aceite
Os tipos suportados geram artefatos estáveis, legíveis e idempotentes.

---

# M7 — Google Docs

## Objetivo
Formatar e inserir o ebook sem depender do LLM para operações mecânicas.

## Escopo
- Google Docs/Drive APIs;
- upload temporário de imagens quando necessário;
- estilos;
- quebras;
- cabeçalho;
- rodapé;
- paginação;
- legendas;
- fontes;
- inserção por âncoras;
- idempotência;
- fallback mínimo de UI para recurso não exposto adequadamente.

## Aceite
Documento cru de teste → documento formatado sem edição manual significativa e sem duplicação em rerun.

---

# M8 — QA final

## Objetivo
Impedir `COMPLETE` com inconsistência crítica.

## Escopo
- 18 partes;
- consolidação;
- referências;
- planejamento visual;
- imagens;
- hashes;
- Docs;
- relatório final;
- política de severity.

## Aceite
Falha crítica mantém projeto não concluído e aponta a unidade exata.

---

# M9 — Interface do operador

## Objetivo
Permitir uso pelo Simão sem terminal.

## Escopo
- criar projeto;
- visualizar etapas;
- continuar;
- pausar;
- retry;
- abrir artefatos;
- mostrar erros;
- logs resumidos.

## Aceite
Operação normal não exige editar YAML, SQLite ou terminal.

---

# M10 — Hardening

## Objetivo
Transformar ferramenta funcional em ferramenta confiável de produção recorrente.

## Cenários obrigatórios
- queda de internet;
- encerramento abrupto;
- reboot;
- sessão expirada;
- UI alterada;
- resposta incompleta;
- arquivo já existente;
- Docs parcialmente alterado;
- âncora ambígua;
- prompt versionado novo;
- imagem falhou;
- conflito DB/filesystem.

## Aceite
Nenhum cenário suportado destrói trabalho válido anterior ou reinicia o ebook inteiro sem necessidade.
