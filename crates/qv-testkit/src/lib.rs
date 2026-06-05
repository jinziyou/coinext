//! `qv-testkit` — sample instruments and deterministic synthetic market-data generators used by
//! unit tests, the example backtest, and parity fixtures.

use qv_core::{Currency, Price, Quantity, UnixNanos};
use qv_model::{
    AggregationSource, Bar, BarAggregation, BarSpec, BarType, CryptoPerpetual, CurrencyPair,
    Instrument, InstrumentId, MarketEvent, PriceType,
};
use rust_decimal::Decimal;
use std::sync::Arc;

/// A BTCUSDT spot pair on a given venue (2dp price, 3dp size).
pub fn sample_spot(venue: &str) -> Arc<dyn Instrument> {
    let usdt = Currency::new("USDT", 8).unwrap();
    let btc = Currency::new("BTC", 8).unwrap();
    Arc::new(CurrencyPair {
        id: InstrumentId::parse(&format!("BTCUSDT.{venue}")).unwrap(),
        base: btc,
        quote: usdt,
        price_precision: 2,
        size_precision: 3,
        price_increment: Price::from_decimal(Decimal::new(1, 2), 2).unwrap(),
        size_increment: Quantity::from_decimal(Decimal::new(1, 3), 3).unwrap(),
        min_notional: None,
        maker_fee: Decimal::new(2, 4), // 0.0002
        taker_fee: Decimal::new(4, 4), // 0.0004
    })
}

/// A BTCUSDT-PERP USDⓈ-M linear perpetual on a given venue.
pub fn sample_perp(venue: &str) -> Arc<dyn Instrument> {
    let usdt = Currency::new("USDT", 8).unwrap();
    let btc = Currency::new("BTC", 8).unwrap();
    Arc::new(CryptoPerpetual {
        id: InstrumentId::parse(&format!("BTCUSDT-PERP.{venue}")).unwrap(),
        base: btc,
        quote: usdt,
        settlement: usdt,
        price_precision: 2,
        size_precision: 3,
        price_increment: Price::from_decimal(Decimal::new(1, 2), 2).unwrap(),
        size_increment: Quantity::from_decimal(Decimal::new(1, 3), 3).unwrap(),
        min_notional: None,
        multiplier: Quantity::from_raw(1, 0).unwrap(),
        maker_fee: Decimal::new(2, 4),
        taker_fee: Decimal::new(4, 4),
        is_inverse: false,
        funding_interval_ns: 8 * 3600 * 1_000_000_000,
    })
}

/// Build a 1-minute External bar series from a slice of close prices (OHLC collapsed to close).
pub fn bars_from_closes(
    iid: &InstrumentId,
    start_ns: u64,
    step_ns: u64,
    closes: &[f64],
) -> Vec<MarketEvent> {
    let bar_type = BarType {
        instrument_id: iid.clone(),
        spec: BarSpec {
            step: 1,
            aggregation: BarAggregation::Minute,
            price_type: PriceType::Last,
        },
        source: AggregationSource::External,
    };
    closes
        .iter()
        .enumerate()
        .map(|(i, &c)| {
            let px = Price::from_f64(c, 2).unwrap();
            let ts = UnixNanos(start_ns + i as u64 * step_ns);
            MarketEvent::Bar(Bar {
                bar_type: bar_type.clone(),
                open: px,
                high: px,
                low: px,
                close: px,
                volume: Quantity::from_f64(10.0, 3).unwrap(),
                ts_event: ts,
                ts_init: ts,
            })
        })
        .collect()
}

/// A deterministic sine-wave price series (no RNG — reproducible across runs/platforms).
pub fn sine_closes(n: usize, base: f64, amplitude: f64, period: usize) -> Vec<f64> {
    (0..n)
        .map(|i| {
            let phase = (i as f64 / period as f64) * std::f64::consts::TAU;
            base + amplitude * phase.sin()
        })
        .collect()
}
