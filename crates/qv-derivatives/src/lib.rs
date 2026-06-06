//! `qv-derivatives` — European option pricing (Black–Scholes) + greeks + implied vol.
//!
//! Pure `f64` math, no dependencies (the normal CDF uses an Abramowitz–Stegun erf approximation,
//! max abs error ~1.5e-7). Inputs are decimals, NOT percentages: `rate = 0.05`, `vol = 0.2`,
//! `t_years = 0.5`. Greeks are returned in their natural units — vega per `1.0` of vol (divide by
//! 100 for "per 1%"), theta per year (divide by 365 for "per day"), rho per `1.0` of rate. This is
//! the SAME library a Python strategy uses through `qv_py` to price options, compute greeks, and
//! back out implied vol from market premiums (e.g. to delta-hedge).

use std::f64::consts::PI;

/// Black–Scholes inputs (spot, strike, time-to-expiry in years, risk-free rate, volatility).
#[derive(Debug, Clone, Copy)]
pub struct BsInputs {
    pub spot: f64,
    pub strike: f64,
    pub t_years: f64,
    pub rate: f64,
    pub vol: f64,
}

/// The five first-order greeks (see the module docs for units).
#[derive(Debug, Clone, Copy, PartialEq)]
pub struct Greeks {
    pub delta: f64,
    pub gamma: f64,
    pub vega: f64,
    pub theta: f64,
    pub rho: f64,
}

/// Standard normal PDF `φ(x)`.
pub fn norm_pdf(x: f64) -> f64 {
    (-0.5 * x * x).exp() / (2.0 * PI).sqrt()
}

/// Standard normal CDF `Φ(x) = ½(1 + erf(x/√2))` via the A&S 7.1.26 erf approximation.
pub fn norm_cdf(x: f64) -> f64 {
    0.5 * (1.0 + erf(x / std::f64::consts::SQRT_2))
}

fn erf(x: f64) -> f64 {
    // A&S 7.1.26 (|error| <= 1.5e-7).
    let sign = if x < 0.0 { -1.0 } else { 1.0 };
    let x = x.abs();
    let t = 1.0 / (1.0 + 0.3275911 * x);
    let y = 1.0
        - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
            + 0.254829592)
            * t
            * (-x * x).exp();
    sign * y
}

/// Intrinsic value at expiry/degenerate inputs: `max(S-K,0)` for a call, `max(K-S,0)` for a put.
fn intrinsic(i: &BsInputs, is_call: bool) -> f64 {
    if is_call {
        (i.spot - i.strike).max(0.0)
    } else {
        (i.strike - i.spot).max(0.0)
    }
}

/// `(d1, d2)`; `None` when the diffusion term `σ√T` is non-positive (priced as intrinsic instead).
fn d1d2(i: &BsInputs) -> Option<(f64, f64)> {
    let vol_sqrt_t = i.vol * i.t_years.sqrt();
    if vol_sqrt_t <= 0.0 || i.spot <= 0.0 || i.strike <= 0.0 {
        return None;
    }
    let d1 = ((i.spot / i.strike).ln() + (i.rate + 0.5 * i.vol * i.vol) * i.t_years) / vol_sqrt_t;
    Some((d1, d1 - vol_sqrt_t))
}

/// Black–Scholes price of a European call/put. Degenerate inputs (`T<=0`, `vol<=0`) → intrinsic.
pub fn price(i: &BsInputs, is_call: bool) -> f64 {
    let Some((d1, d2)) = d1d2(i) else {
        return intrinsic(i, is_call);
    };
    let disc = (-i.rate * i.t_years).exp();
    if is_call {
        i.spot * norm_cdf(d1) - i.strike * disc * norm_cdf(d2)
    } else {
        i.strike * disc * norm_cdf(-d2) - i.spot * norm_cdf(-d1)
    }
}

/// All five greeks. Degenerate inputs return zeros (no sensitivity once `σ√T` collapses).
pub fn greeks(i: &BsInputs, is_call: bool) -> Greeks {
    let Some((d1, d2)) = d1d2(i) else {
        return Greeks {
            delta: 0.0,
            gamma: 0.0,
            vega: 0.0,
            theta: 0.0,
            rho: 0.0,
        };
    };
    let disc = (-i.rate * i.t_years).exp();
    let sqrt_t = i.t_years.sqrt();
    let pdf_d1 = norm_pdf(d1);
    let gamma = pdf_d1 / (i.spot * i.vol * sqrt_t);
    let vega = i.spot * pdf_d1 * sqrt_t; // per 1.0 vol
    let common_theta = -(i.spot * pdf_d1 * i.vol) / (2.0 * sqrt_t);
    if is_call {
        Greeks {
            delta: norm_cdf(d1),
            gamma,
            vega,
            theta: common_theta - i.rate * i.strike * disc * norm_cdf(d2),
            rho: i.strike * i.t_years * disc * norm_cdf(d2),
        }
    } else {
        Greeks {
            delta: norm_cdf(d1) - 1.0,
            gamma,
            vega,
            theta: common_theta + i.rate * i.strike * disc * norm_cdf(-d2),
            rho: -i.strike * i.t_years * disc * norm_cdf(-d2),
        }
    }
}

