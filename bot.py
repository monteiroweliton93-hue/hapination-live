import os
import sqlite3
from dotenv import load_dotenv
load_dotenv()
from pathlib import Path
import discord
from discord import app_commands
from discord.ext import commands, tasks
from datetime import timedelta

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DATA_DIR = Path("data")
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "hapination.db"
SESSION_PRICE_CENTS = 50
MAX_UPLOAD_MB = 25

DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    with db() as con:
        con.execute("""CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            credits_cents INTEGER NOT NULL DEFAULT 0
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS videos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            path TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )""")
        con.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            video_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            expires_at DATETIME,
            ended_at DATETIME
        )""")
        # Migração simples para instalações que já tenham criado a tabela antiga.
        columns = [row['name'] for row in con.execute("PRAGMA table_info(sessions)").fetchall()]
        if 'expires_at' not in columns:
            con.execute("ALTER TABLE sessions ADD COLUMN expires_at DATETIME")

def ensure_user(user_id):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))

def get_credits(user_id):
    ensure_user(user_id)
    with db() as con:
        row = con.execute("SELECT credits_cents FROM users WHERE user_id=?", (user_id,)).fetchone()
        return row["credits_cents"]

class HapinationBot(commands.Bot):
    async def setup_hook(self):
        init_db()
        await self.tree.sync()

intents = discord.Intents.default()
bot = HapinationBot(command_prefix="!", intents=intents)

@tasks.loop(minutes=1)
async def expire_sessions():
    with db() as con:
        con.execute("""
            UPDATE sessions
            SET status='expired', ended_at=CURRENT_TIMESTAMP
            WHERE status='running' AND expires_at IS NOT NULL
              AND expires_at <= CURRENT_TIMESTAMP
        """)

@bot.event
async def on_ready():
    if not expire_sessions.is_running():
        expire_sessions.start()
    print(f"Hapination Live conectado como {bot.user}")

@bot.tree.command(name="painel", description="Mostra o painel de demonstração do Hapination Live")
async def painel(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎬 Hapination Live",
        description=(
            "Protótipo de gerenciamento de sessões. "
            "Nesta versão, as transmissões são apenas simuladas; não há conexão com o TikTok."
        ),
        color=discord.Color.blurple()
    )
    embed.add_field(name="Preço demonstrativo", value="R$ 0,50 em créditos fictícios por sessão", inline=False)
    embed.add_field(name="Comandos", value=(
        "`/enviar_video` — cadastrar um vídeo de teste\n"
        "`/meus_videos` — listar seus vídeos\n"
        "`/iniciar` — iniciar uma sessão simulada\n"
        "`/encerrar` — encerrar sua sessão\n"
        "`/saldo` — consultar créditos fictícios"
    ), inline=False)
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=embed, ephemeral=True)

@bot.tree.command(name="saldo", description="Mostra seus créditos fictícios de teste")
async def saldo(interaction: discord.Interaction):
    cents = get_credits(interaction.user.id)
    await interaction.response.send_message(
        f"Seu saldo de demonstração: **R$ {cents/100:.2f}** (sem dinheiro real).",
        ephemeral=True
    )

@bot.tree.command(name="adicionar_creditos_teste", description="(Admin) Adiciona créditos fictícios")
@app_commands.describe(usuario="Pessoa que receberá créditos", centavos="Quantidade em centavos")
@app_commands.checks.has_permissions(administrator=True)
async def adicionar_creditos_teste(interaction: discord.Interaction, usuario: discord.Member, centavos: app_commands.Range[int, 1, 10000]):
    ensure_user(usuario.id)
    with db() as con:
        con.execute("UPDATE users SET credits_cents=credits_cents+? WHERE user_id=?", (centavos, usuario.id))
    await interaction.response.send_message(
        f"Adicionados **R$ {centavos/100:.2f}** fictícios para {usuario.mention}.",
        ephemeral=True
    )

@bot.tree.command(name="enviar_video", description="Envia um vídeo para a biblioteca de testes")
@app_commands.describe(arquivo="Vídeo próprio para teste")
async def enviar_video(interaction: discord.Interaction, arquivo: discord.Attachment):
    if not (arquivo.content_type or "").startswith("video/"):
        await interaction.response.send_message("Envie um arquivo de vídeo válido.", ephemeral=True)
        return
    if arquivo.size > MAX_UPLOAD_MB * 1024 * 1024:
        await interaction.response.send_message(f"O limite deste protótipo é {MAX_UPLOAD_MB} MB.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    safe_name = Path(arquivo.filename).name.replace("/", "_").replace("\\\\", "_")
    stored_name = f"{interaction.user.id}_{discord.utils.utcnow().timestamp():.0f}_{safe_name}"
    target = UPLOAD_DIR / stored_name
    try:
        await arquivo.save(target)
    except Exception:
        await interaction.followup.send("Não consegui salvar o arquivo. Tente novamente.", ephemeral=True)
        return

    with db() as con:
        cur = con.execute(
            "INSERT INTO videos(user_id, filename, path) VALUES (?, ?, ?)",
            (interaction.user.id, safe_name, str(target))
        )
        video_id = cur.lastrowid
    await interaction.followup.send(f"Vídeo cadastrado! ID: **{video_id}** — `{safe_name}`", ephemeral=True)

@bot.tree.command(name="meus_videos", description="Lista seus vídeos de teste")
async def meus_videos(interaction: discord.Interaction):
    with db() as con:
        rows = con.execute(
            "SELECT id, filename FROM videos WHERE user_id=? ORDER BY id DESC LIMIT 10",
            (interaction.user.id,)
        ).fetchall()
    if not rows:
        await interaction.response.send_message("Você ainda não enviou vídeos.", ephemeral=True)
        return
    lines = [f"**{r['id']}** — {r['filename']}" for r in rows]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)

@bot.tree.command(name="iniciar", description="Inicia uma sessão simulada com um vídeo seu")
@app_commands.describe(video_id="ID mostrado em /meus_videos")
async def iniciar(interaction: discord.Interaction, video_id: int):
    ensure_user(interaction.user.id)
    with db() as con:
        existing = con.execute(
            "SELECT id FROM sessions WHERE user_id=? AND status='running' "
            "AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)",
            (interaction.user.id,)
        ).fetchone()
        if existing:
            await interaction.response.send_message("Você já tem uma sessão ativa.", ephemeral=True)
            return
        video = con.execute(
            "SELECT id FROM videos WHERE id=? AND user_id=?",
            (video_id, interaction.user.id)
        ).fetchone()
        if not video:
            await interaction.response.send_message("Não encontrei esse vídeo na sua biblioteca.", ephemeral=True)
            return
        user = con.execute("SELECT credits_cents FROM users WHERE user_id=?", (interaction.user.id,)).fetchone()
        if user["credits_cents"] < SESSION_PRICE_CENTS:
            await interaction.response.send_message(
                "Créditos fictícios insuficientes. Peça a um administrador para adicionar créditos de teste.",
                ephemeral=True
            )
            return
        con.execute("UPDATE users SET credits_cents=credits_cents-? WHERE user_id=?",
                    (SESSION_PRICE_CENTS, interaction.user.id))
        cur = con.execute(
            "INSERT INTO sessions(user_id, video_id, status, expires_at) "
            "VALUES (?, ?, 'running', datetime('now', '+24 hours'))",
            (interaction.user.id, video_id)
        )
        session_id = cur.lastrowid
    await interaction.response.send_message(
        f"🟢 Sessão simulada **#{session_id}** iniciada com o vídeo **#{video_id}**. "
        "A validade é de **24 horas**. Depois disso, será necessário usar `/renovar` "
        "ou iniciar uma nova sessão. Nenhuma transmissão externa foi iniciada.",
        ephemeral=True
    )

@bot.tree.command(name="renovar", description="Renova sua sessão por mais 24 horas")
async def renovar(interaction: discord.Interaction):
    ensure_user(interaction.user.id)
    with db() as con:
        active = con.execute(
            "SELECT id FROM sessions WHERE user_id=? AND status='running'",
            (interaction.user.id,)
        ).fetchone()
        if active:
            await interaction.response.send_message(
                "Você já tem uma sessão ativa. Ela não precisa ser renovada antes do vencimento.",
                ephemeral=True
            )
            return
        previous = con.execute(
            "SELECT id FROM sessions WHERE user_id=? AND status IN ('expired','ended') "
            "ORDER BY id DESC LIMIT 1",
            (interaction.user.id,)
        ).fetchone()
        if not previous:
            await interaction.response.send_message(
                "Não encontrei uma sessão anterior para renovar. Use `/iniciar` para abrir uma nova.",
                ephemeral=True
            )
            return
        user = con.execute(
            "SELECT credits_cents FROM users WHERE user_id=?",
            (interaction.user.id,)
        ).fetchone()
        if user["credits_cents"] < SESSION_PRICE_CENTS:
            await interaction.response.send_message(
                "Créditos fictícios insuficientes. Peça a um administrador para adicionar créditos de teste.",
                ephemeral=True
            )
            return
        con.execute(
            "UPDATE users SET credits_cents=credits_cents-? WHERE user_id=?",
            (SESSION_PRICE_CENTS, interaction.user.id)
        )
        con.execute(
            "UPDATE sessions SET status='running', started_at=CURRENT_TIMESTAMP, "
            "expires_at=datetime('now', '+24 hours'), ended_at=NULL WHERE id=?",
            (previous["id"],)
        )
    await interaction.response.send_message(
        f"🔄 Sessão **#{previous['id']}** renovada por mais **24 horas**. "
        "Cobrança ainda é simulada com créditos fictícios.",
        ephemeral=True
    )

@bot.tree.command(name="encerrar", description="Encerra sua sessão simulada")
async def encerrar(interaction: discord.Interaction):
    with db() as con:
        row = con.execute(
            "SELECT id FROM sessions WHERE user_id=? AND status='running'",
            (interaction.user.id,)
        ).fetchone()
        if not row:
            await interaction.response.send_message("Você não tem sessão ativa.", ephemeral=True)
            return
        con.execute(
            "UPDATE sessions SET status='ended', ended_at=CURRENT_TIMESTAMP WHERE id=?",
            (row["id"],)
        )
    await interaction.response.send_message(f"⏹️ Sessão **#{row['id']}** encerrada.", ephemeral=True)

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    message = "Não foi possível executar o comando."
    if isinstance(error, app_commands.MissingPermissions):
        message = "Você precisa ser administrador para usar esse comando."
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)

if not TOKEN:
    raise SystemExit("Defina DISCORD_BOT_TOKEN no arquivo .env ou nas variáveis de ambiente.")
bot.run(TOKEN)
