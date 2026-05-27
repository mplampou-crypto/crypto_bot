import os
from dotenv import load_dotenv

load_dotenv()

# Bybit API
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY", "")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "true").lower() == "true"

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Copy trading settings
POLL_INTERVAL_SEC = 8          # Πόσο συχνά κάνουμε fetch το leaderboard
MAX_TRADERS_TO_FOLLOW = 5      # Πόσους traders παρακολουθούμε
COPY_RATIO = 0.1               # 10% του balance σου = 1x θέση trader
MAX_POSITION_USDT = 500        # Μέγιστο ποσό ανά θέση σε USDT
STOP_LOSS_PCT = 0.02           # 2% stop loss αν ο trader δεν έχει βάλει
LEADERBOARD_PERIOD = "weekly"  # daily | weekly | monthly | all
