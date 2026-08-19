# roundtable

A script-driven **round-table coordinator for CLI coding agents** (Claude Code,
Codex, Gemini CLI, Aider, …). Several agents discuss your question as peers —
**anonymized as "Peer A", "Peer B", …, so the human participant is
indistinguishable from the agents** — and when consensus emerges you pick one
agent to actually write code — then bring the diff back to the table for
review.

The coordinator itself contains **no model calls**. It only spawns
subprocesses, moves markdown files around, and tracks state. Python ≥ 3.11,
standard library only.

## Install

```bash
# from this directory
pip install .          # provides the `roundtable` command

# or run without installing
python -m roundtable --help
```

Requires: Python ≥ 3.11, `git` (only for `pick`/`table` diff features; the
tool degrades gracefully with a warning outside a git repo), and at least one
real CLI coding agent installed and configured.

## Quick start

```bash
cd your-project
roundtable init                       # scaffold roundtable.toml + .roundtable/
$EDITOR roundtable.toml               # configure your agents (see below)
roundtable ask "How should we refactor the auth module?"
# ... review .roundtable/rounds/round-001/*.md ...
roundtable review                     # writes your verdict into human.md ($EDITOR)
roundtable next                       # round 2: agents see each other + your verdict
roundtable pick claude                # hand over the phone: interactive write phase
roundtable table                      # back to the table: others review the diff
roundtable status                     # where are we?
roundtable show                       # print the latest round's markdown
```

## Configuration

`roundtable.toml` lives in the target project root. `roundtable init`
scaffolds a commented example. Placeholders in argument lists:

- `{prompt}` — substituted with the full prompt text (a single argv element).
  If `{prompt}` is absent from the args, the prompt is piped to the process'
  **stdin** instead.
- `{session}` — the agent's session id from the previous round (for resume),
  if known. `session_regex` (one capture group, applied to stdout) teaches the
  coordinator how to learn it.
- `readonly_args` — extra argv elements **appended to discussion-round
  invocations only** (never to the interactive or `--relay` write phase).
  This enforces the no-write rule at the CLI level when the agent supports
  it; see the claude example below.

```toml
[settings]
state_dir = ".roundtable"      # relative to project root
max_diff_chars = 60000         # truncate git diff injected into review prompts
timeout_seconds = 1800         # per-agent round timeout
editor = ""                    # override $EDITOR

[agents.claude]
command = "claude"
args_round_first  = ["-p", "{prompt}", "--output-format", "json"]
args_round_resume = ["--resume", "{session}", "-p", "{prompt}", "--output-format", "json"]
args_interactive_first  = ["{prompt}"]
args_interactive_resume = ["--resume", "{session}", "{prompt}"]
session_regex = '"session_id"\s*:\s*"([^"]+)"'
readonly_args = ["--permission-mode", "plan", "--disallowed-tools", "Write", "Edit", "NotebookEdit"]

[agents.codex]
command = "codex"
args_round_first  = ["exec", "--skip-git-repo-check", "{prompt}"]
args_round_resume = ["exec", "resume", "--skip-git-repo-check", "{session}", "{prompt}"]
args_interactive_first  = ["{prompt}"]
args_interactive_resume = ["resume", "{session}", "{prompt}"]
session_regex = 'session id:\s*(\S+)'
readonly_args = ["-c", 'sandbox_mode="read-only"']  # "exec resume" rejects --sandbox
```

Rules enforced at load time:

- Unknown placeholders → config error with a clear message (checked in every
  args list, including `readonly_args`).
- An agent missing `args_interactive_*` falls back to `args_round_*` semantics
  minus the no-write constraint (coordinator warns).
- At least one agent is required; a single-agent "round-table" is allowed but
  warns. Two or more agents are recommended.

Use `roundtable agents` to see the resolved command lines for debugging.

## Workflow walkthrough

1. **Ask.** `roundtable ask "<question>"` starts a new discussion thread:
   round 1. Every configured agent is invoked **in parallel** with a prompt
   that strictly forbids writing/changing code and demands a comprehensive
   markdown answer (analysis, options, file-by-file proposals, risks, open
   questions). Answers land in `.roundtable/rounds/round-001/<agent>.md`,
   either captured from the answer file the agent wrote or from its stdout.
   Each `ask` also shuffles fresh **peer labels** ("Peer A", "Peer B", …)
   for every participant — agents *and* you — stored in `state.json`.
2. **Review.** Read the MD files, then `roundtable review` seeds
   `round-001/human.md` with a template and opens `$EDITOR`. Your verdict is
   fed to every agent next round — quoted under your peer label, exactly
   like any agent's entry.
3. **Next round.** `roundtable next` runs round N+1: each agent receives the
   original question and **all other participants' latest MDs quoted under
   their peer labels** — no agent names, and your `human.md` is just another
   peer with no special framing. Agents are told to weigh every contribution
   on its reasoning alone, to say plainly where a peer is right and they were
   wrong, and *not* to converge for the sake of converging. Still strictly
   no writes.
