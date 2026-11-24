"""Test fixtures. Everything here runs offline: the model is replaced by a scripted
backend, so the tests pin down the harness (gates, memory, transitions, tools,
guard) independently of any model's behaviour."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

os.environ["SOP_LLM_MODE"] = "mock"
os.environ["SOP_TODAY"] = "2026-03-02"
# Pin the models too, so a developer's .env cannot change what the wire tests expect.
os.environ["SOP_MODEL"] = "claude-opus-5-5"
os.environ["SOP_MODEL_UNDERSTAND"] = "claude-haiku-5-5"
# And the serving settings (README, "Scaling and limits"): the suite runs against the defaults
# whatever a developer exported for a load test (a slow offline double, a SQLite store, a limiter).
os.environ["SOP_MOCK_LATENCY_MS"] = "0"
os.environ["SOP_SESSION_STORE"] = "memory"
os.environ["SOP_RATE_LIMIT_PER_MINUTE"] = "0"
os.environ["SOP_TRUST_PROXY"] = "0"
os.environ["SOP_MAX_TURNS_PER_CONVERSATION"] = "60"
os.environ["SOP_MAX_CONCURRENT_TURNS"] = "64"
os.environ["SOP_MAX_SESSIONS"] = "50000"
os.environ["SOP_MAX_STORE_MB"] = "1024"
# The SQL stores' settings too (a developer's .env may set a data key or a short lease).
os.environ["SOP_DATA_KEY"] = ""
os.environ["SOP_TURN_LEASE_SECONDS"] = "20"
os.environ["SOP_TURN_MAX_SECONDS"] = "300"
os.environ["SOP_AUDIT_RETENTION_DAYS"] = "90"
os.environ["SOP_STORE_POOL_SIZE"] = "20"
# No background dispatcher in the app the tests import (a test that wants one builds it), and
# the demo transports whatever a developer configured for real delivery.
os.environ["SOP_DISPATCH"] = "off"
os.environ["SOP_EMAIL_TRANSPORT"] = "demo"
os.environ["SOP_HANDOFF_TRANSPORT"] = "demo"
os.environ["SOP_DISPATCH_MAX_ATTEMPTS"] = "8"
os.environ["SOP_DISPATCH_POLL_SECONDS"] = "1"
os.environ["SOP_SHUTDOWN_GRACE_SECONDS"] = "30"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from insurance_claims.agent import Agent  # noqa: E402
from insurance_claims.config import load_settings  # noqa: E402
from insurance_claims.data import Repository  # noqa: E402
from insurance_claims.mock_llm import MockBackend  # noqa: E402
from insurance_claims.state import SessionState  # noqa: E402


def reading(**overrides: Any) -> dict[str, Any]:
    """A raw 'understanding' as the model would return it, with neutral defaults.

    Shortcuts: identity fields, role/rep_name/relationship and claim hint keys can be
    passed flat, e.g. reading(full_name="Margaret Chen", status="denied").
    """
    identity_keys = ("full_name", "dob", "phone", "email", "id_last4", "policy_number")
    hint_keys = ("case_id", "case_type", "status", "time_reference", "description")
    raw: dict[str, Any] = {
        "summary": "test message",
        "identity": {k: "" for k in identity_keys},
        "caller": {"role": "unknown", "representative_name": "", "relationship": ""},
        "claim_hints": {k: "" for k in hint_keys},
        "intent": "none",
        "scope": "in_scope",
        "out_of_scope_topic": "",
        "emotion": "neutral",
        "emotion_intensity": "none",
        "refusal": "none",
        "refused_fields": [],
        "asks_why": False,
        "requests_human": False,
        "wants_to_end": False,
        "wants_different_claim": False,
        "yes_no": "none",
        "email_summary_choice": "none",
        "email_to_other_address": False,
        "notes": [],
    }
    for key, value in overrides.items():
        if key in identity_keys:
            raw["identity"][key] = value
        elif key in hint_keys:
            raw["claim_hints"][key] = value
        elif key == "role":
            raw["caller"]["role"] = value
        elif key == "rep_name":
            raw["caller"]["representative_name"] = value
        elif key == "relationship":
            raw["caller"]["relationship"] = value
        else:
            raw[key] = value
    return raw


class ScriptedBackend(MockBackend):
    """Understanding comes from the test; claim resolution, summaries and reply
    wording use the deterministic mock."""

    name = "scripted"

    def __init__(self) -> None:
        self.next_reading: dict[str, Any] | None = None
        self.forced_reply: str | None = None
        self.replies_requested = 0

    def understand(self, state, user_text, settings):  # type: ignore[override]
        if self.next_reading is None:
            return super().understand(state, user_text, settings)
        raw, self.next_reading = self.next_reading, None
        return raw, {"call": "understand", "model": "scripted"}

    def respond(self, state, directive, control_block, tool_specs, tool_executor, settings):  # type: ignore[override]
        self.replies_requested += 1
        self.last_control_block = control_block
        self.last_tool_specs = tool_specs
        result = super().respond(state, directive, control_block, tool_specs, tool_executor, settings)
        if self.forced_reply is not None:
            result.text = self.forced_reply
        return result


class Conversation:
    def __init__(self, agent: Agent, backend: ScriptedBackend, scenario: str = "default") -> None:
        self.agent = agent
        self.backend = backend
        self.state: SessionState = agent.new_session(scenario)
        self.last_trace: dict[str, Any] = {}

    def say(self, text: str = "(test)", **read: Any) -> str:
        """Send a caller message. With keyword arguments the understanding is scripted;
        without, the mock's regex reading of `text` is used."""
        if read:
            self.backend.next_reading = reading(**read)
        reply, self.last_trace = self.agent.handle_turn(self.state, text, self.backend)
        return reply

    def events(self, kind: str) -> list[str]:
        return [e["label"] for e in self.last_trace["events"] if e["kind"] == kind]

    @property
    def action(self) -> str:
        return self.events("directive")[-1]


