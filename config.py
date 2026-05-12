import os
from dotenv import load_dotenv
load_dotenv()

# --- TELEGRAM ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_CHAT_ID      = int(os.getenv("ADMIN_CHAT_ID", "0"))

# --- BYBIT ---
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BYBIT_TESTNET    = os.getenv("BYBIT_TESTNET", "false").lower() == "true"

# --- NEWS ---
NEWSAPI_KEY = os.getenv("NEWSAPI_KEY")

# --- DATABASE ---
DATABASE_URL = os.getenv("DATABASE_URL")

# --- TRADING DEFAULTS ---
# 100 USDT × 50x = 5,000 USDT position
# TP +1.0% → +50 USDT κέρδος
# SL -0.5% → -25 USDT ζημιά
# RR 1:2 → χρειάζεσαι μόνο 35% winrate για κέρδος
DEFAULT_LEVERAGE  = 50
DEFAULT_USDT      = 50
DEFAULT_SL_PCT    = 0.5    # -25 USDT
DEFAULT_TP_PCT    = 1.0    # +50 USDT

MIN_SIGNAL_SCORE  = 75

# --- BREAK EVEN ---
BREAKEVEN_TRIGGER_PCT = 50
TRAILING_STOP_ACTIVE  = True

# --- RISK MANAGEMENT ---
MAX_DAILY_TRADES       = 15
MAX_CONSECUTIVE_LOSSES = 3

# --- SUBSCRIPTION ---
SUBSCRIPTION_PRICE  = 25
PAYSAFE_CODE_LENGTH = 12
SUBSCRIPTION_DAYS   = 30

# --- TRADING PAIRS ---
TRADING_PAIRS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT",
    "BNBUSDT", "XRPUSDT", "DOGEUSDT",
]

# --- SENTIMENT KEYWORDS ---
BULLISH_KEYWORDS = [
    "bull", "bullish", "moon", "pump", "surge", "rally",
    "breakout", "buy", "long", "ath", "adoption", "upgrade",
    "partnership", "launch", "accumulate",
]
BEARISH_KEYWORDS = [
    "bear", "bearish", "dump", "crash", "sell", "short",
    "hack", "ban", "regulation", "lawsuit", "fear", "collapse",
    "liquidation", "scam", "fraud",
]
NEWS_KEYWORDS = [
    "bitcoin", "ethereum", "crypto", "cryptocurrency",
    "solana", "ripple", "dogecoin", "binance", "bybit",
    "elon musk crypto", "michael saylor", "crypto regulation",
]