4. **Pick.** When a plan wins, `roundtable pick <agent>` enters the single-agent
   **write phase**. By default the coordinator *hands over the phone*: it
   spawns the picked agent's interactive CLI attached to your terminal
   (resuming its session when known), primed with the winning plan + your
   latest comments + **the de-anonymized peer-label mapping** (the agent
   learns which peer label it wrote under) + explicit permission to write
   code, and blocks until you exit the session. A git snapshot marker
   (`base_commit`) is recorded.
   For headless use, `roundtable pick <agent> --relay` runs one
   non-interactive write-allowed round instead.
5. **Table.** `roundtable table ["optional new framing"]` returns to
   round-table mode: the coordinator computes `git diff` since the write phase
   began (plus `diff --stat`), asks every **other** agent for a review round
   (issues, severity, suggested fixes — still no writes), and asks the picked
   agent for a worklog MD. After that, normal discussion rounds resume
   (step 3): review the round MDs, write `human.md`, `next` or `pick` again.

At any time: `roundtable status` (mode, round, sessions, files, last events),
`roundtable show [round]` (all MDs of a round, human.md last).

## Your seat at the table

- **You are anonymous.** In round ≥2 prompts your `human.md` is quoted as
  `## Response of Peer <X> (previous round)` — same formatting as every
  agent, with no "human" framing. Only you can see the label mapping
  (`roundtable status`); agents never do, until `pick` de-anonymizes. Note
  that anonymity hides *who* you are, not *how you write*: "as a human I
  think…" blows your cover regardless of what the coordinator does.
- **No comments? Skip straight to the next round.** `roundtable next`
  refuses to run with an empty `human.md`; pass `--skip-human` to proceed
  anyway. Your peer entry is then omitted from the prompts entirely — an
  explicit "the human said nothing" placeholder would itself be special
  framing.
- **`human.md` is not just for verdicts.** Questions, doubts, "Peer C's
  claim about X looks wrong — check Y" are all valid content. Whatever you
  write is fed verbatim to every agent next round under your peer label,
  weighted the same as any agent's entry.
- **Agents see each other.** Every RETHINK prompt quotes *all other*
  participants' latest MDs in full under their peer labels (sorted by label
  for a deterministic layout), alongside the original question and the
  anti-false-consensus instructions. An agent's own previous answer is not
  re-quoted — it lives in that agent's resumed session memory instead.

## State and files

```
.roundtable/
├── state.json            # version/mode/round/picked_agent/sessions/labels/write_phase/history
├── LOCK                  # concurrency guard (override a stale one with --force)
└── rounds/
    └── round-001/
        ├── QUESTION.md   # the question for this discussion thread
        ├── claude.md     # agent outputs (filename == agent name)
        ├── codex.md
        ├── human.md      # your verdict
        └── _meta.json    # per-agent: exit code, duration, session id, cmd
```

Modes: `awaiting_human` (round MDs ready, waiting for human.md +
`next`/`pick`), `discuss` (human.md exists; `next` may run), `write`
(single-agent phase; only `table`/`status` meaningful).

Anonymization lives purely in prompt content — on-disk filenames stay
`<agent>.md` / `human.md`. `roundtable status` shows the peer-label mapping
to you (you chair the table); the agents only ever see the labels.

Runner mechanics: parallel via `ThreadPoolExecutor`; per-agent timeout from
config; MD capture prefers a non-empty answer file, else stdout (ANSI codes
stripped); a failing agent never crashes the round — it is marked in
`_meta.json` and reported in the summary.

## Testing

```bash
python -m unittest discover -s tests -v
```

The suite drives the real CLI (`python -m roundtable --project <tmpdir>`)
against `tests/fake_agent.sh`, a configurable fake agent that prints
`session id: fake-<name>-<n>`, emits markdown, logs every invocation, and can
simulate sleeps/failures. It covers the full
init → ask → human.md → next (cross-pollination) → pick --relay → commit →
table (diff injection + worklog) → status/show scenario, plus timeout,
missing-human.md, bad agent name, lock-file, and config-validation edge cases.

## Caveats

- **Agent CLIs vary — a lot.** Flags for non-interactive mode, resume, and
  output formats differ between tools *and between versions of the same tool*.
  The example configs above are starting points: **you must adapt the command
  templates to your installed CLIs.** Run `roundtable agents` to inspect the
  resolved argv, and test each agent manually first.
- Some agents ignore "do not write files" instructions. When the agent's CLI
  supports it, enforce the no-write rule at the capability level via
  `readonly_args` (see the claude example above: `--permission-mode plan`
  plus `--disallowed-tools` strips the write tools during discussion rounds;
  the flags are never passed to the write phase). For CLIs without such
  flags, the prompt-only rule remains the fallback — commit before rounds if
  you want a clean safety net (`git status` is your friend).
- Session resume depends on `session_regex` matching the agent's actual
  stdout. If it stops matching, rounds still work — the agent simply starts a
  fresh session each time (losing conversational memory).
- The interactive handover inherits your terminal; it does not work over
  non-TTY contexts. Use `pick --relay` for headless/CI use.
- Git is required only for `pick`/`table` diff injection; outside a git repo
  those features degrade to a warning and the diff sections are omitted.
