"""transfer_to_human: hand the customer to a human agent. Ends the conversation.

WHEN to transfer is the model's judgement (guided by the description below):
code can't tell from keywords (in task 50 the customer never says "human").
Code makes sure of the rest:
  - only once            (the baseline called it 3 times, task 3)
  - a summary is given   (code adds the verified customer in front)
  - no verification needed (task 50's expected path is a transfer only)
  - unfinished change -> ONE reminder (option A): the first call is refused with
    a reminder; a second call goes through and drops the change. Never loops.
  - the closing sentence is sent by code word for word, so the model can't leak
    its reasoning to the customer (it did in task 3).
"""

from fde_agent.errors import ToolError
from fde_agent.notes import Notes
from fde_agent.store import StoreCall, ToolSteps

NAME = "transfer_to_human"

TRANSFER_MESSAGE = "YOU ARE BEING TRANSFERRED TO A HUMAN AGENT. PLEASE HOLD ON."

DESCRIPTION = (
    "Transfer the customer to a human agent. This ENDS the conversation. Use it only when "
    "(a) the customer asks for a human, supervisor or representative, or (b) they need something "
    "no tool can do, e.g. undo a cancellation. If a request is against the rules (e.g. a refund "
    "to a different card), first explain that it can't be done; transfer only if they then ask "
    "for a human. Before transferring, finish everything the customer already agreed to. "
    "Give a short summary of the customer's issue."
)

PARAMETERS = {
    "type": "object",
    "properties": {"summary": {"type": "string", "description": "The customer's issue, in one or two sentences."}},
    "required": ["summary"],
}


def transfer_to_human(notes: Notes, summary: str = "") -> ToolSteps:
    notes.check_open()   # a second transfer is refused here
    summary = (summary or "").strip()
    if not summary:
        raise ToolError("Give a short summary of the customer's issue for the human agent.")

    p = notes.prepared
    if p is not None and notes.transfer_warned != p.number:
        notes.transfer_warned = p.number
        raise ToolError(
            f"Wait: change #{p.number} ({p.customer_text.splitlines()[0]}) is prepared but not done. "
            f"After a transfer nothing more can be done. If the customer still wants it, get their "
            f"clear yes and confirm_change first. If they don't, call transfer_to_human again."
        )

    if notes.customer is not None:
        summary = f"Customer: {notes.customer.name} ({notes.customer.user_id}). {summary}"

    [r] = yield [StoreCall("transfer_to_human_agents", {"summary": summary})]
    if r.error:
        raise ToolError(f"The transfer failed ({r.content}).")

    dropped = ""
    if p is not None:
        dropped = f" Change #{p.number} was dropped (not done)."
        notes.finish_change(confirmed=False)
    notes.transferred = True
    return (f"Transferred.{dropped} Code now sends the customer: \"{TRANSFER_MESSAGE}\" "
            f"The conversation is over.")