@pytest.fixture(scope="session")
def settings():
    return load_settings()


@pytest.fixture(autouse=True)
def _model_breaker_starts_closed():
    """The model circuit breaker is one per process: no test inherits another test's failures."""
    from insurance_claims import llm

    llm.reset_model_health()
    yield


@pytest.fixture(scope="session")
def repo():
    return Repository.load()


@pytest.fixture()
def backend():
    return ScriptedBackend()


@pytest.fixture()
def agent(repo, settings):
    return Agent(repo, settings)


@pytest.fixture()
def convo(agent, backend):
    return Conversation(agent, backend)


@pytest.fixture()
def new_convo(agent, backend):
    def make(scenario: str = "default") -> Conversation:
        return Conversation(agent, backend, scenario)

    return make


MARGARET = {"full_name": "Margaret Chen", "dob": "1985-03-15", "id_last4": "4472"}


@pytest.fixture()
def verified(convo):
    """A conversation where Margaret Chen (P9) has just been verified with no stated need."""
    convo.say("id", **MARGARET)
    assert convo.state.verified
    return convo


# ---------------------------------------------------------------------------
# Session stores (tests/test_store*.py): a fake clock, PostgreSQL, a store of each kind
# ---------------------------------------------------------------------------
class StoreClock:
    """A store's notion of time, moved by hand (waiting for a busy conversation still uses real
    time). PostgreSQL stores apply the difference to the database's clock."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class EmbeddedPostgres:
    """A throwaway PostgreSQL server (pixeltable-pgserver's binaries) on a fixed port of
    127.0.0.1, which a test can stop and start again: the URL stays the same."""

    restartable = True

    def __init__(self, bin_dir: Path, pgdata: Path, template: Path) -> None:
        import socket

        self.bin_dir, self.pgdata, self.template = bin_dir, pgdata, template
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        self.url = f"postgresql://postgres@127.0.0.1:{self.port}/postgres"
        self.log = pgdata.parent / f"{pgdata.name}.log"

    def _run(self, name: str, *args: str, timeout: float = 120) -> int:
        import subprocess

        exe = self.bin_dir / (f"{name}.exe" if os.name == "nt" else name)
        # No pipes: the server inherits pg_ctl's output handles, so a pipe would never see EOF.
        with open(self.log, "ab") as log:
            return subprocess.run(
                [str(exe), *args],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                timeout=timeout,
                check=False,
                creationflags=0x00000200 | 0x08000000 if os.name == "nt" else 0,  # own group, no window
            ).returncode

    def start(self) -> None:
        options = f"-h 127.0.0.1 -p {self.port}" + ("" if os.name == "nt" else " -c unix_socket_directories=")
        if self._run("pg_ctl", "-w", "-s", "-t", "60", "-o", options, "-D", str(self.pgdata), "start"):
            raise RuntimeError(f"pg_ctl start failed; see {self.log}")

    def stop(self, mode: str = "fast") -> None:
        self._run("pg_ctl", "-w", "-s", "-m", mode, "-D", str(self.pgdata), "stop", timeout=60)

    def copy(self, pgdata: Path) -> EmbeddedPostgres:
        """Another server, from the same initialised template."""
        import shutil

        shutil.copytree(self.template, pgdata)
        return EmbeddedPostgres(self.bin_dir, pgdata, self.template)


class ExternalPostgres:
    restartable = False

    def __init__(self, url: str) -> None:
        self.url = url


def _embedded_postgres_reason() -> str | None:
    """Why the embedded PostgreSQL server cannot run here (None if it can)."""
    try:
        import pixeltable_pgserver  # noqa: F401
    except ImportError:
        return (
            "PostgreSQL store tests need SOP_TEST_POSTGRES_URL (a server where the tests may create "
            "schemas) or pixeltable-pgserver for an embedded server (requirements-dev.txt installs it on Windows)"
        )
    if os.name != "nt" and os.geteuid() == 0:
        return (
            "PostgreSQL refuses to run as root, so the embedded server cannot start here: set "
            "SOP_TEST_POSTGRES_URL to run the PostgreSQL store tests"
        )
    return None


def _postgres_template(request: Any, tmp_path_factory: Any, bin_dir: Path, version: int) -> Path:
    """An initialised data directory to copy (initdb takes ~25 s on Windows; a copy ~1 s). Kept
    in pytest's cache between runs when the cache is on."""
    import shutil
    import subprocess
    import uuid

    cache = getattr(request.config, "cache", None)
    root = Path(cache.mkdir("sop-store-postgres")) if cache is not None else tmp_path_factory.mktemp("pg-template")
    template = root / f"pgdata-{version}"
    if (template / "PG_VERSION").exists():
        return template
    staging = root / f"staging-{os.getpid()}-{uuid.uuid4().hex[:6]}"
    initdb = bin_dir / ("initdb.exe" if os.name == "nt" else "initdb")
    with open(root / "initdb.log", "ab") as log:
        subprocess.run(
            [str(initdb), "--auth=trust", "--auth-local=trust", "--encoding=utf8", "-U", "postgres", "-D", str(staging)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            timeout=300,
            check=True,
        )
    try:
        staging.rename(template)
    except OSError:  # another session made it meanwhile
        shutil.rmtree(staging, ignore_errors=True)
    return template


@pytest.fixture(scope="session")
def postgres_server(request, tmp_path_factory):
    """SOP_TEST_POSTGRES_URL, or one embedded server for the whole session, or a skip."""
    url = os.environ.get("SOP_TEST_POSTGRES_URL", "").strip()
    if url:
        yield ExternalPostgres(url)
        return
    reason = _embedded_postgres_reason()
    if reason:
        pytest.skip(reason)
    from pixeltable_pgserver.utils import POSTGRES_VERSIONS, TARGET_POSTGRES_VERSION

    bin_dir = POSTGRES_VERSIONS[TARGET_POSTGRES_VERSION]
    try:
        template = _postgres_template(request, tmp_path_factory, bin_dir, TARGET_POSTGRES_VERSION)
        server = EmbeddedPostgres(bin_dir, template, template).copy(tmp_path_factory.mktemp("pg") / "pgdata")
        server.start()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"the embedded PostgreSQL server (pixeltable-pgserver) could not start: {exc}")
    yield server
    server.stop()


@pytest.fixture()
def postgres_url(postgres_server):
    """A URL whose tables land in a fresh schema of their own (dropped afterwards)."""
    import uuid

    import psycopg

    schema = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(postgres_server.url, autocommit=True, connect_timeout=10) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in postgres_server.url else "?"
    yield f"{postgres_server.url}{separator}options=-csearch_path%3D{schema}"
    try:
        with psycopg.connect(
            postgres_server.url, autocommit=True, connect_timeout=10, options="-c lock_timeout=10000"
        ) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    except psycopg.Error:
        pass


@pytest.fixture()
def restartable_postgres(postgres_server, tmp_path):
    """A server of its own that the test may stop and start (embedded servers only)."""
    if not postgres_server.restartable:
        pytest.skip("needs the embedded PostgreSQL server, to stop and start it (SOP_TEST_POSTGRES_URL is set)")
    server = postgres_server.copy(tmp_path / "pgdata")
    server.start()
    yield server
    server.stop("immediate")


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store_kind(request):
    return request.param


@pytest.fixture()
def store_factory(store_kind, tmp_path, request):
    """store_factory(clock=None, real_time=False, **options) -> a store of the parametrised kind,
    on a StoreClock unless real_time. The SQL ones of one test share one file or database, and
    run without their background thread unless asked (background=True): tests drive renewals
    and sweeps by hand."""
    from insurance_claims.store import MemoryStore, PostgresStore, SqliteStore

    made: list[Any] = []

    def make(clock: StoreClock | None = None, real_time: bool = False, **options: Any) -> Any:
        clock = None if real_time else clock if clock is not None else StoreClock()
        options.setdefault("ttl_seconds", 100)
        options.setdefault("max_sessions", 1000)
        options.setdefault("wait_seconds", 0.2)
        if store_kind == "memory":
            store = MemoryStore(clock=clock, **options)
        else:
            options.setdefault("lease_seconds", 30)
            options.setdefault("background", False)
            if store_kind == "sqlite":
                store = SqliteStore(options.pop("path", tmp_path / "sessions.db"), clock=clock, **options)
            else:
                store = PostgresStore(request.getfixturevalue("postgres_url"), clock=clock, **options)
        store.test_clock = clock
        made.append(store)
        return store

    yield make
    for store in made:
        store.close()  # so the temporary SQLite files can be deleted (Windows keeps open files)


@pytest.fixture()
def app_on(store_factory, monkeypatch):
    """app_on(store=None, **settings) -> (client, runtime): the app serving a store of the
    parametrised kind (a new one, in real time, unless given) with those settings changed."""
    import dataclasses

    from fastapi.testclient import TestClient

    from insurance_claims import server

    def make(store: Any = None, **changes: Any) -> tuple[Any, Any]:
        store = store if store is not None else store_factory(real_time=True)
        rt = server.Runtime(dataclasses.replace(server.settings, **changes), store=store)
        monkeypatch.setattr(server, "runtime", rt)
        return TestClient(server.app), rt

    return make
