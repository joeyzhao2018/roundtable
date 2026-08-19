"""argparse dispatch and all user-facing commands."""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from . import __version__
from .agents import AgentSpec
from .config import (
    Config,
    ConfigError,
    config_warnings,
    load_config,
    scaffold,
)
from .handover import git_diff, git_diff_stat, git_head, hand_over, is_git_repo
from .prompts import (
    handover_primer,
    human_template,
    rethink_prompt,
    review_prompt,
    round_prompt,
    worklog_prompt,
    write_relay_prompt,
)
from .runner import format_cmd, run_round
from .state import LockError, StateStore, new_labels

EXIT_OK = 0
EXIT_ERROR = 2

_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


class RoundtableError(Exception):
    """User-facing runtime error."""


# --------------------------------------------------------------------------
# helpers


def _err(msg: str) -> None:
    print(f"roundtable: error: {msg}", file=sys.stderr)


def _warn(msg: str) -> None:
    print(f"roundtable: warning: {msg}", file=sys.stderr)


def _load(args) -> tuple[Config, StateStore, dict]:
    config = load_config(args.project)
    for warning in config_warnings(config):
        _warn(warning)
    store = StateStore(config.project_root, config.settings.state_dir)
    store.ensure_dirs()
    state = store.load()
    return config, store, state


def _specs(config: Config) -> list[AgentSpec]:
    return [AgentSpec(cfg) for cfg in config.agents.values()]


def _human_md_substantive(text: str) -> bool:
    """True when human.md has content outside HTML comments/whitespace."""
    return bool(_HTML_COMMENT_RE.sub("", text).strip())


def _report_round_results(
    store: StateStore,
    round_no: int,
    results: dict,
    *,
    merge: bool = False,
    key_suffix: str = "",
) -> list[str]:
    """Write _meta.json, print a per-agent summary, return failed agent names.

    With ``merge=True`` the new entries are merged into any existing
    _meta.json for the round (instead of overwriting it); ``key_suffix``
    is appended to each agent key (e.g. ".relay" for relay entries).
    """
    meta = {name + key_suffix: r.meta_entry() for name, r in results.items()}
    if merge:
        existing = store.read_meta(round_no)
        existing.update(meta)
        meta = existing
    store.write_meta(round_no, meta)
    failed: list[str] = []
    print(f"\nRound {round_no} results:")
    for name, r in results.items():
        status = "ok"
        if r.timed_out:
            status = f"TIMEOUT ({r.error})"
        elif r.error:
            status = f"FAILED ({r.error})"
        elif r.source is None:
            status = "FAILED (no output captured)"
        if r.exit_code not in (0, None) and not r.error:
            status = f"FAILED (exit code {r.exit_code})"
        out = r.output_path or "(no output)"
        print(f"  {name:<12} {status:<40} {out}")
        if status != "ok":
            failed.append(name)
    return failed


def _update_sessions(state: dict, results: dict) -> None:
    for name, r in results.items():
        if r.session_id:
            state["sessions"][name] = r.session_id


def _print_round_files(store: StateStore, round_no: int, agent_names: list[str]) -> None:
    print(f"\nReview the markdown files in {store.round_dir(round_no)}:")
    for name in agent_names:
        print(f"  {store.agent_md_path(round_no, name)}")
    print(f"  {store.human_md_path(round_no)}  <- write your verdict here")
    print("Then run `roundtable review` (or edit human.md directly), "
          "followed by `roundtable next` or `roundtable pick <agent>`.")


# --------------------------------------------------------------------------
# commands


def cmd_init(args) -> int:
    root = Path(args.project).resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        path = scaffold(root, force=args.force)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_ERROR
    (root / ".roundtable").mkdir(exist_ok=True)
    print(f"Wrote {path}")
    print(f"Created {root / '.roundtable'}")
    print("Edit roundtable.toml to configure your agents, then "
          "`roundtable ask \"<question>\"`.")
    return EXIT_OK


