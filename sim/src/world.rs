//! Simulated worlds: projects and their kinds of task, executors with true skills and costs, the lead, and the
//! stream of work items. Every item must be completed: a failed attempt is retried by another actor.

use rand::rngs::SmallRng;
use rand::{Rng, SeedableRng};
use rand_distr::{Distribution, Normal};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scenario {
    /// One project of five kinds; executors with spread skills and costs, better ones dearer.
    Baseline,
    /// Baseline plus cheap executors that usually fail.
    CheapBad,
    /// The best-value executor's skill collapses halfway.
    VeteranBreaks,
    /// The best-value executor's skill collapses halfway and recovers at three quarters.
    BreakRecovers,
    /// The best-value executor gets moderately worse halfway, staying above one half.
    ModerateBreak,
    /// The best-value executor's cost rises fivefold halfway.
    CostBreak,
    /// Runs of hard tasks in each kind, so failures cluster for everyone.
    ClusteredHard,
    /// Families release new versions and retire old ones; a quarter of releases are much worse than their family.
    Churn,
    /// Two kinds give their final verdict 150 to 300 steps after the close; failures there cost more the longer
    /// they go unverified, skill there is only loosely related to skill elsewhere, and cheap bad newcomers arrive.
    SlowCompound,
    /// Three projects of five kinds at the same total load as baseline.
    SteadyProjects,
    /// Executors' skill varies strongly by kind.
    CapabilityGaps,
    /// Two cheap executors that usually fail disguise most failures as successes, found out 30 to 150 steps later.
    Fabricators,
    /// The best-value executor commits an integrity defect, caught in review, on 2% of its tasks.
    Slip,
    /// The lead is cheap enough to be the best choice for many tasks.
    CheapLead,
    /// The lead's cost is measured at 70% of its true value, as unattributed turns would cause.
    LeadUndercount,
}

impl Scenario {
    pub const ALL: [Scenario; 15] = [
        Scenario::Baseline,
        Scenario::CheapBad,
        Scenario::VeteranBreaks,
        Scenario::BreakRecovers,
        Scenario::ModerateBreak,
        Scenario::CostBreak,
        Scenario::ClusteredHard,
        Scenario::Churn,
        Scenario::SlowCompound,
        Scenario::SteadyProjects,
        Scenario::CapabilityGaps,
        Scenario::Fabricators,
        Scenario::Slip,
        Scenario::CheapLead,
        Scenario::LeadUndercount,
    ];

    pub fn name(self) -> &'static str {
        match self {
            Scenario::Baseline => "baseline",
            Scenario::CheapBad => "cheap-bad",
            Scenario::VeteranBreaks => "veteran-breaks",
            Scenario::BreakRecovers => "break-recovers",
            Scenario::ModerateBreak => "moderate-break",
            Scenario::CostBreak => "cost-break",
            Scenario::ClusteredHard => "clustered-hard",
            Scenario::Churn => "churn",
            Scenario::SlowCompound => "slow-compound",
            Scenario::SteadyProjects => "steady-projects",
            Scenario::CapabilityGaps => "capability-gaps",
            Scenario::Fabricators => "fabricators",
            Scenario::Slip => "slip",
            Scenario::CheapLead => "cheap-lead",
            Scenario::LeadUndercount => "lead-undercount",
        }
    }
}

/// The shape of a population and its tasks.
#[derive(Clone, Copy)]
pub struct Pop {
    pub n: usize,
    /// Executors per family.
    pub per_family: f64,
    /// Spread of family skills on the logit scale.
    pub spread: f64,
    /// Log cost gains this times skill.
    pub cost_skill: f64,
    /// Spread of log cost not explained by skill; small values make a tight cost-quality frontier.
    pub cost_noise: f64,
    /// Spread of an executor's skill across kinds.
    pub idio: f64,
    /// A newcomer's believed cost is its true cost times exp(-price_bias).
    pub price_bias: f64,
    /// Multiplies every correction cost.
    pub correction_scale: f64,
    /// Base rate on the logit scale (2.2 puts a typical task near 90%).
    pub mu: f64,
}

pub const DEFAULT_POP: Pop = Pop {
    n: 15,
    per_family: 2.5,
    spread: 0.8,
    cost_skill: 0.5,
    cost_noise: 1.0,
    idio: 0.4,
    price_bias: 0.0,
    correction_scale: 1.0,
    mu: 2.2,
};

/// Waiting has a cost: each step a task waits for an actor costs this share of a typical correction.
pub const WAIT_COST: f64 = 0.05;

