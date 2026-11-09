# fde_agent_log.json

Written by `fde_agent/agent.py` after every turn of a conversation, into that
conversation's artifacts folder, next to the benchmark's `llm_debug/` folder:

    data/simulations/<run>/artifacts/task_<id>/sim_<simulation id>/fde_agent_log.json

It is rewritten whole each turn (write-then-rename), so it can be read while a run is
going. The benchmark's `results.json` only has the store calls; this file has
everything the agent did in between.

## Top level

| field | meaning |
|---|---|
| `agent` | always `"fde_agent"` |
| `model` | e.g. `"ollama_chat/gpt-oss:20b"` |
| `llm_args` | settings for every model call, e.g. `{"temperature": 0.0, "seed": 300}` |
| `instructions_sha256` | fingerprint of `fde_agent/instructions.md` as it was during the run |
| `events` | the list below, in order |

## Events

Every event has `n` (1, 2, 3, ... in order), `time` (Unix seconds) and `kind`.

| kind | extra fields | what happened |
|---|---|---|
| `agent` | `text`, `by` | a message to the customer. `by`: `"model"` (the model wrote it), `"code"` (code sent it: a change summary, the "Done. This change is made" message after a change, the transfer sentence, the safe message) or `"benchmark"` (the opening greeting) |
| `customer` | `text` | a message from the customer |
| `model_call` | `generation_time_seconds`, `usage`, `cost`, `hidden` | one real call to the model. `hidden`: how many entries of the history were left out of what the model saw for this call (failed steps after the model recovered, F26). `generation_time_seconds` equals the field of the same name in that call's `llm_debug/*_agent_response_*.json` file: that is how to find its raw call |
| `tool_call` | `id` (`tc-N`), `tool`, `args` | the model called one of OUR 7 tools |
| `tool_result` | `id` (same `tc-N`), `tool`, `ok`, `text`, and `fact` when not ok | what that tool returned: exactly what the model saw. `fact`: the error states a fact about the store or the rules; after the model recovers, only such facts stay in its view (as a note "Fact from an earlier failed call: ...") |
| `store_calls` | `tool`, `calls` = {`fde-N`: {`name`, `args`}} | store calls our tool sent through the benchmark. The `fde-N` IDs are the tool-call IDs in `results.json` |
| `store_results` | `tool`, `results` = {`fde-N`: {`content`, `error`}} | the store's answers |
| `note` | `text`, and `failed: true` for notes about a failed step | something code told the model (not the customer), e.g. "The summary of change #1 was shown…" or "Your reply was not sent…". Notes with `failed` are left out of the model's view once it has recovered |
| `invalid_call` | `error` | CHECK: a model reply rejected before anything ran (unknown tool, bad inputs, …) |
| `fixed` | `what` | CHECK: something code fixed on the way (junk cut from a tool name, a number turned into text, bold removed, text dropped from a reply that also called a tool) |
| `ignored_inputs` | `tool`, `change_kind`, `inputs` | CHECK: `prepare_change` got inputs its kind doesn't use (counted to decide whether to split that tool) |
| `blocked_reply` | `ids`, `text` | CHECK: a model reply with internal IDs, NOT sent to the customer |
| `empty_reply` | – | CHECK: the model replied with nothing |
| `safe_message` | `reason` | CHECK: 5 failing tries in a row; code sent the safe message |
| `tool_crash` | `tool`, `traceback` | a bug in one of our tools (the model got a generic error) |
| `yes_waiting` | `tried`, and `text` when it was a reply | CHECK: the customer clearly said yes to the shown change, and the model tried something other than `confirm_change`: another tool (`tried` = its name) or a message to the customer (`tried` = `"reply"`). Not run / not sent (F20) |
| `duplicate_call` | `tool`, `of` | CHECK: the model called `tool` with exactly the same inputs as the successful call `of` (`tc-N`) since the last change made. Not run; the model is pointed to that result (F27) |
| `ungrounded_identity` | `values` | CHECK: `verify_customer` was given a name, zip code or email the customer never said, e.g. `["this first name 'John'"]`. Not run (F23) |
| `yes_check` | `reply`, `answer`, `agreed`, `error`, and when the model answered: `generation_time_seconds`, `usage`, `cost` | CHECK: the code's yes rule said no to the customer's reply, or the rule said yes but the model wanted to do something else (the checker decides, F20), so a model with NO conversation was asked (only the summary and the reply). `answer`: its raw answer. `agreed`: true only for YES; then the change goes ahead. The verdict stands for that reply. `error`: why there is no answer (the model failed); that counts as no. Its raw call is the `llm_debug/*_agent_yes_check_*.json` file with the same `generation_time_seconds`. It is a real model call but NOT a `model_call` event: those are the agent's decisions |

## How the events line up with results.json

- Our store calls are the assistant tool calls in `results.json`, same `fde-N` IDs.
- An assistant message written by the model has `generation_time_seconds` = the
  `model_call` that wrote it. Messages written by code have none.
- A turn's token and cost totals in `results.json` include its `yes_check` calls.
- Several `model_call`s can happen in ONE benchmark turn (a bad call and its retry, or
  a tool that failed before reaching the store). Only the log shows those.