def cmd_ask(args) -> int:
    config, store, state = _load(args)
    question = args.question
    try:
        store.acquire_lock(force=args.force)
    except LockError as exc:
        _err(str(exc))
        return EXIT_ERROR
    try:
        # A new discussion thread: reset thread-scoped state, including the
        # anonymized peer labels (reshuffled for every new thread).
        specs = _specs(config)
        names = [s.name for s in specs]
        state.update(
            round=1, picked_agent=None, sessions={}, write_phase=None,
            labels=new_labels(names),
        )
        store.write_question(1, question)
        prompts = {
            name: round_prompt(question, name, 1, str(store.agent_md_path(1, name)))
            for name in names
        }
        answer_paths = {name: store.agent_md_path(1, name) for name in names}
        print(f"Starting round 1 with agents: {', '.join(names)}")
        results = run_round(
            specs, prompts, answer_paths, state["sessions"],
            config.settings.timeout_seconds, config.project_root,
        )
        failed = _report_round_results(store, 1, results)
        _update_sessions(state, results)
        state["mode"] = "awaiting_human"
        store.add_history(state, "ask", question)
        store.save(state)
        _print_round_files(store, 1, names)
        if failed:
            _warn("agents failed this round: " + ", ".join(failed))
            if len(failed) == len(names):
                return EXIT_ERROR
    finally:
        store.release_lock()
    return EXIT_OK


def cmd_review(args) -> int:
    config, store, state = _load(args)
    round_no = state["round"]
    if round_no < 1 or state["mode"] == "write":
        raise RoundtableError(
            "no discussion round awaiting review (mode: "
            f"{state['mode']}, round: {round_no})"
        )
    human_path = store.human_md_path(round_no)
    if not human_path.is_file():
        question = store.read_question()
        human_path.parent.mkdir(parents=True, exist_ok=True)
        human_path.write_text(
            human_template(question, round_no, sorted(config.agents)),
            encoding="utf-8",
        )
        print(f"Seeded {human_path}")
    if args.no_edit:
        print(f"Edit {human_path} and then run `roundtable next` or "
              "`roundtable pick <agent>`.")
        return EXIT_OK
    editor = (
        config.settings.editor
        or os.environ.get("EDITOR")
        or os.environ.get("VISUAL")
        or "vi"
    )
    print(f"Opening {human_path} with {editor!r} ...")
    try:
        rc = subprocess.call(shlex.split(editor) + [str(human_path)])
    except FileNotFoundError:
        _err(f"editor not found: {shlex.split(editor)[0]!r}")
        return EXIT_ERROR
    if rc != 0:
        _warn(f"editor exited with code {rc}")
    store.add_history(state, "review", f"human.md for round {round_no} edited")
    store.save(state)
    print("Review recorded. Run `roundtable next` for another round or "
          "`roundtable pick <agent>` to enter the write phase.")
    return EXIT_OK


def cmd_next(args) -> int:
    config, store, state = _load(args)
    if state["mode"] == "write":
        raise RoundtableError(
            "currently in the write phase — use `roundtable table` to return "
            "to the round table"
        )
    prev_round = state["round"]
    if prev_round < 1:
        raise RoundtableError("no discussion thread yet — run `roundtable ask` first")
    human_path = store.human_md_path(prev_round)
    human_md = store.read_md(human_path)
    if not args.skip_human and not _human_md_substantive(human_md):
        raise RoundtableError(
            f"{human_path} is missing or empty — write your verdict first "
            "(`roundtable review`) or re-run with --skip-human"
        )
    state["mode"] = "discuss"  # human.md exists; next may run
    try:
        store.acquire_lock(force=args.force)
    except LockError as exc:
        _err(str(exc))
        return EXIT_ERROR
    try:
        round_no = prev_round + 1
        question = store.read_question()
        store.write_question(round_no, question)
        specs = _specs(config)
        names = [s.name for s in specs]
        prev_mds = store.collect_agent_mds(prev_round, names)
        # Anonymize: each agent sees the other participants' MDs — including
        # the human's — quoted under their shuffled peer labels, never under
        # agent names or a "human" framing. Falls back to plain names for
        # state files predating the labels feature.
        labels = state.get("labels") or {}
        prompts: dict[str, str] = {}
        for name in names:
            peer_mds = {
                labels.get(n, n): t for n, t in prev_mds.items() if n != name
            }
            if human_md.strip():
                peer_mds[labels.get("human", "human")] = human_md
            peer_mds = dict(sorted(peer_mds.items()))
            prompts[name] = rethink_prompt(
                question, name, round_no,
                str(store.agent_md_path(round_no, name)), peer_mds,
            )
        answer_paths = {name: store.agent_md_path(round_no, name) for name in names}
        print(f"Starting round {round_no} with agents: {', '.join(names)}")
        results = run_round(
            specs, prompts, answer_paths, state["sessions"],
            config.settings.timeout_seconds, config.project_root,
        )
        failed = _report_round_results(store, round_no, results)
        _update_sessions(state, results)
        state["round"] = round_no
        state["mode"] = "awaiting_human"
        store.add_history(state, "round", f"round {round_no} completed")
        store.save(state)
        _print_round_files(store, round_no, names)
        if failed:
            _warn("agents failed this round: " + ", ".join(failed))
            if len(failed) == len(names):
                return EXIT_ERROR
    finally:
        store.release_lock()
    return EXIT_OK


