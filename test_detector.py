"""
test_detector.py
----------------
Τεστάρει τον detector με fake δεδομένα.
Δεν χρειάζεται σύνδεση στο Bybit.

Τρέξε με: python test_detector.py
"""

from datetime import datetime
from leaderboard import Position
from detector import PositionDetector, EventType


def make_pos(symbol, side, size, entry, leverage=10, pnl=0.0):
    return Position(
        uid="trader_001",
        symbol=symbol,
        side=side,
        size=size,
        entry_price=entry,
        leverage=leverage,
        unrealised_pnl=pnl,
        created_time=datetime.now(),
    )


def run_tests():
    detector = PositionDetector()
    detector.set_nickname("trader_001", "CryptoKing")

    print("=" * 55)
    print("  🧪 DETECTOR TESTS")
    print("=" * 55)

    # ── Test 1: Πρώτο poll — δεν υπάρχει snapshot ακόμα ──────────────────────
    print("\n📌 Test 1: Πρώτο poll (δεν υπάρχει snapshot)")
    snapshot_1 = [
        make_pos("BTCUSDT", "Buy",  0.5, 67000),
        make_pos("ETHUSDT", "Sell", 2.0, 3500),
    ]
    events = detector.detect("trader_001", snapshot_1)
    assert len(events) == 0, "Πρώτο poll = 0 events"
    print("  ✅ Σωστά — 0 events (δεν ξέρουμε τι ήταν πριν)")

    # ── Test 2: Νέα θέση ──────────────────────────────────────────────────────
    print("\n📌 Test 2: Ο trader άνοιξε νέα θέση (SOLUSDT)")
    snapshot_2 = [
        make_pos("BTCUSDT", "Buy",  0.5, 67000),
        make_pos("ETHUSDT", "Sell", 2.0, 3500),
        make_pos("SOLUSDT", "Buy",  10.0, 180),   # ← νέα
    ]
    events = detector.detect("trader_001", snapshot_2)
    assert len(events) == 1
    assert events[0].event_type == EventType.OPEN
    assert events[0].position.symbol == "SOLUSDT"
    print(f"  ✅ Σωστά — {events[0].summary()}")

    # ── Test 3: Έκλεισε θέση ─────────────────────────────────────────────────
    print("\n📌 Test 3: Ο trader έκλεισε το ETHUSDT")
    snapshot_3 = [
        make_pos("BTCUSDT", "Buy",  0.5, 67000),
        # ETHUSDT δεν υπάρχει πια
        make_pos("SOLUSDT", "Buy",  10.0, 180),
    ]
    events = detector.detect("trader_001", snapshot_3)
    assert len(events) == 1
    assert events[0].event_type == EventType.CLOSE
    assert events[0].position.symbol == "ETHUSDT"
    print(f"  ✅ Σωστά — {events[0].summary()}")

    # ── Test 4: Άλλαξε size (scale in) ───────────────────────────────────────
    print("\n📌 Test 4: Ο trader πρόσθεσε στο BTC (0.5 → 1.2)")
    snapshot_4 = [
        make_pos("BTCUSDT", "Buy",  1.2, 67000),   # ← από 0.5 σε 1.2
        make_pos("SOLUSDT", "Buy",  10.0, 180),
    ]
    events = detector.detect("trader_001", snapshot_4)
    assert len(events) == 1
    assert events[0].event_type == EventType.SCALE
    assert events[0].position.size == 1.2
    assert events[0].old_position.size == 0.5
    print(f"  ✅ Σωστά — {events[0].summary()}")

    # ── Test 5: Πολλαπλές αλλαγές ταυτόχρονα ─────────────────────────────────
    print("\n📌 Test 5: Πολλαπλές αλλαγές ταυτόχρονα")
    snapshot_5 = [
        make_pos("BTCUSDT", "Buy",  1.2, 67000),
        make_pos("SOLUSDT", "Buy",  5.0, 180),    # scale down
        make_pos("BNBUSDT", "Sell", 3.0, 600),    # νέα θέση
        # XRPUSDT κλείνει... (δεν υπήρχε, απλώς test)
    ]
    events = detector.detect("trader_001", snapshot_5)
    types = {e.event_type for e in events}
    assert EventType.SCALE in types
    assert EventType.OPEN  in types
    print(f"  ✅ Σωστά — {len(events)} events: {[e.event_type.value for e in events]}")

    # ── Test 6: Κανένα event αν δεν άλλαξε τίποτα ────────────────────────────
    print("\n📌 Test 6: Ίδιο snapshot — 0 events")
    events = detector.detect("trader_001", snapshot_5)
    assert len(events) == 0
    print("  ✅ Σωστά — 0 events (τίποτα δεν άλλαξε)")

    print("\n" + "=" * 55)
    print("  ✅ ΟΛΑ ΤΑ TESTS ΠΕΡΑΣΑΝ!")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    run_tests()