pub struct Kind {
    pub difficulty: f64,
    pub review: f64,
    pub correction: f64,
    /// Reviewed kinds are where drop rules apply.
    pub reviewed: bool,
    pub lead_cost: f64,
    /// What the lead's cost looks like before the lead has done the kind: its price times a usage guess.
    pub lead_prior: f64,
    /// Steps from a task's start to its close.
    pub run: (usize, usize),
    /// Extra steps from the close to the final verdict.
    pub lag: (usize, usize),
    /// Verdicts arrive late; the oracle knows, a policy must measure or be told.
    pub slow: bool,
    /// A failure costs correction x (1 + rho x steps from start to verdict / 100).
    pub rho: f64,
}

pub struct Exec {
    pub family: usize,
    pub skill: f64,
    pub idio: Vec<f64>,
    pub cost: f64,
    pub prior_cost: f64,
    pub arrive: usize,
    pub retire: usize,
    pub break_at: Option<usize>,
    pub recover_at: Option<usize>,
    pub break_drop: f64,
    pub cost_break: f64,
    /// Share of its failures reported as successes, found out later.
    pub fab: f64,
    /// Chance per task of an integrity defect caught in review.
    pub slip: f64,
    pub bad_newcomer: bool,
}

pub struct World {
    pub seed: u64,
    pub mu: f64,
    pub kinds: Vec<Kind>,
    pub execs: Vec<Exec>,
    pub lead_skill: f64,
    pub lead_capacity: u32,
    /// The lead's measured cost is its true cost times this.
    pub lead_measured: f64,
    pub steps: usize,
    pub items_per_step: usize,
    pub hard: Option<(f64, f64, f64)>,
}

pub fn sigmoid(x: f64) -> f64 {
    1.0 / (1.0 + (-x).exp())
}

pub fn logit(p: f64) -> f64 {
    let p = p.clamp(1e-6, 1.0 - 1e-6);
    (p / (1.0 - p)).ln()
}

/// Extra damage of a disguised failure found late, or of an integrity defect: rework and re-check.
pub fn integrity_damage(kind: &Kind) -> f64 {
    2.0 * kind.correction
}

/// An actor: an executor, or the lead.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum Actor {
    Exec(usize),
    Lead,
}

impl Kind {
    pub fn failure_cost(&self, elapsed: usize) -> f64 {
        self.correction * (1.0 + self.rho * elapsed as f64 / 100.0)
    }

    pub fn expected_failure_cost(&self) -> f64 {
        let elapsed = (self.run.0 + self.run.1 + self.lag.0 + self.lag.1) / 2;
        self.failure_cost(elapsed)
    }
}

impl World {
    pub fn alive(&self, e: usize, t: usize) -> bool {
        let x = &self.execs[e];
        x.arrive <= t && t < x.retire
    }

    pub fn broken(&self, e: usize, t: usize) -> bool {
        let x = &self.execs[e];
        x.break_at.is_some_and(|b| t >= b) && !x.recover_at.is_some_and(|r| t >= r)
    }

    pub fn q(&self, e: usize, k: usize, t: usize, hard: f64) -> f64 {
        let x = &self.execs[e];
        let skill = x.skill - if self.broken(e, t) { x.break_drop } else { 0.0 };
        sigmoid(self.mu + skill + x.idio[k] - self.kinds[k].difficulty - hard)
    }

    pub fn cost(&self, e: usize, t: usize) -> f64 {
        let x = &self.execs[e];
        x.cost * if self.broken(e, t) { x.cost_break } else { 1.0 }
    }

    pub fn lead_q(&self, k: usize, hard: f64) -> f64 {
        sigmoid(self.mu + self.lead_skill - self.kinds[k].difficulty - hard)
    }

    /// One attempt by A: its chance of standing, its cost, and the expected extra cost of its failures.
    fn attempt(&self, a: Actor, k: usize, t: usize, hard: f64) -> (f64, f64) {
        let kind = &self.kinds[k];
        match a {
            Actor::Exec(e) => {
                let q = self.q(e, k, t, hard);
                let x = &self.execs[e];
                let damage = (1.0 - q) * (kind.expected_failure_cost() + x.fab * integrity_damage(kind))
                    + x.slip * integrity_damage(kind);
                (q, self.cost(e, t) + kind.review + damage)
            }
            Actor::Lead => {
                let q = self.lead_q(k, hard);
                (q, kind.lead_cost + (1.0 - q) * kind.expected_failure_cost())
            }
        }
    }

    fn actors(&self, t: usize) -> Vec<Actor> {
        let mut v: Vec<Actor> = (0..self.execs.len()).filter(|&e| self.alive(e, t)).map(Actor::Exec).collect();
        v.push(Actor::Lead);
        v
    }

