"""coinext_parity — pre-live promotion gate and advisory cross-check.

See root ``ARCHITECTURE.md`` §1 and §6 for the parity invariant.
"""

from __future__ import annotations

from .gate import (
    AcceptanceCriterion,
    Verdict,
    cross_check,
    evaluate,
    render_verdict,
    run_gate,
)
from .metrics import ParityMetrics, parity_metrics
from .session import (
    SandboxRecording,
    SessionResult,
    dump_sandbox_recording,
    load_sandbox_recording,
)

__all__ = [
    "AcceptanceCriterion",
    "ParityMetrics",
    "SandboxRecording",
    "SessionResult",
    "Verdict",
    "cross_check",
    "dump_sandbox_recording",
    "evaluate",
    "load_sandbox_recording",
    "parity_metrics",
    "render_verdict",
    "run_gate",
]
