//! Optional Binance `ExecutionClient` bridge for exec-svc.
//!
//! When `COINEXT__BINANCE__API_KEY` + `SECRET` are set, the service connects a real
//! [`BinanceExecutionClient`] (testnet by default) and routes Submit/Cancel to the venue.
//! On connect, [`reconcile_on_start`] diffs venue open orders vs the local event log and
//! publishes a reconcile summary (+ Accepted reports for venue-only opens).

use crate::oms::{encode_event_envelope, STREAM_EXEC};
use coinext_adapters_binance::{BinanceConfig, BinanceExecutionClient};
use coinext_core::{Quantity, UnixNanos};
use coinext_model::{
    ClientOrderId, InstrumentId, Order, OrderEvent, OrderFlags, OrderSide, OrderType, StrategyId,
    TimeInForce,
};
use coinext_persistence::EventStore;
use coinext_ports::{CancelOrder, ExecutionClient, ExecutionReport, SubmitOrder};
use std::collections::HashSet;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use tokio::sync::Mutex;

/// Shared venue handle (connected, reports already taken into a pump task).
pub struct VenueBridge {
    client: Mutex<BinanceExecutionClient>,
    /// Venue open orders seen at last reconcile.
    pub reconcile_venue_open: AtomicU64,
    /// Local open orders missing on venue (orphans).
    pub reconcile_orphans: AtomicU64,
    /// Venue open orders missing locally.
    pub reconcile_missing_local: AtomicU64,
}

/// Result of a restart reconcile pass.
#[derive(Debug, Clone, Default)]
pub struct ReconcileSummary {
    pub venue_open: u64,
    pub local_open: u64,
    pub orphans: u64,
    pub missing_local: u64,
    pub ok: bool,
}

impl VenueBridge {
    /// Build + connect + reconcile-on-start + spawn report pump. Returns `None` without keys.
    pub async fn try_from_env(
        store: Arc<dyn EventStore>,
        redis_url: Option<String>,
    ) -> Result<Option<Arc<Self>>, String> {
        let key = std::env::var("COINEXT__BINANCE__API_KEY")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let secret = std::env::var("COINEXT__BINANCE__API_SECRET")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let (Some(api_key), Some(api_secret)) = (key, secret) else {
            return Ok(None);
        };
        let testnet = std::env::var("COINEXT__BINANCE__TESTNET")
            .map(|v| !v.eq_ignore_ascii_case("false"))
            .unwrap_or(true);

        let config = BinanceConfig {
            api_key: Some(api_key),
            api_secret: Some(api_secret),
            testnet,
        };
        let mut client = BinanceExecutionClient::new(config).map_err(|e| e.to_string())?;
        client.connect().await.map_err(|e| e.to_string())?;

        // Reconcile BEFORE taking the report stream so open-order Accepted folds land first.
        let summary = reconcile_on_start(&client, store.as_ref(), redis_url.as_deref()).await?;

        let mut reports = client.take_reports();
        let bridge = Arc::new(VenueBridge {
            client: Mutex::new(client),
            reconcile_venue_open: AtomicU64::new(summary.venue_open),
            reconcile_orphans: AtomicU64::new(summary.orphans),
            reconcile_missing_local: AtomicU64::new(summary.missing_local),
        });

        let pump_store = store.clone();
        let pump_redis = redis_url;
        tokio::spawn(async move {
            while let Some(report) = reports.recv().await {
                if let Err(e) =
                    publish_venue_report(pump_store.as_ref(), pump_redis.as_deref(), &report).await
                {
                    eprintln!("coinext-exec-svc: venue report publish failed: {e}");
                }
            }
            eprintln!("coinext-exec-svc: venue report stream closed");
        });

        println!(
            "coinext-exec-svc: venue mode ENABLED (Binance {} ExecutionClient connected)",
            if testnet { "testnet" } else { "live" }
        );
        println!(
            "coinext-exec-svc: reconcile-on-start venue_open={} local_open={} orphans={} missing_local={} ok={}",
            summary.venue_open, summary.local_open, summary.orphans, summary.missing_local, summary.ok
        );
        Ok(Some(bridge))
    }

    /// Re-run reconcile against venue open orders (e.g. operator trigger / future HTTP hook).
    #[allow(dead_code)]
    pub async fn reconcile_now(
        &self,
        store: &dyn EventStore,
        redis_url: Option<&str>,
    ) -> Result<ReconcileSummary, String> {
        let guard = self.client.lock().await;
        let summary = reconcile_on_start(&*guard, store, redis_url).await?;
        self.reconcile_venue_open
            .store(summary.venue_open, Ordering::Relaxed);
        self.reconcile_orphans
            .store(summary.orphans, Ordering::Relaxed);
        self.reconcile_missing_local
            .store(summary.missing_local, Ordering::Relaxed);
        Ok(summary)
    }

