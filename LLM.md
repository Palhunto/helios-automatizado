# LLM.md — Política de uso de modelos

## Regra central

> LLM produz significado; código garante invariantes.

## Motor primário

A arquitetura preserva ChatGPT Plus por ser custo fixo já absorvido.
API paga é adapter opcional futuro, não requisito do projeto inicial.

## Prompts canônicos

Os prompts reais ficam em `prompts/omega_brain/` e são resolvidos por `prompts/registry.yaml`.

Nunca:
- editar snapshot já usado em produção;
- inserir o prompt inteiro como string anônima em código;
- assumir "latest" para projeto existente;
- mudar comportamento editorial dentro do browser adapter.

## Fluxos semânticos previstos

### 1. Planejamento acadêmico — M1

Três unidades independentes:

```text
ACADEMIC_QUESTIONNAIRE
→ CONSOLIDATED_ANSWERS
→ ACADEMIC_PLAN
```

O prompt canônico exige cinco perguntas padronizadas na primeira etapa.

As respostas consolidadas podem conter:
- `CONFIRMADO`;
- `COMPLEMENTO_PROPOSTO`;
- `PREMISSA_EDITORIAL`;
- `PENDENTE`;
- `CONFLITO`.

O parser pode reconhecer marcadores, mas não inferir status ausente.

A autorização para gerar o planejamento é um evento operacional explícito e não deve ser inventada pelo modelo.

### 2. Carregamento do contexto de redação — M2

O prompt de redação recebe dentro de `<planejamento>`:
- respostas consolidadas vigentes;
- planejamento acadêmico vigente.

A primeira resposta esperada é um resumo breve de quatro linhas + confirmação de que o contexto foi avaliado.

O acknowledgement bruto é preservado, mas o software não infere compreensão semanticamente. Um
operador precisa confirmá-lo explicitamente antes da preparação de qualquer unidade. O snapshot
`omega_writing@1` aparece somente nesse pacote inicial.

### 3. Produção textual — M2

O Writing Production Contract define unidades, ordem, limites, headings, listas e continuidade.
No contract `omega_writing_production@1`, as unidades são:

- `INTRO`
- `CH01_A`
- `CH01_B`
- `CH02_A`
- `CH02_B`
- `CH03_A`
- `CH03_B`
- `CH04_A`
- `CH04_B`
- `CH05_A`
- `CH05_B`
- `CH06_A`
- `CH06_B`
- `CH07_A`
- `CH07_B`
- `CH08_A`
- `CH08_B`
- `CONCLUSION`

Cada unidade posterior usa `helios_writing_unit_request@1`, na mesma conversa já carregada; o
prompt canônico completo não é reapresentado. Cada unidade deve manter:
- ID;
- input hash;
- versão do prompt;
- status;
- output bruto;
- output aceito;
- contagem de caracteres;
- validações determinísticas;
- citações detectadas;
- versão.

Regras objetivas do primeiro contract:
- 9.000–10.000 caracteres com espaços;
- sem listas/enumerações;
- sem referência meta a "primeira/segunda parte";
- exercícios somente em `CH01_B` ... `CH08_B`;
- Introdução e Conclusão sem exercícios;
- diversidade nos inícios de parágrafos como warning/review conservador.

Regras semânticas permanecem no LLM:
- densidade teórica;
- articulação entre autores;
- adequação de exemplo real;
- relevância de citação;
- continuidade conceitual;
- não simplificação indevida.

### 4. Citation Ledger — M2; reconciliação — milestone posterior

O software mantém um `reference ledger` por seção.

No M2, código pode:
- detectar padrões autor-data;
- preservar texto, autor observado/normalizado, ano, offsets e ordem.

Código não deve:
- inventar dados bibliográficos;
- preencher DOI;
- decidir sozinho que duas obras homônimas são a mesma.

`Citation Ledger != Reference Reconciliation`. O M2 não cria bibliografia, não associa obra e não
declara reconciliação concluída.

Reparo semântico deve ser dirigido apenas à inconsistência encontrada.

### 5. Planejamento visual — M4