def _validate_pick(state: dict, config: Config, agent_name: str) -> None:
    if agent_name not in config.agents:
        raise RoundtableError(
            f"unknown agent {agent_name!r} — configured agents: "
            + ", ".join(sorted(config.agents))
        )
    if state["mode"] == "write":
        raise RoundtableError(
            f"already in the write phase with {state['picked_agent']!r} — "
            "use `roundtable table` to return first"
        )
    if state["round"] < 1:
        raise RoundtableError("no discussion thread yet — run `roundtable ask` first")


def _enter_write_phase(
    config: Config,
    store: StateStore,
    state: dict,
    agent_name: str,
    base_commit: str | None,
) -> None:
    round_no = state["round"]
    if base_commit is None:
        _warn("not a git repository — diff injection for `table` will be skipped")
    state["picked_agent"] = agent_name
    state["write_phase"] = {
        "agent": agent_name,
        "started_round": round_no,
        "base_commit": base_commit,
        "rounds_dir_snapshot": f"rounds/round-{round_no:03d}",
    }
    state["mode"] = "write"


def cmd_pick(args) -> int:
    config, store, state = _load(args)
    _validate_pick(state, config, args.agent)
    round_no = state["round"]
    question = store.read_question()
    plan_md = store.read_md(store.agent_md_path(round_no, args.agent))
    # The human's latest human.md across rounds (the current round may have
    # none yet if `next` just ran).
    human_md = ""
    for r in range(round_no, 0, -1):
        candidate = store.read_md(store.human_md_path(r))
        if candidate.strip():
            human_md = candidate
            break
    primer = handover_primer(
        args.agent, plan_md, human_md, question, labels=state.get("labels") or None
    )
    spec = AgentSpec(config.agents[args.agent])
    session = state["sessions"].get(args.agent)

    # Capture HEAD *before* the write phase starts (SPEC §5: base_commit is
    # `git rev-parse HEAD` at pick time) so the later `table` diff includes
    # every commit made during the write phase.
    if is_git_repo(config.project_root):
        base_commit = git_head(config.project_root)
    else:
        base_commit = None

    if args.relay:
        # Headless alternative: one non-interactive WRITE-allowed round.
        try:
            store.acquire_lock(force=args.force)
        except LockError as exc:
            _err(str(exc))
            return EXIT_ERROR
        try:
            answer_path = store.round_dir(round_no) / f"{args.agent}.relay.md"
            prompt = write_relay_prompt(primer, str(answer_path))
            print(f"Relaying write phase to {args.agent} (non-interactive) ...")
            results = run_round(
                [spec], {args.agent: prompt}, {args.agent: answer_path},
                state["sessions"], config.settings.timeout_seconds,
                config.project_root, readonly=False,
            )
            # Merge into the existing _meta.json: the relay entry must not
            # clobber the discussion-round entries of the other agents.
            _report_round_results(
                store, round_no, results, merge=True, key_suffix=".relay"
            )
            _update_sessions(state, results)
        finally:
            store.release_lock()
        rc = 0
    else:
        rc, _ = hand_over(spec, primer, session, config.project_root)

    _enter_write_phase(config, store, state, args.agent, base_commit)
    head = git_head(config.project_root) if is_git_repo(config.project_root) else None
    store.add_history(
        state, "pick",
        f"{args.agent} picked ({'relay' if args.relay else 'interactive'}); "
        f"exit={rc} head={head}",
    )
    store.save(state)
    print(f"\nWrite phase active with {args.agent} "
          f"(base commit: {state['write_phase']['base_commit'] or 'n/a'}).")
    print("When done, run `roundtable table` to bring the results back to "
          "the round table for review.")
    return EXIT_OK


