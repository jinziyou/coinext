//! `qv-portfolio` — read-only account/position/PnL analytics over the Cache. Sources marks
//! internally from the Cache (last quote mid for spot; the mark feed for perps), so
//! `unrealized_pnl` takes no mark argument. Returns owned `Money`/`Position` to avoid borrow
//! tangles with the rest of the single-threaded core.

use qv_cache::Cache;
use qv_core::{Currency, Money};
use qv_model::{InstrumentId, Position};
use qv_ports::Portfolio;
use std::cell::RefCell;
use std::rc::Rc;

/// Portfolio facade. `settle` is the common settlement currency used for aggregate figures
/// (gross exposure etc.); per-instrument figures use the instrument's settlement currency.
pub struct PortfolioState {
    cache: Rc<RefCell<Cache>>,
    settle: Currency,
}

impl PortfolioState {
    pub fn new(cache: Rc<RefCell<Cache>>, settle: Currency) -> Self {
        PortfolioState { cache, settle }
    }
}

impl Portfolio for PortfolioState {
    fn position(&self, id: &InstrumentId) -> Option<Position> {
        self.cache.borrow().position(id).cloned()
    }

    fn net_exposure(&self, id: &InstrumentId) -> Money {
        let cache = self.cache.borrow();
        match (cache.position(id), cache.instrument(id), cache.mark(id)) {
            (Some(pos), Some(inst), Some(mark)) => pos.notional(mark, &*inst),
            _ => Money::zero(self.settle),
        }
    }

    fn unrealized_pnl(&self, id: &InstrumentId) -> Money {
        let cache = self.cache.borrow();
        match (cache.position(id), cache.instrument(id), cache.mark(id)) {
            (Some(pos), Some(inst), Some(mark)) => pos.unrealized_pnl(mark, &*inst),
            _ => Money::zero(self.settle),
        }
    }

    fn realized_pnl(&self, id: &InstrumentId) -> Money {
        self.cache
            .borrow()
            .position(id)
            .map(|p| p.realized_pnl)
            .unwrap_or_else(|| Money::zero(self.settle))
    }

    fn gross_exposure(&self) -> Money {
        let cache = self.cache.borrow();
        let mut total = Money::zero(self.settle);
        for pos in cache.positions() {
            if let (Some(inst), Some(mark)) = (
                cache.instrument(&pos.instrument_id),
                cache.mark(&pos.instrument_id),
            ) {
                let notional = pos.notional(mark, &*inst);
                if notional.currency() == self.settle {
                    total = total.checked_add(notional).unwrap_or(total);
                }
            }
        }
        total
    }

    fn balance(&self, ccy: &Currency) -> Money {
        self.cache
            .borrow()
            .account()
            .map(|a| a.balance(ccy))
            .unwrap_or_else(|| Money::zero(*ccy))
    }

    fn equity(&self) -> Money {
        let cache = self.cache.borrow();
        let mut total = cache
            .account()
            .map(|a| a.balance(&self.settle))
            .unwrap_or_else(|| Money::zero(self.settle));
        for pos in cache.positions() {
            if pos.realized_pnl.currency() == self.settle {
                total = total.checked_add(pos.realized_pnl).unwrap_or(total);
            }
            if let (Some(inst), Some(mark)) = (
                cache.instrument(&pos.instrument_id),
                cache.mark(&pos.instrument_id),
            ) {
                let unreal = pos.unrealized_pnl(mark, &*inst);
                if unreal.currency() == self.settle {
                    total = total.checked_add(unreal).unwrap_or(total);
                }
            }
        }
        total
    }
}
