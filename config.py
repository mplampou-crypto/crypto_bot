"""Configuration: environment variables + traders.json."""
import json
import os

from dotenv import load_dotenv

load_dotenv()

# ── Bybit ───────────────────────────────────────────────────────
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# ── Webhook security ────────────────────────────────────────────
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")

# ── Telegram ────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Server / misc ───────────────────────────────────────────────
PORT = int(os.getenv("PORT", "8080"))
TAKER_FEE = float(os.getenv("TAKER_FEE", "0.00055"))

# SQLite path: use Fly volume /data if mounted, else local file
DB_PATH = "/data/trades.db" if os.path.isdir("/data") else "trades.db"

# ── traders.json (symbols + strategies) ─────────────────────────
_CFG_PATH = os.path.join(os.path.dirname(__file__), "traders.json")


def load_config():
    """Returns (symbols, traders) from traders.json."""
    with open(_CFG_PATH) as f:
        data = json.load(f)
    return data.get("symbols", {}), data.get("traders", {})


SYMBOLS, TRADERS = load_config()
