"""LIVE: the real gpt-oss:20b as F19's yes-checker. SKIPPED unless FDE_LIVE=1.

Every reply here is one the code's strict rule refuses (checked below), because only
those ever reach the checker. Half are real yeses it should let through, half are not.
This tests the checker itself, not the agent's instructions.

On Windows (PowerShell), from the repo root, each reply 3 times:
    $env:FDE_LIVE=1; $env:FDE_REPEAT=3; uv run --with pytest python -m pytest fde_agent/tests/test_yes_check_live.py -s; Remove-Item Env:FDE_LIVE, Env:FDE_REPEAT
About 5-20 s per reply on a laptop.
"""
import os

import pytest

from fde_agent.tools.confirm_change import is_clear_yes_to
from fde_agent.yes_check import YesChecker

pytestmark = pytest.mark.skipif(os.environ.get("FDE_LIVE") != "1", reason="set FDE_LIVE=1 to run")

REPEAT = max(1, int(os.environ.get("FDE_REPEAT", "1")))
MODEL = os.environ.get("FDE_MODEL", "ollama_chat/gpt-oss:20b")


def pytest_generate_tests(metafunc):
    if "run" in metafunc.fixturenames:
        metafunc.parametrize("run", range(1, REPEAT + 1), ids=lambda i: f"run{i}")


# The summary from the 6 Nov smoke run, worded as prepare_change words it now.
SUMMARY = (
    "Here's the change I'll make. Nothing has changed yet.\n"
    "Exchange in order #W2378156:\n"
    "- Mechanical Keyboard: switch type linear -> clicky, backlight RGB -> none ($272.33 -> $269.16)\n"
    "- Smart Thermostat: compatibility Apple HomeKit -> Google Assistant ($262.47 -> $249.01)\n"
    "You get $16.63 back, to your Mastercard credit card ending 2478.\n"
    "You'll get an email on how to send the items back. No new order is needed.\n"
    "After this, nothing else in this order can be returned or exchanged. If you want to exchange "
    "another item in this order too, tell me now, so I can include it. Once I confirm it, I can't "
    "change or undo it.\n"
    "Shall I go ahead? I'll make the change as soon as you say yes."
)


class Shown:
    customer_text = SUMMARY


CASES = {
    # real yeses the word rule refuses
    "smoke_reply": ("Yeah, that sounds right. I don't have any other items in that order I need to "
                    "swap out. So yeah, if you can go ahead and get that started, I'd appreciate it.", True),
    "no_other_items": ("No other items, that's all. Please go ahead.", True),
    "no_yes_word": ("Alright, please process it.", True),
    "nope_nothing_else": ("Nope, nothing else to add. Go ahead with the exchange.", True),
    "both_items_named": ("Yes, the keyboard and the thermostat both look right. Go ahead.", True),
    # not a yes
    "changed_mind": ("No, I changed my mind.", False),
    "yes_but_paypal": ("Yes, but use my PayPal instead.", False),
    "thinking": ("Hmm, let me think about it.", False),
    "question_first": ("Wait, will the new keyboard be full size?", False),
    "only_one_item": ("Actually, only exchange the keyboard.", False),
    "just_one_item": ("Just go ahead with the keyboard.", False),     # smoke run, task 8
}


@pytest.mark.parametrize("case", list(CASES))
def test_reply(case, run):
    reply, expected = CASES[case]
    assert not is_clear_yes_to(Shown, reply), "only replies the code refuses reach the checker"
    out = YesChecker(MODEL).check(SUMMARY, reply)
    print(f"\n{case}: answer={out.answer!r} error={out.error} "
          f"time={(out.model_call or {}).get('generation_time_seconds')}")
    assert out.error is None, out.error
    assert out.agreed is expected
