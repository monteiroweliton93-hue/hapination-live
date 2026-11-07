# Changelog — Hapination Live

Todas as mudanças relevantes deste projeto são documentadas aqui.
Formato baseado em [Keep a Changelog](https://keepachangelog.com/pt-BR/1.0.0/).
Versões no estilo [Semantic Versioning](https://semver.org/lang/pt-BR/).

## [2.0.0] — 2026-11-07

Refatoração modular completa (6 cogs + 1 cog de comandos públicos).
Banco de dados com migrações idempotentes e não destrutivas (preserva os dados
da versão antiga na FadeHost).

### Adicionado
- Arquitetura modular em `cogs/` (7 extensões): `UsersCredits`, `SessionsPrices`,
  `VideosCog`, `PanelCog`, `AdminCog`, `SupportBackground`, `Publicos`.
- Comandos públicos preservados da versão antiga: `/saldo`, `/precos`, `/horas`
  (novo cog `cogs/publicos.py`).
- Painel privado por usuário (`👤・painel-<id>`) com botões para enviar vídeo,
  iniciar/renovar/encerrar sessão, ver vídeos, histórico, preços e fechar painel.
- Painel administrativo (`🔒・administracao`) com botões de atalho: suspender,
  reativar, alterar apelido, encerrar sessão, excluir vídeo, anunciar.
- Sistema de tickets de suporte (`🎫・suporte`) com fechamento automático por
  inatividade e registro em `📋・registros`.
- Cobrança proporcional minuto a minuto (milésimos de centavo, sem erro de
  ponto flutuante), com teto no preço total da duração.
- Tolerância de 2 minutos: sessões encerradas em ≤120s têm o valor estornado.
- Recuperação automática após downtime (leitura de `last_heartbeat`, extensão
  de `expires_at`, registro em `downtime_seconds`).
- Avisos: 5 min antes de expirar e quando o saldo cobre ≤5 min de sessão.
- Modo manutenção (bloqueia novas sessões e uploads; ligar por admin+owner,
  desligar só pelo proprietário).
- Backups automáticos diários com retenção configurável e limpeza de arquivos
  órfãos.
- Filtro de apelidos (3–20 caracteres, palavrões, leetspeak, termos de equipe,
  anti-impersonation).
- Auto-cadastro de usuários em toda interação (`ensure_user`).
- Controle de armazenamento por usuário e retenção automática de vídeos.
- Logs de crédito com `balance_after` e tipos `add|remove|correct|consume|refund|manual`.
- Tabela `settings` para configuração em runtime (limite de sessões, upload,
  retenção, etc.).
- Testes: `tests/test_core.py`, `tests/test_integration.py` (migração sobre
  esquema antigo) e `tests/smoke_test.py` (carga das 7 extensões).

### Corrigido
- **Cola entre cogs:** `AdminCog.delete_video` agora procura o cog de vídeos
  pelos nomes `VideosCog` e `Videos` (fallback), em vez de apenas `Videos`
  (que retornava `None`).
- **Métodos expostos:** `UsersCredits.suspend_user`, `reactivate_user` e
  `set_nickname` adicionados para os atalhos do painel admin (antes os botões
  respondiam "atalho indisponível").
- **Encerramento forçado:** `SessionsPrices.force_end_session(session_id)`
  adicionado, reutilizando a finalização com tolerância de 2min, refund e
  resumo por DM.
- **Migração em banco populado:** `ALTER TABLE ADD COLUMN ... DEFAULT
  CURRENT_TIMESTAMP` falhava em tabelas com linhas (SQLite da FadeHost).
  Corrigido: coluna adicionada sem default + backfill via `UPDATE`, e
  `ensure_user` agora grava `created_at` no cadastro.
- `requirements.txt` estava em formato de tabela quebrado; corrigido para
  linhas simples (`discord.py>=2.4,<3`, `python-dotenv>=1.0,<2`).
- `.env.example` ampliado com `OWNER_DISCORD_ID`, `DISCORD_TOKEN` (comentado)
  e `MAX_UPLOAD_MB=25`.

### Segurança
- Separação clara de privilégios: administradores **nunca** alteram créditos
  (recebem erro específico); apenas o proprietário.
- Dados privados (saldo, histórico, nomes de arquivos) sempre em mensagens
  `ephemeral` ou DM.
- Lock de instância única para evitar duas instâncias rodando simultaneamente.
- Backup prévio automático antes de cada migração.

### Removido / Não incluído
- Nenhuma integração real com TikTok, pagamentos reais ou APIs externas
  (permanece como simulação; documentado como pendente).
- Comandos de backup (`/admin backup criar|listar|restaurar`) não foram
  implementados nesta versão (a infraestrutura de `backups` e `backup_db`
  existe, mas os comandos slash ficam para uma próxima release).

## [1.0.0] — versão anterior (protótipo)
- Protótipo monolítico com comandos básicos: `/painel`, `/saldo`,
  `/adicionar_creditos_teste`, `/enviar_video`, `/meus_videos`, `/iniciar`,
  `/renovar`, `/encerrar`. Sessões fixas de 24h, créditos fictícios.
