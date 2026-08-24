# SECURITY.md

## Nunca versionar
- cookies;
- senhas;
- API keys;
- OAuth access/refresh tokens;
- perfil autenticado de navegador.

## Browser
O profile persistente do M3 deve ser dedicado e ficar fora do repositório. Cookies, tokens,
local/session storage e credenciais permanecem exclusivamente nesse profile e nunca são copiados
para SQLite, artifacts, logs ou fixtures.

O channel `chrome` reutiliza somente um profile dedicado explicitamente configurado; o profile
pessoal/default é proibido. Um lock exclusivo do Hélios serializa seus processos e o lock nativo
do Chrome impede abertura concorrente pelo Chrome manual e pelo Playwright. O operador deve fechar
o bootstrap manual antes do probe. O software não copia nem exporta cookies ou storage state.

Automação usa sessão legítima e respeita limites. Login inicial é manual. Sessão expirada,
CAPTCHA, 2FA e security challenge pausam/bloqueiam a integração; não há bypass de rate limit ou
mecanismos anti-abuso.

## Google
Usar menor conjunto de escopos OAuth possível.

## Logs
Não registrar segredos nem reproduzir conteúdo integral desnecessariamente.

No M0, logs estruturados aplicam redaction defensiva para nomes comuns de credenciais.

## Filesystem

- paths de artefato são relativos ao diretório configurado do projeto;
- paths absolutos, traversal, drive prefix e nomes reservados do Windows são rejeitados;
- escrita usa temporário no mesmo diretório e promoção sem sobrescrita;
- destino existente com bytes diferentes gera conflito.

## Backups
Um diretório de projeto deve ser copiável/autocontido, exceto segredos externos.
