# Relatório consolidado de implementação e correções — M4

**Projeto:** Hélios Ebook Automation / OmegaBrain  
**Fechamento técnico:** 06/09/2026  
**Estado registrado:** `CURRENT_MILESTONE: M4` e `STATUS: COMPLETE`

## 1. Resultado entregue

O M4 foi concluído como etapa técnica local. O sistema consegue importar um planejamento visual,
vincular cada figura a uma página canônica, validar sua posição por um trecho literal e finalizar
um manifest versionado para consumo pelo próximo milestone.

A conclusão preservou o conteúdo editorial e o histórico de artefatos. Uma execução repetida com
os mesmos inputs reutiliza os registros existentes; um reparo com conteúdo diferente cria outra
versão. Fontes incompatíveis, âncoras inválidas e evidências corrompidas impedem a finalização.

## 2. Ponto de partida e escopo da intervenção

M0–M3 já estavam concluídos segundo os registros do projeto. M4.0, de paginação canônica, também
já estava implementado. Havia alterações locais do M4.1 ainda não commitadas, que foram
preservadas e usadas como base.

O trabalho desta intervenção consistiu em revisar essa base, corrigir falhas e implementar o
M4.2. Portanto, as funcionalidades preexistentes abaixo não são apresentadas como criações desta
execução.

| Base existente | Funcionalidade preservada |
|---|---|
| M0 | Configuração, SQLite, migrations, estados, arquivos atômicos, hashes e recovery. |
| M1 | Questionário, respostas consolidadas, autorização e planejamento acadêmico. |
| M2 | Contexto de redação, unidades de texto, validação, versões e consolidação. |
| M3 | Automação do ChatGPT com evidências, checkpoints e recuperação. |
| M4.0 | Paginação real em Chromium local, páginas A4, fontes congeladas, HTML, PDF e manifest. |
| M4.1 | Importação do plano visual, parser estrutural, figures e cobertura de páginas. |

## 3. Revisão e correções do planejamento visual

O M4.1 exige uma figura para cada página elegível, na ordem canônica, com numeração global
contígua. O bruto e os campos editoriais continuam preservados; a `Seção` editorial não determina
a unidade operacional.

Foram reforçadas as verificações de integridade para conferir:

- projeto, contexto, consolidação e conjunto de produção;
- IDs e hashes do texto e dos manifests de consolidação e paginação;
- quantidade esperada de figuras;
- identidade, etapa, unidade e versão do `StageRun`;
- produtor e tipo dos artefatos de bruto e relatório;
- snapshot de prompt congelado no projeto;
- coerência entre a validação recalculada e os registros persistidos.

Também foi corrigido o status que podia apresentar um plano estruturalmente inválido como
`current`. Agora, esse estado exige disposição válida e fontes compatíveis.

## 4. Enriquecimento de âncoras — M4.2

Foi criado o serviço `VisualAnchorService`, com exportação de instruções, importação, consulta e
recuperação por figura.

O contrato operacional `helios_visual_anchor_enrichment@1` é separado dos prompts editoriais.
Ele solicita somente um JSON com trecho literal e relação de inserção:

```json
{
  "anchor_text": "Trecho literal existente na página.",
  "position_relative_to_anchor": "after"
}
```

A relação aceita `before` ou `after`. Os campos opcionais `anchor_before` e `anchor_after`
representam contexto literal imediatamente adjacente. O modelo não fornece IDs, hashes, offsets
ou unidade operacional.

A importação rejeita estrutura ausente, campos desconhecidos ou duplicados, tipos incorretos,
cercas Markdown e JSON inválido. O bruto é preservado antes do parsing, inclusive quando rejeitado.

A validação determinística:

1. procura o trecho dentro da página atribuída à figura;
2. conta todas as ocorrências, inclusive sobrepostas;
3. exige exatamente uma ocorrência;
4. exige contenção integral em um único intervalo de unidade daquela página;
5. calcula a unidade singular e os offsets em caracteres Unicode e bytes UTF-8;
6. verifica o contexto opcional, quando informado.

Trecho repetido em outra página não invalida a âncora. Trecho ambíguo na própria página ou que
atravesse uma fronteira de unidade é rejeitado. Contexto opcional não substitui unicidade.

## 5. Versionamento, reparo e retomada

Cada tentativa de âncora registra identidade, versão, predecessor, prompt, bruto, relatório,
hashes, estado e vínculo com a figura.

- Os mesmos bytes e a mesma proveniência reutilizam a identidade existente.
- Conteúdo diferente recebe nova versão por figura.
- Um reparo não modifica a proposta editorial.
- Tentativas inválidas permanecem consultáveis.
- A recuperação usa o bruto persistido e respeita o limite de tentativas.
- Arquivos conflitantes não são sobrescritos.

Os artefatos ficam em `visual-plan/anchors/<figure_id>/vNNNN/`, com `raw.json` e
`validation.json`. A recuperação de uma operação concluída retorna o resultado existente.

## 6. Finalização explícita do plano

