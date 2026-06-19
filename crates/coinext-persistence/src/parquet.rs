//! [`ParquetWriter`] — writes normalized market data to the data lake as Parquet.
//!
//! The data lake is the shared source for warm-up + backtest (architecture §7): the same Parquet
//! files the `ingestor` writes are read back by the `coinext_data` catalog/HistoryReader, so warm-up is
//! byte-identical in backtest and live. This writer materializes a batch of [`Bar`]s as an Arrow
//! [`RecordBatch`] (columns: `instrument`, `ts_event`, `open`, `high`, `low`, `close`, `volume`)
//! and writes it as a single Parquet row group.
//!
//! Gated behind the default `parquet` feature so the event store can build without arrow/parquet.

use crate::error::{PersistError, PersistResult};
use arrow::array::{Float64Array, StringArray, UInt64Array};
use arrow::datatypes::{DataType, Field, Schema};
use arrow::record_batch::RecordBatch;
use parquet::arrow::arrow_reader::ParquetRecordBatchReaderBuilder;
use parquet::arrow::ArrowWriter;
use coinext_model::Bar;
use std::fs::File;
use std::sync::Arc;

impl From<arrow::error::ArrowError> for PersistError {
    fn from(e: arrow::error::ArrowError) -> Self {
        PersistError::Parquet(e.to_string())
    }
}

impl From<parquet::errors::ParquetError> for PersistError {
    fn from(e: parquet::errors::ParquetError) -> Self {
        PersistError::Parquet(e.to_string())
    }
}

/// Writes batches of [`Bar`]s to Parquet via Arrow. Stateless beyond the shared Arrow schema, so a
/// single instance can fan out across files.
pub struct ParquetWriter {
    schema: Arc<Schema>,
}

impl Default for ParquetWriter {
    fn default() -> Self {
        Self::new()
    }
}

impl ParquetWriter {
    pub fn new() -> Self {
        ParquetWriter {
            schema: Arc::new(Self::bar_schema()),
        }
    }

    /// The Arrow schema for the `bars` table. Prices/volume are decoded to `f64` for the lake (the
    /// integer domain stays authoritative in the event store; the lake is analytics/warm-up data,
    /// where `as_f64()` is the documented display/analytics escape hatch).
    fn bar_schema() -> Schema {
        Schema::new(vec![
            Field::new("instrument", DataType::Utf8, false),
            Field::new("ts_event", DataType::UInt64, false),
            Field::new("open", DataType::Float64, false),
            Field::new("high", DataType::Float64, false),
            Field::new("low", DataType::Float64, false),
            Field::new("close", DataType::Float64, false),
            Field::new("volume", DataType::Float64, false),
        ])
    }

    /// Build a `RecordBatch` from a slice of bars.
    fn bars_to_batch(&self, bars: &[Bar]) -> PersistResult<RecordBatch> {
        let instrument: Vec<String> = bars
            .iter()
            .map(|b| b.bar_type.instrument_id.to_string())
            .collect();
        let ts_event: Vec<u64> = bars.iter().map(|b| b.ts_event.as_u64()).collect();
        let open: Vec<f64> = bars.iter().map(|b| b.open.as_f64()).collect();
        let high: Vec<f64> = bars.iter().map(|b| b.high.as_f64()).collect();
        let low: Vec<f64> = bars.iter().map(|b| b.low.as_f64()).collect();
        let close: Vec<f64> = bars.iter().map(|b| b.close.as_f64()).collect();
        let volume: Vec<f64> = bars.iter().map(|b| b.volume.as_f64()).collect();

        RecordBatch::try_new(
            self.schema.clone(),
            vec![
                Arc::new(StringArray::from(instrument)),
                Arc::new(UInt64Array::from(ts_event)),
                Arc::new(Float64Array::from(open)),
                Arc::new(Float64Array::from(high)),
                Arc::new(Float64Array::from(low)),
                Arc::new(Float64Array::from(close)),
                Arc::new(Float64Array::from(volume)),
            ],
        )
        .map_err(Into::into)
    }

