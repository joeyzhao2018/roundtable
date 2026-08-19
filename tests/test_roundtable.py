"""End-to-end tests for roundtable, using tests/fake_agent.sh.

Every invocation goes through ``python -m roundtable --project <tmpdir>`` so
the real CLI is exercised.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from roundtable.runner import extract_markdown

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_AGENT = REPO_ROOT / "tests" / "fake_agent.sh"


def fake_config(timeout: int = 30) -> str:
    """A roundtable.toml wiring two fake agents (alpha, beta)."""
    fake = str(FAKE_AGENT)

    def agent(name: str) -> str:
        return f"""
[agents.{name}]
command = "bash"
args_round_first = ["{fake}", "--name", "{name}", "{{prompt}}"]
args_round_resume = ["{fake}", "--name", "{name}", "--resume", "{{session}}", "{{prompt}}"]
args_interactive_first = ["{fake}", "--name", "{name}", "{{prompt}}"]
args_interactive_resume = ["{fake}", "--name", "{name}", "resume", "{{session}}", "{{prompt}}"]
session_regex = 'session id:\\s*(\\S+)'
readonly_args = ["--ro-no-write"]
"""

    return f"""
[settings]
state_dir = ".roundtable"
max_diff_chars = 60000
timeout_seconds = {timeout}
editor = ""
{agent("alpha")}{agent("beta")}"""


class RoundtableCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="roundtable-test-")
        self.project = Path(self._tmp.name)
        self.log = self.project / "invocations.log"
        self.counter_dir = self.project / "counters"
        # Hermetic git: ignore the user's global/system git config (signing
        # keys, external diff tools, global hooks) in every spawned process.
        self._saved_git_env = {
            k: os.environ.get(k)
            for k in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
        }
        os.environ["GIT_CONFIG_GLOBAL"] = os.devnull
        os.environ["GIT_CONFIG_SYSTEM"] = os.devnull

    def tearDown(self) -> None:
        for key, value in self._saved_git_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------

    def rt(self, *args: str, env_extra: dict | None = None, check: bool = True):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["FAKE_AGENT_LOG"] = str(self.log)
        env["FAKE_COUNTER_DIR"] = str(self.counter_dir)
        env.pop("FAKE_SLEEP", None)
        env.pop("FAKE_FAIL", None)
        env.pop("FAKE_WRITE_MODE", None)
        env.pop("FAKE_COMMIT_FILE", None)
        if env_extra:
            env.update(env_extra)
        proc = subprocess.run(
            [sys.executable, "-m", "roundtable", "--project", str(self.project), *args],
            capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
            timeout=120,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"roundtable {' '.join(args)} failed (rc={proc.returncode})\n"
                f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
            )
        return proc

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args],
            capture_output=True, text=True, cwd=str(self.project), check=True,
        )
        return proc.stdout.strip()

    def write_config(self, timeout: int = 30) -> None:
        (self.project / "roundtable.toml").write_text(fake_config(timeout))

    def state(self) -> dict:
        return json.loads(
            (self.project / ".roundtable" / "state.json").read_text()
        )

    def round_dir(self, n: int) -> Path:
        return self.project / ".roundtable" / "rounds" / f"round-{n:03d}"

    def read_log(self) -> str:
        return self.log.read_text() if self.log.is_file() else ""

    def invocations(self, name: str, number: int) -> str:
        """The logged prompt block for fake agent *name* invocation *number*."""
        import re

        text = self.read_log()
        marker = f"=== invocation: {name} #{number} ==="
        m = re.search(rf"(?m)^{re.escape(marker)}$", text)
        self.assertIsNotNone(m, f"no logged {marker}\nlog:\n{text}")
        start = m.start()
        # Next marker must be at a line start (diff content inside prompts can
        # contain "+=== invocation: ..." lines, which must not match).
        nxt = re.search(r"(?m)^=== invocation:", text[start + 1:])
        end = start + 1 + nxt.start() if nxt else len(text)
        return text[start:end]


class TestEndToEnd(RoundtableCase):
    """init -> ask -> review -> next -> pick --relay -> table -> status/show."""

    def test_full_scenario(self) -> None:
        # --- init ------------------------------------------------------------
        proc = self.rt("init")
        self.assertIn("Wrote", proc.stdout)
        self.assertTrue((self.project / "roundtable.toml").is_file())
        # the scaffold documents the readonly_args enforcement mechanism
        self.assertIn("readonly_args", (self.project / "roundtable.toml").read_text())
        self.assertTrue((self.project / ".roundtable").is_dir())
        # refuses to overwrite without --force
        proc = self.rt("init", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--force", proc.stderr)
        self.rt("init", "--force")
        # now wire in the fake agents
        self.write_config()

        # make it a git repo with an initial commit (needed for pick/table)
        self.git("init", "-q")
        (self.project / "README.md").write_text("# test project\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")

        # --- agents command ----------------------------------------------------
        proc = self.rt("agents")
        self.assertIn("alpha", proc.stdout)
        self.assertIn("beta", proc.stdout)
        self.assertIn("resolved (round 1)", proc.stdout)

        # --- ask (round 1) -----------------------------------------------------
        proc = self.rt("ask", "How should we design the widget module?")
        self.assertIn("round 1", proc.stdout.lower())
        for name in ("alpha", "beta"):
            md = self.round_dir(1) / f"{name}.md"
            self.assertTrue(md.is_file(), f"missing {md}")
            self.assertIn(f"FAKE-TOKEN-{name}-1", md.read_text())
        self.assertTrue((self.round_dir(1) / "QUESTION.md").is_file())
        self.assertTrue((self.round_dir(1) / "_meta.json").is_file())
        meta = json.loads((self.round_dir(1) / "_meta.json").read_text())
        self.assertEqual(meta["alpha"]["exit_code"], 0)
        self.assertEqual(meta["alpha"]["source"], "stdout")
        state = self.state()
        self.assertEqual(state["mode"], "awaiting_human")
        self.assertEqual(state["round"], 1)
        self.assertEqual(state["sessions"]["alpha"], "fake-alpha-1")
        self.assertEqual(state["sessions"]["beta"], "fake-beta-1")
        # anonymized peer labels cover every participant (agents + human)
        labels = state["labels"]
        self.assertEqual(set(labels), {"alpha", "beta", "human"})
        self.assertEqual(len(set(labels.values())), 3)
        for label in labels.values():
            self.assertRegex(label, r"^Peer [A-Z]$")
        # round-1 prompt states the anonymized ground rules, and the round
        # invocation carries readonly_args
        a1 = self.invocations("alpha", 1)
        self.assertIn("identified only by peer labels", a1)
        self.assertIn("--ro-no-write", a1)

        # --- missing human.md guard --------------------------------------------
        proc = self.rt("next", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("human.md", proc.stderr)

        # --- review seeds the template (still not substantive) ------------------
        proc = self.rt("review", "--no-edit")
        self.assertIn("Seeded", proc.stdout)
        human_path = self.round_dir(1) / "human.md"
        self.assertTrue(human_path.is_file())
        proc = self.rt("next", check=False)
        self.assertNotEqual(proc.returncode, 0)  # template-only still blocked
        self.assertIn("--skip-human", proc.stderr)

        # --- human writes a real verdict, then next (round 2) -------------------
        human_path.write_text(
            "# Verdict\n\nHUMAN-VERDICT-TOKEN: I prefer alpha's plan, "
            "but beta raised a good risk.\n"
        )
        proc = self.rt("next")
        self.assertTrue((self.round_dir(2) / "alpha.md").is_file())
        self.assertTrue((self.round_dir(2) / "beta.md").is_file())
        state = self.state()
        self.assertEqual(state["round"], 2)
        self.assertEqual(state["mode"], "awaiting_human")
        # resumed sessions were used and updated
        self.assertEqual(state["sessions"]["alpha"], "fake-alpha-2")

        # cross-pollination: each agent saw the OTHER agent's round-1 output
        # plus human.md — quoted under anonymized peer labels, never under
        # agent names or a "human" framing
        a2 = self.invocations("alpha", 2)
        self.assertIn("FAKE-TOKEN-beta-1", a2)
        self.assertIn("HUMAN-VERDICT-TOKEN", a2)
        self.assertIn(f"Response of {labels['beta']}", a2)
        self.assertIn(f"Response of {labels['human']}", a2)
        self.assertNotIn('Response of agent "', a2)
        self.assertNotIn("human.md", a2)
        # anti-false-consensus language (fragments kept within one template
        # line so wrapping cannot break the match)
        self.assertIn(
            "Say plainly where you now think a peer is right and you were",
            a2,
        )
        self.assertIn(
            "an unresolved disagreement, stated precisely, is a",
            a2,
        )
        self.assertIn("more useful outcome than a false agreement", a2)
        # discussion rounds still carry readonly_args
        self.assertIn("--ro-no-write", a2)
        self.assertIn("--resume", a2)
        self.assertIn("fake-alpha-1", a2)
        b2 = self.invocations("beta", 2)
        self.assertIn("FAKE-TOKEN-alpha-1", b2)
        self.assertIn("HUMAN-VERDICT-TOKEN", b2)
        self.assertIn(f"Response of {labels['alpha']}", b2)
        # no-write rule still present
        self.assertIn("DO NOT write, modify, or create any code", a2)

        # --- bad agent name ------------------------------------------------------
        proc = self.rt("pick", "no-such-agent", "--relay", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown agent", proc.stderr)

        # --- pick alpha --relay ---------------------------------------------------
        base_commit = self.git("rev-parse", "HEAD")
        proc = self.rt("pick", "alpha", "--relay")
        self.assertIn("Write phase active with alpha", proc.stdout)
        state = self.state()
        self.assertEqual(state["mode"], "write")
        self.assertEqual(state["picked_agent"], "alpha")
        wp = state["write_phase"]
        self.assertEqual(wp["agent"], "alpha")
        self.assertEqual(wp["started_round"], 2)
        self.assertEqual(wp["base_commit"], base_commit)
        self.assertEqual(wp["rounds_dir_snapshot"], "rounds/round-002")
        relay_md = self.round_dir(2) / "alpha.relay.md"
        self.assertTrue(relay_md.is_file())
        # primer content reached the agent: plan + human + write permission
        a3 = self.invocations("alpha", 3)
        self.assertIn("HUMAN-VERDICT-TOKEN", a3)
        self.assertIn("You MAY write code", a3)
        # de-anonymization: the picked agent learns its own peer label and
        # the full label -> participant mapping
        self.assertIn(f"you wrote {labels['alpha']}'s entries", a3)
        self.assertIn(f"- {labels['beta']}: beta", a3)
        self.assertIn(f"- {labels['human']}: human", a3)
        # the write phase must NOT carry readonly_args
        self.assertNotIn("--ro-no-write", a3)

        # next/pick are refused during the write phase
        proc = self.rt("next", "--skip-human", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("write phase", proc.stderr)

        # --- simulated write phase: code change committed ------------------------
        src = self.project / "src"
        src.mkdir()
        (src / "widget.py").write_text(
            "def make_widget():\n    return 'UNIQUE-CODE-TOKEN-4711'\n"
        )
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "implement widget")

        # --- table: review round with diff injection + worklog --------------------
        proc = self.rt("table")
        self.assertIn("Back to the table", proc.stdout)
        self.assertTrue((self.round_dir(3) / "alpha.md").is_file())  # worklog
        self.assertTrue((self.round_dir(3) / "beta.md").is_file())   # review
        state = self.state()
        self.assertEqual(state["mode"], "awaiting_human")
        self.assertEqual(state["round"], 3)
        self.assertIsNone(state["write_phase"])

        a4 = self.invocations("alpha", 4)
        self.assertIn("Summarise what you changed and why", a4)  # worklog prompt
        self.assertIn("UNIQUE-CODE-TOKEN-4711", a4)               # diff injected
        b4 = self.invocations("beta", 3)  # beta's 3rd run (no relay for beta)
        self.assertIn("Review the changes made", b4)               # review prompt
        self.assertIn("UNIQUE-CODE-TOKEN-4711", b4)                # diff injected
        self.assertIn("widget.py", b4)                             # diff --stat

        # --- table with a new framing question --------------------------------------
        proc = self.rt("status")
        self.assertIn("mode:          awaiting_human", proc.stdout)
        self.assertIn("round:         3", proc.stdout)
        self.assertIn("fake-alpha-4", proc.stdout)
        self.assertIn("human.md", proc.stdout)
        # the chair can see the peer-label mapping
        for name in ("alpha", "beta", "human"):
            self.assertIn(labels[name], proc.stdout)

        proc = self.rt("show")
        self.assertIn("rounds/round-003/alpha.md", proc.stdout)
        self.assertIn("rounds/round-003/beta.md", proc.stdout)
        # human.md last (absent here, so just check ordering basis works)
        proc = self.rt("show", "1")
        self.assertIn("rounds/round-001/QUESTION.md", proc.stdout)
        self.assertIn("FAKE-TOKEN-alpha-1", proc.stdout)
        # human.md is printed last when present
        alpha_pos = proc.stdout.find("round-001/alpha.md")
        human_pos = proc.stdout.find("round-001/human.md")
        self.assertNotEqual(alpha_pos, -1)
        self.assertNotEqual(human_pos, -1)
        self.assertGreater(human_pos, alpha_pos)

        # --- history events recorded ---------------------------------------------
        events = [e["event"] for e in self.state()["history"]]
        for expected in ("ask", "round", "pick", "table"):
            self.assertIn(expected, events)


class TestPickRelayRegressions(RoundtableCase):
    """Regression tests for `pick --relay` metadata and base_commit timing."""

    def test_relay_preserves_meta_and_base_commit_predates_write_phase(self):
        self.rt("init")
        self.write_config()
        self.git("init", "-q")
        (self.project / "README.md").write_text("# test project\n")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "initial")

        self.rt("ask", "How should we design the widget module?")
        base_commit = self.git("rev-parse", "HEAD")

        # The fake agent creates and commits a file *during* the relay.
        proc = self.rt(
            "pick", "alpha", "--relay",
            env_extra={"FAKE_COMMIT_FILE": "relay_artifact.py"},
        )
        self.assertIn("Write phase active with alpha", proc.stdout)

        # (a) _meta.json still has the discussion-round entries for ALL
        # agents, plus the relay entry under a distinct key.
        meta = json.loads((self.round_dir(1) / "_meta.json").read_text())
        for name in ("alpha", "beta"):
            self.assertIn(name, meta)
            self.assertEqual(meta[name]["exit_code"], 0)
            self.assertEqual(meta[name]["source"], "stdout")
        self.assertIn("alpha.relay", meta)
        self.assertEqual(meta["alpha.relay"]["exit_code"], 0)

        # (b) write_phase.base_commit is the HEAD captured *before* the
        # relay ran, so the relay's own commit is part of the write phase.
        state = self.state()
        self.assertEqual(state["write_phase"]["base_commit"], base_commit)
        self.assertNotEqual(self.git("rev-parse", "HEAD"), base_commit)

        # The later `table` diff must contain the relay commit's content.
        proc = self.rt("table")
        self.assertIn("Back to the table", proc.stdout)
        a3 = self.invocations("alpha", 3)  # worklog
        self.assertIn("RELAY-WRITE-TOKEN-alpha-2", a3)
        self.assertIn("relay_artifact.py", a3)
        b2 = self.invocations("beta", 2)  # review
        self.assertIn("RELAY-WRITE-TOKEN-alpha-2", b2)
        self.assertIn("relay_artifact.py", b2)
        # the pre-write-phase plan is labelled accurately, not as "worklog"
        self.assertIn(
            "Latest report/plan from the implementing agent (pre-write-phase)",
            b2,
        )
        self.assertNotIn("Worklog of the implementing agent", b2)


class TestEdgeCases(RoundtableCase):
    def test_timeout_handling(self) -> None:
        self.rt("init")
        self.write_config(timeout=1)
        proc = self.rt(
            "ask", "slow question",
            env_extra={"FAKE_SLEEP": "3"}, check=False,
        )
        self.assertNotEqual(proc.returncode, 0)  # all agents failed
        self.assertIn("TIMEOUT", proc.stdout)
        meta = json.loads((self.round_dir(1) / "_meta.json").read_text())
        for name in ("alpha", "beta"):
            self.assertTrue(meta[name]["timed_out"], meta[name])
            self.assertIn("timed out", meta[name]["error"])
        # the coordinator still recorded state and did not crash
        self.assertEqual(self.state()["mode"], "awaiting_human")

    def test_agent_failure_does_not_crash_round(self) -> None:
        self.rt("init")
        self.write_config()
        # FAKE_FAIL=<name> makes the fake fail only when its name matches:
        # beta fails, alpha succeeds -> the round completes with a warning.
        proc = self.rt(
            "ask", "question with one failing agent",
            env_extra={"FAKE_FAIL": "beta"},
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("FAILED", proc.stdout)
        self.assertTrue((self.round_dir(1) / "alpha.md").is_file())

    def test_lock_file_behaviour(self) -> None:
        self.rt("init")
        self.write_config()
        lock = self.project / ".roundtable" / "LOCK"
        lock.write_text("pid=12345\n")
        proc = self.rt("ask", "locked question", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("LOCK", proc.stderr)
        # stale-lock override
        proc = self.rt("ask", "locked question", "--force")
        self.assertTrue((self.round_dir(1) / "alpha.md").is_file())
        # lock released afterwards
        self.assertFalse(lock.exists())

    def test_lock_blocks_next_and_table(self) -> None:
        self.rt("init")
        self.write_config()
        self.rt("ask", "q")
        (self.project / ".roundtable" / "LOCK").write_text("pid=1\n")
        proc = self.rt("next", "--skip-human", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("LOCK", proc.stderr)

    def test_unknown_placeholder_rejected(self) -> None:
        self.rt("init")
        bad = fake_config().replace("{prompt}", "{bogus}", 1)
        (self.project / "roundtable.toml").write_text(bad)
        proc = self.rt("agents", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown placeholder", proc.stderr)

    def test_readonly_args_must_be_str_list(self) -> None:
        self.rt("init")
        bad = fake_config().replace(
            'readonly_args = ["--ro-no-write"]', 'readonly_args = "nope"', 1
        )
        (self.project / "roundtable.toml").write_text(bad)
        proc = self.rt("agents", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("must be a list of strings", proc.stderr)

    def test_readonly_args_unknown_placeholder_rejected(self) -> None:
        self.rt("init")
        bad = fake_config().replace('"--ro-no-write"', '"--ro-{bogus}"', 1)
        (self.project / "roundtable.toml").write_text(bad)
        proc = self.rt("agents", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("unknown placeholder", proc.stderr)

    def test_no_config_error(self) -> None:
        proc = self.rt("ask", "q", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("roundtable init", proc.stderr)

    def test_at_least_one_agent_required(self) -> None:
        self.rt("init")  # scaffold has all agents commented out
        proc = self.rt("agents", check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("at least one agent", proc.stderr)

    def test_file_capture_priority(self) -> None:
        """FAKE_WRITE_MODE=file: the MD is read from the answer path."""
        self.rt("init")
        self.write_config()
        proc = self.rt(
            "ask", "write-to-file question",
            env_extra={"FAKE_WRITE_MODE": "file"},
        )
        md = self.round_dir(1) / "alpha.md"
        self.assertIn("FAKE-TOKEN-alpha-1", md.read_text())
        meta = json.loads((self.round_dir(1) / "_meta.json").read_text())
        self.assertEqual(meta["alpha"]["source"], "file")

    def test_show_and_status_before_ask(self) -> None:
        self.rt("init")
        self.write_config()
        proc = self.rt("show", check=False)
        self.assertNotEqual(proc.returncode, 0)
        proc = self.rt("status")
        self.assertIn("mode:", proc.stdout)
        self.assertIn("round:         0", proc.stdout)


class TestExtractMarkdown(unittest.TestCase):
    def test_claude_json_envelope(self) -> None:
        env = json.dumps(
            {"is_error": False, "result": "# Answer\n\nbody", "session_id": "x"}
        )
        self.assertEqual(extract_markdown(env), "# Answer\n\nbody")

    def test_kimi_stream_jsonl(self) -> None:
        lines = "\n".join([
            json.dumps({"role": "assistant", "content": "part one"}),
            json.dumps({"role": "meta", "type": "session.resume_hint",
                        "session_id": "s"}),
            json.dumps({"role": "assistant", "content": "part two"}),
        ])
        self.assertEqual(extract_markdown(lines), "part one\n\npart two")

    def test_plain_text_passthrough(self) -> None:
        self.assertEqual(extract_markdown("# plain md\n"), "# plain md\n")

    def test_unparseable_json_passthrough(self) -> None:
        raw = "{not really json"
        self.assertEqual(extract_markdown(raw), raw)


if __name__ == "__main__":
    unittest.main()
