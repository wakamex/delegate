//! delegate-sim: runs delegate's selection policies against generated worlds and reports regret against an oracle
//! that knows every actor's true success rate and cost. Every work item must be completed; a failed attempt is
//! retried by another actor, and regret is the expected cost to complete each attempt above the best choice.
//!
//! Usage: delegate-sim [sweep] [WORLDS] [STEPS]. It uses at most 8 CPUs (MAX_THREADS).

mod cpd;
mod policy;
mod world;

use policy::{Choice, Event, Policy, SPECS};
use rand::rngs::SmallRng;
use rand::SeedableRng;
use std::cmp::Reverse;
use std::collections::hash_map::DefaultHasher;
use std::collections::BinaryHeap;
use std::hash::{Hash, Hasher};
use world::{generate, integrity_damage, items, Actor, Pop, Scenario, Task, World, DEFAULT_POP, WAIT_COST};

/// Most CPUs a run may use: worlds run as blocking tasks, at most this many at once.
const MAX_THREADS: usize = 8;
/// A work item is abandoned after this many failed attempts.
const MAX_ATTEMPTS: u32 = 6;

const SOURCE: &str = concat!(
    include_str!("main.rs"),
    include_str!("policy.rs"),
    include_str!("world.rs"),
    include_str!("cpd.rs")
);

#[derive(Clone, Copy, Default)]
struct Outcome {
    items: f64,
    attempts: f64,
    regret: f64,
    wait_steps: f64,
    bad: f64,
    lead: f64,
    broken: f64,
    flawed: f64,
    blind: f64,
    false_stops: f64,
    true_stops: f64,
    suspensions: f64,
    false_resets: f64,
    true_resets: f64,
    calibration: f64,
    recheck: f64,
}

struct Attempt {
    task: Task,
    actor: Actor,
    stood: bool,
    disguised: bool,
    defect: bool,
    cost: f64,
    pseudo: Option<bool>,
}

const CLOSE: u8 = 0;
const VERDICT: u8 = 1;
const FOUND_OUT: u8 = 2;