def cmd_table(args) -> int:
    config, store, state = _load(args)
    if state["mode"] != "write" or not state["write_phase"]:
        raise RoundtableError("not in the write phase — nothing to bring to the table")
    wp = state["write_phase"]
    picked = wp["agent"]
    base = wp["base_commit"]

    diff_stat, diff = "", ""
    if base and is_git_repo(config.project_root):
        diff_stat = git_diff_stat(base, config.project_root)
        diff = git_diff(base, config.project_root, config.settings.max_diff_chars)
    else:
        _warn("no git base commit available — skipping diff injection")

    try:
        store.acquire_lock(force=args.force)
    except LockError as exc:
        _err(str(exc))
        return EXIT_ERROR
    try:
        round_no = state["round"] + 1
        question = args.question or store.read_question()
        store.write_question(round_no, question)
        specs = _specs(config)
        names = [s.name for s in specs]
        # The picked agent's MD from the round the write phase started from.
        # It is an actual worklog only if it was produced by a previous
        # `table` cycle at this same round for the same agent; otherwise it
        # is the agent's discussion-round report/plan.
        prev_md = store.read_md(store.agent_md_path(state["round"], picked))
        is_worklog = any(
            e.get("event") == "table"
            and f"round {state['round']}: worklog by {picked}," in e.get("detail", "")
            for e in state.get("history", [])
        )
        prev_label = (
            "Worklog of the implementing agent"
            if is_worklog
            else "Latest report/plan from the implementing agent (pre-write-phase)"
        )
        prompts: dict[str, str] = {}
        for name in names:
            answer_path = str(store.agent_md_path(round_no, name))
            if name == picked:
                prompts[name] = worklog_prompt(name, round_no, answer_path,
                                               diff_stat, diff)
            else:
                prompts[name] = review_prompt(
                    name, round_no, answer_path, diff_stat, diff,
                    prev_md or None,
                    framing_question=args.question,
                    worklog_label=prev_label,
                )
        answer_paths = {name: store.agent_md_path(round_no, name) for name in names}
        print(f"Back to the table — round {round_no} "
              f"(worklog: {picked}; review: {', '.join(n for n in names if n != picked) or 'none'})")
        results = run_round(
            specs, prompts, answer_paths, state["sessions"],
            config.settings.timeout_seconds, config.project_root,
        )
        failed = _report_round_results(store, round_no, results)
        _update_sessions(state, results)
        state["round"] = round_no
        state["write_phase"] = None
        state["mode"] = "awaiting_human"
        store.add_history(
            state, "table",
            f"round {round_no}: worklog by {picked}, review by others"
            + (f"; new framing: {args.question}" if args.question else ""),
        )
        store.save(state)
        _print_round_files(store, round_no, names)
        if failed:
            _warn("agents failed this round: " + ", ".join(failed))
            if len(failed) == len(names):
                return EXIT_ERROR
    finally:
        store.release_lock()
    return EXIT_OK


def cmd_status(args) -> int:
    config, store, state = _load(args)
    round_no = state["round"]
    print("roundtable status")
    print(f"  project:       {config.project_root}")
    print(f"  mode:          {state['mode']}")
    print(f"  round:         {round_no}")
    print(f"  picked agent:  {state['picked_agent'] or '-'}")
    if state["write_phase"]:
        wp = state["write_phase"]
        print(f"  write phase:   agent={wp['agent']} "
              f"started_round={wp['started_round']} "
              f"base_commit={wp['base_commit'] or 'n/a'}")
    print("  agents:")
    for name in sorted(config.agents):
        session = state["sessions"].get(name, "-")
        print(f"    {name:<12} session={session}")
    labels = state.get("labels") or {}
    if labels:
        # The user chairs the table, so the mapping is shown to them; the
        # agents only ever see the labels.
        print("  peer labels:")
        for name, label in sorted(labels.items(), key=lambda kv: kv[1]):
            print(f"    {label:<9} {name}")
    if round_no >= 1:
        print(f"  files in rounds/round-{round_no:03d}:")
        rdir = store.round_dir(round_no)
        expected = ["QUESTION.md"] + [f"{n}.md" for n in sorted(config.agents)] + ["human.md"]
        for fname in expected:
            mark = "present" if (rdir / fname).is_file() else "MISSING"
            print(f"    {fname:<16} {mark}")
    if state["history"]:
        print("  last events:")
        for entry in state["history"][-5:]:
            print(f"    [{entry['ts']}] {entry['event']}: {entry['detail']}")
    return EXIT_OK


