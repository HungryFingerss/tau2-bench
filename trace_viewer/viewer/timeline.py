"""Turn a simulation's message list into the steps shown on the timeline."""

from typing import Any

from viewer.verdicts import tool_results

CUSTOMER, AGENT, GREETING, SYSTEM, TOOL = (
    "customer",
    "agent",
    "greeting",
    "system",
    "tool",
)


def _choice(message: dict) -> dict:
    choices = (message.get("raw_data") or {}).get("choices") or [{}]
    return choices[0]


def _result(tool_message: dict, index: int) -> dict[str, Any]:
    return {
        "content": tool_message.get("content"),
        "error": bool(tool_message.get("error")),
        "execution_status": tool_message.get("execution_status"),
        "message_index": index,
    }


def _step_type(role: str, index: int, message: dict) -> str:
    if role == "assistant":
        # The opening line comes from the language pack, not from a model.
        return GREETING if index == 0 and not message.get("raw_data") else AGENT
    return CUSTOMER if role == "user" else SYSTEM


def build_timeline(messages: list[dict]) -> list[dict]:
    """One step per message. Tool results are attached to the call they answer.

    A step's index is its position in the message list, which is how the raw-call
    route finds it again.
    """
    steps: list[dict] = []
    pending_calls: dict[str, dict] = {}

    for index, message in enumerate(messages):
        role = message.get("role")
        if role == "tool":
            for tool_message in tool_results(message):
                result = _result(tool_message, index)
                call = pending_calls.pop(tool_message.get("id"), None)
                if call is not None:
                    call["result"] = result
                else:
                    steps.append(
                        {
                            "index": index,
                            "type": TOOL,
                            "timestamp": tool_message.get("timestamp"),
                            "tool_calls": [{"name": None, "result": result}],
                        }
                    )
            continue

        calls = []
        for tool_call in message.get("tool_calls") or []:
            call = {
                "id": tool_call.get("id"),
                "name": tool_call.get("name"),
                "arguments": tool_call.get("arguments"),
                "result": None,
            }
            pending_calls[call["id"]] = call
            calls.append(call)

        choice = _choice(message)
        steps.append(
            {
                "index": index,
                "type": _step_type(role, index, message),
                "turn_idx": message.get("turn_idx"),
                "timestamp": message.get("timestamp"),
                "content": message.get("content"),
                "tool_calls": calls,
                "reasoning": (choice.get("message") or {}).get("reasoning_content"),
                "finish_reason": choice.get("finish_reason"),
                "usage": message.get("usage"),
                "generation_time_seconds": message.get("generation_time_seconds"),
                "cost": message.get("cost"),
                "has_model_call": bool(message.get("raw_data")),
            }
        )
    return steps


def first_error_step(steps: list[dict]) -> int | None:
    """The first step holding a failed tool call, for the jump link."""
    for step in steps:
        for call in step.get("tool_calls") or []:
            if (call.get("result") or {}).get("error"):
                return step["index"]
    return None