fn run(world: &World, originals: &[Task], spec: policy::Spec, seed: u64) -> Outcome {
    let mut rng = SmallRng::seed_from_u64(seed ^ 0x5eed);
    let mut cal_rng = SmallRng::seed_from_u64(seed ^ 0xca11);
    let mut fantasy_rng = SmallRng::seed_from_u64(seed ^ 0xfa47);
    let mut policy = Policy::new(spec, world);
    let mut out = Outcome { items: originals.len() as f64, ..Outcome::default() };
    let mut queue: BinaryHeap<Reverse<(usize, usize)>> = BinaryHeap::new();
    let mut tasks: Vec<Task> = originals.to_vec();
    for (i, task) in tasks.iter().enumerate() {
        queue.push(Reverse((task.t, i)));
    }
    let mut events: BinaryHeap<Reverse<(usize, u8, usize)>> = BinaryHeap::new();
    let mut attempts: Vec<Attempt> = Vec::new();
    let mut first_verdict = vec![false; world.execs.len()];
    let mut assigned = vec![0usize; world.execs.len()];
    let mut history: Vec<Vec<usize>> = vec![Vec::new(); world.execs.len()];
    let score_event = |out: &mut Outcome, ev: Event, at: usize| match ev {
        Event::Stop { exec } | Event::Suspend { exec } => {
            if matches!(ev, Event::Suspend { .. }) {
                out.suspensions += 1.0;
            }
            let x = &world.execs[exec];
            let worst = (0..world.kinds.len()).map(|k| world.q(exec, k, at, 0.0)).fold(1.0, f64::min);
            if x.fab > 0.0 || worst < 0.5 {
                out.true_stops += 1.0;
            } else if x.slip == 0.0 {
                out.false_stops += 1.0;
            }
        }
        Event::Reset { exec } => {
            let x = &world.execs[exec];
            let near = |b: Option<usize>| b.is_some_and(|b| at >= b && at < b + 300);
            if near(x.break_at) || near(x.recover_at) {
                out.true_resets += 1.0;
            } else {
                out.false_resets += 1.0;
            }
        }
    };
    for t in 0..world.steps {
        while let Some(&Reverse((at, ev, j))) = events.peek() {
            if at > t {
                break;
            }
            events.pop();
            let a = &attempts[j];
            let k = a.task.kind;
            let kind = &world.kinds[k];
            match ev {
                CLOSE => {
                    let review = if matches!(a.actor, Actor::Exec(_)) { kind.review * (0.3 * a.task.zr - 0.045).exp() } else { 0.0 };
                    if let Some(e) = policy.closed(a.actor, k, a.cost, review) {
                        score_event(&mut out, e, at);
                    }
                }
                VERDICT => {
                    let reported = a.stood || a.disguised;
                    let elapsed = a.task.run + a.task.lag;
                    let failure = kind.failure_cost(elapsed) + if a.defect { integrity_damage(kind) } else { 0.0 };
                    if let Actor::Exec(x) = a.actor {
                        if !first_verdict[x] {
                            first_verdict[x] = true;
                            if world.execs[x].bad_newcomer {
                                out.blind += assigned[x] as f64;
                            }
                        }
                    }
                    let (actor, pseudo, defect, task) = (a.actor, a.pseudo, a.defect, a.task.clone());
                    for e in policy.verdict(world, actor, &task, reported, failure, task.lag, pseudo, defect, at) {
                        if let Event::Suspend { exec } = e {
                            let recent = history[exec].iter().filter(|&&s| s + 300 > at).count();
                            out.recheck += 0.3 * kind.review * recent as f64;
                        }
                        score_event(&mut out, e, at);
                    }
                    if a.disguised {
                        events.push(Reverse((at + a.task.reveal, FOUND_OUT, j)));
                    }
                    if !reported && task.attempt + 1 < MAX_ATTEMPTS {
                        let failed = if let Actor::Exec(x) = actor { Some(x) } else { None };
                        tasks.push(task.retry(world, at, failed));
                        queue.push(Reverse((at, tasks.len() - 1)));
                    }
                }
                _ => {
                    let Actor::Exec(x) = a.actor else { continue };
                    let task = a.task.clone();
                    if let Some(e) = policy.found_out(x, k, integrity_damage(kind), at) {
                        if let Event::Suspend { exec } = e {
                            let recent = history[exec].iter().filter(|&&s| s + 300 > at).count();
                            out.recheck += 0.3 * kind.review * recent as f64;
                        }
                        score_event(&mut out, e, at);
                    }
                    if task.attempt + 1 < MAX_ATTEMPTS {
                        tasks.push(task.retry(world, at, Some(x)));
                        queue.push(Reverse((at, tasks.len() - 1)));
                    }
                }
            }
        }
        out.calibration += policy.calibrate(world, t, &mut cal_rng);
        let mut ready = Vec::new();
        while let Some(&Reverse((at, i))) = queue.peek() {
            if at > t {
                break;
            }
            queue.pop();
            ready.push(i);
        }
        for i in ready {
            let mut task = tasks[i].clone();
            task.t = t;
            let k = task.kind;
            let choice = policy.choose(world, &task, &mut rng);
            let actor = match choice {
                Choice::Wait => {
                    out.wait_steps += 1.0;
                    out.regret += WAIT_COST * world.kinds[k].correction;
                    queue.push(Reverse((t + 1, i)));
                    continue;
                }
                Choice::Exec(e) => Actor::Exec(e),
                Choice::Lead => Actor::Lead,
            };
            let costs = world.costs_to_complete(k, t, task.hard, task.excluded);
            let best = costs.iter().map(|c| c.1).fold(f64::INFINITY, f64::min);
            let chosen = costs.iter().find(|c| c.0 == actor).map_or(best, |c| c.1);
            out.regret += chosen - best;
            out.attempts += 1.0;
            let pseudo = policy.opened(actor, k, &mut fantasy_rng);
            let (q, cost) = match actor {
                Actor::Exec(e) => (world.q(e, k, t, task.hard), world.cost(e, t) * (0.6 * task.z - 0.18).exp()),
                Actor::Lead => (world.lead_q(k, task.hard), world.kinds[k].lead_cost * world.lead_measured),
            };
            let mut stood = task.u < q;
            let mut defect = false;
            let mut disguised = false;
            if let Actor::Exec(e) = actor {
                let x = &world.execs[e];
                assigned[e] += 1;
                history[e].push(t);
                if world.q(e, k, t, 0.0) < 0.5 {
                    out.bad += 1.0;
                }
                if world.broken(e, t) {
                    out.broken += 1.0;
                }
                if x.fab > 0.0 || x.slip > 0.0 {
                    out.flawed += 1.0;
                }
                if x.slip > 0.0 && task.slip_u < x.slip {
                    stood = false;
                    defect = true;
                }
                disguised = !stood && !defect && task.fab_u < x.fab;
            } else {
                out.lead += 1.0;
            }
            let j = attempts.len();
            events.push(Reverse((t + task.run, CLOSE, j)));
            events.push(Reverse((t + task.run + task.lag, VERDICT, j)));
            attempts.push(Attempt { task, actor, stood, disguised, defect, cost, pseudo });
        }
    }
    out.regret += out.calibration + out.recheck;
    out
}

