"""Recorder contracts + the live ``JsonFileRecorder`` implementation.

Design constraints carried forward from the plan:

  * **Recorder failures must never break a live turn.** All on_* methods
    swallow exceptions; the worker's main loop never sees recorder
    errors as turn errors.
  * **Recording is opt-in.** ``Recorder`` is the noop singleton until
    ``init_from_env`` swaps in a ``JsonFileRecorder``.
  * **Raw is the source of truth.** The recorder writes raw JSON only.
    Semantic / intent / narrative layers are produced offline by the
    annotate script.

Cross-process model:

  Worker and API both read ``POLARIS_RECORD=<path/to/raw/scenario.json>``.
  Each derives ``staging_dir_for(path)`` (sibling ``.staging-<scenario>/``)
  and appends events to per-source JSONL files there:

      .staging-golf-landing-page/
      ├── codex-frames.jsonl          ← worker writes via on_codex_frame
      ├── design-intent-nodes.jsonl   ← worker writes via on_design_intent_node
      └── user-actions.jsonl          ← api writes via on_user_action

  Append-only JSONL avoids cross-process coordination — POSIX guarantees
  ``O_APPEND`` writes are atomic up to PIPE_BUF.  Lines carry absolute
  ``ts`` (epoch seconds), so ``merge_staging`` can rebase to a common t0
  without trusting per-process clocks during recording.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import shutil
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ── Public dataclasses ─────────────────────────────────────────────────


@dataclass
class UserActionEvent:
    """One user-side action observed by the web recorder."""

    t: float
    """Seconds since recording start (web-side clock)."""

    kind: str
    """``click`` | ``type`` | ``navigate`` | ``answer_clarification`` | ``wait_*``."""

    concrete: dict[str, Any] = field(default_factory=dict)
    """Raw selector / coords / payload — whatever the web recorder captured."""

    a11y_snapshot: dict[str, Any] | None = None
    """Optional accessibility tree slice taken at action time."""


class RecorderProtocol(Protocol):
    scenario: str
    fixture_path: Path

    async def start(self) -> None: ...

    async def on_codex_frame(self, direction: str, frame: dict[str, Any]) -> None: ...

    async def on_design_intent_node(
        self, node_name: str, node_output: dict[str, Any]
    ) -> None: ...

    async def on_user_action(self, event: UserActionEvent) -> None: ...

    async def finalize(self) -> Path: ...


# ── Helpers ────────────────────────────────────────────────────────────


def staging_dir_for(fixture_path: Path) -> Path:
    """Derive the staging dir from the fixture output path.

    Both worker and api compute this independently from POLARIS_RECORD,
    so they always converge on the same dir without explicit coordination.
    Sits as a sibling to the final raw JSON so cleanup is local.

    Handles both ``.json`` and ``.json.gz`` — ``Path.stem`` only strips
    one extension, so a gzipped fixture's stem is ``<scenario>.json``,
    which would produce a staging dir like ``.staging-foo.json`` and
    desync from the api side.  Strip both so the dir name is always
    just ``<scenario>``.
    """
    return fixture_path.parent / f".staging-{_scenario_from_fixture_path(fixture_path)}"


# Schema version baked into emitted raw fixtures.  Mirrors
# ``tests/fixtures/replay/_schema.SCHEMA_VERSION`` — keep them in sync.
_SCHEMA_VERSION = 1


def _make_jsonable(obj: Any) -> Any:
    """Best-effort conversion of arbitrary node outputs to JSON-friendly form.

    LangGraph nodes return dicts but the values can be LangChain Message
    objects, pydantic models, or other non-stdlib types.  We try a few
    standard tricks before falling back to a truncated ``repr`` — that
    way the recorder never crashes the graph just because some node
    output included a fancy object.

    Annotation pass is allowed to lose information here; the goal is
    durability, not perfect fidelity.
    """
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if isinstance(obj, dict):
        return {str(k): _make_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_make_jsonable(x) for x in obj]
    if isinstance(obj, (set, frozenset)):
        return [_make_jsonable(x) for x in obj]
    # Pydantic v2 — covers DesignIntent, CompiledBrief, PinterestRef.
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:
            pass
    # LangChain BaseMessage shape.
    if hasattr(obj, "type") and hasattr(obj, "content"):
        try:
            return {
                "_message_type": getattr(obj, "type", None),
                "content": _make_jsonable(getattr(obj, "content", None)),
            }
        except Exception:
            pass
    # Anything else — log a stable string so the annotator can flag it.
    return {"_repr": repr(obj)[:500]}


# ── Null-object recorder ───────────────────────────────────────────────


class _NoopRecorder:
    """Default recorder used when no ``POLARIS_RECORD`` path is set.

    Its presence at module load means callers never have to branch on
    ``recorder is None`` — they can always ``await recorder.on_*``.
    """

    scenario: str = ""
    fixture_path: Path = Path("/dev/null")

    async def start(self) -> None:
        return None

    async def on_codex_frame(self, direction: str, frame: dict[str, Any]) -> None:
        return None

    async def on_design_intent_node(
        self, node_name: str, node_output: dict[str, Any]
    ) -> None:
        return None

    async def on_user_action(self, event: UserActionEvent) -> None:
        return None

    async def finalize(self) -> Path:
        return self.fixture_path


# ── Real recorder ──────────────────────────────────────────────────────


class JsonFileRecorder:
    """Append-only JSONL recorder.

    Each tap call appends one line to one of three files in the staging
    dir.  ``merge_staging`` later combines them into a single raw fixture
    JSON.  Why JSONL instead of in-memory buffering:

      * Survives worker crashes mid-recording — events written are
        durable as soon as fsync returns.
      * Cross-process safe — POSIX ``O_APPEND`` makes line-sized writes
        atomic, so worker and api can both append to user-actions.jsonl
        without locks (the api won't actually write codex/design-intent
        files; we keep the contract symmetric in case future recorders
        emit from the api process).
      * No global state — buffer never grows unboundedly in memory.
    """

    def __init__(self, fixture_path: Path, scenario: str | None = None) -> None:
        self.fixture_path = fixture_path
        self.scenario = scenario or fixture_path.stem
        self._staging = staging_dir_for(fixture_path)
        self._staging.mkdir(parents=True, exist_ok=True)
        # Single in-process lock to avoid interleaved JSON within one
        # line.  Cross-process atomicity is provided by O_APPEND, which
        # the kernel handles independently.
        self._lock = asyncio.Lock()
        marker = self._staging / "MARKER"
        if not marker.exists():
            marker.write_text(
                f"recording: {self.scenario}\n"
                f"started_at: {datetime.now(timezone.utc).isoformat()}\n"
                f"output: {self.fixture_path}\n"
            )

    async def start(self) -> None:
        logger.info(
            "replay recorder live: scenario=%s output=%s staging=%s",
            self.scenario,
            self.fixture_path,
            self._staging,
        )

    async def on_codex_frame(self, direction: str, frame: dict[str, Any]) -> None:
        try:
            await self._append(
                "codex-frames.jsonl",
                {
                    "ts": time.time(),
                    "direction": direction,
                    "frame": _make_jsonable(frame),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("recorder.on_codex_frame failed: %s", exc)

    async def on_design_intent_node(
        self, node_name: str, node_output: dict[str, Any]
    ) -> None:
        try:
            await self._append(
                "design-intent-nodes.jsonl",
                {
                    "ts": time.time(),
                    "node": node_name,
                    "output": _make_jsonable(node_output),
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("recorder.on_design_intent_node failed: %s", exc)

    async def on_user_action(self, event: UserActionEvent) -> None:
        try:
            payload = {"ts": time.time(), **asdict(event)}
            await self._append("user-actions.jsonl", payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("recorder.on_user_action failed: %s", exc)

    async def _append(self, filename: str, payload: dict[str, Any]) -> None:
        path = self._staging / filename
        line = json.dumps(payload, default=str)
        async with self._lock:
            await asyncio.to_thread(_sync_append, path, line)

    async def finalize(self) -> Path:
        """Recorder no-op finalize.

        The actual merge from JSONL → raw fixture is :func:`merge_staging`,
        a pure function the operator (or the API's
        ``/replay/record/finalize`` route) calls explicitly.  Splitting
        concerns means the recorder can be killed mid-flight (worker
        crash) and we still have a complete JSONL trail to merge later.
        """
        logger.info(
            "replay recorder finalize: staging=%s ready for merge_staging()",
            self._staging,
        )
        return self.fixture_path


def _sync_append(path: Path, line: str) -> None:
    """Append a single line atomically.

    ``open(..., 'a')`` opens the file with ``O_APPEND``; on Linux the
    kernel guarantees writes ≤ PIPE_BUF (4096 bytes) are atomic with
    respect to other appenders.  Our JSONL lines stay well under that
    even for full codex notifications because we don't pretty-print.
    """
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


# ── Cross-process merge ────────────────────────────────────────────────


def merge_staging(fixture_path: Path, *, cleanup: bool = True) -> Path:
    """Combine all staging JSONLs into a single raw fixture and write it.

    Steps:
      1. Read every line from the three JSONLs (codex / design-intent /
         user-actions).  Malformed lines are skipped with a warning.
      2. Compute ``t0 = min(ts)`` across all sources so timestamps in
         the merged fixture are relative to recording start regardless
         of which process emitted first.
      3. Sort each event stream by relative ``t``.
      4. Write ``raw/<scenario>.json`` matching the ``RawFixture`` schema.
      5. Optionally rmtree the staging dir (the default).

    Idempotent: re-running on an already-merged staging dir produces the
    same output (until staging is wiped).  Operator can disable cleanup
    with ``cleanup=False`` to inspect the staging files post-merge.
    """
    staging = staging_dir_for(fixture_path)
    if not staging.is_dir():
        raise FileNotFoundError(
            f"no staging dir at {staging} — was POLARIS_RECORD set on "
            "both api and worker for this scenario?"
        )

    codex_lines = _read_jsonl(staging / "codex-frames.jsonl")
    design_lines = _read_jsonl(staging / "design-intent-nodes.jsonl")
    user_lines = _read_jsonl(staging / "user-actions.jsonl")

    all_ts: list[float] = []
    for l in (*codex_lines, *design_lines, *user_lines):
        ts = l.get("ts")
        if isinstance(ts, (int, float)):
            all_ts.append(float(ts))
    t0 = min(all_ts) if all_ts else time.time()

    user_actions: list[dict[str, Any]] = []
    for line in user_lines:
        ts = line.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        user_actions.append(
            {
                "t": float(ts) - t0,
                "kind": line.get("kind", ""),
                "concrete": line.get("concrete", {}) or {},
                "a11y_snapshot": line.get("a11y_snapshot"),
            }
        )
    user_actions.sort(key=lambda x: x["t"])

    codex_frames: list[dict[str, Any]] = []
    for line in codex_lines:
        ts = line.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        codex_frames.append(
            {
                "t": float(ts) - t0,
                "direction": line.get("direction", "in"),
                "frame": line.get("frame", {}) or {},
            }
        )
    codex_frames.sort(key=lambda x: x["t"])

    design_intent_nodes: list[dict[str, Any]] = []
    for line in design_lines:
        ts = line.get("ts")
        if not isinstance(ts, (int, float)):
            continue
        design_intent_nodes.append(
            {
                "t": float(ts) - t0,
                "node": line.get("node", ""),
                "output": line.get("output", {}) or {},
            }
        )
    design_intent_nodes.sort(key=lambda x: x["t"])

    raw = {
        "version": _SCHEMA_VERSION,
        "scenario": _scenario_from_fixture_path(fixture_path),
        "recorded_at": datetime.fromtimestamp(t0, tz=timezone.utc).isoformat(),
        "user_actions": user_actions,
        "agent_io": {
            "codex_frames": codex_frames,
            "design_intent_nodes": design_intent_nodes,
        },
    }

    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    if fixture_path.suffix == ".gz":
        # Gzip path: minify (no indent, no extra whitespace) + gzip.
        # Real recordings hit ~5-10 MB raw; gzip cuts that 35–45 %, JSON
        # minify alone barely moves the needle because most bytes are
        # unique LangChain messages.  Combined we typically land
        # <6 MB which is fine to git-commit.
        body = json.dumps(
            raw, separators=(",", ":"), default=str, ensure_ascii=False
        ).encode("utf-8")
        with gzip.open(fixture_path, "wb", compresslevel=9) as f:
            f.write(body)
    else:
        # Plain JSON path — kept for the dummy fixture / debugging.
        # Pretty-printed so manual inspection is bearable.
        fixture_path.write_text(
            json.dumps(raw, indent=2, default=str, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if cleanup:
        shutil.rmtree(staging, ignore_errors=True)
    return fixture_path


def _scenario_from_fixture_path(fixture_path: Path) -> str:
    """Strip both ``.json`` and ``.json.gz`` to get the scenario stem.

    ``Path.stem`` only strips one extension, so a ``.json.gz`` file's
    stem is ``<scenario>.json``.  The double-strip keeps the in-fixture
    ``scenario`` field consistent regardless of compression choice.
    """
    name = fixture_path.name
    for suffix in (".json.gz", ".json"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return fixture_path.stem


def load_raw_fixture(fixture_path: Path) -> dict[str, Any]:
    """Read a raw fixture from disk, transparently un-gzipping when needed.

    Used by the replay reader and by tests that load fixtures for
    assertions.  We accept both extensions because the dummy stays
    plain (so PRs can eyeball-diff it) while real recordings ship
    gzipped to keep git working trees lean.
    """
    if fixture_path.suffix == ".gz":
        with gzip.open(fixture_path, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    return json.loads(fixture_path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning(
                "merge_staging: skipping malformed line in %s: %s (%s)",
                path.name,
                line[:80],
                exc,
            )
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


# ── Module-level singleton ─────────────────────────────────────────────
#
# Bootstrap (Phase 1, see bootstrap.py) swaps this at process start when
# ``POLARIS_RECORD`` is set.  Until then, every tap call hits the noop
# and returns immediately.

Recorder: RecorderProtocol = _NoopRecorder()


def install_recorder(recorder: RecorderProtocol) -> None:
    """Replace the module-level ``Recorder`` singleton.

    Used by the bootstrap module after instantiating ``JsonFileRecorder``
    from env, and by tests that want to inject a fake recorder without
    monkey-patching.
    """
    global Recorder
    Recorder = recorder
