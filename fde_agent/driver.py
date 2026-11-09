"""The driver: runs our tools inside the benchmark's turns.

Our tools ask for store calls with `yield` and wait for the results. Inside the
benchmark we can't call the store ourselves: we RETURN store calls, and their
results arrive in the NEXT turn. So a tool pauses between two turns:

    customer says something  -> on_customer(text)
        the model picks a tool -> the tool asks for store calls
        <- StoreCalls([...])            (the tool is now paused)
    the store's results arrive -> on_store_results({call_id: result})
        the tool resumes -> asks for more calls, or finishes with its text
        the model sees the text and decides again
        <- Say("...")                   (a message to the customer)

Rules (spec, piece 1):
  1. One tool at a time. While a tool is paused, only its results may arrive.
  2. A tool that fails before any store call costs no benchmark step: its error
     goes straight back to the model, inside the same turn.
  3. At most MAX_FAILED_TRIES failing tool calls in a row, then a safe message.
  4. Store results are matched by call ID, never by position.
  5. Every internal step is logged (the benchmark only records store calls).

Loop rules (spec, piece 4): the moments where code speaks or listens, not the model.
  6. Every customer message goes into the notes (for the yes rule).
  7. After prepare_change succeeds, code sends its summary word for word and marks
     it shown; the model isn't asked anything until the customer answers.
  8. After transfer_to_human succeeds, code sends the exact transfer sentence.
  9. Every message the model writes is checked first (outgoing.py): internal IDs
     -> not sent, the model rewrites it; bold markers -> removed.

This file knows nothing about the benchmark's message classes or about the real
model. The adapter (piece 6) and the model client (piece 2) plug into it.
"""

import re
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Protocol

from fde_agent.errors import ToolError
from fde_agent.notes import Notes
from fde_agent.outgoing import internal_ids, strip_bold
from fde_agent.store import StoreCall, StoreResult
from fde_agent.tools import confirm_change, prepare_change, transfer_to_human, verify_customer
from fde_agent.toolset import TOOLS

MAX_FAILED_TRIES = 5
SAFE_MESSAGE = "Sorry, I couldn't do that just now. Could you tell me again what you need?"
# F20: while the customer's clear yes to the shown change waits, only confirm_change may run.
# Practice run, 7 Nov (task 11): after "Yes." the model re-verified the customer and told them
# "Your return has been requested". Nothing was returned.
YES_WAITING = ('The customer said yes to change #{n} ("{reply}"). Call confirm_change(change_number={n}) '
               "now, before anything else. Then do the rest.")
# F27: an identical successful call since the last change made: not run again.
DUPLICATE = ("Not run: you already called {tool} with exactly these inputs ({id}), and nothing has "
             "changed since. Its result is above: use it.")
# F23: identity values must come from the customer. Practice run (task 4): "John Doe, 90210".
UNGROUNDED = ("Not run: the customer hasn't said {what}. Ask the customer for it. Never guess or "
              "invent a name, zip code or email.")
IDENTITY = {"email": "this email", "first_name": "this first name", "last_name": "this last name",
            "zip": "this zip code"}

WAITING_NOTE = ("Change #{n} is still waiting for the customer's answer. If they agreed to it, call "
                "confirm_change(change_number={n}). If they asked something, answer it, then ask again "
                "whether to go ahead with change #{n}. If they want something different, call "
                "prepare_change again.")


# ---------- what the model decides, and what the driver returns ----------

@dataclass(frozen=True)
class Decision:
    """The model's choice: call one tool, or say something to the customer.

    The model client (piece 2) may also report what its checks found:
      invalid  the call was rejected before running (error text for the model)
      repeat   the same call as the previous one: answer it from the last result
      fixes    things it fixed or dropped (logged, e.g. junk cut from a tool name)
      note     something the model should know (e.g. extra calls that weren't run)
    """
    say: str | None = None
    tool: str | None = None
    args: dict[str, Any] = field(default_factory=dict)
    invalid: str | None = None
    repeat: bool = False
    fixes: tuple[str, ...] = ()
    note: str | None = None
    model_call: dict | None = None   # facts of the real model call (time, tokens), if any

    @staticmethod
    def call(tool: str, **args) -> "Decision":
        return Decision(tool=tool, args=args)

    @staticmethod
    def reply(text: str) -> "Decision":
        return Decision(say=text)


class Model(Protocol):
    """Anything that looks at the transcript and decides. Piece 2 is the real one."""
    def decide(self, transcript: list[dict]) -> Decision: ...


