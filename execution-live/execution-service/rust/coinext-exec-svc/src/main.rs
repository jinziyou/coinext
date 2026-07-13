//! `coinext-exec-svc` — the standalone **OMS/execution service** (service name `exec-svc`).
//!
//! Status: **partial**.
//!
//! 1. Durable [`SqliteEventStore`] + [`SqliteSeqCursor`].
//! 2. HTTP control on `:8081` (`/health`, kill-switch get/post).
//! 3. Prometheus text stub on `:9102/metrics` (includes OMS counters).
//! 4. Redis `coinext.control` → kill-switch.
//! 5. Redis `coinext.exec.cmd` → paper OMS (mint ClientOrderId, append events, publish
//!    reports on `coinext.exec`). Venue ExecutionClient remains deferred.

mod oms;
mod venue;

use coinext_persistence::{EventStore, SeqCursor, SqliteEventStore, SqliteSeqCursor};
use oms::{
    decode_cmd_envelope, encode_event_envelope, PaperOms, STREAM_EXEC, STREAM_EXEC_CMD,
};
use venue::VenueBridge;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::signal;
use tokio::task::spawn_blocking;
use tokio::time::sleep;

const STREAM_CTRL: &str = "coinext.control";

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

fn stream_field_bytes(msg: &redis::streams::StreamId) -> Option<Vec<u8>> {
    for key in ["e", "data"] {
        if let Some(data) = msg.map.get(key) {
            match data {
                redis::Value::BulkString(b) => return Some(b.clone()),
                redis::Value::SimpleString(s) => return Some(s.as_bytes().to_vec()),
                _ => {}
            }
        }
    }
    None
}

