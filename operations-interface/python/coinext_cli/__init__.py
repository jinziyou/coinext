"""coinext_cli — the ``coinext`` command-line entry point for the Coinext control plane.

Exposes a Typer ``app`` (the ``coinext`` console script, see root ``pyproject.toml``
``[project.scripts]``) with subcommands: ``backtest``, ``backtest-multi``, ``parity``,
``testnet-gate``, ``optimize``, ``screen``, ``download``, ``live``, ``reconcile``, ``catalog``.

Typer is optional: if it is not installed, ``coinext_cli.main`` falls back to an ``argparse`` driver so
``python -m coinext_cli.main ...`` still works with NO heavy deps. See :mod:`coinext_cli.main`.
"""

from __future__ import annotations

__all__ = ["main"]
