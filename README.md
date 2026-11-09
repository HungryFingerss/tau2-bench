# Agent on τ²-bench: error analysis, a trace viewer and my own agent

I'm building a customer-support AI agent and measuring it honestly on
[τ²-bench](https://github.com/sierra-research/tau2-bench) (Sierra Research), retail domain.
This repository contains **only my work**: the trace viewer, my error analysis, my own agent, the
run data and the small changes I had to make to the benchmark. The benchmark itself belongs to
Sierra Research and is not copied here.

**Status: finished (9 Nov 2026).** My agent went from the baseline's 3 of 9 to 6 of 9 on the practice
tasks (fixes found on those tasks) and 6 of 10 on tasks it had never seen. On the last 10 fresh tasks
the local simulated customer timed out so often that only 4 tasks were scored for both agents (baseline 1,
mine 2). I stopped there: each round now costs a day of laptop time, and the customer model decides
more of the result than the agent does. What I learned and why I stopped: `MY_WORK.md`, sections 9–10.

**Start here → [`trace_viewer/`](trace_viewer/)**

| Read | What it is |
|---|---|
| [`trace_viewer/MY_WORK.md`](trace_viewer/MY_WORK.md) | Exactly what I did, step by step, and what I found |
| [`fde_agent/`](fde_agent/) | My own agent, built from scratch: 7 tools, the agent loop, 27 design decisions enforced in code, 263 tests without a model. See MY_WORK.md, sections 6–8 |
| [`trace_viewer/designs/v1-fde-agent.yaml`](trace_viewer/designs/v1-fde-agent.yaml) | My agent's design: every decision, linked to its code and to the baseline decision it replaces |
| [`trace_viewer/STUDY.md`](trace_viewer/STUDY.md) | The study design: setup, rules, metrics, final results, limitations |
| [`trace_viewer/README.md`](trace_viewer/README.md) | How to run the trace viewer |
| [`runs/`](runs/) | Every run behind the results: all conversations, raw model calls, and my agent's internal log for each conversation |
| [`trace_viewer/annotations/`](trace_viewer/annotations/) | My failure labels for the baseline run, linked to design decisions |
| [`benchmark-changes/`](benchmark-changes/) | The only two changes I made to the benchmark, and why |

![Runs](trace_viewer/docs/runs.png)

![Tools page: running my agent's tools by hand](trace_viewer/docs/tools.png)

## Use it

1. Clone τ²-bench at the commit this work used:
   ```
   git clone https://github.com/sierra-research/tau2-bench
   cd tau2-bench
   git checkout da0835f6c67baef58bdda18f2a3b037dfb3a1754
   uv sync
   ```
2. Copy this repository's `trace_viewer/` folder into the `tau2-bench` folder.
3. To browse my runs, copy the folders in `runs/` into `tau2-bench/data/simulations/`.
4. Start the viewer from the `tau2-bench` folder (Windows): `.\trace_viewer\start.cmd`
5. To reproduce my runs with local judges, apply `benchmark-changes/local-judges.patch` (see its README).
6. To run my agent's tests, copy `fde_agent/` into the `tau2-bench` folder, then:
   `uv run --with pytest python -m pytest fde_agent/tests -v`
7. To run my agent on the benchmark: `uv run python -m fde_agent.run run --domain retail --agent fde_agent ...`
   (or `.\trace_viewer\record_run.cmd --design v1-fde-agent ...`, which also saves the run's details).

## Credit

τ²-bench is © Sierra Research, MIT License (copy in `benchmark-changes/TAU2-BENCH-LICENSE`).
The trace viewer was specified, reviewed and verified by me and implemented with AI coding
assistance (Claude Code). My agent's design decisions are mine; its code was written by Claude from
specs we agreed piece by piece, and I reviewed and ran everything. Details in `MY_WORK.md`.