Foi criado `VisualFinalizationService`, responsável pelo aceite em `helios_visual_manifest@1`.

O comando `finalize` exige plano e fontes atuais, cobertura completa e a tentativa mais recente
válida de cada figura. Uma tentativa posterior inválida ou incompleta impede o avanço; o serviço
não seleciona silenciosamente uma versão anterior válida.

O manifest congela:

- identificação e versão da finalização;
- proveniência completa do plano;
- páginas elegíveis e contagens;
- campos editoriais de cada figura;
- `visual_id`, correspondente ao ID da figura;
- unidade resolvida;
- versão, conteúdo e validação da âncora selecionada;
- IDs e hashes que permitem verificar a origem dos dados.

O arquivo aceito é salvo em `visual-plan/accepted/vNNNN.json`. Seu próprio hash fica no Artifact
e no SQLite, evitando autorreferência no conteúdo. A aceitação só é registrada junto do Artifact
e do `StageRun` concluído. Uma reserva interrompida não equivale a aceite.

Reparos posteriores preservam os manifests anteriores. Um novo aceite depende de outro
`finalize` explícito.

## 7. Banco de dados e integridade antes do commit

Foi adicionada a migration `0009_m4_visual_anchors.sql`, com três tabelas:

| Tabela | Responsabilidade |
|---|---|
| `visual_anchors` | Histórico das tentativas de âncora por figura. |
| `visual_finalizations` | Reservas e finalizações versionadas por projeto. |
| `visual_finalization_anchors` | Seleção exata de âncoras congelada em cada finalização. |

As chaves estrangeiras compostas vinculam projeto, plano, figura, âncora e artefatos. As migrations
anteriores não foram reescritas. A migration 0008 pertencia à base M4.1 preexistente.

Também foram acrescentadas releituras de fontes e dos arquivos produzidos depois dos checkpoints,
na fronteira do commit. Alteração ou corrupção detectada nesse intervalo impede a promoção para
concluído, preservando os registros e arquivos para diagnóstico.

## 8. Correções de processamento local

O cálculo do fingerprint do renderer abria Chromium a cada consulta. Essa abertura foi removida:
o cálculo continua verificando o executável e as versões do ambiente, sem abrir browser.

Os testes identificaram avisos de inicialização pendente ao encerrar rapidamente o driver.
Foi acrescentada uma troca local de mensagens com o driver antes do encerramento, sem requisição
HTTP, para concluir sua inicialização.

As operações de âncora também deixaram de repetir a extração textual integral do PDF por figura.
Continuam verificando hashes, tamanho, geometria, intervalos e fontes operacionais. A extração
completa permanece na validação específica da paginação. Dentro de uma chamada de validação de
lote, as fontes verificadas podem ser reutilizadas em memória; não há cache global persistente.

Essas mudanças não alteraram layout, fontes, conteúdo editorial ou identidade do fingerprint.
Não foi estabelecido um percentual geral de ganho de desempenho.

## 9. Comandos disponíveis

| Comando | Função |
|---|---|
| `ebook pagination create` | Criar ou retomar paginação canônica. |
| `ebook pagination validate` | Validar paginação e evidência PDF. |
| `ebook visual plan import` | Importar o planejamento visual V2. |
| `ebook visual plan show` | Consultar plano, relatório e bruto opcional. |
| `ebook visual figure list` / `show` | Consultar figuras e vínculos de página. |
| `ebook visual anchor request` | Exportar instrução, figura e trechos da página. |
| `ebook visual anchor import` | Importar uma tentativa de âncora. |
| `ebook visual anchor show` | Consultar resultado ou versão histórica. |
| `ebook visual anchor recover` | Retomar a partir do bruto persistido. |
| `ebook visual plan finalize` | Aceitar explicitamente o conjunto validado. |
| `ebook visual plan manifest` | Consultar manifest aceito atual ou histórico. |
| `ebook visual plan validate` | Verificar imports, âncoras e finalizações. |
| `ebook visual plan status` | Consultar estado estrutural e da finalização. |

Os comandos de paginação, plano e figura já integravam a base M4.0/M4.1. Foram acrescentados os
comandos de âncora, `finalize` e `manifest`, e ampliados `status` e `validate`.
Os argumentos completos e exemplos estão em `RUNBOOK.md`.

## 10. Arquivos e contratos envolvidos

| Grupo | Arquivos principais |
|---|---|
| Validação literal | `src/ebook_pipeline/visual_planning/anchors.py` |
| Serviço de enriquecimento | `src/ebook_pipeline/visual_planning/enrichment.py` |
| Finalização | `src/ebook_pipeline/visual_planning/finalization.py` |
| Persistência das novas entidades | `src/ebook_pipeline/visual_planning/anchor_repository.py` |
| Leitura, verificações e falhas operacionais | `src/ebook_pipeline/visual_planning/operation_io.py` |
| Modelos e integração com M4.1 | `src/ebook_pipeline/visual_planning/models.py` e `service.py` |
| Interface de terminal | `src/ebook_pipeline/cli.py` |
| Paginação | `src/ebook_pipeline/pagination/renderer.py` e `service.py` |
| Migration nova | `src/ebook_pipeline/storage/sql/0009_m4_visual_anchors.sql` |
| Prompt novo | `prompts/operational/visual_anchor_enrichment_v1.txt` |
| Registro do prompt | `prompts/registry.yaml` |
| Dependências de desenvolvimento | `pyproject.toml` |

