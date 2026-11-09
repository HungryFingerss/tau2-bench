"""Working notes: facts the agent has established, kept by code, not by the model.

Problem (baseline): the model had to remember everything from the chat history.
It acted before verifying the customer (tasks 0, 1), missed one of the
customer's orders (task 3), and lost track of conditions (tasks 1, 6, 7).

Fix: tools write established facts here. Other tools read them to enforce
rules in code, e.g. "only one customer per conversation" and, later,
"only act on this customer's own orders".

Only what verify_customer needs exists for now. Later tools add their fields.
"""

from dataclasses import dataclass, field

from fde_agent.errors import ToolError
from fde_agent.ids import SeenIds
from fde_agent.store import StoreCall


class NotesError(ToolError):
    """Raised when an action breaks a rule the notes enforce. Message is for the model."""


@dataclass(frozen=True)
class Customer:
    user_id: str
    name: str  # "Yusuf Rossi"


@dataclass(frozen=True)
class PreparedChange:
    """A change checked by code and shown to the customer, waiting for their yes.
    confirm_change runs exactly `call`; the model can't alter it."""
    number: int
    kind: str
    call: StoreCall        # the store write to run on confirm
    customer_text: str     # what the customer is asked to approve


@dataclass
class Notes:
    customer: Customer | None = None
    orders: dict[str, str] = field(default_factory=dict)  # order_id -> status
    payment_methods: dict[str, str] = field(default_factory=dict)  # id -> "Mastercard ... ending 2478"
    seen: SeenIds = field(default_factory=SeenIds)
    prepared: PreparedChange | None = None  # at most one at a time
    changes_made: int = 0                   # numbering: #1, #2, ...
    shown: int | None = None                # number of the change code has shown the customer
    reply: str | None = None                # customer's latest message AFTER it was shown
    reply_is_yes: bool | None = None        # the yes-checker's verdict on `reply`, once asked (F19)
    done: set[int] = field(default_factory=set)  # confirmed change numbers
    transferred: bool = False               # after a transfer, every tool refuses
    transfer_warned: int | None = None      # change number the transfer reminder was given for
    order_products: dict[str, dict[str, str]] = field(default_factory=dict)  # order -> {product_id: name}
    announce: tuple[int, str] | None = None  # (change number, text) code tells the customer: done (F22)
    verified: tuple[str, str, int] | None = None  # (user_id, account text, changes done then) (F25)

    def check_open(self) -> None:
        """First line of every tool: nothing is possible after a transfer."""
        if self.transferred:
            raise NotesError("The customer was transferred to a human agent. "
                             "The conversation is over; no more actions are possible.")

    def check_same_customer(self, user_id: str) -> None:
        """Refuse a different customer once one is verified."""
        if self.customer is not None and self.customer.user_id != user_id:
            raise NotesError(
                f"{self.customer.name} is already verified in this conversation. "
                f"You can only help one customer per conversation."
            )

    def set_customer(self, user_id: str, name: str) -> None:
        """Record the verified customer."""
        self.check_same_customer(user_id)
        self.customer = Customer(user_id, name)

    def record_order(self, order_id: str, status: str, items: list[dict] | None = None) -> None:
        """Remember an order of the verified customer, its latest known status and,
        when given, which products are in it."""
        if self.customer is None:
            raise NotesError("Verify the customer before looking at orders.")
        self.orders[order_id] = status
        if items is not None:
            self.order_products[order_id] = {it["product_id"]: it["name"] for it in items}

    def is_customers_order(self, order_id: str) -> bool:
        return order_id in self.orders

    def prepare(self, kind: str, call: StoreCall, customer_text: str) -> PreparedChange:
        """Store a new prepared change. It replaces any earlier unconfirmed one."""
        self.changes_made += 1
        self.prepared = PreparedChange(self.changes_made, kind, call, customer_text)
        self.shown, self.reply, self.reply_is_yes = None, None, None
        return self.prepared

    # --- called by the agent loop (not by the model) ---

    def mark_shown(self, number: int) -> None:
        """Code sent change #number's summary to the customer, word for word."""
        if self.prepared is not None and self.prepared.number == number:
            self.shown, self.reply, self.reply_is_yes = number, None, None

    def customer_said(self, text: str) -> None:
        """Every customer message. Only one AFTER the summary was shown counts as a reply."""
        if self.prepared is not None and self.shown == self.prepared.number:
            self.reply, self.reply_is_yes = text, None

    # --- called by confirm_change ---

    def finish_change(self, confirmed: bool) -> None:
        """The prepared change was run (confirmed=True) or dropped after a store error."""
        if confirmed and self.prepared is not None:
            self.done.add(self.prepared.number)
        self.prepared, self.shown, self.reply, self.reply_is_yes = None, None, None, None
