# crypto_bot

TradingView indicators → **webhook** → **market trades στο Bybit** → **Telegram** alerts + win rate.

- Μόνο **buy / sell** σήματα (τίποτα άλλο). Αντίθετο σήμα = flip.
- **5 νομίσματα στα 50x**: BTC, ETH, SOL, XRP, DOGE (αλλάζονται στο `traders.json`).
- Πολλοί traders μπορούν να μοιράζονται σύμβολο — ο bot τους **νετάρει** σε μία θέση,
  αλλά κρατάει **ξεχωριστό win rate** για τον καθένα.
- Stats μέσω **Telegram**: `/stats`, `/leaderboard`, `/traders`, `/balance`.

## Αρχεία
```
bot.py          aiohttp webhook server + buy/sell logic + netting + Telegram
executor.py     Bybit V5 (orders, positions, leverage, balance)
leaderboard.py  SQLite (positions + trades) + stats + Telegram text
config.py       env + traders.json loader
traders.json    ΕΔΩ ρυθμίζεις σύμβολα/leverage και traders
Dockerfile      για Fly.io
fly.toml        Fly.io config (always-on + volume για το DB)
```

---

## ⚠ Πριν πας live (πραγματικά λεφτά)
1. **API key μόνο Trade.** Bybit → API → δικαίωμα *Contract – Orders & Positions*.
   **Όχι** Withdrawal. Βάλε **IP whitelist** το IP της Fly app.
2. **WEBHOOK_SECRET υποχρεωτικό** — χωρίς αυτό όποιος ξέρει το URL ανοίγει trades.
3. **50x = γρήγορη ρευστοποίηση.** Το `size_usdt` είναι notional (qty = size/price),
   άρα margin = size/50. Ξεκίνα μικρά.
4. Λογαριασμός **Unified (UTA)**, **One-Way mode** (ο bot το βάζει αυτόματα).
5. Δεν είμαι σύμβουλος επενδύσεων — εσύ ευθύνεσαι για ρίσκο/μεγέθη/στρατηγικές.

---

## 1. Telegram bot (προαιρετικό αλλά χρήσιμο)
1. Μίλα στο **@BotFather** → `/newbot` → πάρε το **token**.
2. Στείλε μήνυμα στο νέο σου bot, μετά `/start` → σου απαντά με το **chat id** σου.
3. Βάλε `TELEGRAM_BOT_TOKEN` και `TELEGRAM_CHAT_ID` στα secrets.

## 2. Deploy στο Fly.io
```bash
# εγκατάσταση flyctl: https://fly.io/docs/flyctl/install/
fly auth login
cd crypto_bot

fly launch --no-deploy          # διάλεξε μοναδικό app name + region fra
fly volumes create data --size 1 --region fra   # μόνιμο DB

# secrets (ΕΣΥ τα βάζεις — δεν μπαίνουν στον κώδικα/git)
fly secrets set \
  BYBIT_API_KEY=xxxx \
  BYBIT_API_SECRET=xxxx \
  BYBIT_TESTNET=false \
  WEBHOOK_SECRET=$(python -c "import secrets;print(secrets.token_hex(24))") \
  TELEGRAM_BOT_TOKEN=xxxx \
  TELEGRAM_CHAT_ID=xxxx

fly deploy
fly logs                         # δες ότι ξεκίνησε
```
Το webhook URL σου: **`https://<app-name>.fly.dev/webhook`**

> Σημ.: αν αλλάξεις το `app` στο `fly.toml`, βάλε το ίδιο όνομα στο `fly launch`
> και στο `fly volumes create ... --region` βάλε το ίδιο region.

## 3. Τοπικό τρέξιμο (για δοκιμή)
```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env     # βάλε τα κλειδιά σου
python bot.py                          # ακούει στο :8080
```

---

## 4. TradingView alerts
**Webhook URL:** `https://<app-name>.fly.dev/webhook`
**Message** (JSON). Φτιάξε **2 alerts** ανά indicator — ένα buy, ένα sell.

BTC, bullish CHoCH → Long:
```json
{"secret":"TO_SECRET_SOY","strategy":"hull_btc","action":"buy"}
```
BTC, bearish CHoCH → Short:
```json
{"secret":"TO_SECRET_SOY","strategy":"hull_btc","action":"sell"}
```
Άλλα νομίσματα: άλλαξε μόνο το `strategy` → `hull_eth`, `hull_sol`, `hull_xrp`, `hull_doge`.
(`action` δέχεται μόνο `buy`/`sell` — οτιδήποτε άλλο αγνοείται.)

---

## 5. Προσθήκη/αλλαγή traders & νομισμάτων (χωρίς κώδικα)
Στο `traders.json`:
- **Νέο νόμισμα:** πρόσθεσέ το στο `"symbols"` με leverage, μετά φτιάξε έναν trader που
  το δείχνει.
- **Νέος trader:** αντίγραψε ένα block, βάλε νέο όνομα-κλειδί, `enabled: true`, στόχευσε
  ένα alert με `"strategy":"to_onoma"`.
- **On/off:** `enabled: true/false`.
- **Μέγεθος:** `size_usdt` (notional).

Μετά από αλλαγές: `fly deploy` (ή restart τοπικά).

## Πώς βγαίνει το win rate
Κάθε flip κλείνει τη θέση και καταγράφει trade με **PnL = κίνηση τιμής × qty − fees**
(taker fee, ρυθμίζεται). **Win = PnL > 0.** Live win rate = wins / σύνολο, ανά trader και
συνολικά. Το `backtest_win_rate` στο `traders.json` είναι δικό σου σχόλιο.
