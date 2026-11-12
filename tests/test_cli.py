import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from unittest import mock

from delegate import cli
from delegate.config import LEAD
from tests.helpers import BASE, HOST, NOW, FakePools

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).with_name("fixtures") / "aop-result.json"
NO_POOLS = HOST.replace('pool = "cheap-pool"\n', "").replace('pool = "dear-pool"\n', "")


class PoolCheckTests(unittest.TestCase):
    """pool_check against usage-burn's recorded output for each state, run through a stub that replays it."""

    RECORDED = json.loads((Path(__file__).with_name("fixtures") / "usage-burn.json").read_text())

    def check(self, state, *args, now="2026-10-31T19:00:00+00:00"):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        recorded, argv = self.RECORDED[state], directory / "argv.json"
        stub = directory / "usage-burn"
        stub.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"open({str(argv)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
            f"print({recorded['stdout']!r})\nsys.exit({recorded['exit']})\n"
        )
        stub.chmod(0o755)
        with mock.patch.object(cli, "USAGE_BURN", str(stub)), mock.patch.dict(os.environ, {"DELEGATE_NOW": now}):
            result = cli.pool_check(*args)
        return result, json.loads(argv.read_text())

    def test_admitted_and_priced_at_its_quota_due_to_expire(self):
        # Cursor's month at 0.11x pace leaves about 89 points to expire, so a 1-point run there costs nothing.
        result, argv = self.check("admitted", "cursor", None)
        self.assertEqual(result, {"room": True, "marginal": 0.0, "state": "admitted", "reasons": []})
        self.assertEqual(argv, ["--check", "cursor"])

    def test_a_provider_with_several_accounts_names_the_one_the_run_lands_on(self):
        result, argv = self.check("admitted-claude", "claude", 1.0)
        self.assertEqual((result["room"], result["account"]), (True, "second"))
        self.assertEqual(argv, ["--check", "claude", "--cost", "1.0000"])

    def test_the_window_leaving_a_run_the_least_expiring_quota_binds(self):
        windows = [
            {"run_points": 10, "pace": 0.5},  # 50 points expire: free
            {"run_points": 10, "pace": 0.95},  # 5 expire: half the run is paid for
            {"run_points": None, "pace": 0.1},  # no price for this window: no say
        ]
        self.assertAlmostEqual(cli.marginal(windows), 0.5)
        self.assertEqual(cli.marginal([{"run_points": 4, "pace": None}]), 1.0)  # too early in the window to tell
        self.assertEqual(cli.marginal([]), 1.0)

    def test_refused_and_unavailable_have_no_room(self):
        refused, argv = self.check("refused", "devin", 4.0)
        self.assertEqual(refused["room"], False)
        self.assertIn("Daily: 98% used leaves no room for 40 more", refused["reasons"])
        self.assertEqual(argv, ["--check", "devin", "--cost", "4.0000"])
        unavailable, _ = self.check("unavailable", "zcode", None)
        self.assertEqual((unavailable["room"], unavailable["state"]), (False, "unavailable"))

    def test_an_unpriced_pool_has_headroom_only_while_every_window_is_under_its_pace(self):
        # Cursor's month at 4% used against 56% allowed by now, as usage-burn reported it on 2026-11-08.
        self.assertTrue(cli.headroom([{"used_pct": 4.05, "allowed_pct": 56.27}]))
        self.assertFalse(cli.headroom([{"used_pct": 4.05, "allowed_pct": 56.27}, {"used_pct": 90, "allowed_pct": 80}]))
        # Muse's windows are priced, so its check was unavailable for a 23-hour-old reading, not a missing price.
        self.assertFalse(cli.headroom([{"used_pct": 0.0, "allowed_pct": 16.8, "run_points": 2.1}]))
        self.assertFalse(cli.headroom([]))

    def test_a_check_that_cannot_be_made_has_no_room_and_says_why(self):
        result, _ = self.check("error", "agy", None)
        self.assertEqual((result["room"], result["state"]), (False, "error"))
        self.assertIn("name one of Claude and GPT, Gemini as :SCOPE", result["reasons"][0])
        late = subprocess.TimeoutExpired(["usage-burn"], 60)
        with (
            mock.patch.object(cli, "USAGE_BURN", "usage-burn"),
            mock.patch.object(cli.subprocess, "run", side_effect=late),
        ):
            result = cli.pool_check("claude", 18.59)
        self.assertEqual((result["room"], result["state"]), (False, "error"))
        self.assertIn("timed out after 60 seconds", result["reasons"][0])