O snapshot canônico V2 recebe um `VisualPaginationSnapshot` compatível com a consolidação e exige
exatamente uma proposta para cada página elegível dos capítulos 1–8. Dentro de cada página, o LLM
seleciona o conceito de maior ganho pedagógico/editorial; cobertura obrigatória não permite figura
decorativa, vazia ou semanticamente irrelevante.

Por figura, pede:
- número/nome;
- página;
- seção;
- posição exata no texto;
- conceito principal;
- síntese conceitual;
- justificativa;
- objetivo;
- tipo;
- complexidade visual;
- prompt independente.

Complexidade visual permitida pelo prompt:
- `Editorial direta`;
- `Editorial estruturada`;
- `Síntese conceitual`.

A página editorial é preservada como observada. `page_key`, capítulo e `page_unit_ids` são ligações
operacionais validadas pelo código contra o snapshot, nunca inferidas pelo LLM. A unidade singular
só é resolvida por uma âncora válida no M4.2. A numeração é global
`1..N` na ordem das páginas elegíveis. Zero figuras só é permitido quando zero páginas são
elegíveis. O parser é estruturalmente estrito e tolera apenas LF/CRLF e whitespace de borda.

### 6. Enriquecimento de âncora — M4

Como "posição exata" pode ser semanticamente clara, mas não máquina-localizável, uma chamada separada pode pedir:
- trecho literal curto;
- relação `before|after`;
- contexto opcional.

Python então valida literalidade e unicidade dentro da `page_key` e exige contenção integral em
exatamente um dos `page_unit_ids` da figura. Esse span fornece o `unit_id` singular da âncora;
boundary atravessado ou ocorrência ambígua entre units da página é rejeitado. O mesmo literal pode
existir em outra página do ebook; não se exige unicidade global.

Não alterar a proposta visual nessa subetapa.

O enriquecimento usa contrato operacional versionado separado. Domínio/import vêm primeiro; uma
integração posterior com browser reutiliza M3 sem colocar regras de negócio no adapter.

No M4.2, o contrato é `helios_visual_anchor_enrichment@1`, exportável por `visual anchor request`.
Seu JSON contém `anchor_text`, `position_relative_to_anchor` e contexto adjacente opcional
`anchor_before`/`anchor_after`. A CLI importa o bruto por figura; código resolve a unidade e os
offsets. A importação não envia mensagens ao ChatGPT. A aceitação exige `visual plan finalize`
explícito e todas as últimas versões de âncora válidas.

### 7. Image Manager — M5; produção — M6

Cada figura usa:
1. `generation_prompt` integral do planejamento;
2. `image_global_style` versionado.

Não misturar prompts de figuras diferentes.

M5 controla lifecycle, versões, batches, hashes, estados e artifacts. M6 executa produção real via
GPT, captura/download, reconciliação por `visual_id`, retry somente de `missing|failed` e validação
de completude. Renderer determinístico é fallback excepcional.

## Reparo semântico

Usar apenas quando validação determinística encontrou erro que código não pode corrigir sem alterar significado.

Exemplos:
- texto com 8.700 caracteres → expansão dirigida;
- exercício ausente em segunda parte → inserir apenas a seção necessária;
- âncora não única → fornecer outra âncora literal;
- referência ambígua → corrigir somente a entrada afetada.

Não regenerar unidade inteira quando reparo localizado basta.

## Nunca usar LLM para

- contagem;
- hash;
- existência de arquivo;
- busca literal;
- ordenação;
- nomes de arquivo;
- progress tracking;
- retry policy;
- JSON Schema validation;
- criação de checkpoint;
- comparação de bytes;
- decisão de pular unidade já `done`.

## Outputs estruturados

Quando código consumir uma resposta, preferir schema formal.

Se ChatGPT Web não oferecer structured output garantido:
1. preservar resposta bruta;
2. extrair bloco delimitado/estrutura esperada;
3. validar;
4. rejeitar output inválido;
5. solicitar reparo pontual;
6. nunca inventar campo faltante.

## Reexecução

Uma unidade semântica `done` só pode ser chamada novamente se:
- usuário solicitar;
- inputs mudarem;
- prompt version mudar;
- validação posterior revelar erro objetivo;
- operação explícita de reprocessamento for criada.

Toda reexecução aumenta versão/tentativa e preserva histórico.
