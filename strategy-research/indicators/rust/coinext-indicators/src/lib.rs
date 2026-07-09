//! `coinext-indicators` — streaming, incremental technical indicators. The SAME code feeds warm-up
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
        // Reject non-finite ticks: NaN/Inf would permanently poison the running sum
        // (NaN-NaN=NaN, Inf-Inf=NaN). Skip the update, keeping the last good state.
        if !value.is_finite() {
            return;
        }
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
        // Skip non-finite ticks so a bad observation can't poison the recursive state.
        if !value.is_finite() {
            return;
        }
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
        // Skip non-finite ticks so a bad observation can't poison the smoothed averages.
        if !value.is_finite() {
            return;
        }
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
        // Skip the bar if any leg is non-finite, leaving the smoothed ATR untouched.
        if !high.is_finite() || !low.is_finite() || !close.is_finite() {
            return;
        }
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

/// Moving Average Convergence/Divergence: `macd = EMA(fast) - EMA(slow)`, signal = `EMA(signal)`
/// of the macd line, histogram = `macd - signal`.
#[derive(Debug, Clone)]
pub struct Macd {
    fast: Ema,
    slow: Ema,
    signal: Ema,
}

impl Macd {
    pub fn new(fast: usize, slow: usize, signal: usize) -> Self {
        Macd {
            fast: Ema::new(fast),
            slow: Ema::new(slow),
            signal: Ema::new(signal),
        }
    }

    pub fn update(&mut self, value: f64) {
        // Skip non-finite ticks entirely so neither the component EMAs nor the signal EMA
        // advance on a poisoned observation.
        if !value.is_finite() {
            return;
        }
        self.fast.update(value);
        self.slow.update(value);
        // Feed the signal EMA the macd line only once both EMAs are warm.
        if let (Some(f), Some(s)) = (self.fast.value(), self.slow.value()) {
            self.signal.update(f - s);
        }
    }

    /// `(macd, signal, histogram)` once warm.
    pub fn value(&self) -> Option<(f64, f64, f64)> {
        let macd = self.fast.value()? - self.slow.value()?;
        let signal = self.signal.value()?;
        Some((macd, signal, macd - signal))
    }

    pub fn is_ready(&self) -> bool {
        self.value().is_some()
    }
}

/// Bollinger Bands: a `period` SMA mid-band with `k` population-stddev bands.
#[derive(Debug, Clone)]
pub struct Bollinger {
    period: usize,
    k: f64,
    buf: VecDeque<f64>,
    sum: f64,
    sum_sq: f64,
}

impl Bollinger {
    pub fn new(period: usize, k: f64) -> Self {
        assert!(period > 0, "Bollinger period must be > 0");
        Bollinger {
            period,
            k,
            buf: VecDeque::with_capacity(period),
            sum: 0.0,
            sum_sq: 0.0,
        }
    }

    pub fn update(&mut self, value: f64) {
        // Reject non-finite ticks: they would permanently poison the running sum / sum-of-squares.
        if !value.is_finite() {
            return;
        }
        self.buf.push_back(value);
        self.sum += value;
        self.sum_sq += value * value;
        if self.buf.len() > self.period {
            if let Some(old) = self.buf.pop_front() {
                self.sum -= old;
                self.sum_sq -= old * old;
            }
        }
    }

    /// `(lower, mid, upper)` once the window is full.
    pub fn value(&self) -> Option<(f64, f64, f64)> {
        if self.buf.len() < self.period {
            return None;
        }
        let n = self.period as f64;
        let mean = self.sum / n;
        let var = (self.sum_sq / n - mean * mean).max(0.0); // guard tiny negative from rounding
        let sd = var.sqrt();
        Some((mean - self.k * sd, mean, mean + self.k * sd))
    }

    pub fn is_ready(&self) -> bool {
        self.value().is_some()
    }
}

/// Rolling Volume-Weighted Average Price over a `period`-bar window: `sum(price*vol)/sum(vol)`.
#[derive(Debug, Clone)]
pub struct Vwap {
    period: usize,
    win: VecDeque<(f64, f64)>, // (price, volume)
    sum_pv: f64,
    sum_v: f64,
}

