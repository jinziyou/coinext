//! [`LocalOrderBook`] — a depth-diff applier with Binance gap detection.
//!
//! Binance ships order-book updates as *diffs* (`<symbol>@depth@100ms`), each carrying a first
//! update id `U` and a last update id `u` (and, for the diff-depth stream, a previous-`u` field
//! `pu`). The canonical maintenance rules (architecture §7, "depth-diff resync") are:
//!
//!   1. Buffer diffs while fetching a REST snapshot with `lastUpdateId`.
//!   2. Drop buffered diffs with `u <= lastUpdateId`.
//!   3. The FIRST applied diff must satisfy `U <= lastUpdateId + 1 <= u`; else the snapshot is
//!      stale -> resync.
//!   4. Each SUBSEQUENT diff must satisfy `pu == previous u` (i.e. `U == previous u + 1`); a
//!      mismatch means an update was dropped -> resync.
//!
//! [`check_first`] / [`check_next`] are PURE gap-detection functions tested with a crafted
//! sequence; [`LocalOrderBook::apply_diff`] threads them through stateful application.

/// One depth-diff update's identifying ids, parsed from a `@depth` frame's `U`/`u`/`pu` fields.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DepthUpdate {
    /// First update id in the event (`U`).
    pub first_update_id: u64,
    /// Last update id in the event (`u`).
    pub last_update_id: u64,
    /// Previous event's `u` (`pu`); `None` for the legacy partial-depth stream that omits it.
    pub prev_update_id: Option<u64>,
}

/// Outcome of validating a diff against the book's current state.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ApplyOutcome {
    /// The diff was in-order and applied; the book's `last_update_id` advanced.
    Applied,
    /// The diff is older than the snapshot/last applied id and was safely ignored.
    Skipped,
    /// A gap was detected — the caller MUST resync (refetch snapshot + replay).
    Resync,
}

/// PURE: does the FIRST diff after a snapshot bridge it correctly?
/// Requires `U <= lastUpdateId + 1 <= u`. A diff entirely before the snapshot (`u <= lastUpdateId`)
/// is stale and should be skipped by the caller before this check.
pub fn check_first(update: &DepthUpdate, snapshot_last_id: u64) -> bool {
    update.first_update_id <= snapshot_last_id + 1 && snapshot_last_id + 1 <= update.last_update_id
}

/// PURE: is a SUBSEQUENT diff contiguous with the previously applied one?
/// Binance's diff-depth stream guarantees `pu == previous u`. When `pu` is present we check it
/// directly; otherwise we fall back to the `U == previous u + 1` rule.
pub fn check_next(update: &DepthUpdate, prev_last_id: u64) -> bool {
    match update.prev_update_id {
        Some(pu) => pu == prev_last_id,
        None => update.first_update_id == prev_last_id + 1,
    }
}

/// Stateful local book just tracking update-id continuity (price levels are emitted as
/// `OrderBookDelta`s by the data adapter; this type owns the gap-detection state machine).
#[derive(Debug, Clone)]
pub struct LocalOrderBook {
    /// `u` of the last applied diff (or the snapshot's `lastUpdateId` after a resync).
    last_update_id: u64,
    /// Whether a valid snapshot has been installed and the first diff bridged.
    synced: bool,
}

impl LocalOrderBook {
    /// A fresh, unsynced book. Call [`install_snapshot`] before applying diffs.
    pub fn new() -> Self {
        LocalOrderBook {
            last_update_id: 0,
            synced: false,
        }
    }

    /// Install a REST snapshot's `lastUpdateId`. The next diff is validated with [`check_first`].
    pub fn install_snapshot(&mut self, snapshot_last_id: u64) {
        self.last_update_id = snapshot_last_id;
        self.synced = false; // becomes true once the first bridging diff is applied
    }

    pub fn last_update_id(&self) -> u64 {
        self.last_update_id
    }

    pub fn is_synced(&self) -> bool {
        self.synced
    }