class LaunchTests(unittest.TestCase):
    def test_a_reused_unit_s_log_gives_only_the_runs_that_started_in_the_task_s_window(self):
        jobs = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, jobs)
        old, new = "11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"
        (jobs / "m8-shop.log").write_text(f"aop: run_id={old}\naop: run_id={new}\n")
        started = {old: NOW - timedelta(hours=3), new: NOW + timedelta(minutes=5)}
        result = {"aop_result": lambda run: {"started_at": started[run].isoformat()}}
        with mock.patch.object(cli, "AGENT_JOBS", jobs), mock.patch.dict(cli.ENV, result):
            self.assertEqual(cli.unit_runs(["m8-shop", "missing"], NOW), [new])
            self.assertEqual(cli.unit_runs(["m8-shop"], NOW - timedelta(hours=4), NOW), [old])

    def test_claude_home_finds_the_home_signed_in_as_the_account(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home)
        (home / ".claude.json").write_text(json.dumps({"oauthAccount": {"emailAddress": "first@example.com"}}))
        (home / ".claude-b").mkdir()
        (home / ".claude-b" / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": "second@example.com"}})
        )
        with mock.patch.object(cli.Path, "home", lambda: home):
            self.assertIsNone(cli.claude_home("first"))
            self.assertEqual(cli.claude_home("second"), str(home / ".claude-b"))
            with self.assertRaisesRegex(cli.UsageError, "0 Claude homes"):
                cli.claude_home("third")

    def test_launch_starts_an_agent_job_running_the_command_in_its_directory(self):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with mock.patch.object(cli.subprocess, "run", run):
            cli.launch_job(
                "port-0140", ["aop", "run", "--prompt-file", "p q.txt"], {"HOME": "/h"}, Path("/w"), 240, "12G"
            )
        self.assertEqual(calls[0][:7], ["agent-job", "port-0140", "--max", "240min", "--mem", "12G", "--"])
        self.assertEqual(calls[0][-1], "cd /w && exec aop run --prompt-file 'p q.txt'")
        self.assertIn("HOME=/h", calls[0])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        (self.directory / "delegate.toml").write_text(BASE)
        (self.directory / "host.toml").write_text(HOST)
        self.results = self.directory / "results"
        self.results.mkdir()
        pools = FakePools({"cheap-pool": True, "dear-pool": True, "lead-pool": True})
        patches = {
            "pool": pools,
            "aop_result": lambda run: (
                json.loads(p.read_text()) if (p := self.results / f"{run}.json").exists() else None
            ),
            "lead_cost": lambda config, task, runs=(), session=None, other_opens=(): None,
        }
        patcher = mock.patch.dict(cli.ENV, patches)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.at(NOW)

    def at(self, moment):
        patcher = mock.patch.dict(
            os.environ, {"DELEGATE_NOW": moment.isoformat(), "DELEGATE_HOST": str(self.directory / "host.toml")}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_cli(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["--config", str(self.directory / "delegate.toml"), *args])
        return code, json.loads(output.getvalue())

    def events(self):
        return [json.loads(line) for line in (self.directory / "ledger.jsonl").read_text().splitlines()]

    def test_a_task_from_select_to_close_takes_its_cost_from_aop(self):
        code, chosen = self.run_cli("select", "tool", "bounded", "--resources", "checkout")
        self.assertEqual((code, chosen["answer"], chosen["resources"]), (0, "delegate", {"checkout": "a"}))
        task = chosen["task"]
        run = json.loads(FIXTURE.read_text())
        (self.results / f"{run['run_id']}.json").write_text(FIXTURE.read_text())
        self.assertEqual(self.run_cli("started", task, "--aop-run", run["run_id"], "--unit", "m4-x")[0], 0)
        code, closed = self.run_cli("close", task, "--outcome", "held", "--lead-cost", "0.75", "--commit", "abc123")
        self.assertEqual(code, 0)
        self.assertEqual(closed["cost"], {"actor": 2.5, "lead": 0.75, "brief": None})
        self.assertEqual(closed["minutes"]["run"], 10.0)
        self.assertEqual([e["event"] for e in self.events()], ["open", "started", "close"])

    def test_amend_corrects_a_closed_task_and_the_record_uses_it(self):
        # As in w3sim, where port-0110 was closed with --actor-cost 0 though its aop run cost $0.99.
        _, chosen = self.run_cli("select", "tool", "bounded")
        task = chosen["task"]
        run = json.loads(FIXTURE.read_text())
        (self.results / f"{run['run_id']}.json").write_text(FIXTURE.read_text())
        self.run_cli("started", task, "--aop-run", run["run_id"])
        self.run_cli("close", task, "--outcome", "held", "--actor-cost", "0", "--lead-cost", "0.5")
        code, amend = self.run_cli("amend", task, "--from-aop", "--reason", "closed with a mistyped cost")
        self.assertEqual(code, 0)
        self.assertEqual(amend["changes"]["cost.actor"], {"old": 0.0, "new": 2.5})
        self.assertEqual((amend["by"], amend["reason"]), ("lead", "closed with a mistyped cost"))
        folded = cli.Ledger(self.directory / "ledger.jsonl").read().tasks[task]
        self.assertEqual((folded.cost("actor"), folded.cost("lead")), (2.5, 0.5))
        self.assertEqual(folded.close["minutes"]["run"], 10.0)
        code, commits = self.run_cli("amend", task, "--commit", "abc123", "def456", "--reason", "missed commits")
        self.assertEqual(commits["changes"], {"commits": {"old": [], "new": ["abc123", "def456"]}})
        self.assertEqual(self.run_cli("amend", task, "--commit", "abc123", "def456", "--reason", "again")[0], 2)
        with mock.patch.dict(cli.ENV, {"lead_cost": lambda *args: (0.25, 0.1)}):
            code, lead = self.run_cli("amend", task, "--from-lead-session", "--reason", "charged another task's turns")
        self.assertEqual(
            (code, lead["changes"]),
            (0, {"cost.lead": {"old": 0.5, "new": 0.25}, "cost.brief": {"old": None, "new": 0.1}}),
        )
        _, other = self.run_cli("select", "tool", "bounded")
        self.assertIn(
            "not closed", self.run_cli("amend", other["task"], "--actor-cost", "1", "--reason", "x")[1]["error"]
        )

    def test_amend_corrects_a_mistyped_score_and_the_scorecard_counts_the_rescore(self):
        self.run_cli("suspend", "dear", "port", "--reason", "only cheap in this test")
        _, port = self.run_cli("select", "port", "bounded", "--assign", "cheap", "--why", "this test")
        self.run_cli("close", port["task"], "--outcome", "held", "--score", "6", "--score-reason", "bounded port")
        code, amend = self.run_cli("amend", port["task"], "--score", "60", "--reason", "typed 6 for 60")
        self.assertEqual((code, amend["changes"]), (0, {"score.value": {"old": 6.0, "new": 60.0}}))
        folded = cli.Ledger(self.directory / "ledger.jsonl").read().tasks[port["task"]]
        self.assertEqual((folded.score, folded.close["score"]["reason"]), (60.0, "bounded port"))
        row = next(r for r in self.run_cli("scorecard", "port")[1]["scorecard"] if r["actor"] == "cheap")
        self.assertEqual(row["rescored"], 1)
        self.assertEqual(self.run_cli("amend", port["task"], "--score", "101", "--reason", "x")[0], 2)
        _, tool = self.run_cli("select", "tool", "bounded")
        self.run_cli("close", tool["task"], "--outcome", "held", "--actor-cost", "1")
        self.assertIn(
            "no lead score", self.run_cli("amend", tool["task"], "--score", "50", "--reason", "x")[1]["error"]
        )

    def test_run_launches_the_executor_and_close_finds_its_runs_in_the_unit_logs(self):
        # The run id exists only once aop is going, so close reads it from the agent-job log of the unit delegate
        # started, as w3sim's lead once had to copy it by hand and 43 closes went without it.
        config = self.directory / "delegate.toml"
        config.write_text(config.read_text() + '\n[launch]\nworkdir = "/code/{checkout}"\ndirectory = "team"\n')
        launched = []
        jobs = self.directory / "agent-jobs"
        jobs.mkdir()
        run = json.loads(FIXTURE.read_text())
        (self.results / f"{run['run_id']}.json").write_text(json.dumps({**run, "started_at": NOW.isoformat()}))
        patches = {"launch": lambda *a: launched.append(a), "claude_home": lambda account: "/home/x/.claude-b"}
        with mock.patch.dict(cli.ENV, patches), mock.patch.object(cli, "AGENT_JOBS", jobs):
            _, chosen = self.run_cli("select", "tool", "bounded", "--resources", "checkout")
            task = chosen["task"]
            prompt = self.directory / "prompt.txt"
            prompt.write_text("do it")
            code, started = self.run_cli("run", task, "--prompt-file", str(prompt))
            self.assertEqual((code, started["units"]), (0, [task]))
            unit, aop, environment, directory, minutes, memory = launched[0]
            self.assertEqual(unit, task)
            self.assertEqual(aop[:2], ["aop", "run"])
            self.assertIn("--workdir", aop)
            self.assertEqual(aop[aop.index("--workdir") + 1], "/code/a")
            self.assertEqual(directory, self.directory / "team")
            self.assertIn("already started", self.run_cli("run", task, "--prompt-file", str(prompt))[1]["error"])
            self.assertIn("no aop run", self.run_cli("continue", task, "--prompt-file", str(prompt))[1]["error"])
            (jobs / f"{task}.log").write_text(f"aop: run_id={run['run_id']} calculated_cost=$2.5\n")
            code, resumed = self.run_cli("continue", task, "--prompt-file", str(prompt))
            self.assertEqual((code, resumed["units"]), (0, [f"{task}-r1"]))
            self.assertEqual(launched[1][1][:2], ["aop", "resume"])
            self.assertEqual(launched[1][1][-1], run["run_id"])
            code, closed = self.run_cli("close", task, "--outcome", "held", "--lead-cost", "0")
        self.assertEqual(
            (code, closed["aop_runs"], closed["cost"]["actor"], closed["resumes"]), (0, [run["run_id"]], 2.5, 1)
        )
        folded = cli.Ledger(self.directory / "ledger.jsonl").read().tasks[task]
        self.assertEqual(folded.started["units"], [task, f"{task}-r1"])

    def test_amend_from_aop_finds_runs_a_close_left_out_in_the_unit_logs(self):
        jobs = self.directory / "agent-jobs"
        jobs.mkdir()
        run = json.loads(FIXTURE.read_text())
        (self.results / f"{run['run_id']}.json").write_text(json.dumps({**run, "started_at": NOW.isoformat()}))
        with mock.patch.object(cli, "AGENT_JOBS", jobs):
            _, chosen = self.run_cli("select", "tool", "bounded")
            task = chosen["task"]
            self.run_cli("started", task, "--unit", "m8-shop")
            (jobs / "m8-shop.log").write_text(f"aop: run_id={run['run_id']}\n")
            # As in w3sim from November 2 to 4: closed without the run id, so with no actor cost.
            with mock.patch.dict(cli.ENV, {"unit_runs": lambda *a: []}):
                _, closed = self.run_cli("close", task, "--outcome", "held", "--lead-cost", "0")
            self.assertIsNone(closed["cost"]["actor"])
            code, amend = self.run_cli("amend", task, "--from-aop", "--reason", "closed without its aop run")
        self.assertEqual(code, 0)
        self.assertEqual(amend["changes"]["aop_runs"], {"old": [], "new": [run["run_id"]]})
        self.assertEqual(amend["changes"]["cost.actor"]["new"], 2.5)

    def test_a_run_aop_could_not_price_leaves_the_cost_unknown(self):
        # aop has no price for Devin's swe-2 or Cursor's composer models, so their results carry no calculated cost.
        run = json.loads(FIXTURE.read_text())
        unpriced = {**run, "run_id": "0" * 8 + run["run_id"][8:], "calculated_cost": None}
        for result in (run, unpriced):
            (self.results / f"{result['run_id']}.json").write_text(json.dumps(result))
        _, chosen = self.run_cli("select", "tool", "bounded")
        task = chosen["task"]
        code, closed = self.run_cli(
            "close", task, "--outcome", "held", "--lead-cost", "0", "--aop-run", run["run_id"], unpriced["run_id"]
        )
        self.assertEqual(code, 0)
        self.assertIsNone(closed["cost"]["actor"])
        self.assertGreater(closed["minutes"]["run"], 0)
        # A close that counted such a run as free is corrected to unknown.
        self.run_cli("amend", task, "--actor-cost", "0", "--reason", "as recorded before")
        code, amend = self.run_cli("amend", task, "--from-aop", "--reason", "aop priced a run at nothing")
        self.assertEqual((code, amend["changes"]["cost.actor"]), (0, {"old": 0.0, "new": None}))

    def test_repeated_list_options_append(self):
        _, chosen = self.run_cli("select", "tool", "bounded")
        code, closed = self.run_cli(
            "close", chosen["task"], "--outcome", "held", "--commit", "d14bf870", "--commit", "74ffd39f"
        )
        self.assertEqual((code, closed["commits"]), (0, ["d14bf870", "74ffd39f"]))

    def test_close_checks_the_kind_s_rules(self):
        _, chosen = self.run_cli("select", "recon", "bounded", "--assign", "cheap", "--why", "this test")
        code, error = self.run_cli("close", chosen["task"], "--outcome", "held")
        self.assertEqual(code, 2)
        self.assertIn("awaiting", error["error"])
        self.run_cli("suspend", "dear", "port", "--reason", "only cheap in this test")
        _, port = self.run_cli("select", "port", "bounded", "--assign", "cheap", "--why", "this test")
        self.assertIn("needs --score", self.run_cli("close", port["task"], "--outcome", "held")[1]["error"])
        self.assertEqual(
            self.run_cli("close", port["task"], "--outcome", "held", "--score", "60", "--score-reason", "bounded port")[
                0
            ],
            0,
        )
        _, probe = self.run_cli("select", "probe", "bounded", "--assign", "cheap", "--why", "this test")
        self.assertIn("evidence", self.run_cli("close", probe["task"], "--outcome", "held")[1]["error"])
        close = ("close", probe["task"], "--outcome", "held", "--evidence", "a.txt", "--falsifier-checked")
        code, answer = self.run_cli(*close, "some text")
        self.assertEqual(code, 2)
        self.assertIn("choose from 'true', 'false'", answer["error"])
        self.assertEqual(self.run_cli(*close, "True")[0], 0)
        self.assertIs(self.events()[-1]["falsifier_checked"], True)

    def test_a_deferred_task_closes_awaiting_and_takes_its_verdict(self):
        _, chosen = self.run_cli("select", "recon", "bounded", "--assign", "cheap", "--why", "this test")
        task = chosen["task"]
        self.run_cli("started", task, "--unit", "m4-recon")
        self.assertEqual(self.run_cli("close", task, "--outcome", "awaiting", "--actor-cost", "1")[0], 0)
        self.assertIn("evidence", self.run_cli("verdict", task, "--outcome", "held")[1]["error"])
        long_note = "block 1,134,720,010 " + "matches the fixture " * 20
        code, verdict = self.run_cli(
            "verdict", task, "--outcome", "held", "--evidence", "block 1,134,720,010", long_note
        )
        self.assertEqual((code, verdict["evidence"]), (0, ["block 1,134,720,010", long_note]))
        code, overturn = self.run_cli("overturn", task, "--redo-cost", "8", "--reason", "native diff")
        self.assertEqual(code, 0)

    def test_reap_closes_an_open_that_never_started(self):
        _, never = self.run_cli("select", "tool", "bounded", "--resources", "checkout")
        _, running = self.run_cli("select", "tool", "bounded", "--resources", "checkout")
        self.run_cli("started", running["task"], "--aop-run", "r1", "--unit", "m4-a")
        self.at(NOW + timedelta(minutes=31))
        code, result = self.run_cli("reap")
        self.assertEqual((code, result["reaped"]), (0, [{"task": never["task"], "event": "close"}]))
        _, again = self.run_cli("select", "tool", "bounded", "--resources", "checkout")
        self.assertEqual(again["resources"], {"checkout": "a"})

    def test_candidates_lists_models_of_the_roster_s_agents_the_roster_does_not_run(self):
        models = [
            {"agent": "devin", "model": "swe-2-high", "input_per_million_usd": None, "output_per_million_usd": None},
            {"agent": "devin", "model": "swe-3", "input_per_million_usd": 1.0, "output_per_million_usd": 4.0},
            {"agent": "codex", "model": "gpt-6.1-sol", "input_per_million_usd": 2.0, "output_per_million_usd": 10.0},
            {"agent": "codex", "model": "gpt-7", "input_per_million_usd": 3.0, "output_per_million_usd": 15.0},
            {"agent": "agy", "model": "gemini-5", "input_per_million_usd": 2.0, "output_per_million_usd": 10.0},
        ]
        with mock.patch.dict(cli.ENV, {"models": lambda: models}):
            code, result = self.run_cli("candidates")
            self.assertEqual(code, 0)
            listed = {c["agent"]: [m["model"] for m in c["models"]] for c in result["candidates"]}
            self.assertEqual(listed, {"codex": ["gpt-7"], "devin": ["swe-3"]})
            self.assertTrue(all(c["pool_room"] for c in result["candidates"]))
            _, everything = self.run_cli("candidates", "--all-agents")
            self.assertIn("agy", [c["agent"] for c in everything["candidates"]])

    def test_a_lead_task_s_cost_is_its_actor_cost_and_an_executor_s_lead_cost_splits_off_its_brief(self):
        with mock.patch.dict(cli.ENV, {"lead_cost": lambda *args: (1.5, 0.5)}):
            _, own = self.run_cli("select", "tool", "bounded", "--assign", LEAD, "--why", "this test")
            _, closed = self.run_cli("close", own["task"], "--outcome", "held")
            self.assertEqual(closed["cost"], {"actor": 1.5, "lead": None, "brief": None})
            _, run = self.run_cli("select", "tool", "bounded", "--assign", "cheap", "--why", "this test")
            _, closed = self.run_cli("close", run["task"], "--outcome", "held", "--actor-cost", "1")
            self.assertEqual(closed["cost"], {"actor": 1.0, "lead": 1.5, "brief": 0.5})

    def test_suspend_stops_an_actor_everywhere_or_in_one_kind(self):
        self.assertEqual(self.run_cli("suspend", "cheap", "--reason", "rate limited")[0], 0)
        _, chosen = self.run_cli("select", "tool", "bounded")
        self.assertNotEqual(chosen["actor"], "cheap")
        self.assertEqual(self.run_cli("suspend", "nobody", "--reason", "x")[0], 2)
        self.run_cli("resume", "cheap", "--reason", "back")
        self.assertEqual([e["event"] for e in self.events()][-1], "resume")

    def test_an_overturn_records_an_integrity_defect(self):
        _, run = self.run_cli("select", "tool", "bounded", "--assign", "cheap", "--why", "this test")
        self.run_cli("close", run["task"], "--outcome", "held", "--actor-cost", "1", "--lead-cost", "0")
        code, overturn = self.run_cli(
            "overturn", run["task"], "--redo-cost", "2", "--reason", "faked the gate", "--defect", "integrity"
        )
        self.assertEqual((code, overturn["defect"]), (0, "integrity"))
        self.assertTrue(cli.Ledger(self.directory / "ledger.jsonl").read().tasks[run["task"]].integrity)

    def test_migrate_records_an_earlier_config_s_actors_and_lists_its_removed_keys(self):
        config = self.directory / "delegate.toml"
        config.write_text(
            "wait_deadline_minutes = 60\n"
            + BASE.replace('check = "gated"', 'check = "gated"\nstate = "on"')
            + '\n[executors.luna]\nagent = "codex"\nmodel = "gpt-6-luna"\neffort = "max"\n'
        )
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(["--config", str(config), "migrate"]), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["actors"]["luna"], {"agent": "codex", "model": "gpt-6-luna", "effort": "max"})
        self.assertEqual(result["actors"]["lead"]["role"], "lead")
        self.assertEqual(sorted(result["remove"]), ["executors", "kinds.tool.state", "wait_deadline_minutes"])
        self.assertEqual(self.events()[-1]["event"], "actors")
        with redirect_stdout(io.StringIO()):
            cli.main(["--config", str(config), "migrate"])
        self.assertEqual(sum(e["event"] == "actors" for e in self.events()), 1)

    def test_ignore_pool_needs_assign(self):
        self.assertIn("--assign", self.run_cli("select", "tool", "bounded", "--ignore-pool")[1]["error"])

    def test_every_error_prints_json_and_exits_2(self):
        for args in (
            ["select", "nosuch", "bounded"],
            ["close", "tool-9999", "--outcome", "held"],
            ["bogus"],
            ["select", "tool", "bounded", "--token-cap", "lots"],
        ):
            code, output = self.run_cli(*args)
            self.assertEqual(code, 2, args)
            self.assertIn("error", output)


