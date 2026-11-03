"""How our tools talk to the store.

Our tools never call the store directly. They are generators:
    results = yield [StoreCall(...), StoreCall(...)]   # ask for store calls
    ...
    return "text for the model"                        # finished
Whoever runs the tool performs the calls and sends the results back in.

- In the agent: the calls go out through the benchmark's orchestrator,
  so they are real, recorded tool calls.
- In tests: run_now() performs them immediately on a real retail environment.
Same tool code in both places.
"""

from dataclasses import dataclass
from typing import Any, Generator

from fde_agent.errors import ToolError


@dataclass(frozen=True)
class StoreCall:
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class StoreResult:
    content: str  # JSON text on success, "Error: ..." on failure
    error: bool


# A tool: yields lists of StoreCall, receives lists of StoreResult, returns text.
ToolSteps = Generator[list[StoreCall], list[StoreResult], str]


@dataclass
class Outcome:
    text: str              # what the model will see
    ok: bool               # False if the tool raised ToolError
    calls: list[StoreCall]  # every store call made, in order


def run_now(steps: ToolSteps, env) -> Outcome:
    """Test driver: run a tool's store calls immediately on a tau2 Environment."""
    from tau2.data_model.message import ToolCall  # only needed here

    made: list[StoreCall] = []
    try:
        calls = next(steps)
        while True:
            results = []
            for c in calls:
                made.append(c)
                msg = env.get_response(ToolCall(id=str(len(made)), name=c.name, arguments=c.args))
                results.append(StoreResult(msg.content, msg.error))
            calls = steps.send(results)
    except StopIteration as done:
        return Outcome(done.value, True, made)
    except ToolError as e:
        return Outcome(f"Error: {e}", False, made)
