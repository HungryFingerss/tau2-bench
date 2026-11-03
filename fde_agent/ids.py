"""Typed ID labels.

Problem (baseline): the store uses bare digit strings for both item IDs and
product IDs, e.g. "8722653925" (an item) and "4896585277" (a product).
The model mixed them up and sometimes invented IDs.

Fix: the model only ever sees labelled IDs ("item_8722653925",
"product_4896585277"). Before any store call, code checks the label and
strips it. Two mistakes are caught in code, before the store is touched:
  1. wrong kind   -> a product ID passed where an item ID is needed
  2. unseen ID    -> an ID that never appeared in any tool output (invented)

Order IDs (#W...), user IDs and payment IDs are not labelled: they already
look different from each other.
"""

ITEM = "item"
PRODUCT = "product"
KINDS = (ITEM, PRODUCT)


from fde_agent.errors import ToolError


class IdError(ToolError):
    """Raised when the model passes a bad ID. The message is shown to the model."""


def label(kind: str, raw_id: str) -> str:
    """Store ID -> what the model sees.   label("item", "872...") -> "item_872..." """
    if kind not in KINDS:
        raise ValueError(f"unknown ID kind: {kind!r}")
    return f"{kind}_{raw_id}"


class SeenIds:
    """Every labelled ID our tools have shown the model in this conversation."""

    def __init__(self) -> None:
        self._seen: set[tuple[str, str]] = set()

    def remember(self, kind: str, raw_id: str) -> None:
        self._seen.add((kind, raw_id))

    def has(self, kind: str, raw_id: str) -> bool:
        return (kind, raw_id) in self._seen


def _a(word: str) -> str:
    """'item' -> 'an item', 'product' -> 'a product'."""
    return ("an " if word[0] in "aeiou" else "a ") + word


def unlabel(kind: str, labelled: str, seen: SeenIds) -> str:
    """What the model sent -> store ID. Raises IdError with a message for the model."""
    text = (labelled or "").strip()

    # 1. Right kind?  e.g. kind="item" but got "product_489..."
    for other in KINDS:
        if other != kind and text.startswith(other + "_"):
            raise IdError(
                f"'{text}' is {_a(other)} ID, but {_a(kind)} ID is needed here "
                f"(it starts with '{kind}_')."
            )

    # 2. Has a label at all?  e.g. bare "8722653925"
    prefix = kind + "_"
    if not text.startswith(prefix):
        raise IdError(
            f"'{text}' is not {_a(kind)} ID. Use the {kind} ID exactly as a tool "
            f"showed it (it starts with '{prefix}')."
        )

    raw_id = text[len(prefix):]

    # 3. Did a tool actually show this ID?  (catches invented IDs)
    if not seen.has(kind, raw_id):
        raise IdError(
            f"'{text}' has not appeared in any tool result in this conversation. "
            f"Only use IDs that a tool has shown you."
        )

    return raw_id
