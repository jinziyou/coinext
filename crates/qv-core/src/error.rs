//! The single error type shared by the domain. Re-exported by `qv-model` as `ModelError`
//! so the documented contract signatures (`Result<_, ModelError>`) hold.

use thiserror::Error;

/// Fail-fast domain error. Every value-type constructor and FSM transition returns this
/// on bad input rather than silently producing a corrupt value (the integer-domain invariant).
#[derive(Debug, Clone, PartialEq, Eq, Error)]
pub enum ModelError {
    /// A float input was NaN or infinite (rejected at the f64 -> domain boundary).
    #[error("value is not finite: {0}")]
    NotFinite(String),
    /// A value exceeded the representable integer range for its precision.
    #[error("value out of range: {0}")]
    OutOfRange(String),
    /// A non-negative-only value (Quantity, size) was given a negative input.
    #[error("negative value not allowed: {0}")]
    Negative(String),
    /// Checked integer arithmetic overflowed.
    #[error("arithmetic overflow")]
    Overflow,
    /// Two `Money` values of different currencies were combined.
    #[error("currency mismatch: {0} vs {1}")]
    CurrencyMismatch(String, String),
    /// Two fixed-precision values of different precision were combined.
    #[error("precision mismatch: {0} vs {1}")]
    PrecisionMismatch(u8, u8),
    /// An illegal Order/Position state transition was attempted.
    #[error("invalid transition: {0}")]
    InvalidTransition(String),
    /// Any other invariant violation, with context.
    #[error("invalid value: {0}")]
    Invalid(String),
}

/// Convenience alias used throughout the core.
pub type Result<T> = core::result::Result<T, ModelError>;
