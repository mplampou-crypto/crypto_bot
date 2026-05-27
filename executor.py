"""
executor.py
-----------
Ανοίγει και κλείνει θέσεις στο Bybit του χρήστη
βάσει των events που βρίσκει ο detector.

Χρησιμοποιεί το επίσημο pybit SDK.
"""

import logging
from dataclasses import dataclass
from pybit.unified_trading import HTTP
from detector import TradeEvent, EventType
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET,
    COPY_RATIO, MAX_POSITION_USDT, STOP_LOSS_PCT
)

logger = logging.getLogger(__name__)


@dataclass
class OrderResult:
    success: bool
    order_id: str = ""
    symbol: str = ""
    side: str = ""
    size: float = 0.0
    error: str = ""

    def __str__(self):
        if self.success:
            return f"✅ Order OK — {self.symbol} {self.side} {self.size} (id: {self.order_id})"
        return f"❌ Order FAILED — {self.error}"


class Executor:
    """
    Συνδέεται με το Bybit API του χρήστη
    και εκτελεί orders βάσει των trade events.
    """

    def __init__(self):
        self.client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
        self._balance_cache: float = 0.0

    # ── Βοηθητικές συναρτήσεις ───────────────────────────────────────────────

    def get_balance(self) -> float:
        """Επιστρέφει το διαθέσιμο USDT balance."""
        try:
            resp = self.client.get_wallet_balance(
                accountType="UNIFIED", coin="USDT"
            )
            balance = float(
                resp["result"]["list"][0]["coin"][0]["availableToWithdraw"]
            )
            self._balance_cache = balance
            return balance
        except Exception as e:
            logger.error(f"Αδύνατη λήψη balance: {e}")
            return self._balance_cache

    def get_min_qty(self, symbol: str) -> float:
        """Παίρνει το ελάχιστο επιτρεπτό size για ένα symbol."""
        try:
            resp = self.client.get_instruments_info(
                category="linear", symbol=symbol
            )
            lot_filter = resp["result"]["list"][0]["lotSizeFilter"]
            return float(lot_filter["minOrderQty"])
        except Exception:
            return 0.001  # safe default

    def calculate_size(self, event: TradeEvent) -> float:
        """
        Υπολογίζει το size της δικής μας θέσης.

        Λογική:
          - Παίρνουμε COPY_RATIO% του balance μας
          - Δεν ξεπερνάμε MAX_POSITION_USDT
          - Δεν πηγαίνουμε κάτω από min_qty του symbol
        """
        balance = self.get_balance()
        p = event.position

        # Αξία σε USDT που θέλουμε να ρισκάρουμε
        usdt_amount = min(balance * COPY_RATIO, MAX_POSITION_USDT)

        # Μετατροπή σε contracts
        if p.entry_price > 0:
            size = usdt_amount / p.entry_price
        else:
            size = 0.001

        # Στρογγυλοποίηση στα 3 δεκαδικά
        size = round(size, 3)

        # Έλεγχος min qty
        min_qty = self.get_min_qty(p.symbol)
        if size < min_qty:
            logger.warning(
                f"{p.symbol}: size {size} < min {min_qty}, "
                f"χρησιμοποιούμε min"
            )
            size = min_qty

        logger.info(
            f"Size calculation: balance={balance:.0f} USDT, "
            f"ratio={COPY_RATIO}, amount={usdt_amount:.0f} USDT, "
            f"size={size} {p.symbol}"
        )
        return size

    def _set_leverage(self, symbol: str, leverage: int):
        """Ορίζει leverage για ένα symbol."""
        try:
            self.client.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(leverage),
                sellLeverage=str(leverage),
            )
        except Exception as e:
            # Συχνά επιστρέφει error αν το leverage είναι ήδη σωστό
            if "leverage not modified" not in str(e).lower():
                logger.warning(f"Set leverage warning για {symbol}: {e}")

    # ── Άνοιγμα θέσης ────────────────────────────────────────────────────────

    def open_position(self, event: TradeEvent) -> OrderResult:
        """
        Ανοίγει θέση βάσει OPEN event.
        Βάζει αυτόματα stop-loss αν ο trader δεν έχει βάλει.
        """
        p = event.position

        try:
            size = self.calculate_size(event)
            self._set_leverage(p.symbol, p.leverage)

            # Stop loss price
            if p.side == "Buy":
                sl_price = round(p.entry_price * (1 - STOP_LOSS_PCT), 2)
            else:
                sl_price = round(p.entry_price * (1 + STOP_LOSS_PCT), 2)

            resp = self.client.place_order(
                category="linear",
                symbol=p.symbol,
                side=p.side,           # "Buy" ή "Sell"
                orderType="Market",
                qty=str(size),
                stopLoss=str(sl_price),
                slTriggerBy="MarkPrice",
                timeInForce="IOC",
            )

            if resp["retCode"] == 0:
                order_id = resp["result"]["orderId"]
                logger.info(f"✅ OPEN {p.symbol} {p.side} size={size} sl={sl_price}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    symbol=p.symbol,
                    side=p.side,
                    size=size,
                )
            else:
                err = resp["retMsg"]
                logger.error(f"❌ OPEN failed: {err}")
                return OrderResult(success=False, symbol=p.symbol, error=err)

        except Exception as e:
            logger.error(f"❌ Exception κατά open_position: {e}")
            return OrderResult(success=False, symbol=p.symbol, error=str(e))

    # ── Κλείσιμο θέσης ───────────────────────────────────────────────────────

    def close_position(self, event: TradeEvent) -> OrderResult:
        """
        Κλείνει τη θέση μας για το συγκεκριμένο symbol.
        Χρησιμοποιεί reduceOnly=True για ασφάλεια.
        """
        p = event.position
        close_side = "Sell" if p.side == "Buy" else "Buy"

        try:
            # Βρίσκουμε το τρέχον size της δικής μας θέσης
            resp = self.client.get_positions(
                category="linear", symbol=p.symbol
            )
            positions = resp["result"]["list"]
            our_size = 0.0
            for pos in positions:
                if pos["side"] == p.side and float(pos["size"]) > 0:
                    our_size = float(pos["size"])
                    break

            if our_size == 0:
                logger.warning(f"Δεν βρέθηκε ανοιχτή θέση για {p.symbol}")
                return OrderResult(
                    success=False,
                    symbol=p.symbol,
                    error="Δεν υπάρχει ανοιχτή θέση"
                )

            resp = self.client.place_order(
                category="linear",
                symbol=p.symbol,
                side=close_side,
                orderType="Market",
                qty=str(our_size),
                reduceOnly=True,
                timeInForce="IOC",
            )

            if resp["retCode"] == 0:
                order_id = resp["result"]["orderId"]
                logger.info(f"✅ CLOSE {p.symbol} size={our_size}")
                return OrderResult(
                    success=True,
                    order_id=order_id,
                    symbol=p.symbol,
                    side=close_side,
                    size=our_size,
                )
            else:
                err = resp["retMsg"]
                logger.error(f"❌ CLOSE failed: {err}")
                return OrderResult(success=False, symbol=p.symbol, error=err)

        except Exception as e:
            logger.error(f"❌ Exception κατά close_position: {e}")
            return OrderResult(success=False, symbol=p.symbol, error=str(e))

    # ── Κύρια συνάρτηση — χειρίζεται κάθε event ──────────────────────────────

    def handle_event(self, event: TradeEvent) -> OrderResult | None:
        """
        Δέχεται ένα TradeEvent και εκτελεί την κατάλληλη ενέργεια.
        Επιστρέφει None για SCALE events (δεν κάνουμε τίποτα προς το παρόν).
        """
        if event.event_type == EventType.OPEN:
            return self.open_position(event)

        elif event.event_type == EventType.CLOSE:
            return self.close_position(event)

        elif event.event_type == EventType.SCALE:
            # TODO: προαιρετικά scale και εμείς
            logger.info(
                f"SCALE event για {event.position.symbol} — "
                f"δεν κάνουμε ενέργεια προς το παρόν"
            )
            return None
