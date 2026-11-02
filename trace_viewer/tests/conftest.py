import pytest
from fastapi.testclient import TestClient

from viewer.api import create_app
from viewer.store import DEFAULT_SIMULATIONS_DIR, Store

BASELINE = "baseline-practice-10-v2"


@pytest.fixture(scope="session")
def store() -> Store:
    """The real simulations folder. It is gitignored, so it may not exist."""
    if not (DEFAULT_SIMULATIONS_DIR / BASELINE).is_dir():
        pytest.skip(f"{BASELINE} is not on this machine")
    return Store()


@pytest.fixture(scope="session")
def client(store: Store) -> TestClient:
    return TestClient(create_app(store.simulations_dir))
