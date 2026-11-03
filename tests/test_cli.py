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
from tests.helpers import BASE, NOW, FakePools

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).with_name("fixtures") / "aop-result.json"
NO_POOLS = BASE.replace('pool = "cheap-pool"\n', "").replace('pool = "dear-pool"\n', "")


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
        result, argv = self.check("admitted", "cursor", None, 1.0)
        self.assertEqual(result, {"room": True, "marginal": 0.0, "state": "admitted", "reasons": []})
        self.assertEqual(argv, ["--check", "cursor", "--need", "1.0000"])

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
        refused, argv = self.check("refused", "devin", None, 40.0)
        self.assertEqual(refused["room"], False)
        self.assertIn("Daily: 98% used leaves no room for 40 more", refused["reasons"])
        self.assertEqual(argv, ["--check", "devin", "--need", "40.0000"])
        unavailable, _ = self.check("unavailable", "zcode", None, None)
        self.assertEqual((unavailable["room"], unavailable["state"]), (False, "unavailable"))

    def test_a_need_per_window_length_is_passed_as_one_option(self):
        # OpenCode's week just reset, so 25 points of it is over the day's lead; 6 of its half-used month fits.
        result, argv = self.check("refused-per-window", "opencode", None, {"5h": 25.0, "7d": 25.0, "30d": 6.0})
        self.assertEqual(argv, ["--check", "opencode", "--need", "5h=25.0000,7d=25.0000,30d=6.0000"])
        self.assertEqual(result["room"], False)
        self.assertEqual([r.split(":")[0] for r in result["reasons"]], ["Week (7d)"])

    def test_a_check_that_cannot_be_made_has_no_room_and_says_why(self):
        result, _ = self.check("error", "agy", None, None)
        self.assertEqual((result["room"], result["state"]), (False, "error"))
        self.assertIn("name one of Claude and GPT, Gemini as :SCOPE", result["reasons"][0])
        late = subprocess.TimeoutExpired(["usage-burn"], 60)
        with (
            mock.patch.object(cli, "USAGE_BURN", "usage-burn"),
            mock.patch.object(cli.subprocess, "run", side_effect=late),
        ):
            result = cli.pool_check("claude", 18.59, None)
        self.assertEqual((result["room"], result["state"]), (False, "error"))
        self.assertIn("timed out after 60 seconds", result["reasons"][0])


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        (self.directory / "delegate.toml").write_text(BASE)
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
        patcher = mock.patch.dict(os.environ, {"DELEGATE_NOW": moment.isoformat()})
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
        self.assertEqual(closed["cost"], {"actor": 2.5, "lead": 0.75})
        self.assertEqual(closed["minutes"]["run"], 10.0)
        self.assertEqual([e["event"] for e in self.events()], ["open", "started", "close"])

    def test_repeated_list_options_append(self):
        _, chosen = self.run_cli("select", "tool", "bounded")
        code, closed = self.run_cli(
            "close", chosen["task"], "--outcome", "held", "--commit", "d14bf870", "--commit", "74ffd39f"
        )
        self.assertEqual((code, closed["commits"]), (0, ["d14bf870", "74ffd39f"]))

    def test_close_checks_the_kind_s_rules(self):
        _, chosen = self.run_cli("select", "recon", "bounded", "--admit", "cheap")
        code, error = self.run_cli("close", chosen["task"], "--outcome", "held")
        self.assertEqual(code, 2)
        self.assertIn("awaiting", error["error"])
        self.run_cli("drop", "dear", "port", "--reason", "only cheap in this test")
        _, port = self.run_cli("select", "port", "bounded", "--admit", "cheap")
        self.assertIn("needs --score", self.run_cli("close", port["task"], "--outcome", "held")[1]["error"])
        self.assertEqual(
            self.run_cli("close", port["task"], "--outcome", "held", "--score", "60", "--score-reason", "bounded port")[
                0
            ],
            0,
        )
        _, probe = self.run_cli("select", "probe", "bounded", "--admit", "cheap")
        self.assertIn("evidence", self.run_cli("close", probe["task"], "--outcome", "held")[1]["error"])
        close = ("close", probe["task"], "--outcome", "held", "--evidence", "a.txt", "--falsifier-checked")
        code, answer = self.run_cli(*close, "some text")
        self.assertEqual(code, 2)
        self.assertIn("choose from 'true', 'false'", answer["error"])
        self.assertEqual(self.run_cli(*close, "True")[0], 0)
        self.assertIs(self.events()[-1]["falsifier_checked"], True)

    def test_a_deferred_task_closes_awaiting_and_takes_its_verdict(self):
        _, chosen = self.run_cli("select", "recon", "bounded", "--admit", "cheap")
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

    def test_candidates_lists_unconfigured_models_of_the_project_s_agents(self):
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
        config = self.directory / "delegate.toml"
        config.write_text(config.read_text() + '\n[candidates]\nfamilies = ["^(.*/)?gpt-7", "^swe-[2-9]"]\n')
        models.append(
            {"agent": "codex", "model": "gpt-6.5-luna", "input_per_million_usd": 0.1, "output_per_million_usd": 1.0}
        )
        with mock.patch.dict(cli.ENV, {"models": lambda: models}):
            _, narrowed = self.run_cli("candidates")
            self.assertEqual(
                {c["agent"]: [m["model"] for m in c["models"]] for c in narrowed["candidates"]},
                {"codex": ["gpt-7"], "devin": ["swe-3"]},
            )
            _, wide = self.run_cli("candidates", "--any-family")
            self.assertIn("gpt-6.5-luna", [m["model"] for c in wide["candidates"] for m in c["models"]])

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
        (directory / "delegate.toml").write_text(NO_POOLS.replace('checkout = ["a", "b"]', 'checkout = ["only"]'))
        env = {**os.environ, "PYTHONPATH": str(ROOT), "DELEGATE_NOW": NOW.isoformat()}
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
        self.assertEqual(sorted(answers), ["delegate"] + ["wait"] * 5)


if __name__ == "__main__":
    unittest.main()
