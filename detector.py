"""
detector.py
-----------
Συγκρίνει snapshots θέσεων και βρίσκει τι άλλαξε.

Λογική:
  snapshot_A  =  θέσεις πριν 8 δευτερόλεπτα
  snapshot_B  =  θέσεις τώρα

  B έχει κάτι που δεν είχε το A  →  νέα θέση (OPEN)
  A είχε κάτι που δεν έχει το B  →  έκλεισε θέση (CLOSE)
  και τα δύο έχουν ίδιο symbol
  αλλά διαφορετικό size          →  άλλαξε μέγεθος (SCALE)
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from leaderboard import Position

logger = logging.getLogger(__name__)


# ── Τύποι αλλαγής ─────────────────────────────────────────────────────────────

class EventType(Enum):
    OPEN  = "OPEN"   # νέα θέση ανοίχτηκε
    CLOSE = "CLOSE"  # θέση έκλεισε
    SCALE = "SCALE"  # size άλλαξε (πρόσθεσε ή μείωσε)


# ── Αποτέλεσμα σύγκρισης ──────────────────────────────────────────────────────

@dataclass
class TradeEvent:
    event_type: EventType
    trader_uid: str
    trader_nickname: str
    position: Position           # η νέα/τρέχουσα θέση
    old_position: Optional[Position] = None  # για CLOSE/SCALE

    @property
    def emoji(self) -> str:
        return {"OPEN": "🟢", "CLOSE": "🔴", "SCALE": "🔄"}[self.event_type.value]

    def summary(self) -> str:
        """Σύντομη περιγραφή για Telegram notification."""
        p = self.position
        direction = "LONG" if p.side == "Buy" else "SHORT"

        if self.event_type == EventType.OPEN:
            return (
                f"{self.emoji} <b>{self.trader_nickname}</b> άνοιξε {direction}\n"
                f"📌 {p.symbol} @ <b>{p.entry_price}</b>\n"
                f"📦 Size: {p.size} | Leverage: x{p.leverage}"
            )

        elif self.event_type == EventType.CLOSE:
            pnl = self.old_position.unrealised_pnl if self.old_position else 0
            sign = "+" if pnl >= 0 else ""
            return (
                f"{self.emoji} <b>{self.trader_nickname}</b> έκλεισε {direction}\n"
                f"📌 {p.symbol}\n"
                f"💰 PnL: <b>{sign}{pnl:.2f} USDT</b>"
            )

        else:  # SCALE
            old_size = self.old_position.size if self.old_position else 0
            diff = p.size - old_size
            direction_word = "πρόσθεσε" if diff > 0 else "μείωσε"
            return (
                f"{self.emoji} <b>{self.trader_nickname}</b> {direction_word} θέση\n"
                f"📌 {p.symbol} {direction}\n"
                f"📦 {old_size} → <b>{p.size}</b>"
            )


# ── Ο Detector ────────────────────────────────────────────────────────────────

class PositionDetector:
    """
    Κρατάει το τελευταίο snapshot κάθε trader
    και βρίσκει αλλαγές στο επόμενο poll.
    """

    def __init__(self):
        # uid -> { symbol+side -> Position }
        self._snapshots: dict[str, dict[str, Position]] = {}
        # uid -> nickname (για τα notifications)
        self._nicknames: dict[str, str] = {}

    def set_nickname(self, uid: str, nickname: str):
        self._nicknames[uid] = nickname

    def _key(self, pos: Position) -> str:
        """Unique key για κάθε θέση: symbol + side."""
        return f"{pos.symbol}_{pos.side}"

    # ── Κύρια συνάρτηση ───────────────────────────────────────────────────────

    def detect(
        self,
        uid: str,
        new_positions: list[Position],
    ) -> list[TradeEvent]:
        """
        Συγκρίνει τις νέες θέσεις με το αποθηκευμένο snapshot.
        Επιστρέφει λίστα με TradeEvent για κάθε αλλαγή που βρέθηκε.
        """
        nickname = self._nicknames.get(uid, uid)
        new_map  = {self._key(p): p for p in new_positions}
        is_first_poll = uid not in self._snapshots
        old_map  = self._snapshots.get(uid, {})
        events   = []

        # Πρώτο poll: αποθηκεύουμε snapshot χωρίς events
        # (δεν ξέρουμε τι υπήρχε πριν, δεν θέλουμε false signals)
        if is_first_poll:
            self._snapshots[uid] = new_map
            logger.info(f"[INIT] {nickname} — αποθηκεύτηκαν {len(new_map)} θέσεις")
            return []

        # ── Νέες θέσεις (υπάρχουν στο B, όχι στο A) ──────────────────────────
        for key, new_pos in new_map.items():
            if key not in old_map:
                events.append(TradeEvent(
                    event_type=EventType.OPEN,
                    trader_uid=uid,
                    trader_nickname=nickname,
                    position=new_pos,
                ))
                logger.info(f"[OPEN]  {nickname} → {new_pos.symbol} {new_pos.direction}")

            # ── Αλλαγή size (ίδιο symbol, διαφορετικό size) ──────────────────
            elif abs(new_pos.size - old_map[key].size) > 0.0001:
                events.append(TradeEvent(
                    event_type=EventType.SCALE,
                    trader_uid=uid,
                    trader_nickname=nickname,
                    position=new_pos,
                    old_position=old_map[key],
                ))
                logger.info(
                    f"[SCALE] {nickname} → {new_pos.symbol} "
                    f"{old_map[key].size} → {new_pos.size}"
                )

        # ── Κλειστές θέσεις (υπήρχαν στο A, δεν υπάρχουν στο B) ─────────────
        for key, old_pos in old_map.items():
            if key not in new_map:
                events.append(TradeEvent(
                    event_type=EventType.CLOSE,
                    trader_uid=uid,
                    trader_nickname=nickname,
                    position=old_pos,
                    old_position=old_pos,
                ))
                logger.info(f"[CLOSE] {nickname} → {old_pos.symbol} {old_pos.direction}")

        # ── Ενημέρωσε το snapshot ─────────────────────────────────────────────
        self._snapshots[uid] = new_map

        return events

    def detect_all(
        self,
        positions_map: dict[str, list[Position]],
    ) -> list[TradeEvent]:
        """
        Τρέχει detect() για όλους τους tracked traders.
        Επιστρέφει όλα τα events μαζί.
        """
        all_events = []
        for uid, positions in positions_map.items():
            events = self.detect(uid, positions)
            all_events.extend(events)
        return all_events

    def get_snapshot(self, uid: str) -> list[Position]:
        """Επιστρέφει το τρέχον snapshot ενός trader."""
        return list(self._snapshots.get(uid, {}).values())

    def clear(self, uid: str):
        """Διαγράφει το snapshot ενός trader (π.χ. αν τον κάνεις unfollow)."""
        self._snapshots.pop(uid, None)