    /// Expected cost to complete a task of kind K for each actor it may go to, by giving this attempt to that actor,
    /// failures being retried by the best other actor until one stands: the workflow's rule that a failed task goes
    /// to someone else.
    pub fn costs_to_complete(&self, k: usize, t: usize, hard: f64, excluded: Option<usize>) -> Vec<(Actor, f64)> {
        let attempts: Vec<(Actor, f64, f64)> = self
            .actors(t)
            .into_iter()
            .filter(|&a| Some(a) != excluded.map(Actor::Exec))
            .map(|a| {
                let (q, c) = self.attempt(a, k, t, hard);
                (a, q, c)
            })
            .collect();
        // The best and second-best cost per standing result, so each actor's retry is the best other one.
        let (mut best, mut second, mut best_actor) = (f64::INFINITY, f64::INFINITY, None);
        for &(a, q, c) in &attempts {
            let unit = c / q.max(1e-3);
            if unit < best {
                second = best;
                best = unit;
                best_actor = Some(a);
            } else if unit < second {
                second = unit;
            }
        }
        attempts
            .into_iter()
            .map(|(a, q, c)| {
                let retry = if Some(a) == best_actor { second } else { best };
                (a, c + (1.0 - q) * retry)
            })
            .collect()
    }
}

pub fn generate(scenario: Scenario, seed: u64, steps: usize, pop: Pop) -> World {
    let mut rng = SmallRng::seed_from_u64(seed);
    let rng = &mut rng;
    let norm = |sd: f64| Normal::new(0.0, sd.max(1e-9)).unwrap();
    let projects = if scenario == Scenario::SteadyProjects { 3 } else { 1 };
    let mut kinds = Vec::new();
    for _ in 0..projects {
        let project_difficulty = norm(0.4).sample(rng);
        for k in 0..5 {
            let slow = scenario == Scenario::SlowCompound && k < 2;
            let lead_cost: f64 = if scenario == Scenario::CheapLead { rng.gen_range(1.5..4.0) } else { rng.gen_range(6.0..15.0) };
            kinds.push(Kind {
                difficulty: project_difficulty + norm(0.4).sample(rng),
                review: rng.gen_range(0.5..2.0),
                correction: rng.gen_range(1.0..4.0) * pop.correction_scale,
                reviewed: k % 2 == 0 || slow,
                lead_cost,
                lead_prior: lead_cost * norm(0.4).sample(rng).exp(),
                run: (1, 5),
                lag: if slow { (150, 300) } else { (0, 0) },
                slow,
                rho: if slow { 2.0 } else { 0.0 },
            });
        }
    }
    let n_kinds = kinds.len();
    let idio_sd = if scenario == Scenario::CapabilityGaps { 1.3 } else { pop.idio };
    let families = ((pop.n as f64 / pop.per_family).ceil() as usize).max(1);
    let fam_skill: Vec<f64> = (0..families).map(|_| norm(pop.spread).sample(rng)).collect();
    let make = |family: usize, offset: f64, arrive: usize, retire: usize, rng: &mut SmallRng| {
        let skill = fam_skill[family] + offset;
        let cost = (norm(pop.cost_noise).sample(rng) + 2f64.ln() + pop.cost_skill * skill).exp();
        let idio = (0..n_kinds)
            .map(|k| norm(if scenario == Scenario::SlowCompound && k < 2 { 1.3 } else { idio_sd }).sample(rng))
            .collect();
        Exec {
            family,
            skill,
            idio,
            cost,
            prior_cost: cost * (norm(0.4).sample(rng) - pop.price_bias).exp(),
            arrive,
            retire,
            break_at: None,
            recover_at: None,
            break_drop: 0.0,
            cost_break: 1.0,
            fab: 0.0,
            slip: 0.0,
            bad_newcomer: false,
        }
    };
    let mut execs = Vec::new();
    if scenario == Scenario::Churn {
        let gap = steps / 4;
        for f in 0..families {
            let mut start = 0;
            let mut first = true;
            let offset_start = rng.gen_range(0..gap);
            while start < steps {
                let next = if first { offset_start + gap / 2 } else { start + gap };
                let bad = !first && rng.gen_bool(0.25);
                let offset = if bad { -2.5 } else { norm(0.3).sample(rng) };
                let mut x = make(f, offset, start, next + gap / 4, rng);
                x.bad_newcomer = bad;
                execs.push(x);
                start = next;
                first = false;
            }
        }
    } else {
        for i in 0..pop.n {
            let offset = norm(0.3).sample(rng);
            execs.push(make(i % families, offset, 0, usize::MAX, rng));
        }
    }
    let top_family = (0..families).max_by(|&a, &b| fam_skill[a].total_cmp(&fam_skill[b])).unwrap();
    let mut add_bad = |count: usize, arrive: usize, skill: f64, cost: f64, family: usize, rng: &mut SmallRng| {
        for _ in 0..count {
            let mut x = make(family, 0.0, arrive, usize::MAX, rng);
            x.skill = skill;
            x.cost = cost;
            x.prior_cost = cost * (-pop.price_bias).exp();
            x.bad_newcomer = arrive > 0;
            execs.push(x);
        }
    };
    match scenario {
        Scenario::CheapBad => {
            let fam = rng.gen_range(0..families);
            add_bad(3, 0, -3.5, 0.1, fam, rng);
        }
        Scenario::SlowCompound => add_bad(3, steps / 4, -3.0, 0.15, top_family, rng),
        Scenario::Fabricators => {
            let fam = rng.gen_range(0..families);
            add_bad(2, 0, -2.0, 0.5, fam, rng);
            let n = execs.len();
            execs[n - 1].fab = 0.9;
            execs[n - 2].fab = 0.9;
        }
        _ => {}
    }
    let mut world = World {
        seed,
        mu: pop.mu,
        kinds,
        execs,
        lead_skill: 1.5,
        lead_capacity: 3,
        lead_measured: if scenario == Scenario::LeadUndercount { 0.7 } else { 1.0 },
        steps,
        items_per_step: 2,
        hard: (scenario == Scenario::ClusteredHard).then_some((0.03, 0.8, 2.5)),
    };
    let value = |w: &World, e: usize| {
        -(0..n_kinds)
            .map(|k| w.costs_to_complete(k, 0, 0.0, None).into_iter().find(|c| c.0 == Actor::Exec(e)).map_or(0.0, |c| c.1))
            .sum::<f64>()
    };
    if matches!(
        scenario,
        Scenario::VeteranBreaks | Scenario::BreakRecovers | Scenario::ModerateBreak | Scenario::CostBreak | Scenario::Slip
    ) {
        let best = (0..world.execs.len()).max_by(|&a, &b| value(&world, a).total_cmp(&value(&world, b))).unwrap();
        let x = &mut world.execs[best];
        match scenario {
            Scenario::Slip => x.slip = 0.02,
            _ => {
                x.break_at = Some(steps / 2);
                match scenario {
                    Scenario::VeteranBreaks => x.break_drop = 4.0,
                    Scenario::BreakRecovers => {
                        x.break_drop = 4.0;
                        x.recover_at = Some(3 * steps / 4);
                    }
                    Scenario::ModerateBreak => x.break_drop = 1.5,
                    _ => x.cost_break = 5.0,
                }
            }
        }
    }
    world
}

