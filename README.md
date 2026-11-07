# Hapination Live

Bot Discord para gerenciar uploads de vídeo e **sessões simuladas** com sistema
de créditos fictícios, painel privado por usuário, painel administrativo,
tickets de suporte e cobrança proporcional minuto a minuto.

> **Este projeto é um protótipo educativo/simulado.**
> - ❌ **Não transmite para o TikTok** nem para nenhuma plataforma real.
> - ❌ **Não processa pagamentos reais** — todos os créditos são fictícios.
> - ❌ **Não contorna restrições de plataforma** nem APIs externas.
> - ✅ Simula todo o fluxo de gestão (cadastro, créditos, sessões, cobrança,
>   estornos, suporte, administração) dentro do Discord, usando SQLite local.

Para transmissões reais, use apenas fluxos de ingestão e APIs oficialmente
autorizados pela plataforma, respeitando termos de uso, direitos autorais,
privacidade e requisitos de idade.

---

## Requisitos

- Python **3.10+**
- `discord.py >= 2.4, < 3` e `python-dotenv >= 1.0, < 2` (ver `requirements.txt`)
- Uma aplicação/bot criada no [Discord Developer Portal](https://discord.com/developers/applications)
- Intents ativados no portal: **MESSAGE CONTENT INTENT** e **SERVER MEMBERS INTENT**
- Escopos de convite: `bot` + `applications.commands`
- Permissões do bot: gerenciar canais, gerenciar mensagens, enviar mensagens,
  anexar arquivos, usar comandos de aplicação, incorporar links.
- (Opcional) `ffmpeg` no `PATH` para gerar miniaturas de vídeo. Se não existir,
  as miniaturas são puladas silenciosamente — nada quebra.

Ambientes testados: Linux (FadeHost), Termux (Android), Windows, macOS.

---

## Instalação

```bash
# 1. Clonar/descompactar o projeto e entrar na pasta
cd hapination-live

# 2. Ambiente virtual (recomendado)
python3 -m venv .venv
# Linux/macOS/Termux:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate

# 3. Instalar dependências
pip install -r requirements.txt

# 4. Configurar variáveis de ambiente
cp .env.example .env
# Edite .env e coloque seu token e seu ID de proprietário
```

### Configuração (`.env`)

| Variável | Obrigatória | Descrição |
|---|---|---|
| `DISCORD_BOT_TOKEN` | sim | Token do bot (Developer Portal). |
| `DISCORD_TOKEN` | não | Nome alternativo para o mesmo token (comentado no exemplo). |
| `OWNER_DISCORD_ID` | sim | ID numérico do Discord do proprietário. É o único que pode alterar créditos, suspender/reativar usuários e gerenciar admins. |
| `MAX_UPLOAD_MB` | não | Tamanho máximo de upload de vídeo em MB. Padrão: `25`. |

Nunca compartilhe o arquivo `.env` nem publique tokens.

### Executar

```bash
python3 bot.py
```

Na primeira execução, o bot:
1. Cria a estrutura em `data/` (banco, uploads, backups, logs).
2. Aplica migrações idempotentes (com backup prévio automático).
3. Carrega os 7 cogs e sincroniza os comandos slash (pode levar até 1 hora para
   aparecerem em todos os servidores; use `guild` para teste instantâneo).
4. Cria a categoria `🎬 HAPINATION LIVE` e os 5 canais (painel, suporte, avisos,
   registros, administração) se não existirem.

Há um lock de instância única: se outra cópia do bot estiver rodando, a nova
encerra com mensagem explicativa.

---

## Estrutura de pastas

```
hapination-live/
├── bot.py                  # Ponto de entrada (fino): configura bot, migra, carrega cogs
├── requirements.txt
├── .env.example
├── README.md / CHANGELOG.md / SPEC.md / GUIA_FADEHOST.md
├── core/                   # Núcleo compartilhado (sem lógica de Discord)
│   ├── config.py           # Variáveis de ambiente, caminhos, cores, padrões
│   ├── common.py           # Embeds preto+roxo, cooldown, permissões, log, DM, lock
│   ├── database.py         # SQLite + migrações idempotentes (não destrutivas)
│   └── services.py         # Leituras compartilhadas e matemática de cobrança
├── cogs/                   # 7 extensões Discord (uma responsabilidade cada)
│   ├── users_credits.py    # Usuários, apelidos, suspensão, créditos, admins
│   ├── sessions_prices.py  # Durações/preços, sessões, cobrança, loops
│   ├── videos.py           # Upload, listagem, exclusão e limpeza de vídeos
│   ├── panel.py            # Painel público + painel privado por usuário
│   ├── admin.py            # Painel admin, relatórios, anúncios, manutenção
│   ├── support_background.py # Tickets de suporte + loops de fundo/backup
│   └── publicos.py         # /saldo, /precos, /horas (comandos públicos)
├── tests/
│   ├── test_core.py        # Testes unitários do núcleo
│   ├── test_integration.py # Migração sobre banco com esquema antigo
│   └── smoke_test.py       # Carga das 7 extensões + comandos + métodos de cola
└── data/                   # Criado em runtime (NÃO versionar)
    ├── hapination.db       # Banco SQLite (WAL)
    ├── uploads/<user_id>/  # Vídeos enviados
    ├── thumbnails/         # Miniaturas (se ffmpeg disponível)
    ├── backups/            # Backups automáticos e pré-migração
    └── logs/hapination.log
```

---

## Comandos

No total, **42 comandos slash** na árvore (incluindo grupos). Lista organizada:

### Qualquer usuário
| Comando | Descrição |
|---|---|
| `/saldo` | Seu saldo de créditos (ephemeral). |
| `/precos` | Tabela de preços por duração (ephemeral). |
| `/horas` | Durações disponíveis (ephemeral). |
| `/definir_apelido <apelido>` | Define seu apelido (3–20 chars, com filtro). |
| `/historico_creditos` | Suas últimas 20 movimentações (ephemeral). |
| `/enviar_video <anexo>` | Envia um vídeo para suas sessões. |
| `/meus_videos` | Lista seus vídeos (ephemeral). |
| `/excluir_video` | Exclui um vídeo seu (com confirmação). |
| `/iniciar` | Inicia uma sessão simulada (escolhe vídeo + duração). |
| `/renovar` | Renova/extende uma sessão ativa. |
| `/encerrar` | Encerra uma sessão ativa (resumo por DM). |
| `/sessoes` | Histórico das suas sessões. |

> Os botões do painel privado executam as mesmas ações diretamente (não é
> preciso digitar comandos).

### Equipe administrativa (admin + proprietário)
| Comando | Descrição |
|---|---|
| `/painel` | Reenvia o painel público (cria categoria/canais se faltarem). |
| `/admin usuario ver <usuário>` | Dados de um usuário (só leitura). |
| `/admin usuario historico <usuário>` | Histórico de créditos (só leitura). |
| `/admin usuario listar` | Lista usuários cadastrados. |
| `/admin videos listar` | Lista todos os vídeos. |
| `/admin videos excluir <id>` | Exclui qualquer vídeo. |
| `/admin sessoes ativas` | Lista sessões running. |
| `/admin sessoes encerrar <id>` | Encerra uma sessão forçadamente. |
| `/admin duracao listar` | Lista durações/preços. |
| `/admin duracao criar|alterar|excluir` | Gerencia durações (até 48h). |
| `/admin relatorio [dias]` | Relatório + CSV opcional (dados simulados). |
| `/admin manutencao ligar` | Ativa modo manutenção (admin+owner). |

### Somente proprietário
| Comando | Descrição |
|---|---|
| `/admin creditos adicionar|remover|corrigir` | Altera créditos (exclusivo do dono). |
| `/admin usuario suspender <usuário> <motivo>` | Suspende um usuário. |
| `/admin usuario reativar <usuário>` | Reativa um usuário suspenso. |
| `/admin usuario apelido <usuário> <apelido>` | Altera o apelido de qualquer um. |
| `/admin admin adicionar|remover|listar` | Gerencia administradores. |
| `/admin manutencao desligar` | Desativa manutenção (só owner). |

---

## Administração

- **Proprietário** (`OWNER_DISCORD_ID`): privilégio máximo. Altera créditos,
  suspende/reativa, gerencia admins, desliga manutenção.
- **Administradores** (tabela `admins`): podem gerenciar sessões, vídeos,
  durações, ver usuários/histórico, ligar manutenção, gerar relatórios.
  **Nunca** podem alterar créditos — recebem erro específico.
- **Manutenção:** quando ligada, novas sessões e uploads são bloqueados;
  sessões já abertas continuam rodando normalmente.
- **Configuração em runtime:** tabela `settings` (`max_sessions_per_user`,
  `max_upload_mb`, `video_retention_hours`, `ticket_auto_close_hours`,
  `backup_retention_days`, etc.).
- **Backups:** automáticos diários em `data/backups/`, com limpeza dos mais
  antigos. Antes de cada migração, um backup `pre_migration_*.db` é gerado.

---

## FAQ

**P: Os comandos não aparecem.**
R: A sincronização global de comandos slash pode levar até 1 hora. Para teste
instantâneo, sincronize em um servidor específico (`guild`). Verifique também
se o bot foi convidado com o escopo `applications.commands`.

**P: O bot não cria os canais.**
R: Confira se o bot tem permissão de gerenciar canais e se a categoria
`🎬 HAPINATION LIVE` não existe com nome diferente. O bot preserva canais
existentes buscando por nome normalizado.

**P: Um usuário suspenso ainda consegue usar o bot?**
R: Não — os comandos de sessão, vídeo e ticket passam pelo check
`not_suspended`. A suspensão é gravada em `users.status` e notificada por DM.

**P: Como funciona a cobrança?**
R: Proporcional, minuto a minuto, em milésimos de centavo (sem erro de ponto
flutuante), com teto no preço total da duração. Se a sessão encerrar em
≤2 minutos, o valor cobrado é estornado automaticamente.

**P: O bot caiu / a hospedagem reiniciou. Perco créditos?**
R: Não. O bot lê `last_heartbeat`, calcula o tempo de downtime e estende
`expires_at` das sessões ativas por esse gap (registrado em
`downtime_seconds`), sem cobrar pela indisponibilidade.

**P: Posso usar em produção com dinheiro real?**
R: Não. Esta versão é simulada. Para produção real, integre um gateway de
pagamento oficial e uma plataforma de transmissão autorizada, e cumpra as
regulamentações aplicáveis.

---

## Limitações

- Sem transmissão real (TikTok ou outra plataforma) — apenas simulação.
- Sem pagamentos reais — créditos fictícios.
- Sem comandos de backup restaurar (a infraestrutura `backup_db` e a tabela
  `backups` existem, mas `/admin backup ...` fica para próxima release).
- Miniaturas de vídeo só se `ffmpeg` estiver disponível.
- Sincronização de comandos slash global pode demorar.
- SQLite como banco — adequado para o porte do projeto; para escala muito
  grande, migrar para PostgreSQL.

---

## Testes

```bash
python3 -m py_compile bot.py core/*.py cogs/*.py tests/*.py
python3 tests/test_core.py
python3 tests/test_integration.py
python3 tests/smoke_test.py
```

Todos devem passar. O `test_integration.py` simula o upgrade de um banco da
versão antiga (esquema mínimo) para a nova versão, confirmando que os dados
são preservados e as colunas/tabelas novas são criadas.

---

Licença: uso educativo. Respeite os termos de serviço do Discord e das
plataformas de terceiros.