    pub async fn submit_market(
        &self,
        strategy_id: &str,
        coid: &str,
        symbol: &str,
        venue: &str,
        side: &str,
        qty: f64,
        now_ns: u64,
    ) -> Result<(), String> {
        let instrument = format!("{symbol}.{venue}");
        let iid = InstrumentId::parse(&instrument)
            .ok_or_else(|| format!("bad instrument id {instrument}"))?;
        let side = parse_side(side)?;
        let qty = Quantity::from_f64(qty.max(0.0), 8).map_err(|e| e.to_string())?;
        let order = Order::new(
            StrategyId::from(strategy_id),
            ClientOrderId::from(coid),
            iid,
            side,
            OrderType::Market,
            qty,
            None,
            None,
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(now_ns),
        );
        let guard = self.client.lock().await;
        guard
            .submit_order(SubmitOrder { order })
            .await
            .map_err(|e| e.to_string())
    }

    pub async fn cancel(
        &self,
        coid: &str,
        symbol: &str,
        venue: &str,
    ) -> Result<(), String> {
        let instrument = format!("{symbol}.{venue}");
        let iid = InstrumentId::parse(&instrument)
            .ok_or_else(|| format!("bad instrument id {instrument}"))?;
        let guard = self.client.lock().await;
        guard
            .cancel_order(CancelOrder {
                client_order_id: ClientOrderId::from(coid),
                instrument_id: iid,
            })
            .await
            .map_err(|e| e.to_string())
    }
}

fn parse_side(s: &str) -> Result<OrderSide, String> {
    match s.to_ascii_lowercase().as_str() {
        "buy" | "b" => Ok(OrderSide::Buy),
        "sell" | "s" => Ok(OrderSide::Sell),
        other => Err(format!("invalid side {other}")),
    }
}

/// Diff venue open orders vs local event-log open set; publish venue-only Accepted reports.
async fn reconcile_on_start(
    client: &BinanceExecutionClient,
    store: &dyn EventStore,
    redis_url: Option<&str>,
) -> Result<ReconcileSummary, String> {
    let venue_reports = client.reconcile().await.map_err(|e| e.to_string())?;
    let venue_ids: HashSet<String> = venue_reports
        .iter()
        .map(|r| r.client_order_id().as_str().to_string())
        .collect();

    let local_open = local_open_client_ids(store)?;
    let local_ids: HashSet<String> = local_open.iter().cloned().collect();

    let missing_local: Vec<_> = venue_ids.difference(&local_ids).cloned().collect();
    let orphans: Vec<_> = local_ids.difference(&venue_ids).cloned().collect();

    // Seed venue-only opens into the durable log as Accepted so subsequent folds match venue truth.
    let now = UnixNanos(now_ns_u64());
    let strategy = StrategyId::from("reconcile");
    for report in &venue_reports {
        let coid = report.client_order_id().clone();
        if missing_local.iter().any(|id| id == coid.as_str()) {
            if let ExecutionReport::Accepted {
                venue_order_id, ..
            } = report
            {
                let ev = OrderEvent::Accepted {
                    venue_order_id: venue_order_id.clone(),
                    ts: now,
                };
                let _ = store.append(&strategy, &coid, &ev, now);
            }
            let _ = publish_venue_report(store, redis_url, report).await;
        }
    }

    // Publish a summary event for operators / UI.
    let summary = ReconcileSummary {
        venue_open: venue_ids.len() as u64,
        local_open: local_ids.len() as u64,
        orphans: orphans.len() as u64,
        missing_local: missing_local.len() as u64,
        ok: orphans.is_empty() && missing_local.is_empty(),
    };
    let payload = serde_json::json!({
        "kind": "reconcile",
        "source": "venue",
        "venue_open": summary.venue_open,
        "local_open": summary.local_open,
        "orphans": orphans,
        "missing_local": missing_local,
        "ok": summary.ok,
        "ts_ns": now.as_u64(),
    });
    if let Some(url) = redis_url {
        if let Ok(frame) = encode_event_envelope(&payload) {
            let url = url.to_string();
            let _ = tokio::task::spawn_blocking(move || {
                let client = redis::Client::open(url.as_str()).map_err(|e| e.to_string())?;
                let mut con = client.get_connection().map_err(|e| e.to_string())?;
                let _: String = redis::cmd("XADD")
                    .arg(STREAM_EXEC)
                    .arg("*")
                    .arg("e")
                    .arg(frame)
                    .query(&mut con)
                    .map_err(|e| e.to_string())?;
                Ok::<(), String>(())
            })
            .await;
        }
    }
    Ok(summary)
}

/// Fold event streams into open client_order_ids (submitted/accepted without terminal event).
pub(crate) fn local_open_client_ids(store: &dyn EventStore) -> Result<HashSet<String>, String> {
    let orders = store.list_orders().map_err(|e| e.to_string())?;
    let mut open = HashSet::new();
    for (_sid, coid) in orders {
        let events = store.replay(&coid).map_err(|e| e.to_string())?;
        let mut is_open = false;
        for ev in events {
            match ev {
                OrderEvent::Submitted { .. }
                | OrderEvent::Accepted { .. }
                | OrderEvent::PartiallyFilled(_)
                | OrderEvent::PendingCancel { .. }
                | OrderEvent::PendingUpdate { .. } => is_open = true,
                OrderEvent::Filled(_)
                | OrderEvent::Canceled { .. }
                | OrderEvent::Rejected { .. }
                | OrderEvent::Denied { .. }
                | OrderEvent::Expired { .. } => is_open = false,
                OrderEvent::Initialized { .. } | OrderEvent::Updated { .. } => {}
            }
        }
        if is_open {
            open.insert(coid.as_str().to_string());
        }
    }
    Ok(open)
}