fn mean_ci(xs: &[f64]) -> (f64, f64) {
    let n = xs.len() as f64;
    let m = xs.iter().sum::<f64>() / n;
    let v = xs.iter().map(|x| (x - m).powi(2)).sum::<f64>() / (n - 1.0).max(1.0);
    (m, 1.96 * (v / n).sqrt())
}

fn quantile(xs: &[f64], q: f64) -> f64 {
    let mut v = xs.to_vec();
    v.sort_by(|a, b| a.total_cmp(b));
    v[((v.len() as f64 - 1.0) * q).round() as usize]
}

async fn simulate(worlds: u64, steps: usize, pop: Pop) -> Vec<Vec<Vec<Outcome>>> {
    let mut jobs = tokio::task::JoinSet::new();
    for (si, scenario) in Scenario::ALL.into_iter().enumerate() {
        for seed in 0..worlds {
            jobs.spawn_blocking(move || {
                let world = generate(scenario, seed * 1_000 + si as u64, steps, pop);
                let originals = items(&world);
                let results: Vec<Outcome> = SPECS.iter().map(|&spec| run(&world, &originals, spec, seed)).collect();
                (si, results)
            });
        }
    }
    let mut by: Vec<Vec<Vec<Outcome>>> = vec![vec![Vec::new(); SPECS.len()]; Scenario::ALL.len()];
    while let Some(res) = jobs.join_next().await {
        let (si, results) = res.expect("simulation task panicked");
        for (pi, o) in results.into_iter().enumerate() {
            by[si][pi].push(o);
        }
    }
    by
}

fn proposed_index() -> usize {
    SPECS.iter().position(|s| s.name == "proposed").unwrap()
}

fn per_item(os: &[Outcome]) -> Vec<f64> {
    os.iter().map(|o| o.regret / o.items).collect()
}

fn source_hash() -> String {
    let mut h = DefaultHasher::new();
    SOURCE.hash(&mut h);
    format!("{:016x}", h.finish())
}

fn main() {
    tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .max_blocking_threads(MAX_THREADS)
        .build()
        .expect("tokio runtime")
        .block_on(start());
}