impl Vwap {
    pub fn new(period: usize) -> Self {
        assert!(period > 0, "VWAP period must be > 0");
        Vwap {
            period,
            win: VecDeque::with_capacity(period),
            sum_pv: 0.0,
            sum_v: 0.0,
        }
    }

    pub fn update(&mut self, price: f64, volume: f64) {
        // Reject non-finite price/volume: they would permanently poison the running sums.
        if !price.is_finite() || !volume.is_finite() {
            return;
        }
        self.win.push_back((price, volume));
        self.sum_pv += price * volume;
        self.sum_v += volume;
        if self.win.len() > self.period {
            if let Some((p, v)) = self.win.pop_front() {
                self.sum_pv -= p * v;
                self.sum_v -= v;
            }
        }
    }

    pub fn value(&self) -> Option<f64> {
        if self.win.len() < self.period || self.sum_v == 0.0 {
            return None;
        }
        Some(self.sum_pv / self.sum_v)
    }

    pub fn is_ready(&self) -> bool {
        self.value().is_some()
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

    #[test]
    fn macd_warms_and_histogram_is_consistent() {
        let mut m = Macd::new(3, 6, 4);
        assert_eq!(m.value(), None);
        for i in 1..=30 {
            m.update(i as f64);
        }
        let (macd, signal, hist) = m.value().unwrap();
        assert!((hist - (macd - signal)).abs() < 1e-12);
        // On a strictly rising series the fast EMA leads the slow -> macd > 0.
        assert!(macd > 0.0);
    }

    #[test]
    fn bollinger_constant_series_has_zero_width() {
        let mut b = Bollinger::new(4, 2.0);
        for _ in 0..4 {
            b.update(100.0);
        }
        let (lo, mid, up) = b.value().unwrap();
        assert!((mid - 100.0).abs() < 1e-12);
        assert!((up - lo).abs() < 1e-12); // zero stddev -> bands collapse to the mid
    }

    #[test]
    fn bollinger_known_stddev() {
        // window [2,4,4,4,5,5,7,9]: mean 5, population sd 2 -> 1-sigma bands [3, 7].
        let mut b = Bollinger::new(8, 1.0);
        for v in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0] {
            b.update(v);
        }
        let (lo, mid, up) = b.value().unwrap();
        assert!((mid - 5.0).abs() < 1e-9);
        assert!((lo - 3.0).abs() < 1e-9 && (up - 7.0).abs() < 1e-9);
    }

    #[test]
    fn vwap_weights_by_volume() {
        let mut v = Vwap::new(2);
        assert_eq!(v.value(), None);
        v.update(100.0, 1.0);
        v.update(110.0, 3.0); // (100*1 + 110*3) / (1+3) = 430/4 = 107.5
        assert_eq!(v.value(), Some(107.5));
    }

