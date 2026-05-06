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
DEFAULT_LEVERAGE  = 25
DEFAULT_USDT      = 100
DEFAULT_SL_PCT    = 2.0
DEFAULT_TP_PCT    = 4.0
MIN_SIGNAL_SCORE  = 80

# --- MANUAL APPROVAL WINDOW (EET = UTC+3) ---
MANUAL_HOUR_START = 11
MANUAL_HOUR_END   = 14

# --- SUBSCRIPTION ---
SUBSCRIPTION_PRICE  = 25
PAYSAFE_CODE_LENGTH = 12

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
