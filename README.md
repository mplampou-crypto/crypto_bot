# Bybit Webhook Trading Bot

TradingView indicators στέλνουν **alerts** → αυτός ο **Python server** τα λαμβάνει →
ανοίγει/κλείνει **market trades στο Bybit** → **dashboard** δείχνει trades & win rate.

- Τρέξε όσες στρατηγικές θες — προστίθενται από το `config.yaml`, χωρίς αλλαγή κώδικα.
- Πολλές στρατηγικές μπορούν να τρέχουν στο **ίδιο σύμβολο**: ο bot τις **νετάρει**
  σε μία θέση στο Bybit, αλλά κρατάει το win rate της **κάθε μίας ξεχωριστά**.
- Το dashboard δείχνει **live win rate** (από τα πραγματικά trades) + το **backtest**
  ποσοστό που γράφεις εσύ, + περιγραφή «πώς tradάρει» κάθε στρατηγική.

---

## ⚠ Διάβασέ το πρώτα (live χρήματα)

1. **API key μόνο για Trade.** Στο Bybit φτιάξε key με δικαίωμα *Contract – Orders & Positions*.
   **ΜΗΝ** ενεργοποιήσεις Withdrawal. Βάλε **IP whitelist** το IP του VPS σου.
2. **Webhook secret υποχρεωτικό.** Χωρίς αυτό, οποιοσδήποτε ξέρει το URL μπορεί να
   στείλει ψεύτικα σήματα και να αδειάσει το account. Είναι ήδη ενσωματωμένο — απλά βάλε
   ένα μεγάλο τυχαίο string στο `.env` και στα alerts.
3. **Δοκίμασε σε testnet πρώτα** αν μπορείς (`BYBIT_TESTNET=true`), έστω μια μέρα.
4. Ο λογαριασμός πρέπει να είναι **Unified (UTA)** και σε **One-Way mode** (ο bot το
   βάζει αυτόματα στο startup).
5. Δεν είμαι σύμβουλος επενδύσεων — εσύ ευθύνεσαι για τα μεγέθη, το ρίσκο και τις
   στρατηγικές. Ξεκίνα με μικρό `position_size_usdt`.

---

## 1. Εγκατάσταση στο VPS (Ubuntu)

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv
# ανέβασε τον φάκελο bybit-bot στο VPS, μετά:
cd bybit-bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 2. Ρύθμιση κλειδιών

```bash
cp .env.example .env
nano .env        # βάλε BYBIT_API_KEY, BYBIT_API_SECRET, WEBHOOK_SECRET
```

Φτιάξε ένα τυχαίο secret:
```bash
python3 -c "import secrets; print(secrets.token_hex(24))"
```

## 3. Τρέξε

```bash
source venv/bin/activate
python app.py
```
Άνοιξε `http://IP_TOY_VPS:8000` → dashboard.
Το webhook URL είναι `http://IP_TOY_VPS:8000/webhook`.

---

## 4. Στήσιμο alerts στο TradingView

Σε κάθε indicator alert, βάλε στο πεδίο **Message** ένα JSON. Στο **Webhook URL**
βάλε `http://IP_TOY_VPS:8000/webhook`.

Παράδειγμα — για το **Hull Market Structure** indicator πάνω σε **BTC** (φτιάξε 2 alerts):

**Bullish CHoCH → άνοιγμα Long**
```json
{"secret":"TO_SECRET_SOY","strategy":"hull_btc","action":"buy"}
```

**Bearish CHoCH → άνοιγμα Short**
```json
{"secret":"TO_SECRET_SOY","strategy":"hull_btc","action":"sell"}
```

Για τα άλλα νομίσματα άλλαξε μόνο το `strategy`:
`hull_eth`, `hull_sol`, `hull_xrp`, `hull_doge` (το καθένα tradάρει το δικό του σύμβολο
και έχει ξεχωριστό win rate στο dashboard).

Ο bot δέχεται **μόνο** `buy` και `sell`. Όταν έρθει αντίθετο σήμα ενώ είσαι σε θέση,
κλείνει την προηγούμενη και ανοίγει την νέα (flip). Κάθε άλλο `action` αγνοείται.
Προαιρετικά μπορείς να περάσεις και `"symbol":"ETHUSDT"` για override.

> Σημείωση: στα alerts του TradingView το μήνυμα είναι σταθερό κείμενο, γι' αυτό
> φτιάχνεις **ένα alert ανά σήμα** (ένα για buy, ένα για sell).

---

## 5. Προσθήκη νέας στρατηγικής (χωρίς κώδικα)

Στο `config.yaml`, αντίγραψε ένα block:

```yaml
  my_new_strat:
    enabled: true
    symbol: BTCUSDT
    position_size_usdt: 150
    description: >
      Περιγραφή: πώς μπαίνει/βγαίνει η στρατηγική. Φαίνεται στο dashboard.
    backtest_win_rate: "61%"
```

Μετά:
- στο TradingView φτιάξε alerts με `"strategy":"my_new_strat"`
- κάνε restart τον bot

Για **on/off** μιας στρατηγικής: `enabled: true/false` + restart.

---

## 6. Να τρέχει 24/7 (systemd)

Φτιάξε `/etc/systemd/system/bybitbot.service`:

```ini
[Unit]
Description=Bybit Webhook Bot
After=network.target

[Service]
WorkingDirectory=/home/USER/bybit-bot
ExecStart=/home/USER/bybit-bot/venv/bin/python app.py
Restart=always
RestartSec=5
User=USER

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bybitbot
sudo journalctl -u bybitbot -f      # δες logs ζωντανά
```

---

## Πώς υπολογίζεται το win rate

- Κάθε φορά που μια στρατηγική **κλείνει** θέση (close ή flip), καταγράφεται ένα trade
  με PnL = κίνηση τιμής × μέγεθος − προμήθειες (taker fee, ρυθμίζεται στο `config.yaml`).
- **Win = PnL > 0.** Live win rate = wins / σύνολο κλεισμένων trades, ανά στρατηγική
  και συνολικά.
- Το **backtest** ποσοστό είναι δικό σου σχόλιο (από TradingView backtests) — ο bot
  δεν το εφευρίσκει.

## Ασφάλεια dashboard
Αν θες να μην είναι ανοιχτό σε όλους: βάλε `DASHBOARD_TOKEN=...` στο `.env` και άνοιξέ
το με `http://IP:8000/?token=...`. Ή κλείσε την πόρτα στο firewall και χρησιμοποίησε
SSH tunnel / reverse proxy.

## Δομή
```
app.py            webhook server + dashboard API + λογική trade/netting
bybit_client.py   Bybit V5 API (orders, positions, leverage)
database.py       SQLite (state στρατηγικών + ιστορικό trades)
config.yaml       ΕΔΩ προσθέτεις/ρυθμίζεις στρατηγικές
templates/        dashboard HTML
.env              τα κλειδιά σου (δεν ανεβαίνει στο git)
```
