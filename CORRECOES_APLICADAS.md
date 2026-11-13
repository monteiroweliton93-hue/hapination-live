## Correções aplicadas ao pacote de auditoria

- A cobrança proporcional usa arredondamento para cima em milésimos de centavo e mantém o teto do pacote contratado.
- O processamento de sessões não descarta minutos quando o loop de cobrança atrasa; a recuperação de downtime continua sendo responsável por excluir o período em que o bot ficou offline.
- O fechamento de tickets não sombreia mais a função `notify_user` com um parâmetro booleano.
- A inicialização aborta se um cog essencial falhar, em vez de sincronizar um bot parcialmente carregado.
- A migração não prossegue se o backup prévio do banco existente falhar; o backup é validado com `PRAGMA integrity_check`.
- O smoke test agora migra o banco temporário antes de carregar os cogs e verifica tabelas essenciais.
- O painel só marca a preparação como concluída quando todas as guildas forem processadas com sucesso.

## Validação neste ambiente

- `compileall`: aprovado.
- `tests/test_integration.py`: aprovado.
- Teste matemático manual dos cinco pacotes: todos atingem exatamente o preço contratado, sem ultrapassar o teto.
- `tests/test_core.py` e `tests/smoke_test.py`: não puderam ser executados neste ambiente porque `discord.py` não está instalado e o ambiente não conseguiu acessar o índice de pacotes. Execute-os no Termux, onde as dependências do projeto já estão instaladas.

## Segurança na implementação

Este ZIP não inclui `.env`, banco de produção, logs, uploads ou backups locais. Não substitua a pasta `data/` nem o `.env` do servidor ao implementar. Faça backup desses itens antes de atualizar os arquivos.
