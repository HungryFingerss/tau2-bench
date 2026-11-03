"""One error type for everything the model should be told about.

Our tools raise ToolError (or a subclass) with a message written for the model:
what went wrong, and what to do instead. The agent turns it into the tool result.
"""


class ToolError(ValueError):
    pass
