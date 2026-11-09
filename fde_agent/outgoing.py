"""Checks on every message the MODEL writes, before the customer sees it.
(Messages code sends itself, like the change summary, don't need them.)

  - internal IDs (item_..., product_..., credit_card_..., bare 10-digit item numbers):
    the reply is NOT sent; the model rewrites it (decision 2: cutting IDs out
    could leave broken sentences)
  - bold markers (**...**): removed (decision 1: never changes the meaning)

Order numbers (#W2378156) and 12-digit tracking numbers are fine.
"""

import re

INTERNAL_ID = re.compile(
    r"\bitem_\d+|\bproduct_\d+|\b(?:credit_card|gift_card|paypal)_\d+|(?<!W)\b\d{10}\b")
BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)


def internal_ids(text: str) -> list[str]:
    return [m.group(0) for m in INTERNAL_ID.finditer(text)]


def strip_bold(text: str) -> tuple[str, bool]:
    cleaned = BOLD.sub(r"\1", text)
    return cleaned, cleaned != text