    // A single non-finite tick must NOT poison the running state: the indicator should ignore it
    // and end up exactly where it would have without the bad tick. Without input validation,
    // running-sum indicators would return NaN forever even after the bad tick leaves the window.
    #[test]
    fn non_finite_ticks_are_ignored_not_poisoning() {
        // A series long enough to warm every indicator (MACD(3,6,4) needs ~10 obs).
        let clean: Vec<f64> = (1..=20).map(|i| i as f64).collect();
        let poison = [f64::NAN, f64::INFINITY, f64::NEG_INFINITY];

        // Build a series that interleaves the poison ticks among the clean ones.
        let dirty: Vec<f64> = {
            let mut v = Vec::new();
            for (idx, &x) in clean.iter().enumerate() {
                v.push(x);
                if idx < poison.len() {
                    v.push(poison[idx]);
                }
            }
            v
        };

        // SMA
        {
            let (mut a, mut b) = (Sma::new(3), Sma::new(3));
            clean.iter().for_each(|&x| a.update(x));
            dirty.iter().for_each(|&x| b.update(x));
            let av = a.value().unwrap();
            let bv = b.value().unwrap();
            assert!(bv.is_finite());
            assert!((av - bv).abs() < 1e-12, "sma {av} != {bv}");
        }

        // EMA
        {
            let (mut a, mut b) = (Ema::new(3), Ema::new(3));
            clean.iter().for_each(|&x| a.update(x));
            dirty.iter().for_each(|&x| b.update(x));
            let av = a.value().unwrap();
            let bv = b.value().unwrap();
            assert!(bv.is_finite());
            assert!((av - bv).abs() < 1e-12, "ema {av} != {bv}");
        }

        // RSI
        {
            let (mut a, mut b) = (Rsi::new(3), Rsi::new(3));
            clean.iter().for_each(|&x| a.update(x));
            dirty.iter().for_each(|&x| b.update(x));
            let av = a.value().unwrap();
            let bv = b.value().unwrap();
            assert!(bv.is_finite());
            assert!((av - bv).abs() < 1e-12, "rsi {av} != {bv}");
        }

        // MACD
        {
            let (mut a, mut b) = (Macd::new(3, 6, 4), Macd::new(3, 6, 4));
            clean.iter().for_each(|&x| a.update(x));
            dirty.iter().for_each(|&x| b.update(x));
            let (am, asig, ah) = a.value().unwrap();
            let (bm, bsig, bh) = b.value().unwrap();
            assert!(bm.is_finite() && bsig.is_finite() && bh.is_finite());
            assert!((am - bm).abs() < 1e-12, "macd {am} != {bm}");
            assert!((asig - bsig).abs() < 1e-12, "signal {asig} != {bsig}");
            assert!((ah - bh).abs() < 1e-12, "hist {ah} != {bh}");
        }

        // Bollinger
        {
            let (mut a, mut b) = (Bollinger::new(3, 2.0), Bollinger::new(3, 2.0));
            clean.iter().for_each(|&x| a.update(x));
            dirty.iter().for_each(|&x| b.update(x));
            let (alo, amid, aup) = a.value().unwrap();
            let (blo, bmid, bup) = b.value().unwrap();
            assert!(blo.is_finite() && bmid.is_finite() && bup.is_finite());
            assert!((alo - blo).abs() < 1e-12, "boll lo {alo} != {blo}");
            assert!((amid - bmid).abs() < 1e-12, "boll mid {amid} != {bmid}");
            assert!((aup - bup).abs() < 1e-12, "boll up {aup} != {bup}");
        }

        // ATR (update_hlc): inject a non-finite leg into one bar.
        {
            let (mut a, mut b) = (Atr::new(3), Atr::new(3));
            let bars = [
                (10.0, 8.0, 9.0),
                (11.0, 9.0, 10.0),
                (12.0, 10.0, 11.0),
                (13.0, 11.0, 12.0),
                (14.0, 12.0, 13.0),
            ];
            for &(h, l, c) in &bars {
                a.update_hlc(h, l, c);
            }
            // Same 5 bars, but with extra fully-poisoned bars slipped in that must be ignored.
            b.update_hlc(10.0, 8.0, 9.0);
            b.update_hlc(f64::NAN, 10.0, 11.0); // poisoned high -> ignored
            b.update_hlc(11.0, 9.0, 10.0);
            b.update_hlc(12.0, 10.0, 11.0);
            b.update_hlc(13.0, f64::INFINITY, 12.0); // poisoned low -> ignored
            b.update_hlc(13.0, 11.0, 12.0);
            b.update_hlc(10.0, 9.0, f64::NAN); // poisoned close -> ignored
            b.update_hlc(14.0, 12.0, 13.0);
            let av = a.value().unwrap();
            let bv = b.value().unwrap();
            assert!(bv.is_finite());
            assert!((av - bv).abs() < 1e-12, "atr {av} != {bv}");
        }

        // VWAP (price, volume): inject non-finite price/volume that must be ignored.
        {
            let (mut a, mut b) = (Vwap::new(3), Vwap::new(3));
            let bars = [(100.0, 1.0), (110.0, 2.0), (120.0, 3.0), (130.0, 4.0)];
            for &(p, v) in &bars {
                a.update(p, v);
            }
            // Same 4 bars, but with extra poisoned ticks slipped in that must be ignored.
            b.update(100.0, 1.0);
            b.update(f64::NAN, 2.0); // poisoned price -> ignored
            b.update(110.0, 2.0);
            b.update(120.0, 3.0);
            b.update(125.0, f64::INFINITY); // poisoned volume -> ignored
            b.update(130.0, 4.0);
            let av = a.value().unwrap();
            let bv = b.value().unwrap();
            assert!(bv.is_finite());
            assert!((av - bv).abs() < 1e-12, "vwap {av} != {bv}");
        }
    }
}
