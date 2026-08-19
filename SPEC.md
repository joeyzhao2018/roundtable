# SPEC: roundtable — a script-driven round-table coordinator for CLI coding agents

## 1. Purpose

`roundtable` is a pure-script CLI (Python ≥3.11, stdlib only) that orchestrates
multiple CLI coding agents (Claude Code, Codex, Gemini CLI, Aider, …) as a
"round-table" of peers. The human user is an equally-weighted participant.
The coordinator itself contains NO model calls — it only spawns subprocesses,
moves markdown files around, and tracks state.

## 2. Core workflow (contract with the user)

1. Human asks a question: `roundtable ask "<question>"`.
2. Coordinator invokes every configured agent **in parallel**, each with a
   prompt that STRICTLY forbids writing/changing code and demands the answer
   as a comprehensive markdown file (analysis + proposed changes/plan).
   Output captured to `.roundtable/rounds/round-NNN/<agent>.md`.
3. Coordinator shows the human the list of MD files. Human reviews and writes
   their verdict into `.roundtable/rounds/round-NNN/human.md`
   (`roundtable review` opens `$EDITOR` with a seeded template).
4. `roundtable next` starts round N+1: each agent receives (a) the original
   question, (b) ALL other participants' latest MDs quoted under their
   anonymized peer labels ("Peer A", "Peer B", ... — the human's `human.md`
   is just another peer, with no special framing), and is asked to
   reconsider and respond — again strictly no writes, MD output only.
5. At any point after round ≥2 the human either:
   a. `roundtable next` → another discussion round, or
   b. `roundtable pick <agent>` → enter WRITE (single-agent) phase.
      Default behaviour: **"hand over the phone"** — the coordinator spawns the
      picked agent's *interactive* CLI attached to the user's terminal
      (resuming its session if supported), primed with a message containing the
      agent's own winning plan + human's latest comments + explicit permission
      to write code. The coordinator blocks until the interactive session
      exits, then records a git snapshot marker.
6. `roundtable table ["optional new framing question"]` returns to round-table
   mode: the coordinator collects `git diff` since the write phase began, and
   asks every OTHER agent for a review round (still no writes): their MD must
   critique the changes. The picked agent is asked for a worklog MD summarising
   what it did. After that, normal discussion rounds resume (step 3).

## 3. Project layout (deliverable)

```
/mnt/agents/output/roundtable/
├── SPEC.md                  # this file
├── README.md                # user docs: install, config, workflow walkthrough
├── pyproject.toml           # name=roundtable, console_script: roundtable=roundtable.cli:main
├── roundtable/
│   ├── __init__.py          # __version__
│   ├── __main__.py          # `python -m roundtable`
│   ├── cli.py               # argparse dispatch, all user-facing commands
│   ├── config.py            # TOML config load/validate/scaffold
│   ├── agents.py            # AgentSpec, command building, session-id extraction
│   ├── prompts.py           # all prompt templates (single place, easy to tweak)
│   ├── state.py             # StateStore: state.json + round dirs + md collection
│   ├── runner.py            # parallel subprocess execution, MD capture, fallback
│   └── handover.py          # interactive "phone handover" + git snapshot
└── tests/
    ├── fake_agent.sh        # configurable fake CLI agent for tests
    └── test_roundtable.py   # end-to-end: init→ask→review→next→pick→table
```

## 4. Configuration

File: `roundtable.toml` in the target project root (the repo the agents work on).
`roundtable init` scaffolds it with commented examples.

```toml
[settings]
state_dir = ".roundtable"      # relative to project root
max_diff_chars = 60000         # truncate git diff injected into review prompts
timeout_seconds = 1800         # per-agent round timeout
editor = ""                    # override $EDITOR

# Each agent: command template lists. Placeholders:
#   {prompt}  — substituted with the full prompt text (shell-quoted list arg)
#   {session} — session id from previous round (resume), if known
# If {prompt} is absent from args, the prompt is piped to stdin instead.
# session_regex: optional regex with one capture group applied to stdout+json
# to learn the agent's session id for later resume.
# readonly_args: optional extra argv appended to discussion-round (no-write)
# invocations ONLY — never to the interactive/relay write phase. This is the
# mechanism to enforce the no-write rule at the CLI level when the agent
# supports it.

[agents.claude]
command = "claude"
args_round_first  = ["-p", "{prompt}", "--output-format", "json"]
args_round_resume = ["--resume", "{session}", "-p", "{prompt}", "--output-format", "json"]
args_interactive_first  = ["{prompt}"]
args_interactive_resume = ["--resume", "{session}", "{prompt}"]
session_regex = '"session_id"\\s*:\\s*"([^"]+)"'
readonly_args = ["--permission-mode", "plan", "--disallowed-tools", "Write", "Edit", "NotebookEdit"]

[agents.codex]
command = "codex"
args_round_first  = ["exec", "--skip-git-repo-check", "{prompt}"]
args_round_resume = ["exec", "resume", "--skip-git-repo-check", "{session}", "{prompt}"]
args_interactive_first  = ["{prompt}"]
args_interactive_resume = ["resume", "{session}", "{prompt}"]
session_regex = 'session id:\\s*(\\S+)'
readonly_args = ["-c", 'sandbox_mode="read-only"']  # "exec resume" rejects --sandbox
```

