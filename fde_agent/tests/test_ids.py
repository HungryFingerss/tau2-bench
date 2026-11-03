import pytest

from fde_agent.ids import ITEM, PRODUCT, IdError, SeenIds, label, unlabel


def make_seen():
    seen = SeenIds()
    seen.remember(ITEM, "8722653925")
    seen.remember(PRODUCT, "4896585277")
    return seen


def test_label_adds_prefix():
    assert label(ITEM, "8722653925") == "item_8722653925"
    assert label(PRODUCT, "4896585277") == "product_4896585277"


def test_label_rejects_unknown_kind():
    with pytest.raises(ValueError):
        label("order", "#W123")


def test_unlabel_round_trip():
    seen = make_seen()
    assert unlabel(ITEM, "item_8722653925", seen) == "8722653925"
    assert unlabel(PRODUCT, " product_4896585277 ", seen) == "4896585277"  # spaces ok


def test_wrong_kind_is_caught():
    with pytest.raises(IdError, match="is a product ID, but an item ID is needed"):
        unlabel(ITEM, "product_4896585277", make_seen())


def test_bare_id_is_caught():
    with pytest.raises(IdError, match="starts with 'item_'"):
        unlabel(ITEM, "8722653925", make_seen())


def test_unseen_id_is_caught():
    with pytest.raises(IdError, match="has not appeared"):
        unlabel(ITEM, "item_1111111111", make_seen())


def test_seen_is_per_kind():
    # same digits remembered as an item do not count as a product
    seen = SeenIds()
    seen.remember(ITEM, "123")
    with pytest.raises(IdError, match="has not appeared"):
        unlabel(PRODUCT, "product_123", seen)


def test_empty_or_none_is_caught():
    with pytest.raises(IdError):
        unlabel(ITEM, "", make_seen())
    with pytest.raises(IdError):
        unlabel(ITEM, None, make_seen())
