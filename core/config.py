"""Configuração central do Hapination Live.

Lê variáveis de ambiente preservando os nomes já usados no projeto:
  - DISCORD_BOT_TOKEN ou DISCORD_TOKEN
  - OWNER_DISCORD_ID
Nunca coloque tokens no código. Este módulo só lê do ambiente.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# --- Tokens / identidade ---
TOKEN: str | None = os.getenv("DISCORD_BOT_TOKEN") or os.getenv("DISCORD_TOKEN")
_owner_raw = os.getenv("OWNER_DISCORD_ID", "0").strip()
OWNER_DISCORD_ID: int = int(_owner_raw) if _owner_raw.isdigit() else 0

# --- Caminhos (relativos ao diretório do projeto, compatível FadeHost/Termux) ---
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "hapination.db"
LOCK_PATH = DATA_DIR / "bot.lock"
THUMBNAIL_DIR = DATA_DIR / "thumbnails"

for _d in (DATA_DIR, UPLOAD_DIR, BACKUP_DIR, LOG_DIR, THUMBNAIL_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# --- Limites padrão (podem ser sobrescritos pela tabela `settings`) ---
DEFAULT_MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))
DEFAULT_MAX_SESSIONS_PER_USER = 2
DEFAULT_VIDEO_RETENTION_HOURS = 24
DEFAULT_TICKET_AUTO_CLOSE_HOURS = 24
DEFAULT_BACKUP_RETENTION_DAYS = 30
MAX_DURATION_HOURS = 48
GRACE_SECONDS = 120          # tolerância de 2 minutos em falhas/encerramento
LOW_CREDIT_WARN_MINUTES = 5  # aviso quando restam <=5 min de crédito
EXPIRY_WARN_MINUTES = 5      # aviso 5 min antes do encerramento
COMMAND_COOLDOWN_SECONDS = 10

# --- Identidade visual: preto + roxo, estilo tecnológico ---
COLOR_BG = 0x0D0D1A        # quase preto
COLOR_PRIMARY = 0x7B2CBF   # roxo principal
COLOR_ACCENT = 0x9D4EDD    # roxo claro
COLOR_SUCCESS = 0x00C896   # verde esmeralda (contraste em fundo escuro)
COLOR_WARN = 0xF5A623      # âmbar
COLOR_DANGER = 0xE63946    # vermelho
COLOR_INFO = 0x4CC9F0      # ciano tecnológico

CATEGORY_NAME = "🎬 HAPINATION LIVE"
CHANNELS = {
    "painel": "🎛️・painel",
    "suporte": "🎫・suporte",
    "avisos": "📢・avisos",
    "registros": "📋・registros",
    "administracao": "🔒・administracao",
}
# Nome exato do canal de administração (sem emoji problemático) — preservar canais existentes.
ADMIN_CHANNEL_NAME = "🔒・administracao"

FOOTER_TEXT = "Hapination Live • sessões simuladas • créditos fictícios"

# Preços semente (usados apenas se a tabela `durations` estiver vazia na migração).
SEED_PRICES = {1: 100, 6: 125, 12: 150, 18: 175, 24: 200}
