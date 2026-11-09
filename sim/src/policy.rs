//! Selection policies. Each sees only what it would see in use: verdicts, observed costs (the lead's as measured,
//! which may be undercounted), and priced cost guesses; never an executor's true skill, a kind's true review or
//! correction cost, or which kinds are slow (unless a policy is told, as the declared-calibration variant is).

use crate::cpd::{Changepoint, Signal};
use crate::world::{logit, sigmoid, Actor, Task, World};
use rand::rngs::SmallRng;
use rand::Rng;
use rand_distr::{Beta, Distribution, Normal};

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Objective {
    /// delegate today: minimize expected cost per standing result, (cost + review + (1 - q) failure) / q.
    Ratio,
    /// Maximize worth q - cost - review - (1 - q) failure, worth being the lead's cost per standing result.
    NetLead,
    /// The same, worth being the cheapest actor's cost per standing result.
    NetCheapest,
    /// Minimize the expected cost to complete: this attempt, plus, if it fails, the cheapest other actor's cost per
    /// standing result, as the workflow retries a failure with someone else.
    CostToComplete,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Prior {
    Flat,
    KindRate,
    Shared,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Exposure {
    None,
    Hold,
    CapHost,
    /// Each unverified task holds an imagined outcome, drawn when it opened, until its verdict.
    Fantasy,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Drop {
    None,
    Today,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Integrity {
    Ignore,
    /// delegate today: drop the executor in that kind.
    SuspendKind,
    /// Suspend everywhere until a person resumes it, 100 steps later; its recent results are re-checked.
    SuspendAll,
    /// No stop: a learned chance of integrity defects, priced at their damage, enters each choice.
    Head,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Calibration {
    None,
    /// delegate today: hand-built cases, available from the start, in kinds known to be slow.
    Handbuilt,
    /// Replay of the kind's own resolved tasks, gating kinds whose measured verdict lag is long.
    ReplayMeasured,
    /// The same replay, gating kinds declared slow.
    ReplayDeclared,
    /// The same replay in kinds measured slow, recorded as evidence, with no gate.
    EvidenceOnly,
}

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Changes {
    None,
    /// A detected change starts a new epoch for the actor: its estimates restart from their old values with the
    /// weight of a few tasks, on success and on cost separately. Shared estimates keep the old results.
    Epoch,
    /// The earlier version: discount the actor's success evidence to 20 tasks, on success only.
    Discount,
}

#[derive(Clone, Copy)]
pub struct Spec {
    pub name: &'static str,
    pub objective: Objective,
    pub prior: Prior,
    pub exposure: Exposure,
    pub drop: Drop,
    pub drop_vs_lead: bool,
    pub integrity: Integrity,
    pub calibration: Calibration,
    pub changes: Changes,
    pub warmup: bool,
    pub lead_arm: bool,
    pub priced_prior: bool,
}

pub const PROPOSED: Spec = Spec {
    name: "proposed",
    objective: Objective::CostToComplete,
    prior: Prior::Shared,
    exposure: Exposure::None,
    drop: Drop::None,
    drop_vs_lead: false,
    integrity: Integrity::Ignore,
    calibration: Calibration::None,
    changes: Changes::Epoch,
    warmup: false,
    lead_arm: true,
    priced_prior: true,
};

pub const SPECS: [Spec; 20] = [
    Spec {
        name: "today",
        objective: Objective::Ratio,
        prior: Prior::Flat,
        exposure: Exposure::Hold,
        drop: Drop::Today,
        drop_vs_lead: true,
        integrity: Integrity::SuspendKind,
        calibration: Calibration::Handbuilt,
        changes: Changes::None,
        warmup: true,
        lead_arm: false,
        priced_prior: false,
    },
    PROPOSED,
    Spec { name: "worth: lead's cost", objective: Objective::NetLead, ..PROPOSED },
    Spec { name: "worth: cheapest actor", objective: Objective::NetCheapest, ..PROPOSED },
    Spec { name: "ratio objective", objective: Objective::Ratio, ..PROPOSED },
    Spec { name: "no change detection", changes: Changes::None, ..PROPOSED },
    Spec { name: "discount, not epoch", changes: Changes::Discount, ..PROPOSED },
    Spec { name: "integrity: suspend all", integrity: Integrity::SuspendAll, ..PROPOSED },
    Spec { name: "integrity: suspend kind", integrity: Integrity::SuspendKind, ..PROPOSED },
    Spec { name: "integrity: head", integrity: Integrity::Head, ..PROPOSED },
    Spec { name: "+ calibration gate", calibration: Calibration::ReplayMeasured, ..PROPOSED },
    Spec { name: "+ declared calibration gate", calibration: Calibration::ReplayDeclared, ..PROPOSED },
    Spec { name: "+ calibration evidence", calibration: Calibration::EvidenceOnly, ..PROPOSED },
    Spec { name: "+ first-verdict hold", exposure: Exposure::Hold, ..PROPOSED },
    Spec { name: "+ host-wide cap", exposure: Exposure::CapHost, ..PROPOSED },
    Spec { name: "+ fantasy draws", exposure: Exposure::Fantasy, ..PROPOSED },
    Spec { name: "no sharing", prior: Prior::KindRate, ..PROPOSED },
    Spec { name: "+ warm-up", warmup: true, ..PROPOSED },
    Spec { name: "lead only as fallback", lead_arm: false, ..PROPOSED },
    Spec { name: "+ today's drop rule", drop: Drop::Today, ..PROPOSED },
];

const CALIBRATION_CASES: usize = 6;
/// Verdicts an actor needs before change detection watches it.
const WATCH_AFTER: u32 = 20;
/// The weight, in tasks, of an actor's old estimate in a new epoch.
const EPOCH_PRIOR: f64 = 4.0;
/// Steps until a person resumes a suspended actor.
pub const RESUME_AFTER: usize = 100;
/// A kind is slow when its mean measured lag from close to verdict exceeds this many steps.
const SLOW_LAG: f64 = 50.0;

/// What a policy chose for a task.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Choice {
    Exec(usize),
    Lead,
    /// Nothing eligible and the lead at capacity: the task waits a step.
    Wait,
}

pub enum Event {
    Stop { exec: usize },
    Suspend { exec: usize },
    Reset { exec: usize },
}

#[derive(Clone, Default)]
struct Cell {
    s: f64,
    f: f64,
    expect: f64,
    raw_s: f64,
    raw_f: f64,
    verified: u32,
    unverified: u32,
    pseudo_s: f64,
    pseudo_f: f64,
    tasks: u32,
    cost_sum: f64,
    cost_n: f64,
    dropped: bool,
    admitted: bool,
    calibrations: u32,
    calibrate_after: usize,
}

struct Cheapest {
    best: f64,
    second: f64,
    actor: Option<Actor>,
}

#[derive(Clone, Copy, Default)]
struct Agg {
    s: f64,
    n: f64,
    expect: f64,
}

pub struct Policy {
    pub spec: Spec,
    cells: Vec<Vec<Cell>>,
    lead: usize,
    family: Vec<usize>,
    exec_agg: Vec<Agg>,
    fam_agg: Vec<Agg>,
    kind_agg: Vec<(f64, f64)>,
    all_agg: (f64, f64),
    unverified: Vec<u32>,
    verified: Vec<u32>,
    suspended_until: Vec<usize>,
    integrity_defects: Vec<f64>,
    kind_recent: Vec<f64>,
    review_obs: Vec<(f64, f64)>,
    fail_obs: Vec<(f64, f64)>,
    lead_cost_obs: Vec<(f64, f64)>,
    lag_obs: Vec<(f64, f64)>,
    cases: Vec<Vec<f64>>,
    lead_open: u32,
    watch_success: Vec<Changepoint>,
    watch_cost: Vec<Changepoint>,
}

impl Policy {
    pub fn new(spec: Spec, world: &World) -> Self {
        let n = world.execs.len();
        let nk = world.kinds.len();
        let families = world.execs.iter().map(|x| x.family).max().unwrap_or(0) + 1;
        Policy {
            spec,
            cells: vec![vec![Cell::default(); nk]; n + 1],
            lead: n,
            family: world.execs.iter().map(|x| x.family).collect(),
            exec_agg: vec![Agg::default(); n + 1],
            fam_agg: vec![Agg::default(); families],
            kind_agg: vec![(0.0, 0.0); nk],
            all_agg: (0.0, 0.0),
            unverified: vec![0; n + 1],
            verified: vec![0; n + 1],
            suspended_until: vec![0; n],
            integrity_defects: vec![0.0; n],
            kind_recent: vec![0.85; nk],
            review_obs: vec![(0.0, 0.0); nk],
            fail_obs: vec![(0.0, 0.0); nk],
            lead_cost_obs: vec![(0.0, 0.0); nk],
            lag_obs: vec![(0.0, 0.0); nk],
            cases: vec![Vec::new(); nk],
            lead_open: 0,
            watch_success: (0..n).map(|_| Changepoint::new(Signal::Success)).collect(),
            watch_cost: (0..n).map(|_| Changepoint::new(Signal::Cost)).collect(),
        }
    }

    fn row(&self, a: Actor) -> usize {
        match a {
            Actor::Exec(e) => e,
            Actor::Lead => self.lead,
        }
    }

    fn global_rate(&self) -> f64 {
        (self.all_agg.0 + 0.85 * 2.0) / (self.all_agg.1 + 2.0)
    }

    fn kind_rate(&self, k: usize) -> f64 {
        let (s, n) = self.kind_agg[k];
        (s + 2.0 * self.global_rate()) / (n + 2.0)
    }

    fn advantage(&self, e: usize, k: usize) -> f64 {
        let c = &self.cells[e][k];
        let own = self.exec_agg[e];
        let (s, n, expect, m) = if own.n - c.s - c.f > 0.5 {
            (own.s - c.s, own.n - c.s - c.f, own.expect - c.expect, 6.0)
        } else {
            let f = self.fam_agg[self.family[e]];
            (f.s - own.s, f.n - own.n, f.expect - own.expect, 10.0)
        };
        if n < 0.5 {
            return 0.0;
        }
        let rbar = (expect / n).clamp(0.02, 0.98);
        let shrunk = ((s + m * rbar) / (n + m)).clamp(0.02, 0.98);
        logit(shrunk) - logit(rbar)
    }

    fn posterior(&self, row: usize, k: usize) -> (f64, f64) {
        let c = &self.cells[row][k];
        let (s, f) = (c.s.max(0.0) + c.pseudo_s, c.f.max(0.0) + c.pseudo_f);
        let r = self.kind_rate(k);
        match self.spec.prior {
            Prior::Flat => (1.0 + s, 1.0 + f),
            Prior::KindRate => (4.0 * r + s, 4.0 * (1.0 - r) + f),
            Prior::Shared => {
                let r = if row == self.lead { r } else { sigmoid(logit(r) + self.advantage(row, k)) };
                (4.0 * r + s, 4.0 * (1.0 - r) + f)
            }
        }
    }

    fn mean_q(&self, row: usize, k: usize) -> f64 {
        let (a, b) = self.posterior(row, k);
        a / (a + b)
    }

    /// Mean cost of an attempt by executor E in kind K, and how many of its runs are costed.
    fn exec_cost(&self, world: &World, e: usize, k: usize) -> (f64, f64) {
        let row = &self.cells[e];
        let (sum, n) = row.iter().fold((0.0, 0.0), |(a, b), c| (a + c.cost_sum, b + c.cost_n));
        let prior = if n > 0.0 {
            sum / n
        } else if self.spec.priced_prior {
            world.execs[e].prior_cost
        } else {
            let (s, m) = self.cells[..self.lead].iter().fold((0.0, 0.0), |(a, b), r| (a + r[k].cost_sum, b + r[k].cost_n));
            if m > 0.0 {
                s / m
            } else {
                3.0
            }
        };
        let c = &row[k];
        ((c.cost_sum + prior) / (c.cost_n + 1.0), n)
    }

    fn lead_cost(&self, world: &World, k: usize) -> f64 {
        let (s, n) = self.lead_cost_obs[k];
        (s + world.kinds[k].lead_prior) / (n + 1.0)
    }

    fn review(&self, k: usize) -> f64 {
        let (s, n) = self.review_obs[k];
        (s + 1.25) / (n + 1.0)
    }

    fn failure(&self, k: usize) -> f64 {
        let (s, n) = self.fail_obs[k];
        let (gs, gn) = self.fail_obs.iter().fold((0.0, 0.0), |(a, b), &(x, y)| (a + x, b + y));
        let prior = if gn > 0.0 { gs / gn } else { 2.5 };
        (s + prior) / (n + 1.0)
    }

    /// Expected extra cost per attempt from integrity defects, when they are priced.
    fn integrity_cost(&self, e: usize, k: usize) -> f64 {
        if self.spec.integrity != Integrity::Head {
            return 0.0;
        }
        let p = (self.integrity_defects[e] + 0.1) / (self.verified[e] as f64 + 10.0);
        p * 2.0 * self.failure(k)
    }

    /// Cost of one attempt by A at success chance Q and attempt cost COST, and its cost per standing result.
    fn attempt_cost(&self, a: Actor, k: usize, q: f64, cost: f64) -> (f64, f64) {
        let f = self.failure(k);
        let c = match a {
            Actor::Exec(e) => cost + self.review(k) + (1.0 - q) * f + self.integrity_cost(e, k),
            Actor::Lead => cost + (1.0 - q) * f,
        };
        (c, c / q.max(1e-3))
    }

    fn mean_cost(&self, world: &World, a: Actor, k: usize) -> f64 {
        match a {
            Actor::Exec(e) => self.exec_cost(world, e, k).0,
            Actor::Lead => self.lead_cost(world, k),
        }
    }

    /// The cheapest and second-cheapest expected cost per standing result in kind K at posterior means, and the
    /// actor with the cheapest: what a failed task costs to redo with someone else.
    fn cheapest(&self, world: &World, k: usize, t: usize) -> Cheapest {
        let mut out = Cheapest { best: f64::INFINITY, second: f64::INFINITY, actor: None };
        let mut consider = |a: Actor, unit: f64| {
            if unit < out.best {
                out.second = out.best;
                out.best = unit;
                out.actor = Some(a);
            } else if unit < out.second {
                out.second = unit;
            }
        };
        for e in 0..self.lead {
            if !world.alive(e, t) || t < self.suspended_until[e] || self.cells[e][k].dropped {
                continue;
            }
            let a = Actor::Exec(e);
            consider(a, self.attempt_cost(a, k, self.mean_q(e, k), self.mean_cost(world, a, k)).1);
        }
        consider(Actor::Lead, self.attempt_cost(Actor::Lead, k, self.mean_q(self.lead, k), self.lead_cost(world, k)).1);
        out
    }

    /// The score of giving the task to A at drawn success chance Q and cost COST: higher is better.
    fn score(&self, world: &World, a: Actor, k: usize, q: f64, cost: f64, cheapest: &Cheapest) -> f64 {
        let (c, unit) = self.attempt_cost(a, k, q, cost);
        match self.spec.objective {
            Objective::Ratio => -unit,
            Objective::NetLead => {
                let worth = self.attempt_cost(Actor::Lead, k, self.mean_q(self.lead, k), self.lead_cost(world, k)).1;
                worth * q - c
            }
            Objective::NetCheapest => cheapest.best * q - c,
            Objective::CostToComplete => {
                let other = if cheapest.actor == Some(a) { cheapest.second } else { cheapest.best };
                -(c + (1.0 - q) * other)
            }
        }
    }

    fn slow(&self, world: &World, k: usize) -> bool {
        match self.spec.calibration {
            Calibration::Handbuilt | Calibration::ReplayDeclared => world.kinds[k].slow,
            Calibration::ReplayMeasured | Calibration::EvidenceOnly => {
                let (s, n) = self.lag_obs[k];
                n >= 3.0 && s / n > SLOW_LAG
            }
            Calibration::None => false,
        }
    }

    fn gated(&self, world: &World, k: usize) -> bool {
        self.spec.calibration != Calibration::EvidenceOnly && self.slow(world, k)
    }

    fn eligible(&self, world: &World, e: usize, task: &Task) -> bool {
        let (k, t) = (task.kind, task.t);
        let c = &self.cells[e][k];
        if !world.alive(e, t) || c.dropped || t < self.suspended_until[e] || task.excluded == Some(e) {
            return false;
        }
        if self.gated(world, k) && !c.admitted {
            return false;
        }
        match self.spec.exposure {
            Exposure::None | Exposure::Fantasy => true,
            Exposure::Hold => !(c.verified == 0 && c.unverified > 0),
            Exposure::CapHost => (self.unverified[e] as f64) < 1.0 + 0.5 * self.verified[e] as f64,
        }
    }

    fn draw_q(&self, row: usize, k: usize, rng: &mut SmallRng) -> f64 {
        let (a, b) = self.posterior(row, k);
        Beta::new(a.max(1e-3), b.max(1e-3)).unwrap().sample(rng).clamp(1e-6, 1.0 - 1e-6)
    }

    pub fn choose(&self, world: &World, task: &Task, rng: &mut SmallRng) -> Choice {
        let k = task.kind;
        let eligible: Vec<usize> = (0..self.lead).filter(|&e| self.eligible(world, e, task)).collect();
        let lead_free = self.lead_open < world.lead_capacity;
        if self.spec.warmup {
            if let Some(&e) = eligible
                .iter()
                .filter(|&&e| self.cells[e][k].tasks == 0)
                .min_by(|&&a, &&b| self.exec_cost(world, a, k).0.total_cmp(&self.exec_cost(world, b, k).0))
            {
                return Choice::Exec(e);
            }
        }
        let normal = Normal::new(0.0, 1.0).unwrap();
        let cheapest = self.cheapest(world, k, task.t);
        let mut best: Option<(f64, Choice)> = None;
        for &e in &eligible {
            let q = self.draw_q(e, k, rng);
            let (m, n) = self.exec_cost(world, e, k);
            let sd = 0.5 / (1.0 + n).sqrt();
            let cost = m * (sd * normal.sample(rng) - sd * sd / 2.0).exp();
            let s = self.score(world, Actor::Exec(e), k, q, cost, &cheapest);
            if best.is_none_or(|(b, _)| s > b) {
                best = Some((s, Choice::Exec(e)));
            }
        }
        if self.spec.lead_arm && lead_free {
            let q = self.draw_q(self.lead, k, rng);
            let s = self.score(world, Actor::Lead, k, q, self.lead_cost(world, k), &cheapest);
            if best.is_none_or(|(b, _)| s > b) {
                best = Some((s, Choice::Lead));
            }
        }
        match best {
            Some((_, c)) => c,
            None if lead_free => Choice::Lead,
            None => Choice::Wait,
        }
    }

    /// A task opened for A; returns the imagined outcome it holds until its verdict, under fantasy draws.
    pub fn opened(&mut self, a: Actor, k: usize, rng: &mut SmallRng) -> Option<bool> {
        let row = self.row(a);
        let pseudo = (self.spec.exposure == Exposure::Fantasy).then(|| rng.gen::<f64>() < self.mean_q(row, k));
        let c = &mut self.cells[row][k];
        c.unverified += 1;
        c.tasks += 1;
        if let Some(y) = pseudo {
            if y {
                c.pseudo_s += 1.0;
            } else {
                c.pseudo_f += 1.0;
            }
        }
        self.unverified[row] += 1;
        if a == Actor::Lead {
            self.lead_open += 1;
        }
        pseudo
    }

    /// The attempt closed: its cost and review are known now; the lead's capacity is released.
    pub fn closed(&mut self, a: Actor, k: usize, cost: f64, review: f64) -> Option<Event> {
        match a {
            Actor::Lead => {
                self.lead_open -= 1;
                self.lead_cost_obs[k].0 += cost;
                self.lead_cost_obs[k].1 += 1.0;
                None
            }
            Actor::Exec(e) => {
                let (sum, n) = self.cells[e].iter().fold((0.0, 0.0), |(a, b), c| (a + c.cost_sum, b + c.cost_n));
                let predicted = if n > 0.0 { (sum / n).ln() } else { cost.ln() };
                let c = &mut self.cells[e][k];
                c.cost_sum += cost;
                c.cost_n += 1.0;
                self.review_obs[k].0 += review;
                self.review_obs[k].1 += 1.0;
                if self.spec.changes == Changes::Epoch && n >= WATCH_AFTER as f64 && self.watch_cost[e].observe(predicted, cost.ln()) {
                    // A new cost epoch: recent runs only.
                    for c in &mut self.cells[e] {
                        if c.cost_n > 0.0 {
                            c.cost_sum = cost * c.cost_n.min(1.0);
                            c.cost_n = c.cost_n.min(1.0);
                        }
                    }
                    return Some(Event::Reset { exec: e });
                }
                None
            }
        }
    }

    fn add(&mut self, row: usize, k: usize, ds: f64, df: f64, dexpect: f64, shared: bool) {
        let c = &mut self.cells[row][k];
        c.s += ds;
        c.f += df;
        c.expect += dexpect;
        let a = &mut self.exec_agg[row];
        a.s += ds;
        a.n += ds + df;
        a.expect += dexpect;
        if shared && row != self.lead {
            let f = &mut self.fam_agg[self.family[row]];
            f.s += ds;
            f.n += ds + df;
            f.expect += dexpect;
            self.kind_agg[k].0 += ds;
            self.kind_agg[k].1 += ds + df;
            self.all_agg.0 += ds;
            self.all_agg.1 += ds + df;
        }
    }

    /// The final verdict on an attempt, as reported. LAG is the steps from close to verdict.
    #[allow(clippy::too_many_arguments)]
    pub fn verdict(
        &mut self,
        world: &World,
        a: Actor,
        task: &Task,
        stood: bool,
        failure: f64,
        lag: usize,
        pseudo: Option<bool>,
        integrity_defect: bool,
        t: usize,
    ) -> Vec<Event> {
        let k = task.kind;
        let row = self.row(a);
        let mut events = Vec::new();
        {
            let c = &mut self.cells[row][k];
            match pseudo {
                Some(true) => c.pseudo_s -= 1.0,
                Some(false) => c.pseudo_f -= 1.0,
                None => {}
            }
        }
        let (pa, pb) = self.posterior(row, k);
        let trend = logit(self.kind_recent[k]) - logit(self.kind_rate(k));
        let predicted = logit(pa / (pa + pb)) + trend;
        let rate = self.kind_rate(k);
        let y = if stood { 1.0 } else { 0.0 };
        self.add(row, k, y, 1.0 - y, rate, true);
        self.kind_recent[k] = 0.9 * self.kind_recent[k] + 0.1 * y;
        {
            let c = &mut self.cells[row][k];
            c.raw_s += y;
            c.raw_f += 1.0 - y;
            c.verified += 1;
            c.unverified -= 1;
        }
        self.unverified[row] -= 1;
        self.verified[row] += 1;
        self.lag_obs[k].0 += lag as f64;
        self.lag_obs[k].1 += 1.0;
        if stood {
            self.cases[k].push(task.hard);
        } else {
            self.fail_obs[k].0 += failure;
            self.fail_obs[k].1 += 1.0;
        }
        let Actor::Exec(e) = a else {
            return events;
        };
        if integrity_defect {
            if let Some(ev) = self.integrity(e, k, t) {
                events.push(ev);
            }
        }
        if self.verified[e] >= WATCH_AFTER && self.spec.changes != Changes::None && self.watch_success[e].observe(predicted, y) {
            self.new_epoch(e);
            events.push(Event::Reset { exec: e });
        }
        if self.cells[e][k].dropped || !world.kinds[k].reviewed {
            return events;
        }
        let c = &self.cells[e][k];
        let mut drop = self.spec.drop == Drop::Today && betai(1.0 + c.raw_s, 1.0 + c.raw_f, 0.5) > 0.9;
        if !drop && self.spec.drop_vs_lead {
            drop = self.costlier_than_lead(world, e, k);
        }
        if drop {
            self.cells[e][k].dropped = true;
            events.push(Event::Stop { exec: e });
        }
        events
    }

    /// A detected change: the actor's own success estimates restart from their current values with the weight of
    /// a few tasks. Shared estimates (kind, family, base rate) keep its old results.
    fn new_epoch(&mut self, e: usize) {
        for k in 0..self.cells[e].len() {
            let c = &self.cells[e][k];
            let n = c.s + c.f;
            let (target, keep) = match self.spec.changes {
                Changes::Epoch => (EPOCH_PRIOR, 0.0),
                _ => (20.0, 1.0),
            };
            if n <= target {
                continue;
            }
            let r = self.mean_q(e, k);
            let (ns, nf) = if keep > 0.0 {
                (c.s * target / n, c.f * target / n)
            } else {
                (target * r, target * (1.0 - r))
            };
            let (ds, df) = (ns - c.s, nf - c.f);
            let dx = c.expect * (target / n - 1.0);
            self.add(e, k, ds, df, dx, false);
        }
    }

    fn costlier_than_lead(&self, world: &World, e: usize, k: usize) -> bool {
        let c = &self.cells[e][k];
        let l = &self.cells[self.lead][k];
        if c.verified < 5 || l.verified < 5 {
            return false;
        }
        let mut rng = <SmallRng as rand::SeedableRng>::seed_from_u64((e * 1000 + k) as u64 + c.verified as u64);
        let (ae, be) = self.posterior(e, k);
        let (al, bl) = self.posterior(self.lead, k);
        let cost = self.exec_cost(world, e, k).0;
        let lead_cost = self.lead_cost(world, k);
        let worse = (0..200)
            .filter(|_| {
                let qe = Beta::new(ae, be).unwrap().sample(&mut rng);
                let ql = Beta::new(al, bl).unwrap().sample(&mut rng);
                self.attempt_cost(Actor::Exec(e), k, qe, cost).1 > self.attempt_cost(Actor::Lead, k, ql, lead_cost).1
            })
            .count();
        worse as f64 / 200.0 > 0.95
    }

    fn integrity(&mut self, e: usize, k: usize, t: usize) -> Option<Event> {
        match self.spec.integrity {
            Integrity::Ignore => None,
            Integrity::Head => {
                self.integrity_defects[e] += 1.0;
                None
            }
            Integrity::SuspendKind => {
                if self.cells[e][k].dropped {
                    return None;
                }
                self.cells[e][k].dropped = true;
                Some(Event::Stop { exec: e })
            }
            Integrity::SuspendAll => {
                if t < self.suspended_until[e] {
                    return None;
                }
                self.suspended_until[e] = t + RESUME_AFTER;
                Some(Event::Suspend { exec: e })
            }
        }
    }

    /// A result reported as standing is found out as a disguised failure: an overturn and an integrity defect.
    pub fn found_out(&mut self, e: usize, k: usize, damage: f64, t: usize) -> Option<Event> {
        self.add(e, k, -1.0, 1.0, 0.0, true);
        {
            let c = &mut self.cells[e][k];
            c.raw_s = (c.raw_s - 1.0).max(0.0);
            c.raw_f += 1.0;
        }
        self.fail_obs[k].0 += damage;
        self.fail_obs[k].1 += 1.0;
        self.integrity(e, k, t)
    }

    /// Run calibration where the policy uses it; returns the cost of the runs.
    pub fn calibrate(&mut self, world: &World, t: usize, rng: &mut SmallRng) -> f64 {
        if self.spec.calibration == Calibration::None {
            return 0.0;
        }
        let mut spent = 0.0;
        for k in 0..world.kinds.len() {
            if !self.slow(world, k) {
                continue;
            }
            let handbuilt = self.spec.calibration == Calibration::Handbuilt;
            if !handbuilt && self.cases[k].len() < 3 {
                continue;
            }
            let retries = if self.spec.calibration == Calibration::EvidenceOnly { 1 } else { 2 };
            let cheapest = self.cheapest(world, k, t);
            let mut ranked: Vec<(f64, usize)> = (0..self.lead)
                .filter(|&e| {
                    let c = &self.cells[e][k];
                    world.alive(e, t)
                        && t >= self.suspended_until[e]
                        && !c.dropped
                        && !c.admitted
                        && c.calibrations < retries
                        && t >= c.calibrate_after
                })
                .map(|e| {
                    let a = Actor::Exec(e);
                    (self.score(world, a, k, self.mean_q(e, k), self.mean_cost(world, a, k), &cheapest), e)
                })
                .collect();
            ranked.sort_by(|a, b| b.0.total_cmp(&a.0));
            for &(_, e) in ranked.iter().take(3) {
                let hards: Vec<f64> = if handbuilt {
                    vec![0.0; CALIBRATION_CASES]
                } else {
                    self.cases[k].iter().rev().take(CALIBRATION_CASES).copied().collect()
                };
                let n = hards.len();
                let mut passed = 0;
                let mut cost = 0.0;
                for &h in &hards {
                    let q = world.q(e, k, t, h);
                    let q = if handbuilt { sigmoid(logit(q) + 0.3) } else { q };
                    if rng.gen::<f64>() < q {
                        passed += 1;
                    }
                    cost += world.cost(e, t);
                }
                spent += cost + n as f64 * world.kinds[k].review;
                let failed = n - passed;
                let rate = self.kind_rate(k);
                // Evidence about this executor only: calibration cases are not the kind's ordinary work.
                self.add(e, k, passed as f64, failed as f64, n as f64 * rate, false);
                let c = &mut self.cells[e][k];
                c.cost_sum += cost;
                c.cost_n += n as f64;
                c.verified += n as u32;
                c.calibrations += 1;
                c.calibrate_after = t + 300;
                if failed <= n / CALIBRATION_CASES {
                    c.admitted = true;
                }
                self.verified[e] += n as u32;
            }
        }
        spent
    }
}

/// Regularized incomplete beta I_x(a, b), by Lentz's continued fraction.
pub fn betai(a: f64, b: f64, x: f64) -> f64 {
    if x <= 0.0 {
        return 0.0;
    }
    if x >= 1.0 {
        return 1.0;
    }
    let ln_front = ln_gamma(a + b) - ln_gamma(a) - ln_gamma(b) + a * x.ln() + b * (1.0 - x).ln();
    if x < (a + 1.0) / (a + b + 2.0) {
        ln_front.exp() * betacf(a, b, x) / a
    } else {
        1.0 - ln_front.exp() * betacf(b, a, 1.0 - x) / b
    }
}

fn betacf(a: f64, b: f64, x: f64) -> f64 {
    let (qab, qap, qam) = (a + b, a + 1.0, a - 1.0);
    let tiny = 1e-300;
    let clamp = |v: f64| if v.abs() < tiny { tiny } else { v };
    let mut c = 1.0;
    let mut d = 1.0 / clamp(1.0 - qab * x / qap);
    let mut h = d;
    for m in 1..300 {
        let m = m as f64;
        let m2 = 2.0 * m;
        let aa = m * (b - m) * x / ((qam + m2) * (a + m2));
        d = 1.0 / clamp(1.0 + aa * d);
        c = clamp(1.0 + aa / c);
        h *= d * c;
        let aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2));
        d = 1.0 / clamp(1.0 + aa * d);
        c = clamp(1.0 + aa / c);
        let del = d * c;
        h *= del;
        if (del - 1.0).abs() < 1e-12 {
            break;
        }
    }
    h
}

fn ln_gamma(x: f64) -> f64 {
    const C: [f64; 9] = [
        0.999_999_999_999_809_9,
        676.520_368_121_885_1,
        -1_259.139_216_722_402_8,
        771.323_428_777_653_1,
        -176.615_029_162_140_6,
        12.507_343_278_686_905,
        -0.138_571_095_265_720_12,
        9.984_369_578_019_572e-6,
        1.505_632_735_149_311_6e-7,
    ];
    if x < 0.5 {
        return std::f64::consts::PI.ln() - (std::f64::consts::PI * x).sin().ln() - ln_gamma(1.0 - x);
    }
    let x = x - 1.0;
    let t = x + 7.5;
    let a = C.iter().enumerate().skip(1).fold(C[0], |acc, (i, c)| acc + c / (x + i as f64));
    0.5 * (2.0 * std::f64::consts::PI).ln() + (x + 0.5) * t.ln() - t + a.ln()
}
