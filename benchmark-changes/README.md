# Changes I made to τ²-bench

Upstream commit used: `da0835f6c67baef58bdda18f2a3b037dfb3a1754` (sierra-research/tau2-bench, main, 29 Oct 2026).

Only two files were changed. Nothing else in `src/` or `data/` was touched. Apply with
`git apply local-judges.patch` from the `tau2-bench` folder.

## 1. Judge models pointed at a local model (`src/tau2/config.py`)

Some tasks are graded partly by an LLM judge ("did the agent tell the customer X?"). The judge
models are hard-coded to paid OpenAI models (`gpt-5.4-mini`, `gpt-5.5`) with no command-line
option. I run everything locally, so I pointed both judges at `ollama_chat/gemma4:12b`.

- Not the agent's model (`gpt-oss:20b`), so the agent is not grading itself.
- **Consequence:** scores are not comparable with the official leaderboard. They are comparable
  across my own runs, because the judge stays fixed for the whole study.
- I checked the judge by hand on the baseline: 3 of 3 verdicts agreed with what the agent actually said.

## 2. Tolerant parsing of the judge's reply (`src/tau2/evaluator/evaluator_nl_assertions.py`)

The local judge returns correct JSON wrapped in a markdown code fence. The original code calls
`json.loads` on the whole reply and crashes. The fix reads from the first `{` to the last `}`.
The benchmark's other judges already extract JSON this way; this one did not.

## Known issue found, not changed

On Windows, run folder names are built from model names, and Ollama names contain `:`, which
Windows forbids in file names (`WinError 123`). Workaround: always pass `--save-to <name>`.
The cause is `make_run_name` in `src/tau2/runner/helpers.py`.