    /// Apply one diff, enforcing the Binance gap rules. Returns whether it was applied, harmlessly
    /// skipped (stale), or whether a gap forces a resync.
    pub fn apply_diff(&mut self, update: &DepthUpdate) -> ApplyOutcome {
        if !self.synced {
            // Pre-first-diff: drop anything entirely at/before the snapshot.
            if update.last_update_id <= self.last_update_id {
                return ApplyOutcome::Skipped;
            }
            if check_first(update, self.last_update_id) {
                self.last_update_id = update.last_update_id;
                self.synced = true;
                ApplyOutcome::Applied
            } else {
                // The snapshot is stale relative to the buffered diffs.
                ApplyOutcome::Resync
            }
        } else {
            // Steady state: enforce contiguity.
            if update.last_update_id <= self.last_update_id {
                return ApplyOutcome::Skipped; // duplicate / already applied
            }
            if check_next(update, self.last_update_id) {
                self.last_update_id = update.last_update_id;
                ApplyOutcome::Applied
            } else {
                self.synced = false; // force the caller to refetch + bridge again
                ApplyOutcome::Resync
            }
        }
    }
}

impl Default for LocalOrderBook {
    fn default() -> Self {
        LocalOrderBook::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn upd(u_first: u64, u_last: u64, pu: Option<u64>) -> DepthUpdate {
        DepthUpdate {
            first_update_id: u_first,
            last_update_id: u_last,
            prev_update_id: pu,
        }
    }

    #[test]
    fn check_first_bridges_snapshot() {
        // snapshot lastUpdateId = 100; a diff with U=99, u=105 bridges (99 <= 101 <= 105 false?)
        // Actually 100+1 = 101 must be in [U, u]: U=99 <= 101 <= u=105 -> true.
        assert!(check_first(&upd(99, 105, None), 100));
        // A diff entirely after the bridge point (U=103) leaves a gap.
        assert!(!check_first(&upd(103, 110, None), 100));
        // A diff entirely before is not a bridge either.
        assert!(!check_first(&upd(90, 99, None), 100));
    }

    #[test]
    fn check_next_uses_pu_when_present() {
        // pu must equal previous u.
        assert!(check_next(&upd(106, 110, Some(105)), 105));
        assert!(!check_next(&upd(107, 110, Some(106)), 105));
        // Without pu, fall back to U == prev_u + 1.
        assert!(check_next(&upd(106, 110, None), 105));
        assert!(!check_next(&upd(108, 110, None), 105));
    }

    #[test]
    fn in_order_sequence_applies() {
        let mut book = LocalOrderBook::new();
        book.install_snapshot(100);
        // Stale diff before the snapshot is skipped.
        assert_eq!(book.apply_diff(&upd(90, 99, None)), ApplyOutcome::Skipped);
        // First bridging diff.
        assert_eq!(book.apply_diff(&upd(99, 105, None)), ApplyOutcome::Applied);
        assert!(book.is_synced());
        assert_eq!(book.last_update_id(), 105);
        // Contiguous follow-ups (pu == prev u).
        assert_eq!(
            book.apply_diff(&upd(106, 110, Some(105))),
            ApplyOutcome::Applied
        );
        assert_eq!(
            book.apply_diff(&upd(111, 120, Some(110))),
            ApplyOutcome::Applied
        );
        assert_eq!(book.last_update_id(), 120);
    }

    #[test]
    fn a_gap_triggers_resync() {
        let mut book = LocalOrderBook::new();
        book.install_snapshot(100);
        assert_eq!(book.apply_diff(&upd(99, 105, None)), ApplyOutcome::Applied);
        // pu=108 but prev u was 105 -> a dropped update -> resync.
        assert_eq!(
            book.apply_diff(&upd(109, 115, Some(108))),
            ApplyOutcome::Resync
        );
        assert!(!book.is_synced());
    }

    #[test]
    fn stale_snapshot_forces_resync_on_first_diff() {
        let mut book = LocalOrderBook::new();
        book.install_snapshot(100);
        // First non-stale diff starts at U=103 > 101 -> snapshot too old -> resync.
        assert_eq!(
            book.apply_diff(&upd(103, 110, None)),
            ApplyOutcome::Resync
        );
        assert!(!book.is_synced());
    }

    #[test]
    fn duplicate_diff_in_steady_state_is_skipped() {
        let mut book = LocalOrderBook::new();
        book.install_snapshot(100);
        assert_eq!(book.apply_diff(&upd(99, 105, None)), ApplyOutcome::Applied);
        // A re-delivered diff (u <= last) is harmlessly skipped, not a resync.
        assert_eq!(book.apply_diff(&upd(99, 105, None)), ApplyOutcome::Skipped);
    }
}
