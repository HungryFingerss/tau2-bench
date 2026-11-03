"""HTTP routes. Saving an annotation is the only write to disk, and it never touches
run data. Tool sessions write to an in-memory database inside the tools worker."""

from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from viewer import insight, pages
from viewer.annotations import Annotations, InvalidAnnotation, LabelsFileError
from viewer.changes import Changes
from viewer.database import DEFAULT_PAGE_SIZE, Database, UnknownRecord
from viewer.designs import Designs, UnknownDesign
from viewer.fde_tests import SuiteRunner
from viewer.pages import NotFound
from viewer.replay import Replayer
from viewer.store import Store, UnknownRun
from viewer.tools import MAX_SESSION_STEPS, SessionStep, ToolRunner, load_presets

STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
LOCAL_HOSTS = {"127.0.0.1", "localhost"}


class LabelInput(BaseModel):
    id: str
    decisions: list[str] = []


class AnnotationInput(BaseModel):
    labels: list[LabelInput] = []
    note: str = ""


class SessionInput(BaseModel):
    steps: list[SessionStep] = Field(default_factory=list, max_length=MAX_SESSION_STEPS)


def create_app(
    simulations_dir: Path | None = None,
    replayer: Replayer | None = None,
    database: Database | None = None,
    designs: Designs | None = None,
    notes: Annotations | None = None,
    tools: ToolRunner | None = None,
    tests: SuiteRunner | None = None,
    presets_file: Path | None = None,
) -> FastAPI:
    store = Store(simulations_dir)
    replayer = replayer or Replayer()
    database = database or Database()
    tools = tools or ToolRunner()
    tests = tests or SuiteRunner()
    ctx = insight.Context(store, designs or Designs(), notes or Annotations())
    changes = Changes(store)
    app = FastAPI(title="Trace Viewer", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"]
    )

    @app.middleware("http")
    async def local_writes_only(request: Request, call_next):
        """Stop another website from posting here through the user's browser.

        A form on a foreign page cannot send JSON without a preflight this app never
        answers, and a browser always reveals where such a request came from.
        """
        if request.method not in ("GET", "HEAD"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
                return JSONResponse({"detail": "Local pages only."}, status_code=403)
            content_type = request.headers.get("content-type", "")
            if not content_type.startswith("application/json"):
                return JSONResponse({"detail": "Send JSON."}, status_code=415)
        return await call_next(request)

    @app.middleware("http")
    async def always_revalidate(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(UnknownDesign)
    async def unknown_design(_: Request, error: UnknownDesign) -> JSONResponse:
        return JSONResponse({"detail": f"Unknown design: {error}"}, status_code=404)

    @app.exception_handler(InvalidAnnotation)
    async def invalid_annotation(_: Request, error: InvalidAnnotation) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(LabelsFileError)
    async def labels_problem(_: Request, error: LabelsFileError) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=422)

    @app.exception_handler(UnknownRun)
    async def unknown_run(_: Request, error: UnknownRun) -> JSONResponse:
        return JSONResponse({"detail": f"Unknown run: {error}"}, status_code=404)

    @app.exception_handler(NotFound)
    async def not_found(_: Request, error: NotFound) -> JSONResponse:
        return JSONResponse({"detail": str(error)}, status_code=404)

    @app.exception_handler(UnknownRecord)
    async def unknown_record(_: Request, error: UnknownRecord) -> JSONResponse:
        return JSONResponse({"detail": f"Not in database: {error}"}, status_code=404)

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/changes")
    def poll_changes(run: str | None = None) -> dict:
        return changes.snapshot(run)

    @app.get("/api/runs")
    def runs() -> dict:
        return insight.runs_view(ctx)

    @app.get("/api/compare")
    def compare_runs(a: str, b: str) -> dict:
        return insight.compare_view(ctx, a, b)

    @app.get("/api/designs")
    def design_list() -> dict:
        return insight.designs_view(ctx)

    @app.get("/api/designs/compare")
    def compare_designs(a: str, b: str) -> dict:
        return insight.design_compare_view(ctx, a, b)

    @app.get("/api/source")
    def source(file: str) -> dict:
        return insight.source_view(ctx, file)

    @app.get("/api/runs/{run}")
    def run(run: str) -> dict:
        return insight.run_view(ctx, run)

    @app.get("/api/runs/{run}/design")
    def run_design(run: str) -> dict:
        return insight.design_view(ctx, run)

    @app.get("/api/runs/{run}/meta")
    def run_meta(run: str) -> dict:
        return insight.meta_view(ctx, run)

    @app.get("/api/runs/{run}/tasks/{task_id}")
    def task(run: str, task_id: str, trial: int = 0) -> dict:
        return insight.task_view(ctx, run, task_id, trial)

    @app.post("/api/runs/{run}/tasks/{task_id}/annotation")
    def annotate(run: str, task_id: str, body: AnnotationInput, trial: int = 0) -> dict:
        labels = [label.model_dump() for label in body.labels]
        return insight.save_annotation(ctx, run, task_id, trial, labels, body.note)

    @app.get("/api/runs/{run}/sims/{sim_id}/steps/{index}/call")
    def step_call(run: str, sim_id: str, index: int) -> dict:
        return pages.step_call(store, run, sim_id, index)

    @app.get("/api/runs/{run}/sims/{sim_id}/judge-calls")
    def judge_calls(run: str, sim_id: str) -> dict:
        return pages.judge_calls(store, run, sim_id)

    @app.get("/api/runs/{run}/sims/{sim_id}/db-diff")
    def db_diff(run: str, sim_id: str) -> dict:
        return pages.db_diff(store, replayer, run, sim_id)

    @app.get("/api/runs/{run}/attempts/{task_id}/{sim_id}")
    def attempt(run: str, task_id: str, sim_id: str) -> dict:
        return pages.attempt_page(store, run, task_id, sim_id)

    @app.get("/api/runs/{run}/attempts/{task_id}/{sim_id}/calls/{file}")
    def attempt_call(run: str, task_id: str, sim_id: str, file: str) -> dict:
        return pages.attempt_call(store, run, task_id, sim_id, file)

    @app.get("/api/runs/{run}/attempts/{task_id}/{sim_id}/log")
    def attempt_log(run: str, task_id: str, sim_id: str) -> dict:
        return pages.attempt_log(store, run, task_id, sim_id)

    @app.get("/api/db/summary")
    def db_summary() -> dict:
        return database.summary()

    @app.get("/api/db/ids")
    def db_ids() -> dict:
        return database.ids()

    @app.get("/api/db/{kind}")
    def db_list(
        kind: str, q: str = "", offset: int = 0, limit: int = DEFAULT_PAGE_SIZE
    ) -> dict:
        return database.search(kind, q, offset, limit)

    @app.get("/api/db/{kind}/{record_id}")
    def db_record(kind: str, record_id: str) -> dict:
        return database.record(kind, record_id)

    @app.get("/api/tools")
    def tool_catalog() -> dict:
        return tools.catalog()

    @app.get("/api/tools/clear-yes")
    def tool_clear_yes(text: str = "") -> dict:
        return tools.clear_yes(text)

    @app.post("/api/tools/session")
    def tool_session(body: SessionInput) -> dict:
        return tools.run_session([step.model_dump() for step in body.steps])

    @app.get("/api/tools/presets")
    def tool_presets() -> dict:
        return load_presets(presets_file) if presets_file else load_presets()

    @app.post("/api/tools/tests")
    def run_fde_tests() -> dict:
        return tests.run()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
