# Guia de atualização na FadeHost — Hapination Live

Este guia cobre como colocar a nova versão (refatorada em módulos) na sua hospedagem FadeHost, preservando o banco de dados e os dados existentes.

## ⚠️ Antes de começar
1. **Faça backup do banco atual** na FadeHost: baixe o arquivo `data/hapination.db` (e a pasta `data/uploads/`) para o seu computador. A nova versão também faz backup automático antes de migrar, mas ter uma cópia sua é segurança extra.
2. Anote o conteúdo do seu `.env` atual (`DISCORD_BOT_TOKEN` e `OWNER_DISCORD_ID`). Ele será reaproveitado — **não** precisa ser refeito.
3. Confirme que o Python da FadeHost é 3.10+.

## Passo a passo

### 1. Pare o bot
No painel da FadeHost, pare o bot (Stop / Desligar). Não apague os arquivos ainda.

### 2. Baixe a nova versão
Dois caminhos:

**Opção A — via git (recomendado):**
```bash
cd /pasta/do/bot
git fetch origin
git checkout feature/hapination-audit-completa   # branch com as alterações
# se preferir já integrado na main, após fazer merge do PR: git pull origin main
```

**Opção B — manual (se não usar git na FadeHost):**
Extraia o zip `hapination-live-nova-versao.zip` por cima da pasta existente, **mantendo** `.env` e `data/`. Não substitua `.env` nem `data/`.

### 3. Instale dependências
```bash
pip install -r requirements.txt
```
(As dependências continuam sendo apenas `discord.py` e `python-dotenv` — leves, compatíveis com memória limitada.)

### 4. Migração do banco (automática e segura)
A migração roda **automaticamente** na primeira inicialização. Antes de aplicar, o sistema faz um backup em `data/backups/pre_migration_<data>.db` usando a API segura do SQLite (não copia o arquivo em uso). Nenhuma tabela/coluna antiga é apagada.

Para rodar a migração sem iniciar o bot (opcional, para validar):
```bash
python -c "from core.database import migrate; migrate(); print('Migracao OK')"
```

### 5. Inicie o bot
No painel da FadeHost, inicie o bot. Verifique os logs:
- `Banco de dados migrado e verificado.`
- `Cog carregado: cogs.users_credits` (e os outros 5 cogs)
- `X comandos sincronizados.`
- `Hapination Live conectado como ...`

Se algum cog falhar, o log mostra o erro — o bot continua rodando com os demais.

### 6. Verificação pós-implantação (testes manuais no Discord)
1. `/painel` — confirme que a categoria `🎬 HAPINATION LIVE` e os 5 canais foram criados.
2. Clique em "Abrir meu painel" — confirme o canal privado e os botões.
3. Teste um botão (ex: 💲 Preços) — ele deve responder diretamente, sem dizer "use o comando /X".
4. `/admin creditos adicionar` (como proprietário) — valide que só o dono consegue.
5. Envie um vídeo, inicie uma sessão simulada, aguarde o aviso de 5 min (se usar duração curta de teste), encerre e confira o resumo por DM.
6. `/admin backup criar` — valide o backup em `data/backups/`.
7. `/admin relatorio` — confira os números.

### 7. Rollback (se precisar)
1. Pare o bot.
2. Restaure o backup `data/backups/pre_migration_*.db` por cima de `data/hapination.db`.
3. Volte o código para a versão anterior (`git checkout main` ou extraia o zip antigo).
4. Inicie o bot.

## Estrutura de arquivos na nova versão
```
bot.py                      # entrada fina (antes: 932 linhas monolítico)
core/
  config.py                 # variáveis de ambiente, cores, constantes
  database.py               # conexão + migrações idempotentes + backup seguro
  common.py                 # embeds, checks, cooldown, logger, lock de instância
  services.py               # matemática de cobrança + leituras compartilhadas
cogs/
  users_credits.py          # usuários, apelidos, suspensão, créditos, admins
  sessions_prices.py        # durações/preços + sessões + cobrança por minuto
  videos.py                 # upload, listagem, exclusão, limpeza 24h
  panel.py                  # canais automáticos + painel público/privado
  admin.py                  # painel admin, relatórios, anúncios, manutenção
  support_background.py     # tickets + loops de fundo + backups
data/                       # preservado (banco, uploads, logs, backups)
```

## Variáveis de ambiente (inalteradas)
Mantenha seu `.env` atual:
```
DISCORD_BOT_TOKEN=seu_token
OWNER_DISCORD_ID=seu_id
```
Também é aceito `DISCORD_TOKEN`. Novas opcionais: `MAX_UPLOAD_MB=25`.
