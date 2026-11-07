# Especificação de Implementação — Hapination Live (refatoração modular)

Contrato obrigatório para todos os cogs. Leia este arquivo inteiro antes de codar.

## 0. Regras gerais
- Python 3.10+, discord.py 2.x, SQLite. Sem dependências pesadas (FadeHost tem memória/armazenamento limitados). Só `discord.py` e `python-dotenv` em requirements.
- UI e comentários em **português**.
- Dados privados (saldo, nomes de arquivos, histórico) SEMPRE em mensagens `ephemeral` ou canal privado do usuário. NUNCA em canal público.
- Todo comando/ação sensível passa por `core.common.check_cooldown` (10s) e pelos checks `owner_only` / `admin_only` / `not_suspended` conforme aplicável.
- Use `core.database.get_db()` (context manager) para tudo de banco. Não abra `sqlite3.connect` diretamente.
- Use `core.common.embed/success_embed/warn_embed/error_embed/info_embed` (tema preto+roxo). Use `core.common.money` para valores.
- Use `core.services` para leituras compartilhadas e matemática de cobrança — NÃO duplique essas funções.
- NUNCA afirme que uma transmissão real do TikTok iniciou. Sempre que mencionar sessão, diga "sessão simulada". Créditos são fictícios.
- Botões executam a função diretamente (modal/view/fluxo). Nunca responda "use o comando /X".
- Views persistentes (botões do painel público e privado) usam `timeout=None` e `custom_id` com prefixo `hap_`; registradas via `bot.add_view` no `cog_load` para sobreviver a reinícios.

## 1. Esquema do banco (JÁ CRIADO por core/database.py — não recrie)
- `users(user_id PK, credits_cents, nickname, status[active|suspended], suspended_reason, created_at, last_seen_at)`
- `videos(id PK, user_id, filename, path, created_at, size_bytes, mime_type, deleted, deleted_at, thumbnail_path)`
- `sessions(id PK, user_id, video_id, status[running|ended|expired], started_at, expires_at, ended_at, duration_hours, price_cents, billed_cents, billed_millicents, last_billed_at, warned_expiry, warned_low_credit, downtime_seconds, renewals)`
- `credit_logs(id PK, owner_id, user_id, amount_cents, created_at, type, reason, balance_after, session_id)` — type: add|remove|correct|consume|refund|manual
- `durations(id PK, hours UNIQUE, price_cents, active, created_at)` — até 48h
- `admins(user_id PK, added_by, created_at)` — proprietário é implícito (env var), não fica nesta tabela
- `tickets(id PK, user_id, channel_id UNIQUE, status[open|closed], subject, opened_at, last_activity_at, closed_at, closed_by)`
- `announcements(id PK, author_id, channel_id, message_id, content, created_at)`
- `settings(key PK, value, updated_at)` — chaves: max_sessions_per_user, max_upload_mb, video_retention_hours, ticket_auto_close_hours, backup_retention_days, maintenance_mode, maintenance_message, suspended_session_policy, last_heartbeat
- `backups(id PK, filename, path, size_bytes, created_at, status)`

## 2. Cogs (um arquivo cada, em `cogs/`, classe com sufixo `Cog`)

### 2.1 `cogs/users_credits.py` — UsersCreditsCog
Auto-cadastro (ensure_user em toda interação), apelido 3–20 chars (próprio usuário e proprietário; filtro básico contra nomes ofensivos e de staff/proprietário), suspensão/reativação SÓ proprietário, créditos add/remove/correção SÓ proprietário (admins NUNCA — retorne erro específico), histórico de créditos (próprio; admin consulta qualquer um, só visualização), gestão de admins SÓ proprietário.
Métodos expostos (para outros cogs chamarem via `bot.get_cog("UsersCredits")`):
- `async def notify_credit_change(self, user_id: int, delta: int, reason: str) -> None` — DM o usuário.
- `async def is_suspended(self, user_id: int) -> bool`
Comandos: `/definir_apelido`, `/historico_creditos`, `/admin creditos adicionar|remover|corrigir` (owner), `/admin usuario suspender|reativar|ver` (suspender owner), `/admin admin adicionar|remover|listar` (owner).