@dataclass(frozen=True)
class StoreCalls:
    """Driver output: run these store calls and send me the results. id -> call.
    generation_time_seconds: the model call that started them (links the trace
    viewer's step to its raw call); None when no model call led to this turn."""
    calls: dict[str, StoreCall]
    generation_time_seconds: float | None = None


@dataclass(frozen=True)
class Say:
    """Driver output: send this message to the customer. generation_time_seconds is
    set only when the MODEL wrote it; messages written by code have None."""
    text: str
    generation_time_seconds: float | None = None


class DriverError(RuntimeError):
    """A bug in how the driver is being used, never something to show the model."""


@dataclass
class _Paused:
    tool: str
    steps: Any                    # the tool's generator, waiting for results
    waiting_for: list[str]        # call IDs, in the order the tool asked for them


# ---------- the driver ----------

class Driver:
    def __init__(self, model: Model, notes: Notes | None = None, yes_checker=None) -> None:
        self.model = model
        self.notes = notes or Notes()
        self.yes_checker = yes_checker     # yes_check.YesChecker, or None: only the code's rule
        self.transcript: list[dict] = []   # what the model sees (piece 2 formats it)
        self.log: list[dict] = []          # everything, for fde_agent_log.json
        self._paused: _Paused | None = None
        self._failed_in_a_row = 0
        self._next_id = 0          # store call IDs: fde-1, fde-2, ...
        self._call_id = 0          # tool call IDs:  tc-1, tc-2, ...
        self._current = ""         # ID of the tool call being run
        self._pending_note: str | None = None
        self._just_finished: tuple[str, bool] | None = None   # (tool, ok) of the last tool
        self._deciding_time: float | None = None   # time of the model call being acted on
        self.turn_model_calls: list[dict] = []     # every model call since the last output
        self._current_args: dict = {}
        self._last_write = 0       # transcript index of the last change made (F27)
        self._prepared_from: tuple[dict, str, int] | None = None  # (inputs, call ID, change number)

    # --- the two ways in (and the greeting) ---

    def greeted(self, text: str) -> None:
        """The benchmark opened the conversation with its own greeting."""
        self._record("agent", text=text, by="benchmark")
        self.transcript.append({"role": "agent", "text": text})

    def on_customer(self, text: str) -> StoreCalls | Say:
        if self._paused is not None:
            raise DriverError(f"A customer message arrived while {self._paused.tool} "
                              f"was waiting for store results.")
        self._record("customer", text=text)
        self.transcript.append({"role": "customer", "text": text})
        self.notes.customer_said(text)       # rule 6: only counts as a reply after a summary
        self._remind_waiting_change(text)
        return self._think()

    def on_store_results(self, results: dict[str, StoreResult]) -> StoreCalls | Say:
        p = self._paused
        if p is None:
            raise DriverError("Store results arrived, but no tool is waiting for any.")
        if set(results) != set(p.waiting_for):
            raise DriverError(f"{p.tool} waits for {sorted(p.waiting_for)}, "
                              f"got results for {sorted(results)}.")
        ordered = [results[call_id] for call_id in p.waiting_for]   # by ID (rule 4)
        self._record("store_results", tool=p.tool, results={
            i: {"content": results[i].content, "error": results[i].error} for i in p.waiting_for})
        self._paused = None
        self._failed_in_a_row = 0          # this tool reached the store
        out = self._advance(p.tool, p.steps, ordered)
        if out is not None:
            return out
        return self._code_speaks() or self._think()

    # --- the inside ---

    def _think(self) -> StoreCalls | Say:
        """Ask the model until it says something or a tool needs the store."""
        while True:
            if self._failed_in_a_row >= MAX_FAILED_TRIES:
                self._failed_in_a_row = 0
                self._record("safe_message", reason=f"{MAX_FAILED_TRIES} failing tool calls in a row")
                return self._say(SAFE_MESSAGE, by="code")

            view, hidden = model_view(self.transcript)
            decision = self.model.decide(view)
            self._deciding_time = None
            if decision.model_call is not None:
                self._record("model_call", **decision.model_call, hidden=hidden)
                self.turn_model_calls.append(decision.model_call)
                self._deciding_time = decision.model_call.get("generation_time_seconds")
            for fix in decision.fixes:
                self._record("fixed", what=fix)
            if decision.invalid is not None:
                # Rejected by the model client's checks: never run. Counts as a failed try.
                self._record("invalid_call", error=decision.invalid)
                self._note(decision.invalid, failed=True)
                self._failed_in_a_row += 1
                continue
            if decision.tool is None:
                if (decision.say or "").strip():
                    text, stripped = strip_bold(decision.say.strip())        # rule 9
                    if stripped:
                        self._record("fixed", what="removed bold markers from a reply")
                    ids = internal_ids(text)
                    if ids:
                        self._record("blocked_reply", ids=ids, text=text)
                        self._note(f"Your reply was not sent: it showed internal IDs "
                                   f"({', '.join(ids)}). Rewrite it without them: describe items by "
                                   f"name and options, and payment methods like \"your Mastercard "
                                   f"ending 2478\". Order numbers (#W...) are fine.", failed=True)
                        self._failed_in_a_row += 1
                        continue
                    waiting = self._yes_waiting()                            # F20
                    if waiting is not None and self._really_yes():
                        self._record("yes_waiting", tried="reply", text=text)
                        self._note("Your reply was not sent. " + YES_WAITING.format(
                            n=waiting, reply=self.notes.reply), failed=True)
                        self._failed_in_a_row += 1
                        continue
                    return self._say(text, at=self._deciding_time)
                # An empty reply would be an invalid message for the benchmark
                # (the baseline crashed on this). Tell the model; counts as a failed try.
                self._record("empty_reply")
                self._note("Error: your reply was empty. Either call a tool or "
                           "write a message to the customer.", failed=True)
                self._failed_in_a_row += 1
                continue

            self._call_id += 1
            self._current = f"tc-{self._call_id}"
            self._record("tool_call", id=self._current, tool=decision.tool, args=decision.args)
            self._current_args = decision.args
            if decision.tool == prepare_change.NAME:
                ignored = prepare_change.ignored_inputs(decision.args.get("kind"), decision.args)
                if ignored:
                    self._record("ignored_inputs", tool=decision.tool,
                                 change_kind=decision.args.get("kind"), inputs=ignored)
            self.transcript.append({"role": "tool_call", "id": self._current,
                                    "tool": decision.tool, "args": decision.args})
            waiting = self._yes_waiting()                                    # F20
            if waiting is not None and decision.tool != confirm_change.NAME and self._really_yes():
                self._record("yes_waiting", tried=decision.tool)
                self._finish(decision.tool, "Error: Not run. " + YES_WAITING.format(
                    n=waiting, reply=self.notes.reply), ok=False, reached_store=False)
                continue
            duplicate = self._duplicate(decision.tool, decision.args)        # F27
            if duplicate is not None:
                self._record("duplicate_call", tool=decision.tool, of=duplicate)
                self._finish(decision.tool, "Error: " + DUPLICATE.format(tool=decision.tool, id=duplicate),
                             ok=False, reached_store=False)
                continue
            if decision.repeat:
                # Same call as the previous one (which failed), nothing new since: don't run it
                # again. Counts as a failed try: a model stuck repeating itself must hit the limit.
                previous = next(e for e in reversed(self.transcript[:-1]) if e["role"] == "tool_result")
                base = previous["text"].split("\n(This is the same call")[0]
                self._result(decision.tool, base + "\n(This is the same call as your previous one, so "
                             "it was not run again. The result is unchanged.)", ok=previous["ok"],
                             fact=previous.get("fact", False))
                self._failed_in_a_row += 1
                continue
            if decision.tool == verify_customer.NAME:                            # F23
                unheard = self._ungrounded(decision.args)
                if unheard:
                    self._record("ungrounded_identity", values=unheard)
                    self._finish(decision.tool, "Error: " + UNGROUNDED.format(what=" or ".join(unheard)),
                                 ok=False, reached_store=False)
                    continue
            if decision.note:
                self._pending_note = decision.note
            tool = TOOLS.get(decision.tool)
            if tool is None:
                self._finish(decision.tool, f"Error: there is no tool called '{decision.tool}'. "
                             f"The tools are: {', '.join(TOOLS)}.", ok=False, reached_store=False)
                continue
            helpers = {"yes_checker": self._check_yes} if decision.tool == confirm_change.NAME else {}
            try:
                steps = tool(self.notes, **decision.args, **helpers)
            except TypeError as e:          # e.g. an argument name the tool doesn't have
                self._finish(decision.tool, f"Error: wrong inputs for {decision.tool}: {e}.",
                             ok=False, reached_store=False)
                continue
            out = self._advance(decision.tool, steps, None)
            if out is not None:
                return out
            spoken = self._code_speaks()
            if spoken is not None:
                return spoken

    def _advance(self, tool: str, steps, send_value) -> StoreCalls | None:
        """Run the tool until it asks for store calls (-> StoreCalls) or ends (-> None).
        `send_value` is None to start it, or the list of results it asked for."""
        reached_store = send_value is not None
        try:
            calls = next(steps) if send_value is None else steps.send(send_value)
            while not calls:                 # asked for nothing (e.g. 0 orders): answer at once
                calls = steps.send([])
        except StopIteration as done:
            self._finish(tool, done.value, ok=True, reached_store=reached_store)
            return None
        except ToolError as e:
            self._finish(tool, f"Error: {e}", ok=False, reached_store=reached_store,
                         fact=getattr(e, "fact", False))
            return None
        except Exception:                    # a bug in a tool: don't crash the conversation
            self._record("tool_crash", tool=tool, traceback=traceback.format_exc())
            self._finish(tool, f"Error: {tool} failed unexpectedly.", ok=False, reached_store=reached_store)
            return None

        ids = []
        for call in calls:
            self._next_id += 1
            ids.append(f"fde-{self._next_id}")
        self._paused = _Paused(tool, steps, ids)
        self._record("store_calls", tool=tool,
                     calls={i: {"name": c.name, "args": c.args} for i, c in zip(ids, calls)})
        at, self._deciding_time = self._deciding_time, None    # only the first round links
        return StoreCalls(dict(zip(ids, calls)), generation_time_seconds=at)

    def _result(self, tool: str, text: str, ok: bool, fact: bool = False) -> None:
        """A tool's text goes to the model, linked to its call by ID. A failed result says
        whether its error is a fact worth keeping (F26)."""
        extra = {} if ok else {"fact": fact}
        self._record("tool_result", id=self._current, tool=tool, ok=ok, text=text, **extra)
        self.transcript.append({"role": "tool_result", "id": self._current, "tool": tool,
                                "ok": ok, "text": text, **extra})

    def _finish(self, tool: str, text: str, ok: bool, reached_store: bool, fact: bool = False) -> None:
        """A tool ended: its result goes to the model; update the failure count."""
        self._result(tool, text, ok, fact)
        self._just_finished = (tool, ok)
        if ok and tool == confirm_change.NAME:
            self._last_write = len(self.transcript) - 1      # reads before this may be stale
        if ok and tool == prepare_change.NAME and self.notes.prepared is not None:
            self._prepared_from = (_inputs(self._current_args), self._current, self.notes.prepared.number)
        if ok or reached_store:
            self._failed_in_a_row = 0
        else:
            self._failed_in_a_row += 1
        if self._pending_note:
            self._note(self._pending_note)
            self._pending_note = None

    def _remind_waiting_change(self, text: str) -> None:
        """A reply to a shown summary that the yes rule doesn't read as a clear yes: remind the
        model the change is still waiting. Practice run, 7 Nov (task 2): the customer asked a
        side question, the model answered it and never asked about the return again.
        It names confirm_change too, because the second yes check (F19) may still say yes."""
        p, n = self.notes.prepared, self.notes
        if p is None or n.shown != p.number or n.reply != text or confirm_change.is_clear_yes_to(p, text):
            return
        self._note(WAITING_NOTE.format(n=p.number))

    def _check_yes(self, summary: str, reply: str) -> bool:
        """F19: confirm_change asks this only when the code's yes rule said no.
        The check's model call is logged and counted in this turn's cost."""
        if self.yes_checker is None:
            return False
        result = self.yes_checker.check(summary, reply)
        facts = result.model_call or {}
        if result.model_call is not None:
            self.turn_model_calls.append(result.model_call)
        self._record("yes_check", reply=reply, answer=result.answer, agreed=result.agreed,
                     error=result.error, **facts)
        if self.notes.reply == reply:
            self.notes.reply_is_yes = result.agreed           # the verdict stands for this reply
        return result.agreed

    def _note(self, text: str, failed: bool = False) -> None:
        """Something the model must know that isn't a tool result or the customer.
        failed=True: it is about a failed step, hidden once the model recovers (F26)."""
        self._record("note", text=text, **({"failed": True} if failed else {}))   # the log keeps all
        self.transcript.append({"role": "note", "text": text, **({"failed": True} if failed else {})})

    def _yes_waiting(self) -> int | None:
        """F20: the number of the shown change the customer said yes to, if any: by the
        checker's verdict once it was asked, else by the yes rule."""
        p, n = self.notes.prepared, self.notes
        if p is None or n.shown != p.number or n.reply is None or n.reply_is_yes is False:
            return None
        if n.reply_is_yes or confirm_change.is_clear_yes_to(p, n.reply):
            return p.number
        return None

    def _really_yes(self) -> bool:
        """The rule saw a yes, but the model wants to do something else. Who is right? The
        yes-checker decides (its verdict is remembered for this reply). Smoke run, task 8:
        the rule read "Just go ahead with the desk lamp" as a yes and forced the confirm of a
        lamp + water bottle exchange, while the model correctly wanted the lamp only.
        Without a checker, the rule stands."""
        n = self.notes
        if n.reply_is_yes is not None:
            return n.reply_is_yes
        if self.yes_checker is None:
            return True
        return self._check_yes(n.prepared.customer_text, n.reply)

    def _duplicate(self, tool: str, args: dict) -> str | None:
        """F27: the ID of an earlier successful call with exactly these inputs, made since the
        last change (reads before it may be stale). prepare_change: only the current change."""
        key = _inputs(args)
        if tool == prepare_change.NAME:
            f = self._prepared_from
            p = self.notes.prepared
            if f and p is not None and f[2] == p.number and f[0] == key:
                return f[1]
            return None
        calls = {e["id"]: e for e in self.transcript if e["role"] == "tool_call"}
        for i, e in enumerate(self.transcript[:-1]):     # [-1] is this call itself
            if e["role"] == "tool_result" and e["ok"] and i >= self._last_write:
                c = calls.get(e["id"])
                if c and c["tool"] == tool and _inputs(c["args"]) == key:
                    return e["id"]
        return None

    def _ungrounded(self, args: dict) -> list[str]:
        """F23: identity values the customer never said ("this first name 'John'")."""
        heard = _squash(" ".join(e["text"] for e in self.transcript if e["role"] == "customer"))
        return [f"{IDENTITY[k]} '{v.strip()}'" for k, v in args.items()
                if k in IDENTITY and isinstance(v, str) and v.strip() and _squash(v) not in heard]

    def _code_speaks(self) -> Say | None:
        """Rules 7 and 8: after some tools, code (not the model) talks to the customer."""
        tool, ok = self._just_finished or (None, False)
        self._just_finished = None
        if not ok:
            return None
        if tool == prepare_change.NAME and self.notes.prepared is not None:
            change = self.notes.prepared
            said = self._say(change.customer_text, by="code")
            self.notes.mark_shown(change.number)
            self._note(f"The summary of change #{change.number} was shown to the customer, word "
                       f"for word. Wait for their answer.")
            return said
        if tool == confirm_change.NAME and self.notes.announce is not None:      # F22
            number, text = self.notes.announce
            self.notes.announce = None
            said = self._say(text, by="code")
            self._note(f"Code told the customer that change #{number} is done, word for word. "
                       f"Wait for their next message.")
            return said
        if tool == transfer_to_human.NAME and self.notes.transferred:
            return self._say(transfer_to_human.TRANSFER_MESSAGE, by="code")
        return None

    def _say(self, text: str, by: str = "model", at: float | None = None) -> Say:
        self._record("agent", text=text, by=by)
        self.transcript.append({"role": "agent", "text": text})
        self._deciding_time = None
        return Say(text, generation_time_seconds=at if by == "model" else None)

    def _record(self, kind: str, **data) -> None:
        self.log.append({"n": len(self.log) + 1, "time": round(time.time(), 3), "kind": kind, **data})


