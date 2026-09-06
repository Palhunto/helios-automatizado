# Revisão técnica do M4 — 2026-09-06

O projeto já possui o core local de persistência, planejamento acadêmico, produção textual e
automação do ChatGPT dos milestones M0–M3. O M4 acrescenta paginação A4 canônica, importação de
propostas visuais e a ligação de cada figura ao texto por uma âncora verificável. Seu resultado
utilizável pelo próximo milestone é `helios_visual_manifest@1`.

O M4 foi concluído, preservando as alterações locais do M4.1 já existentes. A suíte completa passou
com 570 testes; o novo teste de fingerprint também passou separadamente, totalizando 571 testes
cobertos. Ruff, mypy estrito, 16 schemas e a CLI do wheel foram verificados. As evidências completas
ficam em `PROJECT_STATE.md`; `CURRENT_MILESTONE` continua M4.

## Problemas encontrados e tratamento

| Problema | Tratamento |
|---|---|
| Faltavam âncoras persistentes e aceite do plano | Implementados import, validação literal, reparo por versão, recovery e finalize explícito. |
| A validação do plano comparava apenas parte da proveniência | Passou a conferir todos os vínculos com o snapshot, inputs e produtor dos artifacts. |
| Plano editorial inválido podia aparecer como current | Currentness agora exige disposição estrutural válida. |
| Bytes podiam mudar depois do checkpoint e antes do commit | Fontes e outputs são relidos na fronteira do commit; divergência impede promoção. |
| Consultas repetiam abertura de Chromium e extração integral do PDF | Fingerprint sem browser; hashes, ledger e geometria continuam verificados. Extração textual completa permanece na validação de paginação. |
| Encerramento rápido do driver emitia avisos de inicialização pendente | Um roundtrip local sem HTTP conclui a inicialização antes do cleanup. |
| README descrevia M3 em andamento e M4 não iniciado | Documentação e comandos operacionais foram atualizados. |

As regras editoriais e snapshots históricos foram preservados. Âncora ambígua, cruzamento de
unidade, fontes desatualizadas ou arquivos corrompidos impedem avanço. Uma tentativa rejeitada
continua disponível para auditoria e reparo.

## Nível do produto e trabalho posterior

O resultado é um pipeline local com CLI e histórico persistente. O M4 pode ser operado por
exportação/importação de conteúdo produzido pelo OmegaBrain; sua seleção semântica continua
separada da validação determinística.

O produto completo ainda depende dos milestones seguintes:

- M5: controle do lifecycle de imagens;
- M6: produção e captura das imagens via GPT;
- M7: formatação e inserção no Google Docs;
- M8: QA final de texto, referências e artefatos;
- M9: interface do operador;
- M10: hardening dos cenários de produção.

A reconciliação bibliográfica completa permanece pendente para milestone posterior, conforme
`QA.md`; o Citation Ledger existente não representa uma bibliografia reconciliada.

Não foi atribuída uma porcentagem de conclusão: a quantidade de milestones não representa seu
esforço ou risco. O fechamento do M4 habilita o trabalho seguinte, mas não avança automaticamente
`CURRENT_MILESTONE` nem declara o produto completo.