#[tokio::main]
async fn main() {
    let db_path = env_or("COINEXT__PERSIST__DB", ".coinext/exec-svc.db");
    let metrics_addr: SocketAddr = env_or("COINEXT__EXEC__METRICS_ADDR", "0.0.0.0:9102")
        .parse()
        .unwrap_or_else(|_| "0.0.0.0:9102".parse().unwrap());
    let control_addr: SocketAddr = env_or("COINEXT__EXEC__CONTROL_ADDR", "0.0.0.0:8081")
        .parse()
        .unwrap_or_else(|_| "0.0.0.0:8081".parse().unwrap());
    let redis_url = std::env::var("COINEXT__REDIS__URL").ok().filter(|s| !s.is_empty());

    println!("=========================================================");
    println!("  Coinext exec-svc (coinext-exec-svc)  [PARTIAL]");
    println!("  role    : OMS / execution + pre-trade risk service");
    println!("  persist : {db_path}");
    println!("  metrics : http://{metrics_addr}/metrics");
    println!("  control : http://{control_addr}/health | /killswitch");
    println!(
        "  redis   : {}",
        redis_url
            .as_deref()
            .unwrap_or("(unset — control/cmd consumers off)")
    );
    println!("  cmd     : {STREAM_EXEC_CMD} → paper OMS → {STREAM_EXEC}");
    println!("=========================================================");

    let store: Arc<dyn EventStore> = match SqliteEventStore::new(&db_path) {
        Ok(s) => Arc::new(s),
        Err(e) => {
            eprintln!("coinext-exec-svc: failed to open event store at {db_path}: {e}");
            std::process::exit(1);
        }
    };
    let cursor: Arc<dyn SeqCursor> = match SqliteSeqCursor::new(&db_path) {
        Ok(c) => Arc::new(c),
        Err(e) => {
            eprintln!("coinext-exec-svc: failed to open seq cursor at {db_path}: {e}");
            std::process::exit(1);
        }
    };

    match cursor.next("exec-svc-boot") {
        Ok(seq) => println!("coinext-exec-svc: seq_cursor ready (boot seq={seq})"),
        Err(e) => {
            eprintln!("coinext-exec-svc: seq_cursor next failed: {e}");
            std::process::exit(1);
        }
    }
    let _ = store.replay(&coinext_model::ClientOrderId::from("boot-probe"));

    let kill = Arc::new(AtomicBool::new(false));
    let oms = Arc::new(PaperOms::new(store.clone(), cursor, kill.clone()));

    let venue = match VenueBridge::try_from_env(store.clone(), redis_url.clone()).await {
        Ok(v) => v,
        Err(e) => {
            eprintln!("coinext-exec-svc: venue connect failed ({e}); continuing in paper mode");
            None
        }
    };
    if venue.is_none() {
        println!("coinext-exec-svc: venue mode OFF (paper fills). Set COINEXT__BINANCE__API_KEY/SECRET for testnet.");
    }

    let kill_metrics = kill.clone();
    let oms_metrics = oms.clone();
    let kill_control = kill.clone();
    let kill_redis = kill.clone();
    let oms_cmd = oms.clone();
    let venue_cmd = venue.clone();

    let venue_metrics = venue.clone();
    let metrics_task = tokio::spawn(async move {
        if let Err(e) = serve_metrics(metrics_addr, kill_metrics, oms_metrics, venue_metrics).await
        {
            eprintln!("coinext-exec-svc: metrics server error: {e}");
        }
    });
    let control_task = tokio::spawn(async move {
        if let Err(e) = serve_control(control_addr, kill_control).await {
            eprintln!("coinext-exec-svc: control server error: {e}");
        }
    });

    let redis_tasks = if let Some(url) = redis_url.clone() {
        let url_ctrl = url.clone();
        let url_cmd = url;
        let t1 = tokio::spawn(async move {
            loop {
                if let Err(e) = redis_control_loop(url_ctrl.clone(), kill_redis.clone()).await {
                    eprintln!("coinext-exec-svc: redis control loop error: {e}; retry in 2s");
                    sleep(Duration::from_secs(2)).await;
                } else {
                    break;
                }
            }
        });
        let t2 = tokio::spawn(async move {
            loop {
                if let Err(e) =
                    redis_cmd_loop(url_cmd.clone(), oms_cmd.clone(), venue_cmd.clone()).await
                {
                    eprintln!("coinext-exec-svc: redis cmd loop error: {e}; retry in 2s");
                    sleep(Duration::from_secs(2)).await;
                } else {
                    break;
                }
            }
        });
        Some((t1, t2))
    } else {
        None
    };

    let mode = if venue.is_some() { "venue" } else { "paper" };
    println!(
        "coinext-exec-svc: running (Ctrl-C to stop). OMS mode={mode} on {STREAM_EXEC_CMD}."
    );
    let _ = signal::ctrl_c().await;
    println!("coinext-exec-svc: shutdown requested");
    metrics_task.abort();
    control_task.abort();
    if let Some((t1, t2)) = redis_tasks {
        t1.abort();
        t2.abort();
    }
}

async fn redis_control_loop(url: String, kill: Arc<AtomicBool>) -> Result<(), String> {
    spawn_blocking(move || redis_control_loop_blocking(url, kill))
        .await
        .map_err(|e| e.to_string())?
}

fn redis_control_loop_blocking(url: String, kill: Arc<AtomicBool>) -> Result<(), String> {
    let client = redis::Client::open(url.as_str()).map_err(|e| e.to_string())?;
    let mut con = client.get_connection().map_err(|e| e.to_string())?;
    let mut last_id = "$".to_string();
    println!("coinext-exec-svc: redis control consumer on {STREAM_CTRL}");
    loop {
        let result: redis::RedisResult<redis::streams::StreamReadReply> = redis::cmd("XREAD")
            .arg("BLOCK")
            .arg(5000)
            .arg("STREAMS")
            .arg(STREAM_CTRL)
            .arg(&last_id)
            .query(&mut con);
        match result {
            Ok(reply) => {
                for stream in reply.keys {
                    for msg in stream.ids {
                        last_id = msg.id.clone();
                        if let Some(bytes) = stream_field_bytes(&msg) {
                            if let Some(engaged) = parse_kill_switch(&bytes) {
                                kill.store(engaged, Ordering::SeqCst);
                                println!(
                                    "coinext-exec-svc: control stream killswitch engaged={engaged}"
                                );
                            }
                        }
                    }
                }
            }
            Err(e) => {
                let msg = e.to_string();
                if !msg.to_ascii_lowercase().contains("timeout") {
                    return Err(msg);
                }
            }
        }
    }
}

