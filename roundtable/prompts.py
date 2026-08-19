"""All prompt templates, in a single place so they are easy to tweak."""

from __future__ import annotations

_NO_WRITE_RULE = """\
IMPORTANT — READ CAREFULLY:
You are one of several AI agents in a round-table chaired by a human.
DO NOT write, modify, or create any code or project files.
Your ONLY allowed file write is your answer file at {answer_path}
(if you cannot write files, print the full markdown to stdout).
ANSWER FILE: {answer_path}
"""


def _no_write(answer_path: str) -> str:
    return _NO_WRITE_RULE.format(answer_path=answer_path)


def round_prompt(question: str, agent_name: str, round_no: int, answer_path: str) -> str:
    """First-round prompt: answer the question, analysis/plan only."""
    return f"""\
# Round-table discussion — round {round_no}

You are the agent "{agent_name}".

The human chair asks the following question:

> {question}

This round-table has several participants, identified only by peer labels
("Peer A", "Peer B", ...). Some participants are human and some are AI
agents; you do not know which is which, and you should not try to work it
out — every position must be argued on its merits.

{_no_write(answer_path)}
Answer the question as a comprehensive markdown document.
Be maximally comprehensive: analysis, options, concrete proposed changes
(file-by-file), risks, open questions. A slow, careful, long document is
welcome — it is meant to be read by a human at reading speed.
"""


def rethink_prompt(
    question: str,
    agent_name: str,
    round_no: int,
    answer_path: str,
    peer_mds: dict[str, str],
) -> str:
    """Round >= 2 prompt: the other participants' MDs under peer labels.

    ``peer_mds`` maps anonymized peer labels ("Peer A", ...) to markdown —
    the human's entry is just another peer, with no special framing.
    """
    sections: list[str] = []
    for label, text in peer_mds.items():
        sections.append(
            f"## Response of {label} (previous round)\n\n"
            f"> " + text.strip().replace("\n", "\n> ")
        )
    peers_block = (
        "\n\n".join(sections) if sections else "(no other responses this round)"
    )
    return f"""\
# Round-table discussion — round {round_no}

You are the agent "{agent_name}".

The original question from the chair was:

> {question}

{peers_block}

Every contribution above is identified only by a peer label: some
participants are human and some are AI agents, and you do not know which
is which. Weigh each one on its reasoning alone. Reconsider, defend or
revise. Say plainly where you now think a peer is right and you were
wrong, where you still disagree and exactly why, and what evidence would
change your mind. Convergence is the goal, but do not converge for the
sake of converging — an unresolved disagreement, stated precisely, is a
more useful outcome than a false agreement.

{_no_write(answer_path)}
Respond again as a comprehensive markdown document: updated analysis,
concrete proposed changes (file-by-file), risks, open questions, and
explicit remaining disagreements.
"""


def review_prompt(
    agent_name: str,
    round_no: int,
    answer_path: str,
    diff_stat: str,
    diff: str,
    worklog: str | None,
    framing_question: str | None = None,
    worklog_label: str = "Worklog of the implementing agent",
) -> str:
    """Post-write-phase review prompt for the non-picked agents."""
    framing = (
        f"\nThe human reframes the discussion as:\n\n> {framing_question}\n"
        if framing_question
        else ""
    )
    worklog_block = (
        f"## {worklog_label}\n\n> "
        + worklog.strip().replace("\n", "\n> ")
        if worklog
        else "(no worklog available yet)"
    )
    diff_block = diff if diff else "(no git diff available — not a git repo or no changes)"
    return f"""\
# Round-table review — round {round_no}

You are the agent "{agent_name}". Another agent was picked to implement the
agreed plan and has made changes to the project.
{framing}
## Summary of changes (git diff --stat)

```
{diff_stat or "(unavailable)"}
```

## Full diff (possibly truncated)

```diff
{diff_block}
```

{worklog_block}

Review the changes made. Do NOT modify anything.
Output review as MD: issues found, severity, suggested fixes.

{_no_write(answer_path)}
"""


def worklog_prompt(
    agent_name: str,
    round_no: int,
    answer_path: str,
    diff_stat: str,
    diff: str,
) -> str:
    """Post-write-phase worklog prompt for the picked agent."""
    diff_block = diff if diff else "(no git diff available — not a git repo or no changes)"
    return f"""\
# Round-table worklog — round {round_no}

You are the agent "{agent_name}". You were picked as the single implementer
and made changes to the project during the write phase.

## Summary of changes (git diff --stat)

```
{diff_stat or "(unavailable)"}
```

## Full diff (possibly truncated)

```diff
{diff_block}
```

Summarise what you changed and why, as a markdown worklog: what was done,
design decisions taken, what is left TODO, and anything the other agents
should pay attention to when reviewing. Do NOT write any new code now —
your ONLY allowed file write is your answer file at {answer_path}
(if you cannot write files, print the full markdown to stdout).
ANSWER FILE: {answer_path}
"""


def handover_primer(
    agent_name: str,
    plan_md: str,
    human_md: str,
    question: str,
    labels: dict[str, str] | None = None,
) -> str:
    """Primer message for the interactive (or relayed) write phase.

    ``labels`` (participant -> peer label) de-anonymizes the deliberation:
    the picked agent learns which peer label was its own and who the other
    peers were.
    """
    plan_block = plan_md.strip() if plan_md.strip() else "(no plan on file)"
    human_block = human_md.strip() if human_md.strip() else "(no human comments on file)"
    deanon_block = ""
    if labels:
        mapping = "\n".join(
            f"- {label}: {name}"
            for name, label in sorted(labels.items(), key=lambda kv: kv[1])
        )
        deanon_block = f"""\
## De-anonymization

During the deliberation you wrote {labels.get(agent_name, '?')}'s entries.
The peer labels are now revealed:

{mapping}

The human chair was one of the peers; from here you are talking to them
directly.

"""
    return f"""\
# Write phase — you are the implementer

You are the agent "{agent_name}". The human picked YOU to implement the plan.

The original question was:

> {question}

## Your winning plan

{plan_block}

## The human's latest comments

{human_block}

{deanon_block}You are now the single implementer. You MAY write code.
The human is talking to you directly now.
"""


def human_template(question: str, round_no: int, agent_names: list[str]) -> str:
    """Seeded template for human.md (HTML comments are stripped by checks)."""
    agents = ", ".join(agent_names)
    return f"""\
<!--
  Human verdict — round {round_no}

  Write your verdict below this comment block, then save and close.

  Question under discussion:
    {question}

  Agents this round: {agents}

  Suggested content:
  - which proposal(s) you agree/disagree with, and why
  - corrections, priorities, constraints the agents must respect
  - what you want the next round to focus on

  This file is fed VERBATIM to every agent in the next round and carries
  the same weight as any agent's response. Lines inside HTML comments
  (like this one) are ignored by the emptiness check.
-->

"""


def write_relay_prompt(primer: str, answer_path: str) -> str:
    """Non-interactive variant of the handover primer (pick --relay)."""
    return f"""\
{primer}
NOTE: this is a non-interactive relayed write phase run by the coordinator.
Do the implementation work now (you MAY write code), then report what you
did as markdown: either write it to your answer file at {answer_path} or
print it to stdout.
ANSWER FILE: {answer_path}
"""
