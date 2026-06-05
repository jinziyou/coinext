//! Port-layer error type. Wraps the domain [`ModelError`] and adds I/O/connection variants for
//! the async venue ports.

use qv_core::ModelError;
use thiserror::Error;

#[derive(Debug, Clone, Error)]
pub enum PortError {
    #[error(transparent)]
    Model(#[from] ModelError),
    #[error("not connected")]
    NotConnected,
    #[error("io error: {0}")]
    Io(String),
    #[error("unsupported: {0}")]
    Unsupported(String),
    #[error("rejected by venue: {0}")]
    Rejected(String),
}

pub type PortResult<T> = Result<T, PortError>;
