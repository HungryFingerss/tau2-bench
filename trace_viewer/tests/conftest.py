import pytest
from fastapi.testclient import TestClient

from viewer.api import create_app
from viewer.store import DEFAULT_SIMULATIONS_DIR, REPO_ROOT, Store

BASELINE = "baseline-practice-10-v2"
FDE_AGENT = REPO_ROOT / "fde_agent"


def file_states(folder) -> dict:
    return {path: path.stat().st_mtime_ns for path in folder.rglob("*")}


@pytest.fixture(scope="module")
def fde_agent_untouched():
    """Fails the module if its tests added or changed any file in fde_agent/."""
    before = file_states(FDE_AGENT) if FDE_AGENT.is_dir() else None
    yield
    if before is not None:
        assert file_states(FDE_AGENT) == before, "these tests changed fde_agent/"


@pytest.fixture(scope="session")
def store() -> Store:
    """The real simulations folder. It is gitignored, so it may not exist."""
    if not (DEFAULT_SIMULATIONS_DIR / BASELINE).is_dir():
        pytest.skip(f"{BASELINE} is not on this machine")
    return Store()


@pytest.fixture(scope="session")
def client(store: Store) -> TestClient:
    return TestClient(create_app(store.simulations_dir))
