"""qv_cli — the ``qv`` command-line entry point for the VeloxQuant control plane.

Exposes a Typer ``app`` (the ``qv`` console script, see root ``pyproject.toml``
``[project.scripts]``) with subcommands: ``backtest``, ``optimize``, ``download``, ``live``,
``reconcile``, ``catalog``.

Typer is optional: if it is not installed, ``qv_cli.main`` falls back to an ``argparse`` driver so
``python -m qv_cli.main ...`` still works with NO heavy deps. See :mod:`qv_cli.main`.
"""

from __future__ import annotations

__all__ = ["main"]
