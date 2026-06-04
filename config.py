import os
from dotenv import load_dotenv

load_dotenv()

# Bybit API
BYBIT_API_KEY    = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET    = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Copy trading settings
POLL_INTERVAL_SEC    = 8        # Πόσο συχνά κάνουμε poll (δευτερόλεπτα)
MAX_TRADERS_TO_FOLLOW = 5       # Πόσους traders παρακολουθούμε
LEADERBOARD_PERIOD   = "weekly"

# ── Ρυθμίσεις θέσης ──────────────────────────────────────────────────────────
FIXED_POSITION_USDT = 9         # Σταθερό ποσό ανά θέση σε USDT
FIXED_LEVERAGE      = 50        # Σταθερό leverage για όλες τις θέσεις

# TP/SL — αντιγράφουμε από τον trader
COPY_TP_SL = True               # True = αντιγράφουμε TP/SL από trader
FALLBACK_STOP_LOSS_PCT = 0.05   # 5% fallback SL αν ο trader δεν έχει βάλει

# Legacy (δεν χρησιμοποιείται πλέον αλλά κρατάμε για συμβατότητα)
COPY_RATIO       = 0.01
MAX_POSITION_USDT = FIXED_POSITION_USDT
STOP_LOSS_PCT    = FALLBACK_STOP_LOSS_PCT
