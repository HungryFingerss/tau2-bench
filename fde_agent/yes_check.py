"""The second check on the customer's yes (decision F19).

confirm_change first uses the code's strict yes rule (is_clear_yes). Only when that
rule says NO is this asked: one model call with NO conversation, just the summary the
customer saw and their reply. It catches a real yes that the word rule refuses, e.g.
"Yeah... I don't have any other items in that order... go ahead" (smoke run, 6 Nov).

When the rule says yes, the model is never asked: the rule is strict, so its yes is safe.
Every doubt is a NO: an answer other than YES, an unreadable reply, or no answer at all
(Ollama down or too slow). A NO only costs asking the customer once more.
"""

from dataclasses import dataclass

CALL_NAME = "agent_yes_check"   # its own name in llm_debug, apart from agent_response

INSTRUCTION = (
    "You check one thing: did a store customer agree to a change?\n"
    "You get the summary the customer was shown and their reply to it.\n"
    "Answer YES only if the customer agreed to go ahead with exactly this change. "
    "It is fine if they also answer a side remark in the summary, for example by saying "
    "there are no other items.\n"
    "Answer NO if they want anything changed, added, held or explained first, or if it is "
    "not clear that they agreed.\n"
    "Reply with only one word: YES or NO."
)


@dataclass(frozen=True)
class YesCheck:
    agreed: bool                  # True only for a clear YES from the model
    answer: str | None            # the model's raw answer, for the log
    error: str | None = None      # why there is no answer (the model failed)
    model_call: dict | None = None  # generation_time_seconds, usage, cost (as model_call events)


def to_messages(summary: str, reply: str) -> list:
    from tau2.data_model.message import SystemMessage, UserMessage
    return [
        SystemMessage(role="system", content=INSTRUCTION),
        UserMessage(role="user", content=(
            f"The summary shown to the customer:\n---\n{summary}\n---\n\n"
            f"The customer's reply:\n---\n{reply}\n---\n\n"
            f"Did the customer agree to go ahead with exactly this change? YES or NO.")),
    ]


def read_answer(text: str | None) -> bool:
    """Only YES counts (any case, an ending full stop allowed). "Probably yes" is a NO."""
    return (text or "").strip().rstrip(".!").strip().upper() == "YES"


class YesChecker:
    def __init__(self, model: str, llm_args: dict | None = None, generate_fn=None) -> None:
        self.model = model
        self.llm_args = llm_args if llm_args is not None else {}  # shared: set_seed updates it
        if generate_fn is None:
            from tau2.utils.llm_utils import generate as generate_fn
        self.generate = generate_fn

    def check(self, summary: str, reply: str) -> YesCheck:
        """Never raises: if the model can't answer, the answer is NO."""
        try:
            out = self.generate(model=self.model, messages=to_messages(summary, reply),
                                call_name=CALL_NAME, **{"temperature": 0.0, **self.llm_args})
        except Exception as e:           # one try only: a NO is cheap, a long wait is not
            return YesCheck(False, None, error=f"{type(e).__name__}: {e}")
        from fde_agent.model_client import call_facts
        answer = out.content
        return YesCheck(read_answer(answer), answer, model_call=call_facts(out))