Rules:
- Unknown placeholders → config error at load time with a clear message
  (checked in every args list, including `readonly_args`).
- An agent missing `args_interactive_*` falls back to `args_round_*` semantics
  minus the no-write constraint (coordinator warns).
- At least one agent required; two+ recommended (round-table with one agent is
  allowed but warns).

## 5. State & files

`.roundtable/` inside the target project:

```
.roundtable/
├── state.json
└── rounds/
    ├── round-001/
    │   ├── QUESTION.md        # the question for this discussion thread
    │   ├── claude.md          # agent outputs (exact filenames = agent names)
    │   ├── codex.md
    │   ├── human.md           # human's verdict (seeded template, $EDITOR)
    │   └── _meta.json         # per-agent: exit code, duration, session id, cmd
    └── round-002/ ...
```

`state.json` schema:

```json
{
  "version": 1,
  "mode": "discuss" | "awaiting_human" | "write",
  "round": 2,
  "picked_agent": null | "claude",
  "sessions": {"claude": "abc123"},
  "labels": {"claude": "Peer B", "codex": "Peer A", "human": "Peer C"},
  "write_phase": null | {
      "agent": "claude",
      "started_round": 2,
      "base_commit": "git rev-parse HEAD at pick time or null",
      "rounds_dir_snapshot": "rounds/round-002"
  },
  "history": [{"ts": "...", "event": "ask|round|review|pick|table", "detail": "..."}]
}
```

`labels` maps every participant (each agent + `"human"`) to a shuffled
"Peer X" label, drawn with `random.SystemRandom` at each `ask` (a new
discussion thread reshuffles). Anonymization lives purely in prompt content
— on-disk filenames stay `<agent>.md` / `human.md`. `status` shows the
mapping to the human user (the chair); agents only ever see the labels.

Mode meanings:
- `awaiting_human`: round MDs are ready, waiting for human.md + `next`/`pick`.
- `discuss`: human.md exists; `next` may run.
- `write`: single-agent phase; only `table` (or `status`) is meaningful.

## 6. Prompt templates (`prompts.py`)

All templates receive: question, agent_name, round_no, target paths, other MDs,
diff. Wording requirements:

- ROUND prompt (first round): restate question; "You are one of several AI
  agents in a round-table chaired by a human. DO NOT write, modify, or create
  any code or project files. Your ONLY allowed file write is your answer file
  at {answer_path} (if you cannot write files, print the full markdown to
  stdout). Be maximally comprehensive: analysis, options, concrete proposed
  changes (file-by-file), risks, open questions." Plus ground rules: the
  round-table has several participants identified only by peer labels; some
  are human, some are AI agents, and the agent does not know which is which —
  every position must be argued on its merits; a slow, careful, long document
  is welcome (it is meant to be read by a human at reading speed).
- RETHINK prompt (round ≥2): include original question + every other
  participant's previous MD quoted under its peer label — the human's
  human.md is just another peer entry, with no "human" framing; "Weigh each
  contribution on its reasoning alone. Reconsider, defend or revise; say
  plainly where you now think a peer is right and you were wrong. Convergence
  is the goal, but do not converge for the sake of converging — an
  unresolved disagreement, stated precisely, is a more useful outcome than a
  false agreement." Same no-write rule.
- REVIEW prompt (after write phase, for non-picked agents): include git diff
  (truncated to max_diff_chars, with `diff --stat` summary) + picked agent's
  worklog if present; "Review the changes made. Do NOT modify anything.
  Output review as MD: issues found, severity, suggested fixes."