### 2.2 `cogs/sessions_prices.py` — SessionsPricesCog
CRUD de durações/preços via Discord (até 48h; admins e proprietário). Sessões: iniciar com confirmação (mostra duração, preço, saldo restante — View com Confirmar/Cancelar), limite de N sessões simultâneas (settings, padrão 2), renovação de sessão ATIVA (extende expires_at e cobra proporcional pelo tempo extra — NÃO só após término), encerramento manual, loop `tasks.loop(minutes=1)` de cobrança proporcional (usa `core.services.billing_step`, teto em price_cents, atualiza last_billed_at), aviso 5 min antes de expirar (warned_expiry), aviso de saldo baixo quando <=5 min de crédito (warned_low_credit), encerramento automático ao expirar ou saldo esgotar, resumo ao finalizar (duração usada, créditos consumidos, horário) por DM + histórico. Recuperação após downtime: em `cog_load`/on_ready, lê `last_heartbeat`, calcula gap, estende `expires_at` das sessões running pelo gap, soma em downtime_seconds, zera warned flags se ainda longe, notifica usuários, atualiza `last_heartbeat` a cada minuto no loop. Tolerância 2 min: se sessão encerra com tempo usado <=120s, estorna (refund) o que foi cobrado. Sem cobrança duplicada: use colunas billed_* e UPDATE atômico.
Métodos expostos:
- `async def start_flow(self, interaction) -> None` (botão do painel chama isto)
- `async def renew_flow(self, interaction) -> None`
- `async def end_flow(self, interaction) -> None`
- `async def active_summary(self, user_id: int) -> list[dict]` (id, vídeo, início, expira, consumido até agora)
- `async def history_summary(self, user_id: int, limit=10) -> list[dict]`
Comandos: `/iniciar`, `/renovar`, `/encerrar`, `/sessoes`, `/admin duracao criar|alterar|excluir|listar`.

### 2.3 `cogs/videos.py` — VideosCog
Upload via comando `/enviar_video` (Attachment) e via "modo upload" no canal privado (bot ativa flag e aceita anexo dropado no canal privado — on_message filtrado). Valida vídeo, limite de tamanho (settings max_upload_mb), salva em `data/uploads/<user_id>/`, registra tamanho/mime. Miniatura: somente se `ffmpeg` estiver disponível no PATH (checar com shutil.which); se não existir, pular silenciosamente e documentar — não instalar nada. Listagem própria. Exclusão manual com View de confirmação (2 botões). Exclusão automática após `video_retention_hours` (método `async def cleanup_old(self)` chamado pelo loop de background). Controle de armazenamento: soma bytes por usuário, recusa upload acima do limite (settings, padrão 500MB por usuário — use settings key `user_storage_mb`). Acesso: só proprietário, dono do vídeo e admins. Apaga arquivo do disco ao excluir (com try/except). Sem arquivos temporários: salve direto no destino final.
Métodos expostos: `async def list_summary(self, user_id) -> list[dict]`, `async def cleanup_old(self) -> int`, `async def enable_upload_mode(self, interaction) -> None`.
Comandos: `/enviar_video`, `/meus_videos`, `/excluir_video`.