class EntryPointTests(unittest.TestCase):
    def run_program(self, *args, cwd=None, environment=None):
        env = {**os.environ, "PYTHONPATH": str(ROOT), **(environment or {})}
        return subprocess.run(
            [sys.executable, "-m", "delegate", *args], capture_output=True, text=True, cwd=cwd or ROOT, env=env
        )

    def test_module_entry_point_gives_help(self):
        completed = self.run_program("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("select", completed.stdout)

    def test_selects_running_at_once_give_one_free_instance_to_one(self):
        directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, directory)
        (directory / "delegate.toml").write_text(BASE.replace('checkout = ["a", "b"]', 'checkout = ["only"]'))
        (directory / "host.toml").write_text(NO_POOLS)
        env = {
            **os.environ,
            "PYTHONPATH": str(ROOT),
            "DELEGATE_NOW": NOW.isoformat(),
            "DELEGATE_HOST": str(directory / "host.toml"),
        }
        processes = [
            subprocess.Popen(
                [sys.executable, "-m", "delegate", "select", "tool", "bounded", "--resources", "checkout"],
                cwd=directory,
                env=env,
                stdout=subprocess.PIPE,
                text=True,
            )
            for _ in range(6)
        ]
        answers = [json.loads(p.communicate()[0])["answer"] for p in processes]
        self.assertEqual(sorted(a == "wait" for a in answers), [False] + [True] * 5)


if __name__ == "__main__":
    unittest.main()
