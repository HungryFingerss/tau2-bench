"""The database explorer, against the real db.json."""

import pytest
from fastapi.testclient import TestClient

from viewer.api import create_app
from viewer.database import DB_PATH, MAX_PAGE_SIZE, Database, UnknownRecord

pytestmark = pytest.mark.skipif(not DB_PATH.is_file(), reason="db.json is missing")


@pytest.fixture(scope="module")
def database() -> Database:
    return Database()


@pytest.fixture(scope="module")
def client(database, tmp_path_factory) -> TestClient:
    empty = tmp_path_factory.mktemp("runs")
    return TestClient(create_app(empty, database=database))


def test_counts(database):
    assert database.summary() == {
        "users": 500,
        "orders": 1000,
        "products": 50,
        "items": 591,
        "payments": 695,
    }


def test_nothing_is_read_until_needed(monkeypatch):
    def fail(_):
        raise AssertionError("db.json was read too early")

    monkeypatch.setattr("viewer.database.read_json_file", fail)
    Database()


def test_ids_cover_every_record(database):
    ids = database.ids()
    assert {kind: len(values) for kind, values in ids.items()} == database.summary()
    assert "#W2378156" in ids["orders"]
    assert "yusuf_rossi_9620" in ids["users"]


def test_product_ids_and_item_ids_never_collide(database):
    ids = database.ids()
    assert not set(ids["products"]) & set(ids["items"])


def test_tshirt_has_12_variants_10_available(database):
    rows = database.search("products", "T-Shirt")["rows"]
    assert len(rows) == 1
    assert rows[0]["variants"] == 12
    assert rows[0]["available"] == 10

    product = database.record("products", rows[0]["id"])
    assert len(product["variants"]) == 12
    assert sum(variant["available"] for variant in product["variants"]) == 10
    assert all({"options", "price", "available"} <= set(v) for v in product["variants"])


def test_item_points_back_to_its_product(database):
    item = database.record("items", "4983901480")
    assert item["row"]["product_name"] == "Smart Thermostat"
    assert item["row"]["id"] in {v["id"] for v in item["product"]["variants"]}


def test_order_links_its_user_and_items(database):
    order = database.record("orders", "#W2378156")
    assert order["row"]["user_id"] == "yusuf_rossi_9620"
    assert order["row"]["user_name"] == "Yusuf Rossi"
    assert len(order["items"]) == 5
    assert all(isinstance(item["available_now"], bool) for item in order["items"])
    assert order["row"]["total"] == round(
        sum(item["price"] for item in order["raw"]["items"]), 2
    )


def test_user_lists_orders_and_payment_methods(database):
    user = database.record("users", "yusuf_rossi_9620")
    assert "#W2378156" in {order["id"] for order in user["orders"]}
    assert "credit_card_9513926" in {p["id"] for p in user["payment_methods"]}


def test_payment_method_has_owner_and_orders(database):
    payment = database.record("payments", "credit_card_9513926")
    assert payment["row"]["user_id"] == "yusuf_rossi_9620"
    assert payment["row"]["source"] == "credit_card"
    assert "#W2378156" in {order["id"] for order in payment["orders"]}


def test_gift_card_shows_its_balance(database):
    gift_card = next(i for i in database.ids()["payments"] if i.startswith("gift_card"))
    assert database.record("payments", gift_card)["row"]["detail"].startswith("balance")


@pytest.mark.parametrize(
    "kind, query, expected",
    [
        ("users", "yusuf_rossi_9620", "yusuf_rossi_9620"),
        ("users", "Yusuf Rossi", "yusuf_rossi_9620"),
        ("users", "YUSUF 19122", "yusuf_rossi_9620"),
        ("orders", "#W2378156", "#W2378156"),
        ("orders", "w2378156", "#W2378156"),
        ("orders", "yusuf delivered", "#W2378156"),
        ("orders", "4983901480", "#W2378156"),
        ("items", "4983901480", "4983901480"),
        ("items", "thermostat apple", "4983901480"),
    ],
)
def test_search_finds_records(database, kind, query, expected):
    found = database.search(kind, query, limit=MAX_PAGE_SIZE)["rows"]
    assert expected in {row["id"] for row in found}


def test_search_requires_every_word(database):
    assert database.search("users", "yusuf nosuchword")["total"] == 0


def test_paging(database):
    first = database.search("orders", limit=10)
    second = database.search("orders", offset=10, limit=10)
    assert first["total"] == second["total"] == 1000
    assert len(first["rows"]) == 10
    assert not {r["id"] for r in first["rows"]} & {r["id"] for r in second["rows"]}


def test_page_size_is_capped(database):
    assert len(database.search("orders", limit=99999)["rows"]) == MAX_PAGE_SIZE


@pytest.mark.parametrize(
    "kind, record_id",
    [
        ("orders", "#W0001234"),
        ("users", "mei_kovics_8020"),
        ("items", "4202497724"),
        ("products", "6086499569"),
        ("payments", "gift_card_0000000"),
        ("nonsense", "x"),
    ],
)
def test_unknown_records_raise(database, kind, record_id):
    with pytest.raises(UnknownRecord):
        database.record(kind, record_id)


def test_routes(client):
    assert client.get("/api/db/summary").json()["orders"] == 1000
    assert "products" in client.get("/api/db/ids").json()
    assert client.get("/api/db/products", params={"q": "T-Shirt"}).json()["total"] == 1


def test_order_id_with_hash_works_when_url_encoded(client):
    response = client.get("/api/db/orders/%23W2378156")
    assert response.status_code == 200
    assert response.json()["row"]["id"] == "#W2378156"


@pytest.mark.parametrize(
    "path", ["/api/db/orders/%23W0001234", "/api/db/nonsense", "/api/db/nonsense/x"]
)
def test_bad_database_paths_are_404(client, path):
    assert client.get(path).status_code == 404
