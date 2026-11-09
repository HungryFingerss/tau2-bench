# Study design

## Question

How much can a customer-support agent improve through **design decisions alone** (same model,
same tasks), and which decisions matter most?

## Benchmark

[τ²-bench](https://github.com/sierra-research/tau2-bench) by Sierra Research, retail domain,
English, text mode, at upstream commit `da0835f6c67baef58bdda18f2a3b037dfb3a1754`.
A task's reward is 1 only if the final store database equals the database produced by replaying
a reference solution, and (where defined) an LLM judge confirms what the agent told the customer.

## Setup (fixed for every run)

| Role | Model | Notes |
|---|---|---|
| Agent | `ollama_chat/gpt-oss:20b` | the only role whose design changes |
| Simulated customer | `ollama_chat/gemma4:12b` | different family from the agent |
| LLM judge | `ollama_chat/gemma4:12b` | local; see `../benchmark-changes/` |

- Hardware: a Windows laptop running Ollama, context length 32,768.
- `--num-trials 1 --max-concurrency 1`, default seed.
- Practice set (train split): tasks `0 1 2 3 4 6 7 8 10 11`. Held-out test split (40 tasks) is run once, at the end.

## What is measured

- **Pass rate**, excluding infrastructure errors (as the benchmark does).
- **Reliability (pass^k):** planned, by repeating tasks.
- **Checks the score does not cover**, computed in code: identity verified before any account
  lookup, duplicate tool calls, invalid tool calls, tool errors, writes without an explicit "yes".
- **Failure labels**, assigned by hand and linked to the agent's design decisions (see
  `designs/` and `annotations/`).
- Time per conversation and tool calls per conversation.

## Method

1. Baseline with the benchmark's default agent (`designs/v0-baseline.yaml`).
2. Error analysis: read every failure in the trace viewer, label it, link it to design decisions.
3. Design the next version from the ranking, one decision at a time; record each run with
   `record_run.cmd` (command, note, design version, code diff).
4. Compare runs task by task. Keep a change only if it helps on the practice set without breaking
   other tasks.
5. Final measurement once on the held-out test tasks.

## Results so far

Final results, 9 Nov 2026 (the project stopped here; details in `MY_WORK.md`, sections 8–10):

| Run | Design | Passed | Notes |
|---|---|---|---|
| baseline-practice-10-v2 | v0-baseline | 3 / 9 (33%) | 1 infra error (task 8) |
| v1-practice-10 | v1-fde-agent, round 1 | 3 / 9 | task 4 unscored (customer timeouts) |
| v1-practice-10-b | v1-fde-agent, round 2 | 6 / 9 | in-sample: fixes came from these tasks; task 0 unscored |
| v1-unseen-10 | v1-fde-agent, round 2 | 6 / 10 | tasks 13–24 the fixes had never seen; no baseline run |
| baseline-fresh-10 | v0-baseline | 1 / 5 | 5 of 10 tasks unscored (customer timeouts) |
| v1-fresh-10 | v1-fde-agent, round 3 | 3 / 6 | 4 of 10 unscored; on the 4 tasks scored in both runs: baseline 1, mine 2 |

## Limitations

- **Not comparable with the official leaderboard:** local agent, customer and judge models.
- **Small sample:** 9 scored tasks, 1 trial each. Single-task differences can be luck.
- **Local judge:** checked by hand on 3 verdicts (all correct); still weaker than the official judge.
- **Simulated customers** with a small local model sometimes behave unusually; failures are checked
  in the conversation before being blamed on the agent.
- **The customer model decides which tasks are scored.** On the fresh tasks it timed out (600 s) in
  23 of the baseline's 24 failed attempts and 21 of my agent's 22, sometimes on its first message;
  5 and 4 of the 10 tasks couldn't be scored. Comparisons use only tasks scored in both runs.
- **The 40 held-out test tasks were never run.**
- Two benchmark files were changed (judge model, judge-reply parsing); both are documented.