- WORKLOG prompt (for picked agent after write phase): "Summarise what you
  changed and why" as MD; still no new writes.
- HANDOVER primer (interactive pick): winning plan MD + human's latest
  human.md + de-anonymization (which peer label the picked agent wrote
  under, plus the full label→participant mapping) + "You are now the single
  implementer. You MAY write code. The human is talking to you directly
  now."

## 7. Commands (CLI contract)

| Command | Behaviour |
|---|---|
| `roundtable init` | Scaffold `roundtable.toml` + `.roundtable/`; refuse to overwrite unless `--force`. |
| `roundtable ask "q"` | Start new discussion thread: round=1, run all agents in parallel (ROUND prompt), save MDs, mode→`awaiting_human`. Print where files are. |
| `roundtable review` | Seed `human.md` template for current round if absent; open `$EDITOR`; on exit mode stays until `next`/`pick`. `--no-edit` to skip editor. |
| `roundtable next` | Requires human.md non-empty (unless `--skip-human`). Round+1; RETHINK prompt with all other MDs + human.md; parallel run; mode→`awaiting_human`. |
| `roundtable pick <agent>` | Validate agent name + mode. Snapshot git HEAD. Hand over interactive session (HANDOVER primer, resume if session known). Block until exit. mode→`write`. `--relay` alternative: coordinator runs one non-interactive WRITE-allowed round instead (for headless use). |
| `roundtable table ["q"]` | Only from `write`. Compute git diff vs write_phase.base_commit. Picked agent gets WORKLOG prompt; others get REVIEW prompt (new round dir). mode→`awaiting_human`. |
| `roundtable status` | Pretty state: mode, round, agents, which files exist, last events. |
| `roundtable show [round]` | Print all MDs of a round (default latest) with headers, human.md last. |
| `roundtable agents` | List configured agents + resolved commands (for debugging config). |

Global flags: `--project DIR` (default cwd) to locate `roundtable.toml`.

## 8. Runner mechanics (`runner.py`)

- Parallel via `concurrent.futures.ThreadPoolExecutor`.
- Each run: build argv (shlex-joined config pieces, placeholder substitution),
  cwd = project root, capture stdout+stderr, enforce timeout. Discussion
  rounds append the agent's `readonly_args` to the resolved argv; the
  relayed write phase (`pick --relay`) omits them.
- MD capture priority: (1) file at instructed answer path if non-empty after
  exit; (2) else stdout written to that path (strip ANSI codes); record which
  in `_meta.json`.
- Session id: apply `session_regex` to stdout, store on success.
- Non-zero exit → keep whatever output exists, mark `_meta.json`, do NOT crash
  the whole round; report failures in the summary.
- Concurrency guard: `roundtable` refuses to start a round if a
  `.roundtable/LOCK` file exists (stale-lock override `--force`).

## 9. Handover (`handover.py`)

- Spawn interactive argv with `subprocess.call` inheriting stdin/stdout/stderr
  (the human literally talks to the agent in their terminal).
- Primer message is passed as the agent's initial prompt argument
  (args_interactive_* contain `{prompt}`).
- Before spawn: print a clear banner ("You are now talking directly to
  <agent>. Exit the session to return to the coordinator.").
- After exit: record return code, git HEAD, append history event.

## 10. Testing (must pass before delivery)

`tests/test_roundtable.py` (unittest, stdlib) uses `tests/fake_agent.sh`:
a bash script whose behaviour is driven by env vars / args: it prints a
session line (`session id: fake-<agent>-<n>`), prints markdown to stdout,
and — when told the answer path — writes the MD file directly. It also
appends invocations to a log file so tests can assert prompts contained the
right context (e.g. round-2 prompts must contain round-1 content of other
agents and human.md; review prompts must contain the git diff).

End-to-end scenario asserted:
init → ask (2 fake agents) → human.md written by test → next (verify
cross-pollination) → write a code file + git commit in a temp repo →
pick --relay (headless) → table (verify diff injection + worklog) →
status/show outputs sane. Also: timeout handling, missing-human.md guard,
bad agent name, lock file behaviour.

## 11. Non-goals

- No model/API calls inside the coordinator.
- No TUI/web UI (plain terminal; MD files are the UI).
- No automatic merging of agent proposals.
- Git is required only for `pick`/`table` diff features; degrade gracefully
  with a warning (skip diff injection) outside a git repo.