/// One attempt at a work item, with the common random numbers that decide it whichever policy routes it.
#[derive(Clone)]
pub struct Task {
    pub item: usize,
    pub attempt: u32,
    pub t: usize,
    pub kind: usize,
    pub hard: f64,
    /// An actor that already failed this item, which may not take it again.
    pub excluded: Option<usize>,
    pub run: usize,
    pub lag: usize,
    pub reveal: usize,
    pub u: f64,
    pub fab_u: f64,
    pub slip_u: f64,
    pub z: f64,
    pub zr: f64,
}

impl Task {
    fn draw(world: &World, item: usize, attempt: u32, t: usize, kind: usize, hard: f64, excluded: Option<usize>) -> Task {
        let mut rng = SmallRng::seed_from_u64(world.seed ^ ((item as u64) << 8) ^ attempt as u64 ^ 0x7a5c);
        let normal = Normal::new(0.0, 1.0).unwrap();
        let k = &world.kinds[kind];
        Task {
            item,
            attempt,
            t,
            kind,
            hard,
            excluded,
            run: rng.gen_range(k.run.0..=k.run.1),
            lag: if k.lag.1 > 0 { rng.gen_range(k.lag.0..=k.lag.1) } else { 0 },
            reveal: rng.gen_range(30..=150),
            u: rng.gen(),
            fab_u: rng.gen(),
            slip_u: rng.gen(),
            z: normal.sample(&mut rng),
            zr: normal.sample(&mut rng),
        }
    }

    /// The next attempt at this item, at T, never by the actor that just failed it.
    pub fn retry(&self, world: &World, t: usize, failed: Option<usize>) -> Task {
        Task::draw(world, self.item, self.attempt + 1, t, self.kind, self.hard, failed.or(self.excluded))
    }
}

/// The original work items.
pub fn items(world: &World) -> Vec<Task> {
    let mut rng = SmallRng::seed_from_u64(world.seed ^ 0x17e5);
    let n = world.kinds.len();
    let mut in_hard = vec![false; n];
    let mut out = Vec::with_capacity(world.steps * world.items_per_step);
    for t in 0..world.steps {
        for _ in 0..world.items_per_step {
            let k = rng.gen_range(0..n);
            let mut hard = 0.0;
            if let Some((enter, stay, add)) = world.hard {
                in_hard[k] = if in_hard[k] { rng.gen_bool(stay) } else { rng.gen_bool(enter) };
                if in_hard[k] {
                    hard = add;
                }
            }
            let item = out.len();
            out.push(Task::draw(world, item, 0, t, k, hard, None));
        }
    }
    out
}
