//! Deterministic market semantics shared by live ingestion and historical replay.

pub mod domain;
pub mod events;
pub mod reducer;
pub mod periods;

pub mod persistence_contract;

pub mod exact;

pub mod quant_core;
pub mod source_volume;
pub mod source_clock;

mod native_codec;
pub mod native_store;
pub mod range_index;