Foram acrescentados cinco schemas JSON Draft 2020-12: candidato de âncora, registro de âncora,
relatório de validação, registro de finalização e manifest aceito. O conjunto visual inclui também
os schemas de plano e relatório estrutural provenientes do M4.1.

As dependências `jsonschema` e `types-jsonschema` foram adicionadas ao grupo de desenvolvimento
para verificar os contratos e manter a análise de tipos. Não foi adicionada dependência de API paga.

Foram criados ou ampliados testes de âncoras, fluxo visual, schemas, fingerprint, paginação e
migrations. Fixtures de teste usam contratos sintéticos reduzidos quando adequado, preservando
as 18 unidades e a medição real de páginas; os testes canônicos preexistentes continuam na suíte.

## 11. Documentação atualizada

- `PROJECT_STATE.md`: implementação, hashes, evidências e fechamento do M4.
- `README.md`: estado real do desenvolvimento, antes desatualizado.
- `RUNBOOK.md`: comandos, formato JSON, reparo, histórico e retomada.
- `ARCHITECTURE.md`: serviços de enriquecimento e finalização separados.
- `DECISIONS.md`: D-043, sobre âncoras/aceite versionado, e D-044, sobre validação operacional/PDF.
- `docs/DATA_MODEL.md`: registros, vínculos e separação entre plano e aceitação.
- `QA.md`: invariantes de âncora, commit, recuperação e schema.
- `LLM.md`: contrato operacional e separação entre escolha semântica e validação.
- `docs/M4_REVIEW.md`: avaliação técnica e trabalho posterior.

As alterações anteriores do M4.1 em documentação, schemas e testes foram preservadas. A presença
de um arquivo no diff atual não significa que todo seu conteúdo foi produzido nesta intervenção.

## 12. Evidências de validação

| Verificação | Resultado registrado |
|---|---|
| Suíte completa | 570 testes passaram em 624,78 segundos. |
| Teste adicional de fingerprint | Passou separadamente após sua inclusão. |
| Total de testes coletados ao final | 571, cobertos pelas execuções acima. |
| Ruff | Sem erros em `src` e `tests`. |
| mypy estrito | Sem erros em 146 arquivos. |
| Schemas | 16 schemas do projeto válidos em Draft 2020-12. |
| Artefato de integração | Manifest persistido validado contra seu schema. |
| Driver local | Oito probes produziram um fingerprint estável, sem browser, HTTP ou avisos pendentes. |
| Preservação | Sete snapshots históricos de prompt e migrations 0001–0008 conferidos por SHA. |
| Pacote Python | Wheel construído com CLI, módulos visuais e migrations 0001–0009. |
| CLI do pacote instalado | Consultou V1 e repetiu finalize V2 de um fixture com 16 figuras. |
| Idempotência da CLI empacotada | Sem novos StageRuns, Artifacts, anchors ou finalizations. |
| Whitespace | `git diff --check` sem erros. |

O wheel foi instalado em um diretório isolado de teste utilizando as dependências existentes.
Não se tratou de instalação completa em outra máquina. Os testes de falha cobriram interrupção
nos checkpoints, corrupção, fonte alterada antes do commit, migração com dados, restart,
histórico e bloqueio de finalização com tentativa inválida.

Hashes registrados para os novos contratos persistentes:

```text
0009_m4_visual_anchors.sql
267bad69fd2902a597f375362e0d8f2253c8c50a2381fdf59f9ec9ea4dda9f25

visual_anchor_enrichment_v1.txt
07a273649e9cac3ef40dc780b8dbc70215bb50f9ab54a50cec74df0d9e41849e
```

## 13. Limites da entrega e situação final

O fechamento é técnico/local. Os testes verificam regras estruturais e operacionais, não a
qualidade pedagógica de uma proposta visual real. A escolha semântica continua com OmegaBrain
ou revisão humana; o fluxo M4 pode ser operado por exportação/importação.

Não houve nesta intervenção envio de conteúdo ao ChatGPT, geração de imagens, formatação no
Google Docs ou operação sobre o SQLite do ebook real. Os ensaios de produção utilizaram fixtures.
Os prompts editoriais históricos não foram reescritos.

M5 continua sem iniciar. Permanecem posteriores o lifecycle de imagens, sua produção via GPT,
Google Docs, QA final, interface do operador e hardening de produção. A reconciliação bibliográfica
completa também permanece pendente; o Citation Ledger não equivale a bibliografia reconciliada.

As alterações estão aplicadas no workspace local. Não foi realizado commit, push ou deploy nesta
execução. O projeto permanece em M4, marcado como concluído, sem avanço automático para M5.