async fn redis_cmd_loop(
    url: String,
    oms: Arc<PaperOms>,
    venue: Option<Arc<VenueBridge>>,
) -> Result<(), String> {
    // Blocking XREAD on a worker; async venue submit on the runtime.
    let client = redis::Client::open(url.as_str()).map_err(|e| e.to_string())?;
    let mut last_id = "$".to_string();
    println!("coinext-exec-svc: redis OMS consumer on {STREAM_EXEC_CMD}");
    loop {
        let url_c = url.clone();
        let lid = last_id.clone();
        let poll = spawn_blocking(move || {
            let mut con = redis::Client::open(url_c.as_str())
                .map_err(|e| e.to_string())?
                .get_connection()
                .map_err(|e| e.to_string())?;
            let result: redis::RedisResult<redis::streams::StreamReadReply> = redis::cmd("XREAD")
                .arg("BLOCK")
                .arg(5000)
                .arg("STREAMS")
                .arg(STREAM_EXEC_CMD)
                .arg(&lid)
                .query(&mut con);
            match result {
                Ok(reply) => Ok(Some(reply)),
                Err(e) => {
                    let msg = e.to_string();
                    if msg.to_ascii_lowercase().contains("timeout") {
                        Ok(None)
                    } else {
                        Err(msg)
                    }
                }
            }
        })
        .await
        .map_err(|e| e.to_string())??;

        let Some(reply) = poll else {
            continue;
        };
        for stream in reply.keys {
            for msg in stream.ids {
                last_id = msg.id.clone();
                let Some(bytes) = stream_field_bytes(&msg) else {
                    continue;
                };
                let Some(cmd) = decode_cmd_envelope(&bytes) else {
                    eprintln!("coinext-exec-svc: skip undecodable cmd on {STREAM_EXEC_CMD}");
                    continue;
                };
                let events = oms.handle_async(cmd, venue.as_deref()).await;
                for ev in events {
                    match encode_event_envelope(&ev) {
                        Ok(frame) => {
                            let url_p = url.clone();
                            let _ = spawn_blocking(move || {
                                let mut con = redis::Client::open(url_p.as_str())
                                    .map_err(|e| e.to_string())?
                                    .get_connection()
                                    .map_err(|e| e.to_string())?;
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
                        Err(e) => eprintln!("coinext-exec-svc: encode event failed: {e}"),
                    }
                }
            }
        }
        let _ = &client; // keep
    }
}

fn parse_kill_switch(frame: &[u8]) -> Option<bool> {
    #[derive(serde::Deserialize)]
    struct CtrlPayload {
        kind: String,
        engaged: bool,
    }
    let decoded: (u16, u8, serde_bytes::ByteBuf, u64, serde_bytes::ByteBuf) =
        rmp_serde::from_slice(frame).ok()?;
    let (_schema, msg_type, _trace, _ts, payload) = decoded;
    if msg_type != 8 {
        return None;
    }
    let ctrl: CtrlPayload = rmp_serde::from_slice(payload.as_slice()).ok()?;
    if ctrl.kind != "CtrlKillSwitch" {
        return None;
    }
    Some(ctrl.engaged)
}

async fn serve_metrics(
    addr: SocketAddr,
    kill: Arc<AtomicBool>,
    oms: Arc<PaperOms>,
    venue: Option<Arc<VenueBridge>>,
) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        let (mut sock, _) = listener.accept().await?;
        let engaged = kill.load(Ordering::SeqCst);
        let submits = oms.submits.load(Ordering::SeqCst);
        let cancels = oms.cancels.load(Ordering::SeqCst);
        let denials = oms.denials.load(Ordering::SeqCst);
        let (v_open, v_orph, v_miss, v_mode) = match &venue {
            Some(v) => (
                v.reconcile_venue_open.load(Ordering::Relaxed),
                v.reconcile_orphans.load(Ordering::Relaxed),
                v.reconcile_missing_local.load(Ordering::Relaxed),
                1,
            ),
            None => (0, 0, 0, 0),
        };
        let body = format!(
            "# HELP coinext_exec_svc_up 1 if process is serving\n\
             # TYPE coinext_exec_svc_up gauge\n\
             coinext_exec_svc_up 1\n\
             # HELP coinext_killswitch_engaged 1 if kill-switch is on\n\
             # TYPE coinext_killswitch_engaged gauge\n\
             coinext_killswitch_engaged {engaged}\n\
             # HELP coinext_oms_submits_total OMS submits\n\
             # TYPE coinext_oms_submits_total counter\n\
             coinext_oms_submits_total {submits}\n\
             # HELP coinext_oms_cancels_total OMS cancels\n\
             # TYPE coinext_oms_cancels_total counter\n\
             coinext_oms_cancels_total {cancels}\n\
             # HELP risk_denials Total risk denials (kill-switch + gate)\n\
             # TYPE risk_denials counter\n\
             risk_denials {denials}\n\
             # HELP coinext_venue_mode 1 if Binance venue client is connected\n\
             # TYPE coinext_venue_mode gauge\n\
             coinext_venue_mode {v_mode}\n\
             # HELP coinext_reconcile_venue_open open orders on venue at last reconcile\n\
             # TYPE coinext_reconcile_venue_open gauge\n\
             coinext_reconcile_venue_open {v_open}\n\
             # HELP coinext_reconcile_orphans local opens missing on venue\n\
             # TYPE coinext_reconcile_orphans gauge\n\
             coinext_reconcile_orphans {v_orph}\n\
             # HELP coinext_reconcile_missing_local venue opens missing locally\n\
             # TYPE coinext_reconcile_missing_local gauge\n\
             coinext_reconcile_missing_local {v_miss}\n",
            engaged = if engaged { 1 } else { 0 },
            submits = submits,
            cancels = cancels,
            denials = denials,
            v_mode = v_mode,
            v_open = v_open,
            v_orph = v_orph,
            v_miss = v_miss,
        );
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = sock.write_all(resp.as_bytes()).await;
    }
}

async fn serve_control(addr: SocketAddr, kill: Arc<AtomicBool>) -> std::io::Result<()> {
    let listener = TcpListener::bind(addr).await?;
    loop {
        let (sock, _) = listener.accept().await?;
        let kill = kill.clone();
        tokio::spawn(async move {
            if let Err(e) = handle_control(sock, kill).await {
                eprintln!("coinext-exec-svc: control conn error: {e}");
            }
        });
    }
}

async fn handle_control(mut sock: TcpStream, kill: Arc<AtomicBool>) -> std::io::Result<()> {
    let mut buf = vec![0u8; 4096];
    let n = sock.read(&mut buf).await?;
    let req = String::from_utf8_lossy(&buf[..n]);
    let line = req.lines().next().unwrap_or("");
    let (status, body) = if line.starts_with("GET /health") || line.starts_with("GET /healthz") {
        (
            200,
            format!(
                r#"{{"status":"ok","service":"exec-svc","killswitch":{},"oms":"paper"}}"#,
                kill.load(Ordering::SeqCst)
            ),
        )
    } else if line.starts_with("GET /killswitch") {
        (
            200,
            format!(r#"{{"engaged":{}}}"#, kill.load(Ordering::SeqCst)),
        )
    } else if line.starts_with("POST /killswitch") {
        let engage = !(req.contains("\"engage\":false") || req.contains("\"engage\": false"));
        kill.store(engage, Ordering::SeqCst);
        (
            200,
            format!(r#"{{"engaged":{engage},"note":"in-process flag; paper OMS honours this"}}"#),
        )
    } else if line.starts_with("GET /reconcile") {
        // Last reconcile snapshot is exposed via metrics; this endpoint returns kill state + mode.
        (
            200,
            format!(
                r#"{{"note":"see Prometheus coinext_reconcile_* gauges for last venue reconcile","killswitch":{}}}"#,
                kill.load(Ordering::SeqCst)
            ),
        )
    } else {
        (404, r#"{"error":"not found"}"#.to_string())
    };
    let resp = format!(
        "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
        if status == 200 { "OK" } else { "Not Found" },
        body.len()
    );
    sock.write_all(resp.as_bytes()).await?;
    Ok(())
}
