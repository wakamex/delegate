//! A Bayesian changepoint detector over an actor's recent results, each judged against what was predicted for it.
//!
//! Hypotheses: no change in the window, or a change before the j-th of the last observations, after which the
//! results shifted by some delta from a grid, worse or better. Each possible change position has prior weight
//! HAZARD. When a change is more than THRESHOLD likely and at least three results follow it, the detector fires and
//! clears its window. It works on success (log-odds shifts of a Bernoulli) or on log cost (shifts of a Gaussian).

use std::collections::VecDeque;

const WINDOW: usize = 40;
const HAZARD: f64 = 1.0 / 2000.0;
const THRESHOLD: f64 = 0.95;
const SUCCESS_DELTAS: [f64; 8] = [-4.0, -3.0, -2.0, -1.5, -1.0, -0.5, 1.0, 2.0];
const COST_DELTAS: [f64; 6] = [-0.7, 0.5, 0.7, 1.1, 1.6, 2.3];
/// Spread of a run's log cost around its mean.
const COST_SD: f64 = 0.6;

#[derive(Clone, Copy)]
pub enum Signal {
    Success,
    Cost,
}

pub struct Changepoint {
    signal: Signal,
    window: VecDeque<(f64, f64)>,
}

fn log_lik(signal: Signal, predicted: f64, observed: f64, delta: f64) -> f64 {
    match signal {
        Signal::Success => {
            // predicted is a log-odds, observed is 1 or 0.
            let x = if observed > 0.5 { predicted + delta } else { -(predicted + delta) };
            -(1.0 + (-x).exp()).ln()
        }
        Signal::Cost => {
            // predicted and observed are log costs.
            let r = observed - predicted - delta;
            -0.5 * (r / COST_SD).powi(2)
        }
    }
}

impl Changepoint {
    pub fn new(signal: Signal) -> Self {
        Changepoint { signal, window: VecDeque::with_capacity(WINDOW) }
    }

    /// Add an observation with its prediction; true when a changepoint is detected.
    pub fn observe(&mut self, predicted: f64, observed: f64) -> bool {
        if self.window.len() == WINDOW {
            self.window.pop_front();
        }
        self.window.push_back((predicted, observed));
        let n = self.window.len();
        if n < 4 {
            return false;
        }
        let deltas: &[f64] = match self.signal {
            Signal::Success => &SUCCESS_DELTAS,
            Signal::Cost => &COST_DELTAS,
        };
        let obs: Vec<(f64, f64)> = self.window.iter().copied().collect();
        let mut prefix = vec![0.0; n + 1];
        for (i, &(p, o)) in obs.iter().enumerate() {
            prefix[i + 1] = prefix[i] + log_lik(self.signal, p, o, 0.0);
        }
        let mut suffix = vec![vec![0.0; n + 1]; deltas.len()];
        for (d, &delta) in deltas.iter().enumerate() {
            for j in (0..n).rev() {
                let (p, o) = obs[j];
                suffix[d][j] = suffix[d][j + 1] + log_lik(self.signal, p, o, delta);
            }
        }
        let none = prefix[n];
        let mut changes = Vec::with_capacity(n);
        for j in 0..=n - 3 {
            let terms: Vec<f64> = (0..deltas.len()).map(|d| suffix[d][j]).collect();
            let max = terms.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
            let mix = max + (terms.iter().map(|t| (t - max).exp()).sum::<f64>() / deltas.len() as f64).ln();
            changes.push(HAZARD.ln() + prefix[j] + mix);
        }
        let max = changes.iter().cloned().fold(none, f64::max);
        let p_none = (none - max).exp();
        let p_change: f64 = changes.iter().map(|c| (c - max).exp()).sum();
        if p_change / (p_change + p_none) > THRESHOLD {
            self.window.clear();
            return true;
        }
        false
    }
}
