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
Transformar paginação e planejamento visual canônicos em artefatos estruturados, localizáveis e
com cobertura completa das páginas elegíveis dos capítulos 1–8.

## Escopo
- domínio/importação do planejamento primeiro; captura posterior reutiliza o adapter M3 sem mover
  regra de negócio para o browser;
- `VisualPaginationSnapshot` versionado e ligado à `TextConsolidation` exata;
- layout `helios_pagination_layout@1`, fontes licenciadas congeladas e medição local real em páginas
  DOM A4 explícitas;
- artifacts HTML, PDF e manifest `helios_pagination_snapshot@1`, com PDF determinístico dentro do
  mesmo `renderer_fingerprint`;
- conjunto determinístico de páginas elegíveis com `page_key`, capítulo, unidade e intervalos no
  texto consolidado;
- parser estrutural estrito dos campos canônicos, tolerando somente LF/CRLF e whitespace de borda;
- `VisualFigure`;
- `VisualPlan`;
- enriquecimento de âncora literal;
- validação de âncora na página e unidade canônicas do texto consolidado;
- cobertura obrigatória de exatamente uma figura por página elegível;
- numeração global, positiva, única, contígua e ordenada pelas páginas elegíveis;
- finalização explícita com `helios_visual_manifest@1`;
- repair dirigido;
- testes.

## Aceite
- todas as figuras aceitas têm os campos editoriais obrigatórios;
- o pagination snapshot é current e compatível com a consolidação textual;
- `figure_count == eligible_page_count` e os conjuntos de `page_key` são iguais;
- existe exatamente uma figura por página elegível, sem gaps, duplicidades ou páginas externas;
- cada figura referencia capítulo e unidade tipados e tem âncora operacional validada dentro da
  mesma página e unidade;
- a numeração global é exatamente `1..N`, na ordem das páginas elegíveis;
- zero figuras só é válido quando zero páginas são elegíveis;
- a promoção ocorre somente por `finalize` explícito e produz `helios_visual_manifest@1`;
- nenhum renderer de imagens é executado; o renderer local permitido no M4 limita-se à paginação
  canônica e ao PDF visual correspondente.

---

# M5 — Image Manager

## Objetivo
Controlar o lifecycle de produção de cada figura planejada sem executar ainda a produção real.

## Escopo
- identidade por `visual_id` e vínculo ao manifest aceito do M4;
- versões e batches;
- prompt hash e style hash;
- estados, tentativas e recovery;
- reservations de artifacts e nomes determinísticos;
- não sobrescrever;
- preparação idempotente do conjunto completo.

## Aceite
Rodar o mesmo projeto duas vezes preserva o mesmo lifecycle e não duplica items, batches ou
reservations com os mesmos inputs.

---

# M6 — Produção real de imagens

## Objetivo
Produzir todas as imagens previstas no manifest aceito por meio do GPT e reconciliar os artifacts
capturados com segurança.

## Escopo
- produção real via GPT;
- uma figura por operação identificada por `visual_id`;
- captura/download;
- reconciliação por `visual_id`;
- retry somente de itens `missing` ou `failed`;
- validação de completude do batch;
- preservação de hashes, versões e artifacts do M5;
- renderer determinístico somente como fallback excepcional explícito.

## Aceite
Todas as figures esperadas possuem exatamente um artifact reconciliado e válido; rerun não refaz
items concluídos e retry não alcança items fora de `missing|failed`.

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
