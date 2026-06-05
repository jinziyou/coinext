//! `qv-indicators` — streaming, incremental technical indicators. The SAME code feeds warm-up
//! (replaying history via the HistoryReader) and live, so behavior is identical everywhere.
//!
//! Indicators operate on `f64` signals (not the integer money domain): they produce advisory
//! signals, never prices/quantities that settle, so float is appropriate here.

use std::collections::VecDeque;

/// Common streaming-indicator interface.
pub trait Indicator {
    /// Feed the next observation.
    fn update(&mut self, value: f64);
    /// Current value, if the indicator has enough data.
    fn value(&self) -> Option<f64>;
    /// Whether `value()` will return `Some`.
    fn is_ready(&self) -> bool {
        self.value().is_some()
    }
}

/// Simple Moving Average over a fixed window.
#[derive(Debug, Clone)]
pub struct Sma {
    period: usize,
    buf: VecDeque<f64>,
    sum: f64,
}

impl Sma {
    pub fn new(period: usize) -> Self {
        assert!(period > 0, "SMA period must be > 0");
        Sma {
            period,
            buf: VecDeque::with_capacity(period),
            sum: 0.0,
        }
    }
}

impl Indicator for Sma {
    fn update(&mut self, value: f64) {
        self.buf.push_back(value);
        self.sum += value;
        if self.buf.len() > self.period {
            if let Some(old) = self.buf.pop_front() {
                self.sum -= old;
            }
        }
    }
    fn value(&self) -> Option<f64> {
        if self.buf.len() == self.period {
            Some(self.sum / self.period as f64)
        } else {
            None
        }
    }
}

/// Exponential Moving Average (`alpha = 2/(period+1)`), seeded on the first observation.
#[derive(Debug, Clone)]
pub struct Ema {
    alpha: f64,
    current: Option<f64>,
    count: usize,
    period: usize,
}

impl Ema {
    pub fn new(period: usize) -> Self {
        assert!(period > 0, "EMA period must be > 0");
        Ema {
            alpha: 2.0 / (period as f64 + 1.0),
            current: None,
            count: 0,
            period,
        }
    }
}

impl Indicator for Ema {
    fn update(&mut self, value: f64) {
        self.count += 1;
        self.current = Some(match self.current {
            None => value,
            Some(prev) => self.alpha * value + (1.0 - self.alpha) * prev,
        });
    }
    fn value(&self) -> Option<f64> {
        if self.count >= self.period {
            self.current
        } else {
            None
        }
    }
}

/// Wilder's Relative Strength Index.
#[derive(Debug, Clone)]
pub struct Rsi {
    period: usize,
    prev: Option<f64>,
    avg_gain: f64,
    avg_loss: f64,
    count: usize,
}

impl Rsi {
    pub fn new(period: usize) -> Self {
        assert!(period > 0, "RSI period must be > 0");
        Rsi {
            period,
            prev: None,
            avg_gain: 0.0,
            avg_loss: 0.0,
            count: 0,
        }
    }
}

impl Indicator for Rsi {
    fn update(&mut self, value: f64) {
        let prev = match self.prev {
            None => {
                self.prev = Some(value);
                return;
            }
            Some(p) => p,
        };
        let change = value - prev;
        let (gain, loss) = if change >= 0.0 {
            (change, 0.0)
        } else {
            (0.0, -change)
        };
        self.count += 1;
        if self.count <= self.period {
            self.avg_gain += gain / self.period as f64;
            self.avg_loss += loss / self.period as f64;
        } else {
            let p = self.period as f64;
            self.avg_gain = (self.avg_gain * (p - 1.0) + gain) / p;
            self.avg_loss = (self.avg_loss * (p - 1.0) + loss) / p;
        }
        self.prev = Some(value);
    }
    fn value(&self) -> Option<f64> {
        if self.count < self.period {
            return None;
        }
        if self.avg_loss == 0.0 {
            return Some(100.0);
        }
        let rs = self.avg_gain / self.avg_loss;
        Some(100.0 - 100.0 / (1.0 + rs))
    }
}

/// Average True Range (Wilder smoothing). `update_hlc` takes high/low/close.
#[derive(Debug, Clone)]
pub struct Atr {
    period: usize,
    prev_close: Option<f64>,
    atr: Option<f64>,
    count: usize,
}

impl Atr {
    pub fn new(period: usize) -> Self {
        assert!(period > 0, "ATR period must be > 0");
        Atr {
            period,
            prev_close: None,
            atr: None,
            count: 0,
        }
    }

    pub fn update_hlc(&mut self, high: f64, low: f64, close: f64) {
        let tr = match self.prev_close {
            None => high - low,
            Some(pc) => (high - low).max((high - pc).abs()).max((low - pc).abs()),
        };
        self.count += 1;
        self.atr = Some(match self.atr {
            None => tr,
            Some(prev) => (prev * (self.period as f64 - 1.0) + tr) / self.period as f64,
        });
        self.prev_close = Some(close);
    }

    pub fn value(&self) -> Option<f64> {
        if self.count >= self.period {
            self.atr
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sma_window() {
        let mut s = Sma::new(3);
        s.update(1.0);
        s.update(2.0);
        assert_eq!(s.value(), None);
        s.update(3.0);
        assert_eq!(s.value(), Some(2.0));
        s.update(6.0); // window [2,3,6]
        assert_eq!(s.value(), Some(11.0 / 3.0));
    }

    #[test]
    fn ema_seeds_and_tracks() {
        let mut e = Ema::new(2);
        e.update(10.0);
        e.update(20.0);
        let v = e.value().unwrap();
        assert!(v > 10.0 && v < 20.0);
    }

    #[test]
    fn rsi_all_gains_is_100() {
        let mut r = Rsi::new(3);
        for v in [1.0, 2.0, 3.0, 4.0, 5.0] {
            r.update(v);
        }
        assert_eq!(r.value(), Some(100.0));
    }
}
