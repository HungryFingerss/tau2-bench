"""Browse and search the retail store in data/tau2/domains/retail/db.json.

This is the store's initial state, before any simulation changes it. It is read as
plain JSON with no tau2 import. The benchmark never rewrites this file, so reading
it carries none of the risks of reading results.json.
"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from viewer.store import REPO_ROOT, read_json_file

DB_PATH = REPO_ROOT / "data" / "tau2" / "domains" / "retail" / "db.json"
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


class UnknownRecord(KeyError):
    """No record of this kind has this ID."""


@dataclass
class _Index:
    db: dict
    item_product: dict[str, str] = field(default_factory=dict)
    payment_owner: dict[str, str] = field(default_factory=dict)
    payment_orders: dict[str, list[str]] = field(default_factory=dict)


def _build_index(db: dict) -> _Index:
    index = _Index(db)
    for product_id, product in db["products"].items():
        for item_id in product["variants"]:
            index.item_product[item_id] = product_id
    for user_id, user in db["users"].items():
        for payment_id in user["payment_methods"]:
            index.payment_owner[payment_id] = user_id
    for order_id, order in db["orders"].items():
        for payment_id in {p["payment_method_id"] for p in order["payment_history"]}:
            index.payment_orders.setdefault(payment_id, []).append(order_id)
    return index


def _full_name(user: dict) -> str:
    return f"{user['name']['first_name']} {user['name']['last_name']}"


def _payment_detail(method: dict) -> str:
    if method["source"] == "gift_card":
        return f"balance {method['balance']}"
    if method["source"] == "credit_card":
        return f"{method['brand']} ending {method['last_four']}"
    return ""


def _matches(blob: str, words: list[str]) -> bool:
    return all(word in blob for word in words)


class Database:
    def __init__(self, path: Path = DB_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._index: _Index | None = None

    @property
    def index(self) -> _Index:
        """Load db.json on first use, not at startup."""
        with self._lock:
            if self._index is None:
                self._index = _build_index(read_json_file(self.path))
            return self._index

    def summary(self) -> dict[str, int]:
        index = self.index
        return {
            "users": len(index.db["users"]),
            "orders": len(index.db["orders"]),
            "products": len(index.db["products"]),
            "items": len(index.item_product),
            "payments": len(index.payment_owner),
        }

    def ids(self) -> dict[str, list[str]]:
        """Every ID by kind, so the frontend can check tokens it finds in text."""
        index = self.index
        return {
            "users": list(index.db["users"]),
            "orders": list(index.db["orders"]),
            "products": list(index.db["products"]),
            "items": list(index.item_product),
            "payments": list(index.payment_owner),
        }

    # ---- Lists ----

    def _user_row(self, user_id: str) -> dict[str, Any]:
        user = self.index.db["users"][user_id]
        return {
            "id": user_id,
            "name": _full_name(user),
            "email": user["email"],
            "city": user["address"]["city"],
            "state": user["address"]["state"],
            "zip": user["address"]["zip"],
            "orders": len(user["orders"]),
            "payment_methods": len(user["payment_methods"]),
        }

    def _order_row(self, order_id: str) -> dict[str, Any]:
        db = self.index.db
        order = db["orders"][order_id]
        return {
            "id": order_id,
            "user_id": order["user_id"],
            "user_name": _full_name(db["users"][order["user_id"]]),
            "status": order["status"],
            "items": len(order["items"]),
            "total": round(sum(item["price"] for item in order["items"]), 2),
            "zip": order["address"]["zip"],
        }

    def _product_row(self, product_id: str) -> dict[str, Any]:
        product = self.index.db["products"][product_id]
        variants = product["variants"].values()
        prices = [variant["price"] for variant in variants]
        return {
            "id": product_id,
            "name": product["name"],
            "variants": len(prices),
            "available": sum(1 for variant in variants if variant["available"]),
            "price_min": min(prices),
            "price_max": max(prices),
        }

    def _item_row(self, item_id: str) -> dict[str, Any]:
        product_id = self.index.item_product[item_id]
        product = self.index.db["products"][product_id]
        variant = product["variants"][item_id]
        return {
            "id": item_id,
            "product_id": product_id,
            "product_name": product["name"],
            "options": variant["options"],
            "price": variant["price"],
            "available": variant["available"],
        }

    def _payment_row(self, payment_id: str) -> dict[str, Any]:
        user_id = self.index.payment_owner[payment_id]
        user = self.index.db["users"][user_id]
        method = user["payment_methods"][payment_id]
        return {
            "id": payment_id,
            "source": method["source"],
            "detail": _payment_detail(method),
            "user_id": user_id,
            "user_name": _full_name(user),
        }

    def _lists(self) -> dict[str, tuple[list[str], Callable, Callable]]:
        """For each kind: its IDs, its row builder, and its search text."""
        index = self.index
        db = index.db

        def user_text(user_id: str) -> list[Any]:
            user = db["users"][user_id]
            address = user["address"]
            return [
                user_id,
                _full_name(user),
                user["email"],
                address["city"],
                address["state"],
                address["zip"],
            ]

        def order_text(order_id: str) -> list[Any]:
            order = db["orders"][order_id]
            return [
                order_id,
                order["user_id"],
                _full_name(db["users"][order["user_id"]]),
                order["status"],
                order["address"]["zip"],
                *(item["name"] for item in order["items"]),
                *(item["item_id"] for item in order["items"]),
            ]

        def product_text(product_id: str) -> list[Any]:
            product = db["products"][product_id]
            options = [
                value
                for variant in product["variants"].values()
                for value in variant["options"].values()
            ]
            return [product_id, product["name"], *product["variants"], *options]

        def item_text(item_id: str) -> list[Any]:
            row = self._item_row(item_id)
            return [
                item_id,
                row["product_id"],
                row["product_name"],
                *row["options"].values(),
            ]

        def payment_text(payment_id: str) -> list[Any]:
            row = self._payment_row(payment_id)
            return [payment_id, row["source"], row["user_id"], row["user_name"]]

        return {
            "users": (list(db["users"]), self._user_row, user_text),
            "orders": (list(db["orders"]), self._order_row, order_text),
            "products": (list(db["products"]), self._product_row, product_text),
            "items": (list(index.item_product), self._item_row, item_text),
            "payments": (list(index.payment_owner), self._payment_row, payment_text),
        }

    def search(
        self,
        kind: str,
        query: str = "",
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> dict[str, Any]:
        """One page of records whose text contains every word of the query."""
        lists = self._lists()
        if kind not in lists:
            raise UnknownRecord(kind)
        ids, build_row, text_of = lists[kind]
        words = query.lower().split()
        if words:
            ids = [
                record_id
                for record_id in ids
                if _matches(" ".join(map(str, text_of(record_id))).lower(), words)
            ]
        limit = max(1, min(limit, MAX_PAGE_SIZE))
        offset = max(0, offset)
        return {
            "kind": kind,
            "total": len(ids),
            "offset": offset,
            "limit": limit,
            "rows": [
                build_row(record_id) for record_id in ids[offset : offset + limit]
            ],
        }

    # ---- Single records ----

    def record(self, kind: str, record_id: str) -> dict[str, Any]:
        readers = {
            "users": self._user,
            "orders": self._order,
            "products": self._product,
            "items": self._item,
            "payments": self._payment,
        }
        if kind not in readers:
            raise UnknownRecord(kind)
        try:
            return {"kind": kind, "id": record_id, **readers[kind](record_id)}
        except KeyError:
            raise UnknownRecord(f"{kind}/{record_id}") from None

    def _user(self, user_id: str) -> dict[str, Any]:
        user = self.index.db["users"][user_id]
        return {
            "row": self._user_row(user_id),
            "raw": user,
            "payment_methods": [self._payment_row(p) for p in user["payment_methods"]],
            "orders": [self._order_row(order_id) for order_id in user["orders"]],
        }

    def _order(self, order_id: str) -> dict[str, Any]:
        db = self.index.db
        order = db["orders"][order_id]
        items = []
        for item in order["items"]:
            variant = db["products"][item["product_id"]]["variants"][item["item_id"]]
            items.append({**item, "available_now": variant["available"]})
        return {"row": self._order_row(order_id), "raw": order, "items": items}

    def _product(self, product_id: str) -> dict[str, Any]:
        product = self.index.db["products"][product_id]
        return {
            "row": self._product_row(product_id),
            "raw": product,
            "variants": [self._item_row(item) for item in product["variants"]],
        }

    def _item(self, item_id: str) -> dict[str, Any]:
        product_id = self.index.item_product[item_id]
        return {
            "row": self._item_row(item_id),
            "product": self._product(product_id),
            "raw": self.index.db["products"][product_id]["variants"][item_id],
        }

    def _payment(self, payment_id: str) -> dict[str, Any]:
        user_id = self.index.payment_owner[payment_id]
        orders = self.index.payment_orders.get(payment_id, [])
        return {
            "row": self._payment_row(payment_id),
            "raw": self.index.db["users"][user_id]["payment_methods"][payment_id],
            "orders": [self._order_row(order_id) for order_id in orders],
        }