# ---------- helpers ----------

def _inputs(args: dict) -> dict:
    """A call's inputs without empty ones: {"email": "", "zip": "19122"} == {"zip": "19122"}."""
    return {k: v for k, v in (args or {}).items() if v not in (None, "", [], {})}


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", (text or "").lower())


def model_view(transcript: list[dict]) -> tuple[list[dict], int]:
    """F26: what the model sees. Failed steps (failed tool calls with their results, notes
    about rejected replies) are left out once the model has recovered, i.e. once a later
    tool call succeeded or a message reached the customer. A failed result whose error is a
    FACT is kept as a short note. Customer messages, successes and code's notes always stay.
    Returns (view, how many entries were left out). The log keeps everything."""
    failed_ids = {e["id"] for e in transcript if e["role"] == "tool_result" and not e["ok"]}
    successes = [i for i, e in enumerate(transcript)
                 if (e["role"] == "tool_result" and e["ok"]) or e["role"] == "agent"]
    recovered_before = max(successes, default=-1)
    view, hidden, facts = [], 0, set()
    for i, e in enumerate(transcript):
        failure = ((e["role"] in ("tool_call", "tool_result") and e.get("id") in failed_ids)
                   or (e["role"] == "note" and e.get("failed")))
        if failure and i < recovered_before:
            if e["role"] == "tool_result" and e.get("fact"):
                fact = e["text"].removeprefix("Error: ")
                if fact not in facts:
                    facts.add(fact)
                    view.append({"role": "note", "text": f"Fact from an earlier failed call: {fact}"})
                    continue
            hidden += 1
            continue
        view.append(e)
    return view, hidden
