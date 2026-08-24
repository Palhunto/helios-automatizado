# DEFINITION_OF_DONE.md

Uma tarefa só está pronta quando:
- respeita o milestone;
- happy path funciona;
- erros relevantes são tratados;
- estado é persistido quando aplicável;
- rerun não duplica efeito;
- testes passam;
- documentação afetada está atualizada;
- schema/config afetado está atualizado;
- não adicionou segredo;
- não adicionou dependência desnecessária;
- não antecipou milestone futuro.

Uma mudança em comportamento editorial só está pronta quando:
- snapshot de prompt não foi alterado silenciosamente;
- nova versão foi criada quando necessário;
- registry aponta versão correta;
- compatibilidade/impacto foi registrado.

Um milestone só está pronto quando todos os critérios em `MILESTONES.md` estão satisfeitos.

## Evidência do M0

- criação e segunda execução idempotente cobertas por teste;
- fechamento e reabertura do SQLite cobertos por teste;
- recovery para artifact íntegro, ausente e corrompido coberto por teste;
- migration idempotente e drift cobertos por teste;
- no-clobber e limpeza de temporário cobertos por teste;
- CLI create/list/show/validate coberta por teste;
- Ruff, mypy estrito, build do wheel e inclusão da migration verificados.

## Evidência do M1

- questionário aceita equivalência normalizada e exige confirmação para redação divergente;
- texto observado e contrato canônico permanecem simultaneamente no artefato aceito;
- parser de respostas valida cinco itens únicos, ordenados, ancorados e não vazios;
- `[CONFLITO]` bloqueia autorização e `[PENDENTE]` não;
- plano referencia respostas e autorização atuais; plano anterior permanece preservado quando as
  respostas mudam;
- bruto, aceito, prompt congelado, hashes, versões e decisões sobrevivem a restart;
- recovery acadêmico cobre promoção sem commit, output ausente, divergente, confirmação e rejeição;
- CLI acadêmica completa, migration `0002` empacotada e migration `0001` inalterada;
- pytest com cobertura de linhas/branches, Ruff, mypy estrito, wheel e instalação limpa verificados.

## Evidência do M2

- WritingContext congela Answers + Plan correntes, prompts e contract; o snapshot acadêmico é
  relido sob lock antes do commit;
- acknowledgement bruto e confirmação explícita precedem qualquer preparação;
- `omega_writing@1` existe somente no context package; unit requests usam prompt operacional;
- engine executa contract canônico e contracts sintéticos, inclusive DAG com múltiplas dependências;
- Production Set deriva compatibilidade topológica e propaga mudança upstream sem persistir stale;
- raw/rejected/review/accepted têm versionamento independente, bytes e histórico preservados;
- hard errors ficam restritos a invariantes objetivas; linguística é warning/review no V1;
- Citation Ledger preserva apenas ocorrências observadas e não implementa Reference Reconciliation;
- consolidação é determinística, byte a byte, ordenada pelo contract e versionada por set;
- migration `0003`, CLI manual completa, restart/idempotência e recovery por checkpoint verificados;
- pytest com gates separados de statements/branches, Ruff, mypy, schemas, wheel e instalação limpa
  verificados antes do fechamento.

## Evidência exigida do M3

- APIs M2 por `context_id` e `preparation_id` exatos impedem drift para “latest”;
- todas as projeções acadêmicas são extraídas eager, congeladas e validadas por artifact/SHA;
- profile persistente é dedicado e nenhum segredo entra em DB, artifact, log ou fixture;
- context prompt é enviado uma vez, unidades usam a mesma conversa e confirmação permanece humana;
- response artifact íntegro precede captura/import, sem normalização editorial silenciosa;
- spikes curto/longo fixam um único `capture_method_version` antes do adapter real;
- accepted avança; review/rejected pausam; retry é explícito, limitado e preserva histórico;
- recovery cobre pre-send, pós-send, streaming, response armazenada, captura e pós-import;
- bootstrap recovery exige exatamente um candidato por fingerprint + baseline pré-send;
- restart e interrupção pós-send não duplicam mensagem nem submissão;
- migration `0004`, CLI, pytest/coverage, Ruff, mypy, wheel e instalação limpa passam;
- teste manual real headed passa antes de marcar M3 `COMPLETE`; M4 permanece não iniciado.
- confirmação do WritingContext passa em `project validate` mediante evidência SQLite tipada, e
  runs artifact-producing continuam falhando sem Artifact;
- browser pode ser habilitado por snapshot runtime versionado sem editar o `project.yaml` histórico.