def cmd_show(args) -> int:
    config, store, state = _load(args)
    round_no = args.round if args.round is not None else state["round"]
    if round_no < 1:
        raise RoundtableError("no rounds yet — run `roundtable ask` first")
    rdir = store.round_dir(round_no)
    if not rdir.is_dir():
        raise RoundtableError(f"round {round_no} does not exist ({rdir})")
    mds = sorted(p for p in rdir.glob("*.md") if p.name != "human.md")
    human = rdir / "human.md"
    if human.is_file():
        mds.append(human)  # human.md last
    if not mds:
        print(f"(no markdown files in {rdir})")
        return EXIT_OK
    for path in mds:
        rel = path.relative_to(store.dir)
        print("=" * 72)
        print(f"  {rel}")
        print("=" * 72)
        print(path.read_text(encoding="utf-8"))
    return EXIT_OK


def cmd_agents(args) -> int:
    config, _, _ = _load(args)
    print("Configured agents:")
    for name, cfg in sorted(config.agents.items()):
        print(f"  {name}")
        print(f"    command:              {cfg.command}")
        print(f"    args_round_first:     {cfg.args_round_first}")
        print(f"    args_round_resume:    {cfg.args_round_resume or '(none)'}")
        print(f"    args_interactive_*:   "
              f"{'configured' if (cfg.args_interactive_first or cfg.args_interactive_resume) else '(fallback to args_round_*)'}")
        print(f"    session_regex:        {cfg.session_regex or '(none)'}")
        print(f"    readonly_args:        {cfg.readonly_args or '(none)'}")
        spec = AgentSpec(cfg)
        example = spec.build_round("<prompt>")
        print(f"    resolved (round 1):   {format_cmd(example.argv)}"
              + ("  [prompt via stdin]" if example.stdin_prompt is not None else ""))
    return EXIT_OK


# --------------------------------------------------------------------------
# parser / main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roundtable",
        description="A script-driven round-table coordinator for CLI coding agents.",
    )
    parser.add_argument("--version", action="version",
                        version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--project", default=".", metavar="DIR",
        help="project root containing roundtable.toml (default: cwd)",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    p = sub.add_parser("init", help="scaffold roundtable.toml + .roundtable/")
    p.add_argument("--force", action="store_true", help="overwrite existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("ask", help="start a new discussion thread (round 1)")
    p.add_argument("question", help="the question to put to the round table")
    p.add_argument("--force", action="store_true", help="override a stale LOCK file")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("review", help="seed/open human.md for the current round")
    p.add_argument("--no-edit", action="store_true", help="do not open $EDITOR")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("next", help="run the next discussion round")
    p.add_argument("--skip-human", action="store_true",
                   help="run even if human.md is missing/empty")
    p.add_argument("--force", action="store_true", help="override a stale LOCK file")
    p.set_defaults(func=cmd_next)

    p = sub.add_parser("pick", help="pick an agent for the single-agent write phase")
    p.add_argument("agent", help="name of the configured agent to pick")
    p.add_argument("--relay", action="store_true",
                   help="headless: run one non-interactive write-allowed round "
                        "instead of an interactive handover")
    p.add_argument("--force", action="store_true", help="override a stale LOCK file")
    p.set_defaults(func=cmd_pick)

    p = sub.add_parser("table", help="return from write phase to the round table")
    p.add_argument("question", nargs="?", default=None,
                   help="optional new framing question")
    p.add_argument("--force", action="store_true", help="override a stale LOCK file")
    p.set_defaults(func=cmd_table)

    p = sub.add_parser("status", help="pretty-print coordinator state")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("show", help="print all MDs of a round (human.md last)")
    p.add_argument("round", nargs="?", type=int, default=None,
                   help="round number (default: latest)")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("agents", help="list configured agents + resolved commands")
    p.set_defaults(func=cmd_agents)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except LockError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except RoundtableError as exc:
        _err(str(exc))
        return EXIT_ERROR
    except KeyboardInterrupt:
        _err("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
