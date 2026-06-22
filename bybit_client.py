"""Thin wrapper around Bybit V5 (linear perpetuals) via pybit."""
import logging
from decimal import Decimal, ROUND_DOWN

from pybit.unified_trading import HTTP

log = logging.getLogger("bybit")


class BybitClient:
    def __init__(self, api_key, api_secret, testnet=False):
        self.session = HTTP(testnet=testnet, api_key=api_key,
                            api_secret=api_secret)
        self._instr = {}

    # ── instrument info (qty step / min qty) ────────────────────
    def instrument(self, symbol):
        if symbol not in self._instr:
            r = self.session.get_instruments_info(
                category="linear", symbol=symbol)
            info = r["result"]["list"][0]
            lot = info["lotSizeFilter"]
            self._instr[symbol] = {
                "qty_step": Decimal(lot["qtyStep"]),
                "min_qty": Decimal(lot["minOrderQty"]),
            }
        return self._instr[symbol]

    def round_qty(self, symbol, qty):
        """Round DOWN to the symbol's qty step. Returns Decimal."""
        step = self.instrument(symbol)["qty_step"]
        q = Decimal(str(qty))
        return (q / step).to_integral_value(rounding=ROUND_DOWN) * step

    def min_qty(self, symbol):
        return self.instrument(symbol)["min_qty"]

    # ── market data ─────────────────────────────────────────────
    def last_price(self, symbol):
        r = self.session.get_tickers(category="linear", symbol=symbol)
        return float(r["result"]["list"][0]["lastPrice"])

    # ── account ─────────────────────────────────────────────────
    def position_size(self, symbol):
        """Signed current position on Bybit: + long, - short, 0 flat (Decimal)."""
        r = self.session.get_positions(category="linear", symbol=symbol)
        lst = r["result"]["list"]
        if not lst:
            return Decimal("0")
        p = lst[0]
        size = Decimal(p["size"]) if p["size"] else Decimal("0")
        if size == 0:
            return Decimal("0")
        return size if p["side"] == "Buy" else -size

    def equity(self):
        """(total_equity, available) in USDT, or (None, None) on error."""
        try:
            r = self.session.get_wallet_balance(accountType="UNIFIED",
                                                coin="USDT")
            acc = r["result"]["list"][0]
            eq = float(acc.get("totalEquity") or 0)
            avail = float(acc.get("totalAvailableBalance") or 0)
            return eq, avail
        except Exception as e:
            log.warning("equity fetch failed: %s", e)
            return None, None

    # ── setup ───────────────────────────────────────────────────
    def configure_symbol(self, symbol, leverage):
        """Set One-Way mode + leverage. Ignores 'not modified' errors."""
        try:
            self.session.switch_position_mode(
                category="linear", symbol=symbol, mode=0)  # 0 = one-way
        except Exception as e:
            if "not modified" not in str(e).lower() and "110025" not in str(e):
                log.warning("position mode (%s): %s", symbol, e)
        try:
            self.session.set_leverage(
                category="linear", symbol=symbol,
                buyLeverage=str(leverage), sellLeverage=str(leverage))
        except Exception as e:
            if "not modified" not in str(e).lower() and "110043" not in str(e):
                log.warning("set leverage (%s): %s", symbol, e)

    # ── orders ──────────────────────────────────────────────────
    def market(self, symbol, side, qty):
        """Place a market order. side='Buy'|'Sell', qty=Decimal>0."""
        log.info("ORDER %s %s %s", side, qty, symbol)
        return self.session.place_order(
            category="linear", symbol=symbol, side=side,
            orderType="Market", qty=str(qty))