async fn start() {
    let args: Vec<String> = std::env::args().collect();
    let sweep = args.get(1).is_some_and(|a| a == "sweep");
    let rest = if sweep { &args[2..] } else { &args[1..] };
    let worlds: u64 = rest.first().and_then(|s| s.parse().ok()).unwrap_or(200);
    let steps: usize = rest.get(1).and_then(|s| s.parse().ok()).unwrap_or(1000);
    println!("delegate-sim source {}; {} worlds per cell, {} steps of 2 work items.", source_hash(), worlds, steps);
    let proposed = proposed_index();
    if sweep {
        let pops = [
            ("default", DEFAULT_POP),
            ("4 execs", Pop { n: 4, ..DEFAULT_POP }),
            ("40 execs", Pop { n: 40, ..DEFAULT_POP }),
            ("tight cost", Pop { cost_noise: 0.2, cost_skill: 1.0, ..DEFAULT_POP }),
            ("kind gaps", Pop { idio: 1.3, ..DEFAULT_POP }),
            ("price low", Pop { price_bias: 1.0, ..DEFAULT_POP }),
            ("fix x4", Pop { correction_scale: 4.0, ..DEFAULT_POP }),
            ("base 62%", Pop { mu: 0.5, ..DEFAULT_POP }),
        ];
        let mut results = Vec::new();
        for (_, pop) in pops {
            results.push(simulate(worlds, steps, pop).await);
        }
        println!("Regret per work item minus the proposed policy's, paired by world; * marks a 95% interval excluding zero.\n");
        for (si, scenario) in Scenario::ALL.iter().enumerate() {
            println!("== {}", scenario.name());
            print!("{:28}", "policy");
            for (name, _) in pops {
                print!(" {:>11}", name);
            }
            println!();
            for (pi, spec) in SPECS.iter().enumerate() {
                print!("{:28}", spec.name);
                for r in &results {
                    let base = per_item(&r[si][proposed]);
                    let d: Vec<f64> = per_item(&r[si][pi]).iter().zip(&base).map(|(a, b)| a - b).collect();
                    let (m, c) = mean_ci(&d);
                    let mark = if pi != proposed && (m - c > 0.0 || m + c < 0.0) { "*" } else { " " };
                    print!(" {:>10.3}{}", m, mark);
                }
                println!();
            }
            println!();
        }
        return;
    }
    let by = simulate(worlds, steps, DEFAULT_POP).await;
    println!(
        "Regret per work item (expected cost to complete above the best choice, plus waiting, calibration and \
         re-check costs): mean with 95% interval, the 95th percentile world, and the paired difference from the \
         proposed policy. Counts are per world.\n"
    );
    for (si, scenario) in Scenario::ALL.iter().enumerate() {
        println!("== {}", scenario.name());
        println!(
            "{:28} {:>15} {:>7} {:>17} {:>6} {:>6} {:>6} {:>6} {:>6} {:>6} {:>6} {:>6} {:>6} {:>6} {:>6}",
            "policy", "regret/item", "p95", "vs proposed", "tries", "bad%", "lead%", "broken", "flawed", "blind", "fstop", "susp", "freset", "calib$", "wait"
        );
        let base = per_item(&by[si][proposed]);
        for (pi, spec) in SPECS.iter().enumerate() {
            let os = &by[si][pi];
            let r = per_item(os);
            let d: Vec<f64> = r.iter().zip(&base).map(|(a, b)| a - b).collect();
            let (rm, rc) = mean_ci(&r);
            let (dm, dc) = mean_ci(&d);
            let avg = |f: fn(&Outcome) -> f64| os.iter().map(f).sum::<f64>() / os.len() as f64;
            println!(
                "{:28} {:>7.3} ±{:<6.3} {:>7.3} {:>+8.3} ±{:<6.3} {:>6.3} {:>6.2} {:>6.1} {:>6.1} {:>6.1} {:>6.1} {:>6.2} {:>6.2} {:>6.2} {:>6.1} {:>6.0}",
                spec.name,
                rm,
                rc,
                quantile(&r, 0.95),
                dm,
                dc,
                avg(|o| o.attempts / o.items),
                100.0 * avg(|o| o.bad / o.attempts),
                100.0 * avg(|o| o.lead / o.attempts),
                avg(|o| o.broken),
                avg(|o| o.flawed),
                avg(|o| o.blind),
                avg(|o| o.false_stops),
                avg(|o| o.suspensions),
                avg(|o| o.false_resets),
                avg(|o| o.calibration),
                avg(|o| o.wait_steps),
            );
        }
        println!();
    }
}