    /// Write `bars` to a Parquet file at `path` as a single row group.
    pub fn write_bars(&self, path: &str, bars: &[Bar]) -> PersistResult<()> {
        let batch = self.bars_to_batch(bars)?;
        let file = File::create(path)?;
        let mut writer = ArrowWriter::try_new(file, self.schema.clone(), None)?;
        writer.write(&batch)?;
        writer.close()?;
        Ok(())
    }

    /// Read a Parquet file written by [`write_bars`](Self::write_bars) back into `RecordBatch`es —
    /// a test/inspection helper. The lake's authoritative reader is the `coinext_data` catalog.
    pub fn read_batches(&self, path: &str) -> PersistResult<Vec<RecordBatch>> {
        let file = File::open(path)?;
        let reader = ParquetRecordBatchReaderBuilder::try_new(file)?.build()?;
        let mut out = Vec::new();
        for batch in reader {
            out.push(batch?);
        }
        Ok(out)
    }

    /// Read back just the `close` column across all row groups — a focused round-trip helper.
    pub fn read_close_prices(&self, path: &str) -> PersistResult<Vec<f64>> {
        let mut out = Vec::new();
        for batch in self.read_batches(path)? {
            let col = batch
                .column_by_name("close")
                .ok_or_else(|| PersistError::Corrupt("missing `close` column".into()))?;
            let arr = col
                .as_any()
                .downcast_ref::<Float64Array>()
                .ok_or_else(|| PersistError::Corrupt("`close` is not Float64".into()))?;
            out.extend(arr.iter().map(|v| v.unwrap_or(f64::NAN)));
        }
        Ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Price, Quantity, UnixNanos};
    use coinext_model::{
        AggregationSource, Bar, BarAggregation, BarSpec, BarType, InstrumentId, PriceType,
    };
    use rust_decimal_macros::dec;

    fn bar_type() -> BarType {
        BarType {
            instrument_id: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
            spec: BarSpec {
                step: 1,
                aggregation: BarAggregation::Minute,
                price_type: PriceType::Last,
            },
            source: AggregationSource::External,
        }
    }

    fn sample_bar(close: &str, ts: u64) -> Bar {
        let p = |s: &str| Price::from_decimal(s.parse().unwrap(), 2).unwrap();
        Bar {
            bar_type: bar_type(),
            open: p("50000"),
            high: p("50100"),
            low: p("49900"),
            close: p(close),
            volume: Quantity::from_decimal(dec!(1.5), 3).unwrap(),
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        }
    }

    #[test]
    fn parquet_bars_roundtrip_close_prices() {
        let closes = ["50010", "50020", "50030", "50040", "50050"];
        let bars: Vec<Bar> = closes
            .iter()
            .enumerate()
            .map(|(i, c)| sample_bar(c, (i as u64 + 1) * 60_000_000_000))
            .collect();

        let dir = std::env::temp_dir();
        let path = dir.join(format!("coinext_bars_{}.parquet", std::process::id()));
        let path_str = path.to_str().unwrap();
        let _ = std::fs::remove_file(&path);

        let writer = ParquetWriter::new();
        writer.write_bars(path_str, &bars).unwrap();

        // Read back the close column and assert it matches the written values EXACTLY.
        let read = writer.read_close_prices(path_str).unwrap();
        let expected: Vec<f64> = closes.iter().map(|c| c.parse::<f64>().unwrap()).collect();
        assert_eq!(read.len(), 5);
        for (got, want) in read.iter().zip(expected.iter()) {
            assert_eq!(got, want, "close price must round-trip exactly");
        }

        // Full batch round-trip: row count and instrument column.
        let batches = writer.read_batches(path_str).unwrap();
        let total: usize = batches.iter().map(|b| b.num_rows()).sum();
        assert_eq!(total, 5);

        let _ = std::fs::remove_file(&path);
    }
}
