# Agent delegation workflow

Version 10, of 2026-11-08, which chooses from one model of every actor on every project's tasks, as designed in [proposals/shared-skill.md](proposals/shared-skill.md): the lead competes in every draw, nothing but a person stops an actor, and warm-up, the first-verdict hold, drop rules, kind states and integrity suspension are gone. Version 9 replaced admission trials and the class ladder with per-class records; version 8 was adopted on 2026-10-30 by the lead agents of three projects: [concord](https://github.com/wakamex/concord), a matching-decompilation project; a game-simulator port; and a reverse-engineering project. It draws on concord's executor scorecard, the simulator port's per-kind trust-against-cost view (which survives here as the scorecard), a trading-research project's route bandit, and the reverse-engineering project's executor calibration.

A lead agent in a project can hand well-defined tasks to executor agents, models run through [aop](https://github.com/wakamex/agent-orchestration-process), or do them itself. This document is how a lead decides whether to delegate a task, which executor gets it, how the outcome and cost are recorded, and how quota is kept from being overspent or wasted.

## Goals

- A task is delegated only when an executor is expected to finish it well for less total cost than the lead doing it, counting the lead's time on the brief and on checking the result.
- Each task goes to the actor, the lead included, with the lowest expected cost per result that stands, drawn from what is known about it, so actors whose record is thin keep being tried in proportion to the chance they are best, and a ranking built on a few tasks does not harden into habit. concord sent 34 capability tasks in a row to Codex Luna, and 65 tasks over 36 hours to two Codex models, while free executors sat idle.
- No subscription is spent far ahead of its reset, and allowance about to expire unused gets used.
- What one project learns about an actor informs every other project on the host, while each project's kinds keep their own difficulty and each actor its own strengths per kind.

## What this covers

Tasks whose outcome the lead or an authoritative check judges: a port, a tool, a fix, a search, a reconstruction. It does not cover second opinions, sealed reviews, consultations with another session or a chat bot's replies; those choose their model as their caller likes and are not gated.

## Terms

- Actor: who does a task, an executor (agent, model and effort) or the lead. Every actor, the lead included, is declared once for the host in its roster (`host.toml`), with its family, harness, pool, capabilities and a price index, the typical cost of one run. A new model or effort is a new actor, which starts from what its family, model and lower efforts have shown.
- Task: one brief and every run that serves it, the first run and its resumes in one session. A hard task finished through several resumes is one task, since the session's context is what makes each resume cheap. A task that fails and is redone by another actor is a retry: a new task naming the first, never given to the actor that failed it.
- Kind: what the task is. Each project names its own few (concord: capability, diagnosis, search, rule; the simulator port: port, tool, confirmation; the reverse-engineering project: for example operation reconstruction, error-variant location, post-state difference).
- Class: how hard the task is within its kind, from a short ordered list the project keeps, for example bounded change, first reading of a new area, cross-cutting redesign.
- How a kind's results are checked, which decides how much exploring it can afford:
  - gated: a mechanical check compares the result with a reference the actor did not produce, and catches failures before they land. Examples: concord's whole-build gate (`concord scores --base`, which fails when any function gets worse or matched data is lost), an emulator run of the original bytes, a decompile index, a fresh-build corpus. Tests the actor wrote, or that only check internal consistency, do not make a kind gated: in the simulator port, one tool passed all 30 unit tests while its coverage figure counted inherited method-table slots (89% covered against 3.6% after the fix), and another passed every emulator vector on a pilot that had sampled 19 constant getters out of 20. Tools that compute a metric, choose a sample or report a figure are reviewed;
  - reviewed: the lead must re-derive the result to know whether it holds, as for the simulator port's ports, which push to `main` and share one live client;
  - deferred: only an authoritative check that can come many tasks later shows whether the result holds, as for the reverse-engineering project's reconstructions, judged against a differential of the original system's state. A claim there, from the project's own earlier work, passed review and stood for weeks until a later comparison showed it wrong, so review misses such errors whoever wrote them.
- Outcome of a task:
  - held: the result stood as delivered, or passed the kind's authoritative check;
  - steered: its own result stood after resumes that only continued past a stop the actor declared, either to finish work it said it left incomplete or with a decision it stopped to ask for. When the lead supplies a decision, the missing decision is a brief error against the lead (in the simulator port, one executor stopped on a design question it named, a value it needed to read lacking a clock in the game state; the lead decided and the executor carried out the refactor);
  - corrected: the result did not stand, including when a resume had to supply the cause of a defect in delivered work (one port left eight later retries with wrong path flags, and the resume pointed at the flag transitions that showed it). A correction records two things:
    - caught by: the gate, the lead's review, or the deferred check;
    - defect: integrity (a fitted rule, a test asserting a known-wrong value, an out-of-scope change, a misreported number, a missed critical reject), scope (a part the brief asked for was wrong or missing and the actor did not say so), failed gate (rejected by the gate with no further diagnosis), or unfinished (the actor stopped, saying so, and its work never stood: the lead gave the task up or had another executor do it. In the simulator port one executor stopped four times on a port without porting it, and another landed it in one run; recorded as steered, that read as a success);
  - a lead edit corrects a result when it was needed for the result to work as delivered: it changes behaviour, a test's assertion, a mechanic's value or tag, or fixes a build, an import, a type error or a memory blowup. A rebase, a lint fix or a wording change does not;
  - aborted: the run failed for a reason outside the actor, such as a full disk, a lost host, a live client that exited or a provider at capacity. It counts as neither success nor failure;
  - running: launched and not yet closed. A running task reserves its shared resources and its expected need against its executor's pool;
  - awaiting verdict: delivered in a deferred kind and closed, without its final verdict yet. It reserves nothing.
- Outcome measure: how a kind judges how much a successful task was worth, one per kind, set in the project's config:
  - pass or fail, the default: held and steered tasks count as one success, corrected ones as none;
  - yield: a count of what the task produced, allowed only when a reference check counts the unit, not the actor or the lead, and units are comparable across tasks, such as concord's newly exact functions or cases matched on the reverse-engineering project's authoritative check. A corrected task yields zero. It does not fit the simulator port's ports, where the unit depends on how finely mechanics are tagged and some of the most valuable tasks yield nothing countable: one stopped correctly on missing state, one measured that 11,446 of 12,511 player orders depend on one unresolved check, and one overturned a wrong premise;
  - lead score: the lead's one overall judgment of the result, from 0 to 100, with a one-line reason, counted as that fraction of a success. It judges the result alone: integrity stays a separate field, and the lead gives the score before seeing the task's cost. Each kind keeps a few reference tasks from its ledger as fixed points, in the project's notes, for the simulator port's ports one at about 30 (a correct but narrow result: a boundary confirmed, no code changed), one at about 60 (a bounded port) and two at about 90 (a general mechanism changed or a premise overturned), so scores do not drift with the most recent task. There are no component rubrics: the simulator port's earlier grades summed results, reliability, efficiency and report, and that sum is what put every Claude model between 82 and 86. A kind may use the lead score alongside a gate, where the gate must pass and the score says how good the passing result was.

  In a deferred kind, yield and the lead score are given with the final verdict, not at close; until then the task stays out of the statistics, and an overturn sets them to zero along with the outcome, since a score given at close is a review verdict, the kind that let the reverse-engineering project's wrong claim stand for weeks.
- Cost: what a task cost in total:
  - actor cost: for an executor, the API-equivalent cost of every aop run of the task from aop's records, which includes the cache rereads that waiting on long builds causes, and unknown when aop priced any run at nothing, as it does for a model it has no price for; for the lead, its turns on the task;
  - lead cost: the cost of the lead turns that served the task, each response priced by [clanker-analytics](https://github.com/wakamex/clanker-analytics), split at the task's start into the brief and the rest (review, and correction when the result did not stand). Turns before the start can name only the task id, since its unit and run do not exist yet. Each turn is tagged with the task that triggered it: a turn started by a task's completion message charges that task, the turn that opened the task charges it for the brief (a turn that opens several tasks is split among them at their openings, so no response is charged twice), verification done in a later session charges the task it verifies, and turns answering the user charge no task, except while the lead does a task itself: then every turn not naming another task is that task's. This stays correct with several tasks running at once. A failed task's record carries the lead's cost of finding the failure;
  - redo cost: when a result is overturned later, the cost of redoing what was built on it, recorded on the overturn and charged to the task named as its root cause.
- Cost per standing result of an actor on a task: its expected actor cost, plus the lead's expected brief, review and, weighted by the chance of failure, correction cost, plus the expected cost of an integrity defect, all divided by the chance its result stands times the value of a result that stands (1 in pass-or-fail kinds, the expected lead score as a fraction, or the expected yield). Dividing by the chance of standing counts the further attempts a failure leads to, so a cheap actor that succeeds half the time is genuinely cheap only once each failure carries the lead's cost of catching it. When the actor is the lead, its own turns are its actor cost and no separate lead cost is added. Each expected figure comes from one model over every host project's tasks (below).
- Harness capabilities: what an executor's harness can enforce, listed per executor: sealed inputs with hash-pinned snapshots, enforced no-web (Antigravity cannot enforce aop's `--no-web`), no write authority over the project's cumulative model, access to a VM or tool a kind needs.

## One model of every actor

Every expectation the choice uses comes from one model fitted to every task of every project on the host. It is a mixed model over one table of effects: the actor's family, model, effort, harness and version, the actor itself and its epoch; how the task is checked, its project, kind and class; and the actor's own strengths by check, project, kind and class. Separate parts of the model, each over the same table, predict whether a result stands, its value, an integrity defect, the actor cost, the lead's brief and post-run cost, and the minutes a task holds its resources. Each effect is shrunk toward its parent by a spread the model learns, so a new effort of a known model starts from that model, an actor's record in one kind informs the others, and a kind's difficulty is shared by every actor that works in it. A newcomer's cost starts from its roster price. The design and the simulations behind each choice are in [proposals/shared-skill.md](proposals/shared-skill.md).

- No kind is on or off, and no class is reserved for the lead. The lead is an actor in every draw: it wins where its own cost per standing result is lowest and stops winning when its pool binds.
- No executor is dropped and none is suspended automatically. An unreliable executor, one that costs more than the lead, or one with integrity defects costs more per standing result and loses the draw by itself. A person can still suspend an actor, everywhere or in one kind, with a reason, and resume it.
- An integrity correction, or an overturn that names an integrity defect, enters the model's integrity part, whose expected cost of rework and re-check raises that actor's cost per standing result. An actor that slips rarely keeps working; one that fabricates often prices itself out. In the simulator, suspending on integrity defects cost far more when a good executor slipped than it saved against fabricators.
- Epochs: after each close, verdict or overturn, delegate checks the actor's recent results against what was predicted for each when it was chosen, and its costs against their prediction. When a change is more than 95% likely, it appends an epoch event, and the model re-learns the actor from its old estimate. A broken, fixed or repriced actor is caught the same way.
- A deferred kind needs nothing special: its results count only once their final verdict is in, and the model learns what its failures cost. Calibration gates cost more than they saved in every variant simulated.
- The lead's own work is recorded like any actor's, with real outcomes, so an overturn of lead work has a task to land on and the lead's record is part of the model.

## The workflow

### 1. Write a self-contained brief

The brief states the goal, the checkable rules the result must meet, how to verify it, a stop condition, and a cap on tokens that ends the run. For a large change it states the design decision and a narrow stop; an executor left to choose the design is where steering resumes and their cost go.

### 2. Decide whether to delegate this task

The draw decides, the lead being one of its actors. When writing this task's brief would take most of the work, the lead assigns the task to itself with that reason. A task whose hard part is a question the lead can check the answer to is not such a task: it can be delegated as an investigation (below). A task can also be split: in the simulator port, the lead made a redesign's decisions and an executor carried out the rest.

### Investigations

An investigation is a task whose deliverable is an answer to one bounded question with evidence, rather than a change. Some of the three projects' best delegated work already had this shape: concord's diagnosis kind (one bounded question with a stop limit, where a measured negative result is a complete answer) went 12 of 12 for Devin at about $2 each; in the simulator port, executors located the code a port needed and named the state it read, and measured how many orders depend on one check; and in the reverse-engineering project, questions such as which function produces a given error and on what condition, or where a given record is stored, are the first shape in which that project could delegate at all.

- The brief states the question, the evidence required, a stop limit, and what would falsify the answer, such as the competing function or the branch that would contradict it.
- The evidence is primary artifacts: addresses, exact bytes, the decompile or disassembly files with their hashes, commands and their output. The executor's narrative does not count as evidence. The report states whether the falsifier was checked, and lists what it did not establish: indirect calls on paths it did not read, and any assumption it relied on. In the simulator port, one answer named a real function with real values, which the lead reproduced and acted on, but the function belonged to a different kind of object; a list of what was not established is what would have caught it.
- The answer holds when the lead re-checks those artifacts and the falsifier, with that time charged to lead cost; a measured negative answer holds too. Its outcome measure is pass or fail or the lead score.
- An investigation is usually a reviewed kind, even where the work built on its answer is deferred, since the lead can re-check the answer at once. Its required capabilities can include hash-pinned snapshots of the relevant decompiles as sealed inputs, which answers most such questions while keeping the executor off a single shared VM.

Open-ended exploration, with no question defined yet, stays with the lead, or runs as parallel probes on the same prompt whose findings the lead compares; that pattern is outside this workflow.

### 3. Find the eligible actors

An actor is eligible when all of these hold:

1. No person has suspended it, everywhere or in the kind; its harness has every capability the kind lists; and it did not fail the task this one retries.
2. Its pool has room: the run can start without spending the pool faster than its owner allows. The check is given the run's upper cost, its predicted actor cost at the 90th percentile, plus the lead's expected cost when the lead draws on the same pool, as a Claude lead does with Claude executors, plus the expected cost of the runs already open on the pool across the host, which its reading cannot show yet. In w3sim three OpenCode trials admitted seconds apart, each checked against an idle pool, took that pool from 0.3% to 75% of its week. A pool the check cannot price a run on, such as Cursor, Devin or OpenCode Go, takes one run at a time, and only while each of its windows is below what its pace allows by now. A pool's `runs` cap in the host roster limits the tasks open on it across every project, for a limit its reading cannot show, such as Devin's free-model rate limit, under which four overlapping runs brought lockouts of hours. The lead takes its roster `runs` tasks at once, one by default. By any moment each window allows the owner's pace limit times its elapsed share plus one day's share, at most all of it; the allowances of a provider's accounts add up, and the account the run lands on must still hold it.
3. The shared resources the task needs are free: not reserved by a running task. A project lists its own, such as concord's single checkout or the simulator port's four checkouts and one live client; a machine several projects share, such as one host's memory, is declared once in the host roster and counted across them.

### 4. Choose

The choice is one of three answers:

- delegate to an executor, or lead, drawn by the rule below or assigned: the user or lead names an actor that is not suspended, with the reason and its cause (difficulty, quota, capability or user), which skips the draw and the pool check but still waits for a busy resource. The cause is recorded, so the model can tell tasks assigned for their difficulty from the rest;
- wait, naming the resource or pool that is busy, when every eligible actor is blocked by a reservation or its pool. A wait opens nothing and can be asked as often as needed;
- error, a wait in which every pool check failed to answer, so a check to fix is told apart from quota to wait for.

`select` and `open` are one step under the ledger's lock for every answer but wait, so two sessions, or the lead and a run, cannot both claim a free checkout or VM.

The draw: take one joint draw from the model's posterior for every eligible actor, compute each one's cost per standing result for this task, and give the task to the lowest. An actor whose record is thin has a wide posterior and is chosen as often as it might be best; one that has shown itself worse is chosen rarely and never by rule. The open records each eligible actor's predicted cost per standing result, its spread, and the share of 257 draws in which it was lowest, so the model and the policy can be scored later on the tasks already routed. The same rule applies in every kind: there is no warm-up, no hold on a newcomer until its first verdict, and no deterministic choice in deferred kinds, each of which cost more than it saved in the simulator.

### 5. Use quota that would otherwise expire

Subscription quota that expires unused costs almost nothing to spend. A window used at an average pace of P times the steady pace ends at 100 P percent, so 100 (1 - P) of its points expire; a run whose points fit in that is priced at 5% of its cost, and one that only partly fits at the share that does not, the window leaving the least binding. The draw multiplies each actor's cost, and the lead's part of an executor's cost, by its pool's price, so a pool behind its pace wins close draws while the chance of standing still counts, and a cheap model on spare quota still beats a dear one at equal quality. The price applies at selection only: the record keeps the run's full API-equivalent cost, so the same run does not look cheap or dear depending on when it ran.

### 6. Record the task

The ledger is one append-only file of events per project, written under a lock and folded by task id into one record per task. Every count, cost and scorecard is recomputed from it; concord's summary table, last updated 2026-10-26 while its log grew to 151 rows, shows what a second copy turns into.

- open, from `select` (the task is then running): task id, kind, class, actor and what it is (agent, model and effort), the retried task if any, the choice's reason and, for an assignment, why and its cause, the predictions and selection probabilities of the draw, the quota prices it applied, and the shared resources reserved;
- started: what runs the task: for an executor the aop run, by its run id, once aop has launched, and for any task the agent-job units it runs, which the lead's turns are matched against. An open with no started event after a project-set lease, 30 minutes by default, is closed as aborted, so a session that dies between select and launch cannot hold the checkout or VM;
- close, once the result has landed or been discarded, releasing its reservations (a deferred task is then awaiting verdict): outcome, what caught a failure and its defect, brief error if any, resume count, actor cost, lead cost and its brief part, run duration and minutes each shared resource was held. Before closing, the lead re-measures any number the actor reported that would change a decision, on a fresh build;
- verdict, in a deferred kind: final when the authoritative check has run, its evidence naming that run and the cases it covered. Branches the task left out stay listed as unresolved and do not hold it open; a later failure in one of them is a new task, not an overturn;
- overturn: a later finding that a final result is wrong. It names one primary task as the root cause, the one whose commit introduced the overturned code, turns that task's outcome to corrected, records the redo cost on it, and says whether the defect was one of integrity;
- epoch: written by delegate when an actor's results or costs have changed.

A running task keeps its reservations until the lead closes it, since only the lead can judge the result; a run that crashed is closed by the lead as aborted. None of the three projects has a recorded case of a crashed run holding a resource, so the helper does not watch runs or units itself. The helper runs no cleanup of its own: none of the three projects has a recorded case where state a run left behind broke a later task. The simulator port's stale corpus count is prevented by running comparisons on a fresh build directory, and the reverse-engineering project's capture scripts already clean their VM at exit and at start. A cleanup step comes back when a project records an incident it would have prevented. Running and awaiting-verdict tasks are left out of the model and the scorecard's counts, and their number is shown beside each actor, so none is credited before the check that defines success. A brief error is the lead's: a task that stopped for the missing decision stays steered, a success for the executor, and the extra resumes the error caused are left out of the executor's cost and counted in the lead's.

### 7. Read the scorecard

For each kind, show each actor, the lead included: its tasks, those still open or awaiting a verdict, verdicts and results that stood, integrity defects, epochs, rescored tasks, its recorded spend and spend per standing result, and the model's current chance of standing, cost and cost per standing result. Show counts, not percentages, while samples are small.

## The helper

A small command, `delegate`, does the arithmetic, so no lead does it in its own context:

- `select KIND CLASS [--resources R,...] [--token-cap N] [--retry TASK] [--assign ACTOR --why .. [--cause C]]`: chooses delegate, wait with the busy resource or pool, or lead, gives the reason, and for every answer but wait appends the open event, reserving the named resources, in the same locked step;
- `run` and `continue`: start and resume the chosen executor's aop run, so the ledger names every run without the lead copying ids;
- `started`, `close`, `verdict`, `overturn`, `amend`, `suspend`, `resume`: append the events above, `close` filling costs from aop's records and clanker-analytics, and `close`, `verdict` and `overturn` appending an epoch when the actor has changed;
- `scorecard`: prints each kind's actors.

The host keeps one roster of actors, pools and shared machines and the list of its projects' configs; each project keeps a small config naming its kinds, classes, how each kind is checked and measured, the capabilities and resources each kind needs, and its own resources.

## Choices and their alternatives

Each choice below was tested in the simulator ([sim/](sim/README.md)) against the mechanism it replaced, one change at a time across 15 scenarios and 8 population shapes; [proposals/shared-skill.md](proposals/shared-skill.md) has the numbers.

- One model shared across projects, kinds and families, rather than separate records per project and kind: sharing was never significantly worse and was better in most settings, by up to 0.33 per work item.
- Cost per standing result as the objective: within 0.025 per work item of the exact expected cost to complete a task with retries, and simpler than valuing results.
- Thompson sampling as the only source of exploration: warm-up, a first-verdict hold, a cap on unverified work and calibration gates each cost more than they saved.
- No drop rule, no kind states, and the lead as an actor: the drop rule changed nothing measurable, and the lead as only a fallback was worse when the lead was cheap.
- Integrity defects as a cost, not a suspension.
- Epochs on detected change rather than ageing evidence: they saved most when an actor broke or its cost jumped, and cost little elsewhere.
- No price on time holding a resource: it is handled by reservation and the wait answer.
- The outcome fields kept rather than reduced to pass or fail: the caught-by and defect fields are what made the simulator port's record of its weakest executor readable.
- Lead cost from tagged turns rather than fixed constants: the simulator port's corrections of its weakest executor's ports cost the lead $17.57 against $8.46 of that executor's own runs, about twice as much, which a constant would hide.

## Adopting it

- The host roster (`host.toml`) takes the executors from each project's `[executors]` tables, declared once, with a price index for each and the lead as its own entry, and lists every project's config. Claude actors share one pool, on which the pool check picks the account.
- `delegate migrate`, run once per project before its config drops `[executors]`, records in its ledger what each of its actor names meant. Opens record the actor's identity from then on, so concord's `luna` and w3sim's `luna-max`, one model under two names, are one actor to the model.
- Each project config then drops its executors, pools, kind and class states, candidate families and wait deadline; delegate names each removed key on loading.
- Existing ledgers otherwise carry over unchanged: their tasks are the model's first data, and their drop and suspend events still stop an actor until a person resumes it.
