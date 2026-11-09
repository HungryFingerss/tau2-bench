"""One error type for everything the model should be told about.

Our tools raise ToolError (or a subclass) with a message written for the model:
what went wrong, and what to do instead. The agent turns it into the tool result.

Every ToolError in a tool says whether it is a FACT (fact=True): something true about
the store or the rules that stays useful, e.g. "A refund can only go to the original
payment method or a gift card". Or a USAGE error (fact=False): a mistake in how the
model called the tool, e.g. "Verify the customer first". Once the model has recovered,
the model no longer sees its failed calls (F26), but the facts in them are kept.
IdError and NotesError are always usage errors.
"""


class ToolError(ValueError):
    def __init__(self, message: str, fact: bool = False) -> None:
        super().__init__(message)
        self.fact = fact