### 2.4 `cogs/panel.py` — PanelCog
Garante categoria `🎬 HAPINATION LIVE` e os 5 canais (🎛️・painel, 🎫・suporte, 📢・avisos, 📋・registros, 🔒・administracao) no on_ready — cria os ausentes, preserva os existentes (busca por nome normalizado). Envia mensagem de painel público fixa em 🎛️painel com botão "Abrir meu painel" (view persistente). Ao clicar, cria canal de texto privado `👤・painel-<id>` (overwrites: usuário + proprietário + admins leem/escrevem; @everyone não), posta embed do painel privado com: saldo atual, sessões ativas (chama SessionsPricesCog.active_summary), vídeos cadastrados (VideosCog.list_summary), histórico resumido, tabela de durações/preços, e botões: 📤 Enviar vídeo (chama VideosCog.enable_upload_mode), ▶️ Iniciar sessão (SessionsPricesCog.start_flow), 🔄 Renovar (renew_flow), ⏹️ Encerrar (end_flow), 🎬 Meus vídeos (lista ephemeral), 📜 Histórico (SessionsPricesCog.history_summary), 💲 Preços, ❌ Fechar painel (apaga o canal após confirmação). Comando `/painel` reenvia o painel público (admin/owner). Registra views persistentes no cog_load. Tudo em tema preto+roxo.

### 2.5 `cogs/admin.py` — AdminCog
Painel administrativo em 🔒・administracao (embed + botões persistentes) e comandos `/admin ...`. Funcionalidades: gestão de usuários (ver, pesquisar, ver saldo/histórico — SÓ LEITURA de créditos; alteração de créditos é proibida para admin), gestão de sessões (listar ativas, encerrar forçadamente), gestão de vídeos (listar todos, excluir), relatórios (embed + arquivo CSV opcional: usuários cadastrados, sessões realizadas/ativas, créditos adicionados/consumidos, vídeos armazenados, desempenho — uptime/erros, movimentação financeira SIMULADA), anúncios (enviar embed para 📢avisos, registrar em announcements), manutenção (ligar por admin+owner, desligar SÓ owner; ao ligar, avisa em 📢avisos; bloqueia novas sessões e uploads — checado nos cogs correspondentes via `core.services.maintenance_on`), gestão de admins fica em UsersCreditsCog (owner). Suspensão/reativação fica em UsersCreditsCog (owner) — AdminCog apenas oferece o atalho que chama aquele cog.

### 2.6 `cogs/support_background.py` — SupportBackgroundCog
Tickets: botão persistente em 🎫suporte "Abrir atendimento" cria canal privado `🎫・ticket-<id>` (usuário + equipe), registra em tickets, marca last_activity_at a cada mensagem (on_message no canal de ticket), botão Fechar (equipe ou usuário), fechamento automático após `ticket_auto_close_hours` de inatividade (loop). Registro em 📋registros.
Background loops (tasks.loop): a cada 6h — VideosCog.cleanup_old; a cada 1h — fechar tickets inativos; diariamente — backup via `core.database.backup_db` para data/backups/, registra em backups, apaga backups mais antigos que `backup_retention_days`, notifica proprietário em DM em caso de falha; a cada 1h — limpeza de arquivos órfãos (vídeos marcados deleted mas com arquivo ainda em disco; __pycache__ não). Também heartbeat? Não — heartbeat é do SessionsPricesCog (a cada 1 min).
Backups: `/admin backup criar` (manual, admin+owner), `/admin backup listar`, `/admin backup restaurar <id>` (SÓ owner, com confirmação em duas etapas: valida o arquivo de backup abre conexão e checa schema_version; faz backup do estado atual; sobrescreve DB via API sqlite backup; roda migrate; avisa para reiniciar o bot). Exportação: enviar arquivo .db via DM para owner.

## 3. Entrada
`bot.py` (já escrito) carrega todos os cogs acima. Não mude bot.py a não ser para ajustar nomes de cog.

## 4. O que NÃO fazer
- Não invente integração com TikTok, pagamentos reais ou APIs externas. Documente como "pendente".
- Não apague tabelas/colunas existentes.
- Não coloque tokens no código.
- Não use `print` para erros — use `core.common.log`.
- Não bloqueie o loop de eventos com I/O demorado (use asyncio.to_thread para cópias grandes).
