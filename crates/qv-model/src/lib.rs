//! `qv-model` — the domain dependency hub.
//!
//! Typed identifiers, the [`Instrument`] abstraction, the event-sourced [`Order`] FSM, [`Fill`],
//! [`Position`] PnL accounting, and normalized market-data value types. This is the source of
//! truth mirrored to Python via PyO3 (qv-py). Re-exports [`ModelError`] from `qv-core` so the
//! documented contract signatures hold.

pub mod account;
pub mod enums;
pub mod fill;
pub mod identifiers;
pub mod instrument;
pub mod market_data;
pub mod order;
pub mod position;

pub use account::AccountState;
pub use enums::*;
pub use fill::Fill;
pub use identifiers::*;
pub use instrument::{CryptoPerpetual, CurrencyPair, Instrument};
pub use market_data::*;
pub use order::{Order, OrderEvent, OrderFlags};
pub use position::Position;

// Re-export the shared error and core value types so downstream crates can `use qv_model::*`.
pub use qv_core::{Currency, ModelError, Money, Price, Quantity, UnixNanos};

#[cfg(test)]
mod tests {
    use super::*;
    use qv_core::{Currency, Money, Price, Quantity};
    use rust_decimal_macros::dec;

    fn iid() -> InstrumentId {
        InstrumentId::parse("BTCUSDT.BINANCE").unwrap()
    }

    fn perp() -> CryptoPerpetual {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        CryptoPerpetual {
            id: iid(),
            base: btc,
            quote: usdt,
            settlement: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
            is_inverse: false,
            funding_interval_ns: 8 * 3600 * 1_000_000_000,
        }
    }

    fn fill(side: OrderSide, px: &str, qty: &str, tid: &str) -> Fill {
        let usdt = Currency::new("USDT", 8).unwrap();
        Fill {
            trade_id: TradeId::from(tid),
            client_order_id: ClientOrderId::from("s1-00000000000000000001"),
            venue_order_id: VenueOrderId::from("V1"),
            instrument_id: iid(),
            side,
            last_px: Price::from_decimal(px.parse().unwrap(), 2).unwrap(),
            last_qty: Quantity::from_decimal(qty.parse().unwrap(), 3).unwrap(),
            fee: Money::zero(usdt),
            liquidity: LiquiditySide::Taker,
            ts_event: UnixNanos(1),
            ts_init: UnixNanos(1),
        }
    }

    fn new_order(qty: &str) -> Order {
        Order::new(
            StrategyId::from("s1"),
            ClientOrderId::from("s1-00000000000000000001"),
            iid(),
            OrderSide::Buy,
            OrderType::Limit,
            Quantity::from_decimal(qty.parse().unwrap(), 3).unwrap(),
            Some(Price::from_decimal(dec!(50000), 2).unwrap()),
            None,
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        )
    }

    #[test]
    fn order_happy_path_to_filled() {
        let mut o = new_order("1.0");
        assert_eq!(o.status, OrderStatus::Initialized);
        o.apply(OrderEvent::Submitted { ts: UnixNanos(1) }).unwrap();
        o.apply(OrderEvent::Accepted {
            venue_order_id: VenueOrderId::from("V1"),
            ts: UnixNanos(2),
        })
        .unwrap();
        o.apply(OrderEvent::PartiallyFilled(fill(
            OrderSide::Buy,
            "50000",
            "0.4",
            "t1",
        )))
        .unwrap();
        assert_eq!(o.status, OrderStatus::PartiallyFilled);
        assert_eq!(o.leaves_qty().as_decimal(), dec!(0.600));
        o.apply(OrderEvent::Filled(fill(
            OrderSide::Buy,
            "50010",
            "0.6",
            "t2",
        )))
        .unwrap();
        assert_eq!(o.status, OrderStatus::Filled);
        assert!(o.is_terminal());
        // VWAP = (50000*0.4 + 50010*0.6)/1.0 = 50006
        assert_eq!(o.avg_px.unwrap().as_decimal(), dec!(50006.00));
    }

    #[test]
    fn order_illegal_transition_fails() {
        let mut o = new_order("1.0");
        // Cannot Accept before Submit.
        let r = o.apply(OrderEvent::Accepted {
            venue_order_id: VenueOrderId::from("V1"),
            ts: UnixNanos(1),
        });
        assert!(matches!(r, Err(ModelError::InvalidTransition(_))));
    }

    #[test]
    fn order_modify_path() {
        let mut o = new_order("1.0");
        o.apply(OrderEvent::Submitted { ts: UnixNanos(1) }).unwrap();
        o.apply(OrderEvent::Accepted {
            venue_order_id: VenueOrderId::from("V1"),
            ts: UnixNanos(2),
        })
        .unwrap();
        o.apply(OrderEvent::PendingUpdate { ts: UnixNanos(3) })
            .unwrap();
        assert_eq!(o.status, OrderStatus::PendingUpdate);
        o.apply(OrderEvent::Updated {
            quantity: Some(Quantity::from_decimal(dec!(2.0), 3).unwrap()),
            price: Some(Price::from_decimal(dec!(49000), 2).unwrap()),
            ts: UnixNanos(4),
        })
        .unwrap();
        assert_eq!(o.status, OrderStatus::Accepted);
        assert_eq!(o.quantity.as_decimal(), dec!(2.000));
        assert_eq!(o.price.unwrap().as_decimal(), dec!(49000.00));
    }

    #[test]
    fn position_long_then_close_realizes_pnl() {
        let inst = perp();
        let mut pos = Position::flat(
            PositionId::from("P1"),
            iid(),
            2,
            3,
            inst.settlement_currency(),
        );
        pos.apply_fill(&fill(OrderSide::Buy, "50000", "1.0", "t1"), &inst)
            .unwrap();
        assert_eq!(pos.side, PositionSide::Long);
        assert_eq!(pos.quantity.as_decimal(), dec!(1.000));
        // Mark up 1000 -> unrealized = 1000
        let mark = Price::from_decimal(dec!(51000), 2).unwrap();
        assert_eq!(
            pos.unrealized_pnl(mark, &inst).amount(),
            dec!(1000.00000000)
        );
        // Close at 51000 -> realized = 1000
        pos.apply_fill(&fill(OrderSide::Sell, "51000", "1.0", "t2"), &inst)
            .unwrap();
        assert_eq!(pos.side, PositionSide::Flat);
        assert_eq!(pos.realized_pnl.amount(), dec!(1000.00000000));
    }
}