fn now_ns_u64() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_persistence::{EventStore, SqliteEventStore};

    #[test]
    fn local_open_fold_tracks_submitted_vs_filled() {
        let store = SqliteEventStore::new(":memory:").unwrap();
        let sid = StrategyId::from("s");
        let open_id = ClientOrderId::from("open-1");
        let closed_id = ClientOrderId::from("closed-1");
        let now = UnixNanos(1);
        store
            .append(&sid, &open_id, &OrderEvent::Submitted { ts: now }, now)
            .unwrap();
        store
            .append(
                &sid,
                &open_id,
                &OrderEvent::Accepted {
                    venue_order_id: coinext_model::VenueOrderId::from("V1"),
                    ts: now,
                },
                now,
            )
            .unwrap();
        store
            .append(&sid, &closed_id, &OrderEvent::Submitted { ts: now }, now)
            .unwrap();
        store
            .append(&sid, &closed_id, &OrderEvent::Canceled { ts: now }, now)
            .unwrap();

        let open = local_open_client_ids(&store).unwrap();
        assert!(open.contains("open-1"));
        assert!(!open.contains("closed-1"));
        let listed = store.list_orders().unwrap();
        assert_eq!(listed.len(), 2);
    }
}

async fn publish_venue_report(
    store: &dyn EventStore,
    redis_url: Option<&str>,
    report: &ExecutionReport,
) -> Result<(), String> {
    let (kind, strategy_hint, coid, extra) = match report {
        ExecutionReport::Accepted {
            client_order_id,
            venue_order_id,
        } => (
            "accepted",
            "venue",
            client_order_id.as_str().to_string(),
            serde_json::json!({"venue_order_id": venue_order_id.as_str()}),
        ),
        ExecutionReport::Fill(f) => (
            "filled",
            "venue",
            f.client_order_id.as_str().to_string(),
            serde_json::json!({
                "qty": f.last_qty.as_f64(),
                "px": f.last_px.as_f64(),
                "instrument": f.instrument_id.to_string(),
            }),
        ),
        ExecutionReport::Canceled { client_order_id } => (
            "canceled",
            "venue",
            client_order_id.as_str().to_string(),
            serde_json::json!({}),
        ),
        ExecutionReport::Rejected {
            client_order_id,
            reason,
        } => (
            "rejected",
            "venue",
            client_order_id.as_str().to_string(),
            serde_json::json!({"reason": reason}),
        ),
        other => (
            "report",
            "venue",
            other.client_order_id().as_str().to_string(),
            serde_json::json!({"detail": format!("{other:?}")}),
        ),
    };

    // Best-effort durable note (strategy namespace "venue" when unknown).
    let strategy = StrategyId::from(strategy_hint);
    let coid = ClientOrderId::from(coid.as_str());
    let now = UnixNanos(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0),
    );
    let ev = match report {
        ExecutionReport::Accepted {
            venue_order_id, ..
        } => OrderEvent::Accepted {
            venue_order_id: venue_order_id.clone(),
            ts: now,
        },
        ExecutionReport::Canceled { .. } => OrderEvent::Canceled { ts: now },
        ExecutionReport::Rejected { reason, .. } => OrderEvent::Rejected {
            reason: reason.clone(),
            ts: now,
        },
        ExecutionReport::Fill(f) => OrderEvent::Filled(f.clone()),
        _ => OrderEvent::Submitted { ts: now },
    };
    let _ = store.append(&strategy, &coid, &ev, now);

    let mut payload = serde_json::json!({
        "kind": kind,
        "client_order_id": coid.as_str(),
        "source": "venue",
        "ts_ns": now.as_u64(),
    });
    if let Some(obj) = payload.as_object_mut() {
        if let Some(map) = extra.as_object() {
            for (k, v) in map {
                obj.insert(k.clone(), v.clone());
            }
        }
    }

    if let Some(url) = redis_url {
        let frame = encode_event_envelope(&payload)?;
        let url = url.to_string();
        tokio::task::spawn_blocking(move || {
            let client = redis::Client::open(url.as_str()).map_err(|e| e.to_string())?;
            let mut con = client.get_connection().map_err(|e| e.to_string())?;
            let _: String = redis::cmd("XADD")
                .arg(STREAM_EXEC)
                .arg("*")
                .arg("e")
                .arg(frame)
                .query(&mut con)
                .map_err(|e| e.to_string())?;
            Ok::<(), String>(())
        })
        .await
        .map_err(|e| e.to_string())??;
    }
    Ok(())
}
