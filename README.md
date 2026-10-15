# bot — código do Discord bot + painel (Empório dos Bots / STOMP)

Código-fonte do bot de Discord e do painel web, que rodam numa VM Oracle Cloud via systemd. Esse repositório existe pra permitir editar esses arquivos de sessões que não têm acesso SSH direto à VM (ex: Claude Code pelo celular) — sem precisar da chave SSH `stomp_new_key`.

## Arquivos

- `event_bot.py` — bot de Discord principal (serviço `eventbot.service` na VM).
- `eventsite.py` — painel web / API (Flask + Waitress, serviço `eventsite.service`, porta 8000 interna, atrás de nginx+certbot em `https://146-235-57-112.sslip.io`).
- `albion_shared.py` — funções e dados compartilhados entre os dois (itens, builds, composições).
- `catalog_hostinger.json` — catálogo completo de itens e habilidades do Albion Online (1696 itens, 773 habilidades, PT-BR), baixado do site antigo antes dele ser substituído. Útil pra popular um endpoint de nome/habilidade por item.
- **Falta** `contentsbr_bot.py` (outro bot da VM) — não estava disponível nessa sessão no momento do push; será adicionado quando alguém com acesso à VM subir ele aqui.

## Como isso vira deploy de verdade

Editar aqui e dar `git push` **não** atualiza a VM sozinho — esses arquivos rodam lá via systemd, não via git pull. O fluxo é:

1. Edita e comita aqui (qualquer sessão com esse repo consegue, sem precisar de SSH).
2. Uma sessão com acesso à VM (chave `stomp_new_key`, hoje só em `C:\Users\jever\Documents\Bot\` no computador do Jeverson) puxa as mudanças daqui e roda o pipeline de deploy de verdade: checa sintaxe, faz backup, copia pro `~/eventbot/` na VM via scp, reinicia o serviço (`eventbot`/`eventsite`/`contentsbr`) e confere o log.

Ver `claude/HANDOFF_CONTINUAR_CLAUDE_CODE.md` no projeto "Bot Discord + Site" (claude.ai) pra mais contexto, pendências e os outros acessos (site novo, token do GitHub, etc).
