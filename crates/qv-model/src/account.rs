//! Account state: per-currency balances and margin. A domain type (lives here, not in qv-ports)
//! so both the Cache and the Portfolio/Risk ports can reference it without a dependency cycle.

use crate::identifiers::AccountId;
use qv_core::{Currency, Money};
use std::collections::HashMap;

#[derive(Debug, Clone)]
pub struct AccountState {
    pub account_id: AccountId,
    pub balances: HashMap<Currency, Money>,
    pub margin_used: Money,
}

impl AccountState {
    pub fn new(account_id: AccountId, margin_ccy: Currency) -> Self {
        AccountState {
            account_id,
            balances: HashMap::new(),
            margin_used: Money::zero(margin_ccy),
        }
    }

    pub fn balance(&self, ccy: &Currency) -> Money {
        self.balances
            .get(ccy)
            .copied()
            .unwrap_or_else(|| Money::zero(*ccy))
    }

    pub fn set_balance(&mut self, m: Money) {
        self.balances.insert(m.currency(), m);
    }

    /// Add (or subtract) to a currency balance, creating it if absent.
    pub fn adjust(&mut self, delta: Money) -> Result<(), qv_core::ModelError> {
        let cur = self.balance(&delta.currency());
        self.balances
            .insert(delta.currency(), cur.checked_add(delta)?);
        Ok(())
    }
}
