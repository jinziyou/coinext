//! Paper-trading port clients for offline sandbox/live loops (no venue keys).
//!
//! [`ReplayDataClient`] pushes a fixed market-event list then closes the stream.
//! [`PaperFillExec`] acknowledges submits and immediately fills (Accepted + Fill) so the
//! port-driven [`crate::LiveKernel`] can be exercised end-to-end without Binance credentials.

use coinext_core::{Currency, Money, Price, UnixNanos};
use coinext_model::{
    Bar, BarType, Fill, LiquiditySide, MarketEvent, TradeId, Venue, VenueOrderId,
};
use coinext_ports::{
    CancelOrder, DataClient, ExecutionClient, ExecutionReport, ModifyOrder, PortResult,
    SubmitOrder, Subscription,
};
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::sync::mpsc;

/// A `DataClient` that replays a fixed list of market events on `connect`, then closes the stream.
pub struct ReplayDataClient {
    tx: Option<mpsc::Sender<MarketEvent>>,
    rx: Option<mpsc::Receiver<MarketEvent>>,
    events: Vec<MarketEvent>,
}

impl ReplayDataClient {
    pub fn new(events: Vec<MarketEvent>) -> Self {
        let (tx, rx) = mpsc::channel(events.len().max(1) + 8);
        ReplayDataClient {
            tx: Some(tx),
            rx: Some(rx),
            events,
        }
    }
}

#[async_trait::async_trait]
impl DataClient for ReplayDataClient {
    async fn connect(&mut self) -> PortResult<()> {
        let tx = self.tx.take().expect("connect called twice");
        for ev in self.events.drain(..) {
            let _ = tx.send(ev).await;
        }
        // Dropping `tx` closes the stream so LiveKernel returns after drain.
        Ok(())
    }

    async fn subscribe(&mut self, _sub: Subscription) -> PortResult<()> {
        Ok(())
    }

    async fn unsubscribe(&mut self, _sub: Subscription) -> PortResult<()> {
        Ok(())
    }

    async fn request_bars(
        &self,
        _bar_type: BarType,
        _start: UnixNanos,
        _end: UnixNanos,
    ) -> PortResult<Vec<Bar>> {
        Ok(Vec::new())
    }

    fn take_stream(&mut self) -> mpsc::Receiver<MarketEvent> {
        self.rx.take().expect("take_stream called twice")
    }

    async fn disconnect(&mut self) -> PortResult<()> {
        Ok(())
    }
}

/// Instant paper fills: every submit → Accepted + full Fill (no resting book).
///
/// Market orders fill at [`PaperFillExec::default_price`]; limit orders fill at their limit when set,
/// otherwise at the default. Suitable for offline LiveKernel demos and integration tests.
pub struct PaperFillExec {
    venue: Venue,
    default_price: Price,
    settle: Currency,
    report_tx: mpsc::Sender<ExecutionReport>,
    rx: Option<mpsc::Receiver<ExecutionReport>>,
    trade_seq: AtomicU64,
    /// Number of submits observed (tests / metrics).
    pub submits: AtomicU64,
}

impl PaperFillExec {
    pub fn new(venue: Venue, default_price: Price, settle: Currency) -> Self {
        let (tx, rx) = mpsc::channel(1024);
        PaperFillExec {
            venue,
            default_price,
            settle,
            report_tx: tx,
            rx: Some(rx),
            trade_seq: AtomicU64::new(0),
            submits: AtomicU64::new(0),
        }
    }
}

#[async_trait::async_trait]
impl ExecutionClient for PaperFillExec {
    fn venue(&self) -> Venue {
        self.venue.clone()
    }

    async fn connect(&mut self) -> PortResult<()> {
        Ok(())
    }

    async fn submit_order(&self, cmd: SubmitOrder) -> PortResult<()> {
        self.submits.fetch_add(1, Ordering::SeqCst);
        let o = &cmd.order;
        let venue_order_id = VenueOrderId::from(format!("PAPER-{}", o.client_order_id.as_str()));
        let _ = self
            .report_tx
            .send(ExecutionReport::Accepted {
                client_order_id: o.client_order_id.clone(),
                venue_order_id: venue_order_id.clone(),
            })
            .await;
        let px = o.price.unwrap_or(self.default_price);
        let seq = self.trade_seq.fetch_add(1, Ordering::SeqCst) + 1;
        let fill = Fill {
            trade_id: TradeId::from(format!("PT-{seq}")),
            client_order_id: o.client_order_id.clone(),
            venue_order_id,
            instrument_id: o.instrument_id.clone(),
            side: o.side,
            last_px: px,
            last_qty: o.quantity,
            fee: Money::zero(self.settle),
            liquidity: LiquiditySide::Taker,
            ts_event: UnixNanos(0),
            ts_init: UnixNanos(0),
        };
        let _ = self.report_tx.send(ExecutionReport::Fill(fill)).await;
        Ok(())
    }

    async fn cancel_order(&self, cmd: CancelOrder) -> PortResult<()> {
        let _ = self
            .report_tx
            .send(ExecutionReport::Canceled {
                client_order_id: cmd.client_order_id,
            })
            .await;
        Ok(())
    }

    async fn modify_order(&self, _cmd: ModifyOrder) -> PortResult<()> {
        Err(coinext_ports::PortError::Unsupported(
            "PaperFillExec does not model order modify".into(),
        ))
    }

    async fn reconcile(&self) -> PortResult<Vec<ExecutionReport>> {
        Ok(Vec::new())
    }

    fn take_reports(&mut self) -> mpsc::Receiver<ExecutionReport> {
        self.rx.take().expect("take_reports called twice")
    }

    async fn disconnect(&mut self) -> PortResult<()> {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Currency, Quantity, UnixNanos};
    use coinext_model::{
        ClientOrderId, InstrumentId, Order, OrderFlags, OrderSide, OrderType, StrategyId,
        TimeInForce,
    };
    use coinext_ports::ExecutionClient;
    use rust_decimal_macros::dec;

    #[tokio::test]
    async fn paper_fill_exec_emits_accepted_and_fill() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let px = Price::from_decimal(dec!(50000), 2).unwrap();
        let mut exec = PaperFillExec::new(Venue::from("BINANCE"), px, usdt);
        exec.connect().await.unwrap();
        let mut rx = exec.take_reports();
        let order = Order::new(
            StrategyId::from("t"),
            ClientOrderId::from("c1"),
            InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
            OrderSide::Buy,
            OrderType::Market,
            Quantity::from_decimal(dec!(0.1), 3).unwrap(),
            None,
            None,
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        );
        exec.submit_order(SubmitOrder { order }).await.unwrap();
        let a = rx.recv().await.expect("accepted");
        assert!(matches!(a, ExecutionReport::Accepted { .. }));
        let f = rx.recv().await.expect("fill");
        assert!(matches!(f, ExecutionReport::Fill(_)));
        assert_eq!(exec.submits.load(Ordering::SeqCst), 1);
    }
}
