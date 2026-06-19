"""coinext_risk — Python-side risk config facade + protections pipeline.

The AUTHORITATIVE pre-trade gate is the Rust ``coinext-risk-engine`` (a synchronous gate + atomic
kill-switch, ARCHITECTURE.md §8). This package is the Python-side facade: it owns the *config* the
Rust gate reads (``COINEXT__RISK__*``) and a defense-in-depth **protections pipeline** modelled on
Freqtrade's protections (StoplossGuard / MaxDrawdown / CooldownPeriod) that the out-of-band
``risk-monitor`` service evaluates and can use to trip the global kill-switch.

These protections are advisory/operational — they never replace the in-engine per-order gate; they
add portfolio-level circuit breakers on top of it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RiskLimits:
    """Per-order / portfolio limits (mirrors ``coinext_config.RiskConfig`` and ``COINEXT__RISK__*``).

    The Rust ``RiskEngine`` enforces these per order; the values here let Python services reason
    about the same thresholds without re-querying the engine.
    """

    max_order_notional: float = 50_000.0
    max_position_notional: float = 250_000.0
    max_gross_exposure: float = 1_000_000.0
    max_orders_per_sec: int = 20
    kill_switch: bool = False


@dataclass
class ProtectionConfig:
    """Toggle + thresholds for the protections pipeline (the operational circuit breakers)."""

    stoploss_guard_max_losses: int = 4  # trips after N losing trades in the window
    stoploss_guard_window_secs: int = 3600
    max_drawdown_pct: float = 0.20  # trip if equity drawdown exceeds this
    cooldown_secs: int = 300  # lockout duration after a trip
    enabled: bool = True


@dataclass
class ProtectionVerdict:
    """Result of evaluating a single protection."""

    name: str
    tripped: bool
    until_ts: int | None = None  # locked out until this ts_ns (if tripped)
    reason: str = ""


class Protection:
    """Base protection. Subclasses implement :meth:`evaluate` over a portfolio snapshot."""

    name = "protection"

    def evaluate(self, snapshot: PortfolioSnapshot) -> ProtectionVerdict:  # noqa: D401
        """Return a verdict given the current portfolio snapshot. Override in subclasses."""
        return ProtectionVerdict(self.name, tripped=False)


@dataclass
class PortfolioSnapshot:
    """Minimal portfolio view the protections need (filled from ``coinext_portfolio`` / the bus)."""

    ts_ns: int
    equity: float
    peak_equity: float
    recent_trade_pnls: list[float] = field(default_factory=list)


class StoplossGuard(Protection):
    """Trip after too many losing trades within a rolling window (Freqtrade StoplossGuard)."""

    name = "stoploss_guard"

    def __init__(self, cfg: ProtectionConfig) -> None:
        self.cfg = cfg

    def evaluate(self, snapshot: PortfolioSnapshot) -> ProtectionVerdict:
        losses = sum(1 for p in snapshot.recent_trade_pnls if p < 0)
        if losses >= self.cfg.stoploss_guard_max_losses:
            return ProtectionVerdict(
                self.name,
                tripped=True,
                until_ts=snapshot.ts_ns + self.cfg.cooldown_secs * 1_000_000_000,
                reason=f"{losses} losing trades >= {self.cfg.stoploss_guard_max_losses}",
            )
        return ProtectionVerdict(self.name, tripped=False)


class MaxDrawdown(Protection):
    """Trip when drawdown from peak equity exceeds the configured fraction."""

    name = "max_drawdown"

    def __init__(self, cfg: ProtectionConfig) -> None:
        self.cfg = cfg

    def evaluate(self, snapshot: PortfolioSnapshot) -> ProtectionVerdict:
        if snapshot.peak_equity <= 0:
            return ProtectionVerdict(self.name, tripped=False)
        dd = (snapshot.peak_equity - snapshot.equity) / snapshot.peak_equity
        if dd >= self.cfg.max_drawdown_pct:
            return ProtectionVerdict(
                self.name,
                tripped=True,
                until_ts=snapshot.ts_ns + self.cfg.cooldown_secs * 1_000_000_000,
                reason=f"drawdown {dd:.2%} >= {self.cfg.max_drawdown_pct:.2%}",
            )
        return ProtectionVerdict(self.name, tripped=False)


class CooldownPeriod(Protection):
    """A simple post-trip lockout: blocks new entries until ``until_ts``.

    State (``until_ts``) is set by other protections tripping; this one just reports whether the
    lockout is still active for the current snapshot time.
    """

    name = "cooldown_period"

    def __init__(self, cfg: ProtectionConfig) -> None:
        self.cfg = cfg
        self.until_ts: int | None = None

    def arm(self, until_ts: int) -> None:
        """Arm/extend the cooldown to ``until_ts`` (called when another protection trips)."""
        self.until_ts = max(self.until_ts or 0, until_ts)

    def evaluate(self, snapshot: PortfolioSnapshot) -> ProtectionVerdict:
        if self.until_ts is not None and snapshot.ts_ns < self.until_ts:
            return ProtectionVerdict(
                self.name, tripped=True, until_ts=self.until_ts, reason="in cooldown"
            )
        return ProtectionVerdict(self.name, tripped=False)


class ProtectionsPipeline:
    """Evaluates all protections and aggregates a global ``should_halt`` decision.

    The ``risk-monitor`` runs this over portfolio snapshots from the bus; if any protection trips it
    can flip ``RiskLimits.kill_switch`` (the Rust engine reads it atomically). ARCHITECTURE.md §8.
    """

    def __init__(self, cfg: ProtectionConfig | None = None) -> None:
        self.cfg = cfg or ProtectionConfig()
        self.cooldown = CooldownPeriod(self.cfg)
        self.protections: list[Protection] = [
            StoplossGuard(self.cfg),
            MaxDrawdown(self.cfg),
            self.cooldown,
        ]

    def evaluate(self, snapshot: PortfolioSnapshot) -> list[ProtectionVerdict]:
        """Evaluate every protection; arm the shared cooldown when any trips."""
        if not self.cfg.enabled:
            return []
        verdicts = [p.evaluate(snapshot) for p in self.protections]
        for v in verdicts:
            if v.tripped and v.until_ts is not None and v.name != self.cooldown.name:
                self.cooldown.arm(v.until_ts)
        return verdicts

    def should_halt(self, snapshot: PortfolioSnapshot) -> bool:
        """True if any protection is tripped for ``snapshot`` (caller may trip the kill-switch)."""
        return any(v.tripped for v in self.evaluate(snapshot))


__all__ = [
    "RiskLimits",
    "ProtectionConfig",
    "ProtectionVerdict",
    "PortfolioSnapshot",
    "Protection",
    "StoplossGuard",
    "MaxDrawdown",
    "CooldownPeriod",
    "ProtectionsPipeline",
]
