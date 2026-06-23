"""The Python ``on_book`` handler through the Rust kernel (the L2 order-book parity surface).

Feeds an L2 depth stream (``deltas``) through ``coinext_backtest.run`` and asserts the SAME Rust
kernel folds each delta into the maintained book and dispatches ``on_book`` to the Python strategy —
the same path a native-Rust strategy's ``on_book`` runs. Requires the compiled coinext_py extension.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")

from coinext_backtest import run  # noqa: E402
from coinext_strategy import Strategy  # noqa: E402

BASE, STEP = 1_700_000_000_000_000_000, 60_000_000_000
# action codes: 0=add 1=update 2=delete 3=clear ; side: +1 bid / -1 ask
CLEAR, ADD, UPDATE, DELETE = 3, 0, 1, 2


def test_on_book_receives_maintained_l2_book_from_deltas():
    class BookRec(Strategy):
        def __init__(self):
            self.books = []

        def on_book(self, book, ctx):
            self.books.append(
                (book.symbol, book.best_bid, book.best_ask, book.mid, len(book.bids), len(book.asks))
            )

    deltas = [
        (BASE, +1, 0.0, 0.0, 100, CLEAR),  # snapshot boundary -> wipe
        (BASE, +1, 50000.0, 1.0, 100, ADD),  # add bid
        (BASE, -1, 50010.0, 1.0, 100, ADD),  # add ask
        (BASE + STEP, +1, 50005.0, 2.0, 101, ADD),  # a better bid
        (BASE + 2 * STEP, -1, 50010.0, 0.0, 102, DELETE),  # remove the ask
    ]
    s = BookRec()
    run(s, bars=[], deltas=deltas)

    assert len(s.books) == 5, "on_book fires once per delta"
    assert s.books[0] == ("BTCUSDT", None, None, None, 0, 0), "empty book right after Clear"
    assert s.books[2] == ("BTCUSDT", 50000.0, 50010.0, 50005.0, 1, 1), "rebuilt from the Adds"
    assert s.books[3][1] == 50005.0, "the better bid becomes the top of book"
    assert s.books[4][2] is None, "deleting the only ask empties that side"


def test_on_book_does_not_fire_without_a_delta_feed():
    # Bar-only backtests never emit deltas, so on_book stays silent (parity with the Rust default).
    class Counter(Strategy):
        def __init__(self):
            self.n = 0

        def on_book(self, book, ctx):
            self.n += 1

    s = Counter()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(3)])
    assert s.n == 0