/// Back out the volatility that reprices `market_price`, via Newton's method (using vega) with a
/// bisection fallback. `None` if the price is below intrinsic or no vol in `(0, 5]` reproduces it.
pub fn implied_vol(
    market_price: f64,
    spot: f64,
    strike: f64,
    t_years: f64,
    rate: f64,
    is_call: bool,
) -> Option<f64> {
    if t_years <= 0.0 || market_price <= 0.0 {
        return None;
    }
    let mk = |vol: f64| BsInputs {
        spot,
        strike,
        t_years,
        rate,
        vol,
    };
    // Below intrinsic (discounted) -> no real vol prices it.
    if market_price < intrinsic(&mk(0.0), is_call) - 1e-9 {
        return None;
    }
    // Newton from a 0.2 seed.
    let mut vol = 0.2;
    for _ in 0..100 {
        let p = price(&mk(vol), is_call);
        let v = greeks(&mk(vol), is_call).vega;
        let diff = p - market_price;
        if diff.abs() < 1e-8 {
            return Some(vol);
        }
        if v < 1e-10 {
            break; // vega too flat for Newton; fall through to bisection
        }
        vol -= diff / v;
        if !(1e-9..=5.0).contains(&vol) {
            break;
        }
    }
    // Bisection on [1e-6, 5.0] as a robust fallback.
    let (mut lo, mut hi) = (1e-6_f64, 5.0_f64);
    if (price(&mk(lo), is_call) - market_price) * (price(&mk(hi), is_call) - market_price) > 0.0 {
        return None; // not bracketed
    }
    for _ in 0..200 {
        let mid = 0.5 * (lo + hi);
        let diff = price(&mk(mid), is_call) - market_price;
        if diff.abs() < 1e-8 {
            return Some(mid);
        }
        if (price(&mk(lo), is_call) - market_price) * diff <= 0.0 {
            hi = mid;
        } else {
            lo = mid;
        }
    }
    Some(0.5 * (lo + hi))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn atm() -> BsInputs {
        BsInputs {
            spot: 100.0,
            strike: 100.0,
            t_years: 1.0,
            rate: 0.05,
            vol: 0.2,
        }
    }

    #[test]
    fn norm_cdf_known_points() {
        assert!((norm_cdf(0.0) - 0.5).abs() < 1e-9);
        assert!((norm_cdf(1.96) - 0.975).abs() < 1e-3);
        assert!((norm_cdf(-1.96) - 0.025).abs() < 1e-3);
    }

    #[test]
    fn bs_textbook_price() {
        // S=K=100, T=1, r=5%, vol=20%: call ~10.4506, put ~5.5735 (standard reference values).
        let i = atm();
        assert!(
            (price(&i, true) - 10.4506).abs() < 1e-3,
            "call {}",
            price(&i, true)
        );
        assert!(
            (price(&i, false) - 5.5735).abs() < 1e-3,
            "put {}",
            price(&i, false)
        );
    }

    #[test]
    fn put_call_parity() {
        // C - P = S - K e^{-rT}.
        let i = atm();
        let lhs = price(&i, true) - price(&i, false);
        let rhs = i.spot - i.strike * (-i.rate * i.t_years).exp();
        assert!((lhs - rhs).abs() < 1e-6);
    }

    #[test]
    fn greeks_relations() {
        let i = atm();
        let c = greeks(&i, true);
        let p = greeks(&i, false);
        // call delta - put delta = 1; gamma + vega identical across call/put.
        assert!((c.delta - p.delta - 1.0).abs() < 1e-9);
        assert!((c.gamma - p.gamma).abs() < 1e-12);
        assert!((c.vega - p.vega).abs() < 1e-12);
        assert!((c.delta - 0.6368).abs() < 1e-3, "call delta {}", c.delta);
        assert!(c.vega > 0.0 && c.gamma > 0.0);
    }

    #[test]
    fn implied_vol_round_trips() {
        let i = atm();
        for (is_call, vol) in [(true, 0.15), (true, 0.35), (false, 0.25)] {
            let p = price(&BsInputs { vol, ..i }, is_call);
            let iv = implied_vol(p, i.spot, i.strike, i.t_years, i.rate, is_call).unwrap();
            assert!((iv - vol).abs() < 1e-4, "iv {iv} != {vol}");
        }
    }

    #[test]
    fn degenerate_inputs_price_intrinsic() {
        let i = BsInputs {
            spot: 120.0,
            strike: 100.0,
            t_years: 0.0,
            rate: 0.05,
            vol: 0.2,
        };
        assert_eq!(price(&i, true), 20.0); // expired call, in the money
        assert_eq!(price(&i, false), 0.0); // expired put, worthless
    }
}
