//! Pure shared evaluator and simulation; no storage, networking or binary-state dependencies.
#[path = "analysis/exact.rs"] pub mod exact;
#[path = "analysis/quant.rs"] pub mod quant;
#[path = "analysis/structure.rs"] pub mod structure;
#[path = "analysis/evaluator.rs"] pub mod evaluator;

#[path = "analysis/simulation.rs"] pub mod simulation;
#[cfg(test)] #[path = "analysis/quant_tests.rs"] mod quant_tests;

#[path = "analysis/snapshot.rs"] pub mod snapshot;

#[path="analysis/results.rs"]pub mod results;

#[path="analysis/research_input.rs"]pub mod research_input;
