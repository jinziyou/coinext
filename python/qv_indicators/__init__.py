"""qv_indicators — streaming technical indicators (the SAME Rust code as warm-up + live).

Thin re-export of the ``qv-indicators`` Rust crate through the compiled ``qv_py`` extension, so a
Python strategy uses the IDENTICAL incremental indicator implementations the native-Rust path and
live warm-up use — never a re-rolled Python copy that could drift. Each indicator is stateful:

    sma = Sma(20)
    def on_bar(self, bar, ctx):
        sma.update(bar.close)
        if sma.is_ready():
            ...  # sma.value() is a float; None until warm

* :class:`Sma` / :class:`Ema` / :class:`Rsi` — ``update(value)``; ``value()`` -> ``float | None``.
* :class:`Atr` — ``update(high, low, close)``; ``value()`` -> ``float | None``.

All raise ``ValueError`` for ``period <= 0``. ``qv_py`` must be built (maturin); the import error is
surfaced with the build command if it isn't.
"""

from __future__ import annotations

try:
    import qv_py  # the maturin-built Rust extension
except ImportError as exc:  # pragma: no cover - surfaced as a clear setup error
    raise ImportError(
        "qv_py extension not built. Run: "
        "uvx maturin develop --manifest-path crates/qv-py/Cargo.toml --features python"
    ) from exc

# Re-export the compiled indicator types under clean names.
Sma = qv_py.Sma
Ema = qv_py.Ema
Rsi = qv_py.Rsi
Atr = qv_py.Atr

__all__ = ["Sma", "Ema", "Rsi", "Atr"]
