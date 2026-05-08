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
DEFAULT_LEVERAGE  = 50       # 50x leverage
DEFAULT_USDT      = 100      # 100€ margin
DEFAULT_SL_PCT = 0.8   # -40€
DEFAULT_TP_PCT = 0.2   # +10€
MIN_SIGNAL_SCORE  = 79      # Ελάχιστο score για trade

# --- BREAK EVEN ---
# Μόλις το trade κερδίσει X% της απόστασης TP → μεταφέρε SL στο entry
BREAKEVEN_TRIGGER_PCT = 50   # Όταν φτάσει 50% του δρόμου προς TP
TRAILING_STOP_ACTIVE  = True # Trailing stop μετά το break even

# --- MAX RISK MANAGEMENT ---
MAX_DAILY_TRADES      = 15   # Μέγιστος αριθμός trades ανά μέρα
MAX_CONSECUTIVE_LOSSES = 3   # Σταμάτα μετά από 3 consecutive losses
POSITION_SIZE_PCT     = 100  # % του balance ανά trade (αν δεν οριστεί manual)

# --- MANUAL APPROVAL WINDOW (EET = UTC+3) ---
MANUAL_HOUR_START = 11
MANUAL_HOUR_END   = 14

# --- SUBSCRIPTION ---
SUBSCRIPTION_PRICE    = 25
PAYSAFE_CODE_LENGTH   = 12
SUBSCRIPTION_DAYS     = 30   # 1 μήνας

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
