# My work

What I actually did, in order, and what I found. Everything here can be checked against the run
data in [`../runs/baseline-practice-10-v2/`](../runs/baseline-practice-10-v2/) and my labels in
[`annotations/`](annotations/).

**How the work was split.** I chose the problem, set up and debugged the environment, specified
the trace viewer phase by phase, verified each phase against the raw data, and did the error
analysis. The trace viewer's code was written by Claude Code from my specifications. I used Claude
as a reviewer to check the viewer's numbers independently and to challenge my labels; where it
corrected me, that is written down below.

## Why

My goal is to become a Forward Deployed Engineer. Before this I spent two weeks reading how agents
are built: workflows vs agents, harnesses, context engineering, tool design, evals and agent
security, plus real FDE case studies. This project is where I apply that: build an agent, measure
it, find out why it fails, and fix it with explicit engineering decisions.

## 1. Choosing the benchmark

τ²-bench, retail domain: a fake online store (500 customers, 1,000 orders, 50 product types), a
written policy, about 15 tools, and AI-simulated customers. A task passes only if the store
database ends up exactly as expected, plus (for some tasks) an LLM judge confirms the agent told
the customer the right thing.

Rules I set for myself:
- Same agent model, same customer model, same judge and same tasks in every run. Only the agent
  design changes, so any improvement comes from design, not a bigger model.
- A fixed practice set of 10 training tasks: `0 1 2 3 4 6 7 8 10 11`.
- The 40 held-out test tasks are used once, at the end.

## 2. Getting it to run (1 Nov 2026)

| Problem | What I found | What I did |
|---|---|---|
| Groq free tier failed instantly | One agent request (policy + tool list + conversation) is ~4,000 tokens; the limit was 8,000 tokens per minute, and later requests grow past it | Switched to local models with Ollama |
| Local setup | Agent `gpt-oss:20b`, customer `gemma4:12b` (a different model family, so the two sides don't "think alike") | Raised Ollama's context length to 32k so prompts aren't silently cut |
| Run crashed on Windows | Folder names come from model names; Ollama names contain `:`; Windows forbids `:` in file names | Pass `--save-to` for every run (bug noted for a possible upstream report) |
| Ollama "connection refused" mid-run | I had restarted Ollama during a run | Don't restart Ollama while running |
| "Incorrect API key" on some tasks after ~10 minutes | A hidden third model: the LLM judge, hard-coded to paid OpenAI models | Pointed the judge at local `gemma4:12b` ([details](../benchmark-changes/README.md)) |
| "Expecting value: line 1 column 1" | The local judge wrapped its JSON in a markdown fence | 3-line parsing fix ([details](../benchmark-changes/README.md)) |

## 3. Baseline run (`baseline-practice-10-v2`, 1–2 Nov 2026)

The benchmark's default agent (`llm_agent`) on the 10 practice tasks, 1 trial each, about 7–36
minutes per conversation on my laptop.

**Result: 3 of 9 scored tasks passed (33%).** Task 8 was an infrastructure error (an Ollama call
timed out after 600 s) and is excluded.

## 4. Building a trace viewer (2 Nov 2026)

Reading raw logs was too slow to do real error analysis, so I specified a local viewer and had it
built in three phases, testing each before starting the next.

1. **Runs and conversations:** every run, every conversation as a timeline, every raw model call
   (what was sent, what came back, tokens, time).
2. **Back to the source:** a database explorer, every ID clickable to its record, and the final
   database (expected vs actual) replayed with the benchmark's own evaluator.
3. **Design and analysis:** the agent's design written as 18 named decisions (D1–D18) with links
   to the source lines; failure labels linked to design decisions; automatic checks; run
   comparison; live updates; a run recorder that saves the command, note, design version and code
   diff with each run.

How I checked it (with Claude as an independent reviewer):
- The benchmark code is unchanged apart from my two documented edits (checked with git).
- The viewer's numbers match an independent recount of the raw data: 3/9 passed, 77 tool calls,
  14 tool errors.
- The database replay agrees with the official database check on all 8 scored tasks.
- The design file's key decisions match the agent's real source code.
- The automatic identity check flags exactly tasks 0 and 1.

## 5. Error analysis

I labelled each task, then compared my labels with an independent review against the data. Some
of my first labels were wrong, and the corrections taught me the most:

- I thought the agent looked up **too many** orders. The data showed the opposite: a customer
  record lists order IDs **without status**, so every order must be opened. In task 3 the agent
  opened only 2 of 5 orders, missed the pending one with the small v-neck t-shirt, and said the
  item didn't exist.
- I marked task 4 as "wrong changes applied". The database check passed: the changes were right.
  Only its message was wrong. Lesson: judge actions by the database, not by what the agent says.
- I said task 11 "allowed" a refund to a different payment method. The agent tried, but the tool
  rejected it, and it then did it correctly: a rule enforced in code worked.
- My own catch that the review had missed: in task 1 the agent chose the payment method itself,
  but the policy says the customer must provide it. The score doesn't notice, because the expected
  answer uses the same card.

### Final labels

| Task | Result | Labels | Design decisions |
|---|---|---|---|
| 0 | pass | skipped-identity-check | D3 |
| 1 | fail | skipped-identity-check, wrong-variant-choice, assumed-customer-detail | D3, D14, D15, D11 |
| 2 | fail | misread-tool-output (said 12 t-shirt options, 10 available), malformed-tool-call | D14, D6, D13, D18 |
| 3 | fail | incomplete-lookup | D15, D17 |
| 4 | fail | misread-tool-output | D14 |
| 6 | stopped | malformed-tool-call, ignored-customer-change | D6, D13, D18, D15 |
| 7 | fail | wrong-variant-choice | D14, D15 |
| 8 | infra | setup-problem | – |
| 10 | pass | – | – |
| 11 | pass | attempted-policy-violation (blocked by the tool) | D16 |

### What's breaking (failures per design decision)

| Decision | Failures |
|---|---|
| D14: reading tool results is left to the model | 4 |
| D15: no record of the customer's conditions or a plan | 4 |
| D6 / D13 / D18: tool calls not validated, duplicates not stopped | 2 |
| D3: identity verification is prompt-only | 1 (+1 passing task) |
| D11: confirmation is prompt-only | 1 |
| D17: escalation is prompt-only | 1 |

Problems the official score does not catch: skipped identity verification (task 0 passed anyway)
and an agent-chosen payment method (task 1).

## 6. Next

Design v1: for D14 and D15 first, move what can be decided by code out of the model's hands
(filtering and counting product variants, tracking the customer's requests and conditions), then
identity and tool-call checks. One change at a time, re-measured on the same 10 tasks, then a
single final run on the held-out tasks.
