"""
executor.py
-----------
Ανοίγει και κλείνει θέσεις στο Bybit του χρήστη.
- Σταθερό ποσό: FIXED_POSITION_USDT (π.χ. $9)
- Σταθερό leverage: FIXED_LEVERAGE (π.χ. 50x)
- TP/SL: αντιγράφεται από τον trader αν υπάρχει
"""

import logging
from dataclasses import dataclass
from pybit.unified_trading import HTTP
from detector import TradeEvent, EventType
from config import (
    BYBIT_API_KEY, BYBIT_API_SECRET, BYBIT_TESTNET,
    FIXED_POSITION_USDT, FIXED_LEVERAGE,
    COPY_TP_SL, FALLBACK_STOP_LOSS_PCT
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
            return f"✅ {self.symbol} {self.side} {self.size} (id: {self.order_id})"
        return f"❌ FAILED — {self.error}"


class Executor:

    def __init__(self):
        self.client = HTTP(
            testnet=BYBIT_TESTNET,
            api_key=BYBIT_API_KEY,
            api_secret=BYBIT_API_SECRET,
        )
        self._balance_cache: float = 0.0

    # ── Balance ───────────────────────────────────────────────────────────────

    def get_balance(self) -> float:
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
            logger.error(f"Balance error: {e}")
            return self._balance_cache

    # ── Min qty ───────────────────────────────────────────────────────────────

    def get_min_qty(self, symbol: str) -> float:
        try:
            resp = self.client.get_instruments_info(
                category="linear", symbol=symbol
            )
            lot_filter = resp["result"]["list"][0]["lotSizeFilter"]
            return float(lot_filter["minOrderQty"])
        except Exception:
            return 0.001

    def get_qty_step(self, symbol: str) -> float:
        """Παίρνει το qtyStep για σωστή στρογγυλοποίηση."""
        try:
            resp = self.client.get_instruments_info(
                category="linear", symbol=symbol
            )
            lot_filter = resp["result"]["list"][0]["lotSizeFilter"]
            return float(lot_filter["qtyStep"])
        except Exception:
            return 0.001

    # ── Size calculation ──────────────────────────────────────────────────────

    def calculate_size(self, symbol: str, entry_price: float) -> float:
        """
        Υπολογίζει size βάσει FIXED_POSITION_USDT και FIXED_LEVERAGE.
        $9 με 50x leverage = $450 notional value
        size = (FIXED_POSITION_USDT * FIXED_LEVERAGE) / entry_price
        """
        if entry_price <= 0:
            return self.get_min_qty(symbol)

        notional = FIXED_POSITION_USDT * FIXED_LEVERAGE
        size = notional / entry_price

        # Στρογγυλοποίηση στο σωστό qtyStep
        step = self.get_qty_step(symbol)
        size = round(round(size / step) * step, 8)

        # Έλεγχος min qty
        min_qty = self.get_min_qty(symbol)
        if size < min_qty:
            logger.warning(f"{symbol}: size {size} < min {min_qty}, χρησιμοποιούμε min")
            size = min_qty

        logger.info(
            f"Size: {FIXED_POSITION_USDT} USDT x {FIXED_LEVERAGE}x = "
            f"{notional} notional / {entry_price} = {size} {symbol}"
        )
        return size

    # ── Leverage ──────────────────────────────────────────────────────────────

    def _set_leverage(self, symbol: str):
        """Βάζει FIXED_LEVERAGE για το symbol."""
        try:
            self.client.set_leverage(
                category="linear",
                symbol=symbol,
                buyLeverage=str(FIXED_LEVERAGE),
                sellLeverage=str(FIXED_LEVERAGE),
            )
            logger.info(f"Leverage {FIXED_LEVERAGE}x set για {symbol}")
        except Exception as e:
            if "leverage not modified" not in str(e).lower():
                logger.warning(f"Set leverage warning για {symbol}: {e}")

    # ── Open position ─────────────────────────────────────────────────────────

    def open_position(self, event: TradeEvent) -> OrderResult:
        p = event.position

        try:
            size = self.calculate_size(p.symbol, p.entry_price)
            self._set_leverage(p.symbol)

            # TP/SL από τον trader ή fallback
            order_params = {
                "category": "linear",
                "symbol": p.symbol,
                "side": p.side,
                "orderType": "Market",
                "qty": str(size),
                "timeInForce": "IOC",
            }

            if COPY_TP_SL:
                # Αντιγράφουμε SL από τον trader αν υπάρχει
                if hasattr(p, 'stop_loss') and p.stop_loss and p.stop_loss > 0:
                    order_params["stopLoss"] = str(p.stop_loss)
                    order_params["slTriggerBy"] = "MarkPrice"
                    logger.info(f"Copying SL from trader: {p.stop_loss}")
                else:
                    # Fallback SL
                    if p.side == "Buy":
                        sl = round(p.entry_price * (1 - FALLBACK_STOP_LOSS_PCT), 2)
                    else:
                        sl = round(p.entry_price * (1 + FALLBACK_STOP_LOSS_PCT), 2)
                    order_params["stopLoss"] = str(sl)
                    order_params["slTriggerBy"] = "MarkPrice"
                    logger.info(f"Fallback SL: {sl}")

                # Αντιγράφουμε TP από τον trader αν υπάρχει
                if hasattr(p, 'take_profit') and p.take_profit and p.take_profit > 0:
                    order_params["takeProfit"] = str(p.take_profit)
                    order_params["tpTriggerBy"] = "MarkPrice"
                    logger.info(f"Copying TP from trader: {p.take_profit}")

            resp = self.client.place_order(**order_params)

            if resp["retCode"] == 0:
                order_id = resp["result"]["orderId"]
                logger.info(
                    f"✅ OPEN {p.symbol} {p.side} | "
                    f"size={size} | {FIXED_LEVERAGE}x | "
                    f"margin={FIXED_POSITION_USDT} USDT"
                )
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
            logger.error(f"❌ Exception open_position: {e}")
            return OrderResult(success=False, symbol=p.symbol, error=str(e))

    # ── Close position ────────────────────────────────────────────────────────

    def close_position(self, event: TradeEvent) -> OrderResult:
        p = event.position
        close_side = "Sell" if p.side == "Buy" else "Buy"

        try:
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
                logger.warning(f"Δεν βρέθηκε θέση για {p.symbol}")
                return OrderResult(
                    success=False, symbol=p.symbol,
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
                    success=True, order_id=order_id,
                    symbol=p.symbol, side=close_side, size=our_size,
                )
            else:
                err = resp["retMsg"]
                logger.error(f"❌ CLOSE failed: {err}")
                return OrderResult(success=False, symbol=p.symbol, error=err)

        except Exception as e:
            logger.error(f"❌ Exception close_position: {e}")
            return OrderResult(success=False, symbol=p.symbol, error=str(e))

    # ── Handle event ──────────────────────────────────────────────────────────

    def handle_event(self, event: TradeEvent) -> OrderResult | None:
        if event.event_type == EventType.OPEN:
            return self.open_position(event)
        elif event.event_type == EventType.CLOSE:
            return self.close_position(event)
        elif event.event_type == EventType.SCALE:
            logger.info(f"SCALE event για {event.position.symbol} — παράλειψη")
            return None
