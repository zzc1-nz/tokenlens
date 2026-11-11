#!/usr/bin/env python
"""TokenLens load generator — a synthetic agent fleet that stress-tests the platform.

Why this exists
---------------
An observability platform is only trustworthy if it has been exercised with realistic
traffic *and* with the failure modes it claims to detect. This tool is both:

* a **throughput harness** — millions of tokens through ``POST /api/v1/ingest/snapshots``
  with connection pooling, concurrent batches and a target-token stop condition, so the
  write path, rollups and query API can be measured rather than guessed at;
* a **scenario generator** — ``--mode incident`` deliberately emits the three anomaly
  signatures the detector classifies, which turns "the anomaly engine works" into a
  reproducible end-to-end test.

Usage
-----
::

    python tools/simulate.py --server http://127.0.0.1:8787 --mode burst --duration 30 --report
    python tools/simulate.py --mode incident --target-tokens 1000000 --report
    python tools/simulate.py --dry-run --mode incident --target-tokens 100000 --seed 7

    tokenlens simulate --mode steady --duration 10 --report     # same thing via the CLI

Modes
-----
``steady``
    Flat arrival rate; the baseline shape.
``burst``
    Traffic arrives in waves (a few seconds of high rate, then calm) — exercises rollup
    contention and burn-rate smoothing.
``incident``
    A normal fleet plus one misbehaving agent per anomaly class:
      * ``retry-storm``  — ``retries/requests >= 0.15``
      * ``context-bloat`` — ``context_window >= 120_000`` **and** huge input tokens/request
      * ``tool-dump``     — ``tool_result_bytes/requests >= 200_000``
    The agent names are stable so the detector's per-agent baselines are comparable
    across runs.
``mixed``
    Steady traffic for the first half, then an incident window: the realistic "it was
    fine, then it wasn't" shape that the z-score detector is built for.

Contract compliance
-------------------
* Only the **public ingest API** is used (no direct DB writes) — the tool exercises the
  same path a real agent runtime does.
* Batches are capped at 500 snapshots (contract §5 allows up to 5000, but 500 keeps a
  single request comfortably under a second and matches the documented batching advice).
* Every snapshot carries a unique ``event_id`` so retries are idempotent; ``--replay``
  re-sends the first batch to prove duplicate counting works.
* ``--dry-run`` generates and validates locally (through the real ``SnapshotIn`` model)
  without any HTTP, which is what makes the test suite hermetic.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

# --- make the repo importable when run as `python tools/simulate.py` -------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
for _candidate in (REPO_ROOT / ".pylibs", REPO_ROOT):
    if _candidate.exists() and str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

from tokenlens import pricing, timeutil  # noqa: E402
from tokenlens.schemas import SnapshotIn  # noqa: E402
from tokenlens.testing import AGENTS, MODEL_MIX, PROJECTS, pick_model, runtime_for  # noqa: E402

#: Hard cap per request (the contract allows 5000; 500 keeps p99 latency predictable).
MAX_BATCH = 500
INGEST_PATH = "/api/v1/ingest/snapshots"

MODES = ("burst", "steady", "incident", "mixed")

#: Anomaly-class agents. Stable names so detector baselines stay comparable run to run.
INCIDENT_AGENTS = {
    "retry_storm": "agent-retry-storm",
    "context_bloat": "agent-context-bloat",
    "tool_dump": "agent-tool-dump",
}

#: Detector thresholds mirrored from contract §8 — asserted by the tests.
RETRY_STORM_MIN_RATIO = 0.15
CONTEXT_BLOAT_MIN_WINDOW = 120_000
TOOL_DUMP_MIN_BYTES_PER_REQUEST = 200_000

#: The "normal" context window every non-bloating agent reports. Seeded baseline rows use
#: it unconditionally, and the retry-storm / tool-dump incident agents pin it explicitly so
#: their burst rows match their own baseline (ratio 1.0x) and cannot trip the classifier's
#: context-bloat rule — which is evaluated *before* the tool-output rule.
NORMAL_CONTEXT_WINDOW = 200_000

#: Context window the context-bloat agent reports for the detector's *structural* rule.
#: The rule requires ``window >= 1.5 * baseline``. Because the backfill is long enough for
#: the detector to use a real *history* baseline (see :data:`DEFAULT_BACKFILL_MINUTES`),
#: that baseline is clean (200k) and the 1M peak clears the threshold by 5x.
CONTEXT_BLOAT_GROWTH_TARGET = 1_000_000

#: Default backfill window for incident/mixed runs.
#:
#: This must be **longer than the detector's default lookback** (60 min), because the
#: detector draws its baseline from the *history* window ``[now - 6*lookback, now - lookback)``
#: and only falls back to a (contaminated) warmup baseline when that history is empty.
#: A 60-minute backfill puts every row inside the observation window, so the detector
#: rebuilds "calm" from the observation window minus the p95 minutes — which still holds
#: early burst rows and drags the baseline up. Backfilling 180 minutes yields ~120 minutes
#: of genuinely clean history, which is what makes the classification deterministic.
DEFAULT_BACKFILL_MINUTES = 180

#: The incident burst occupies the trailing slice of the backfill window. Kept at the
#: detector's default lookback so the whole spike lands inside the observation window.
BURST_WINDOW_MINUTES = 60


# --------------------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SimConfig:
    """Resolved knobs for one simulation run."""

    server: str = "http://127.0.0.1:8787"
    mode: str = "steady"
    duration: float = 10.0
    agents: int = 5
    projects: int = 4
    sessions: int = 20
    target_tokens: int = 0
    concurrency: int = 4
    seed: int = 42
    batch_size: int = 100
    dry_run: bool = False
    report: bool = False
    json_report: bool = False
    api_key: str | None = None
    timeout: float = 10.0
    #: Emit at most this many snapshots (0 = unbounded; the duration/target govern).
    max_snapshots: int = 0
    #: Re-send the first batch once at the end to exercise duplicate counting.
    replay: bool = False
    #: Spread generated timestamps over this many trailing minutes instead of stamping
    #: everything "now". The anomaly detector (§8) needs a *per-minute series* to build a
    #: baseline, so a load generator that writes all its rows into one minute can never
    #: produce a detectable incident. ``None`` means "auto": 60 for incident/mixed, 0 else.
    backfill_minutes: int | None = None
    #: Minimum number of distinct minutes the incident agents should occupy so the
    #: detector's baseline has enough samples. Used to size the auto backfill window.
    min_active_minutes: int = 20

    def effective_backfill_minutes(self) -> int:
        """Resolved backfill window in minutes (``0`` = stamp everything at now).

        Incident/mixed runs default to :data:`DEFAULT_BACKFILL_MINUTES` (180), which is
        deliberately longer than the detector's 60-minute lookback: that leaves a clean
        *history* window for the baseline instead of forcing the detector onto its diluted
        warmup fallback. See the constant's docstring.
        """
        if self.backfill_minutes is not None:
            return max(0, int(self.backfill_minutes))
        return DEFAULT_BACKFILL_MINUTES if self.mode in ("incident", "mixed") else 0

    def validate(self) -> "SimConfig":
        """Clamp/validate the knobs, raising ``ValueError`` for nonsense."""
        if self.mode not in MODES:
            raise ValueError(f"unknown mode {self.mode!r}; choose from {', '.join(MODES)}")
        if self.duration <= 0 and not self.target_tokens:
            raise ValueError("--duration must be > 0 (or use --target-tokens)")
        self.batch_size = max(1, min(MAX_BATCH, int(self.batch_size)))
        self.concurrency = max(1, min(64, int(self.concurrency)))
        self.agents = max(1, int(self.agents))
        self.projects = max(1, min(len(PROJECTS) * 4, int(self.projects)))
        self.sessions = max(1, int(self.sessions))
        self.target_tokens = max(0, int(self.target_tokens))
        self.max_snapshots = max(0, int(self.max_snapshots))
        if self.backfill_minutes is not None:
            self.backfill_minutes = max(0, min(7 * 24 * 60, int(self.backfill_minutes)))
        if "://" not in self.server:
            self.server = "http://" + self.server
        self.server = self.server.rstrip("/")
        return self


# --------------------------------------------------------------------------------------
# Workload generation
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class AgentProfile:
    """One synthetic agent in the fleet."""

    name: str
    runtime: str
    project: str
    model: str
    session_id: str
    #: Tokens per request scale factor (context bloat multiplies the input side).
    input_scale: float = 1.0
    #: Extra retries per request (retry storm).
    retry_ratio: float = 0.0
    #: Fixed context window override (context bloat).
    context_window: int | None = None
    #: Bytes of tool output per request (tool dump).
    tool_bytes_per_request: int = 0
    #: Requests bundled into each snapshot.
    requests_per_snapshot: int = 1


def assert_incident_profiles_are_isolated(fleet: Sequence[AgentProfile]) -> None:
    """Verify each incident agent is anomalous in exactly one dimension.

    The detector's classifier evaluates root causes in a fixed order and the first match
    wins (contract §8.4). That makes the *absence* of an unintended signal as important as
    the presence of the intended one: an agent designed to trigger ``tool_dump`` whose
    context window also drifts 1.8x above baseline gets labelled ``context_bloat`` instead,
    because that rule is checked first.

    This guard turns that subtle coupling into an explicit, testable invariant. It raises
    ``AssertionError`` rather than logging, because a violation silently invalidates every
    incident demo built on this fleet.
    """
    for profile in fleet:
        if profile.name not in INCIDENT_AGENTS.values():
            continue
        # Every anomaly profile must pin its context window, otherwise it falls through to
        # the random pick in ``_snapshot_for`` and drifts above its own baseline.
        assert profile.context_window is not None, (
            f"{profile.name}: context_window must be pinned for incident agents — an "
            "unset value falls through to a random 128k-1M draw and can trip the "
            "context-bloat rule before the intended rule is reached"
        )
        if profile.name == INCIDENT_AGENTS["retry_storm"]:
            assert profile.retry_ratio > 0, f"{profile.name}: retry_ratio must be set"
            assert profile.context_window == NORMAL_CONTEXT_WINDOW, (
                f"{profile.name}: context window must stay normal so retries remain the "
                "only anomalous dimension"
            )
            assert profile.tool_bytes_per_request == 0, (
                f"{profile.name}: tool payload must stay normal"
            )
        elif profile.name == INCIDENT_AGENTS["tool_dump"]:
            assert profile.tool_bytes_per_request >= TOOL_DUMP_MIN_BYTES_PER_REQUEST, (
                f"{profile.name}: tool_result_bytes must clear the detector threshold"
            )
            assert profile.context_window == NORMAL_CONTEXT_WINDOW, (
                f"{profile.name}: context window must equal the baseline (200k) so the "
                "context-bloat rule does not match first and steal the classification"
            )
            assert profile.input_scale == 1.0, (
                f"{profile.name}: input tokens must stay normal; the tool payload alone "
                "should explain the cost spike"
            )
        elif profile.name == INCIDENT_AGENTS["context_bloat"]:
            # The classifier's structural rule needs window_context >= 1.5x baseline. The
            # seeded baseline reports NORMAL_CONTEXT_WINDOW and the burst reports the 1M
            # target, so the measured growth lands around 4x — comfortably clear of the
            # rule, and independent of which minutes the detector samples.
            assert profile.context_window is not None, (
                f"{profile.name}: context window must be pinned"
            )
            assert profile.context_window >= CONTEXT_BLOAT_GROWTH_TARGET, (
                f"{profile.name}: context window must be >= {CONTEXT_BLOAT_GROWTH_TARGET:,} "
                f"to clear the 1.5x growth rule against the {NORMAL_CONTEXT_WINDOW:,} baseline"
            )
            assert profile.tool_bytes_per_request == 0, (
                f"{profile.name}: tool payload must stay normal"
            )


def build_fleet(config: SimConfig, *, rng: random.Random) -> list[AgentProfile]:
    """Build the agent fleet: ``--agents`` normal agents (+ 3 incident agents in incident mode).

    The fleet is deterministic for a given ``--seed``/``--agents``/``--projects`` triple so
    ``--dry-run`` output can be asserted on in tests.
    """
    fleet: list[AgentProfile] = []
    agent_pool = list(AGENTS)
    project_pool = list(PROJECTS)[: config.projects] or list(PROJECTS)
    for index in range(config.agents):
        base_name, runtime = agent_pool[index % len(agent_pool)]
        name = base_name if index < len(agent_pool) else f"{base_name}-{index // len(agent_pool) + 1}"
        model = pick_model(rng)
        fleet.append(
            AgentProfile(
                name=name,
                runtime=runtime_for(model) or runtime,
                project=project_pool[index % len(project_pool)],
                model=model,
                session_id=f"sess-{index:03d}",
                requests_per_snapshot=rng.choice([1, 1, 1, 2, 3]),
            )
        )

    if config.mode in ("incident", "mixed"):
        # Each anomaly class gets its own agent so the detector can isolate the cause.
        #
        # CRITICAL DESIGN RULE: each incident agent must be anomalous in *exactly one*
        # dimension, with every other signal left at its baseline value. The classifier
        # evaluates rules in a fixed order (retry storm → context window bloat → tool
        # output → prompt ratio → …) and the first match wins, so an agent that is
        # accidentally extreme in an earlier dimension gets labelled with the wrong root
        # cause. Concretely: leaving ``context_window`` unset falls through to the random
        # pick in ``_snapshot_for`` (~370k average), which is 1.85x the 200k baseline and
        # trips the context-bloat rule *before* the tool-output rule is ever reached.
        #
        # ``baseline_context_window`` pins the value used by the seeded baseline rows so
        # the incident rows can be compared against a known, stable history.
        incident_models = ("claude-opus-4-1", "gpt-5", "claude-sonnet-4-5")
        fleet.append(
            AgentProfile(
                name=INCIDENT_AGENTS["retry_storm"],
                runtime="anthropic",
                project=project_pool[0],
                model=incident_models[0],
                session_id="sess-incident-retry",
                retry_ratio=0.35,  # >= 0.15 threshold with margin
                requests_per_snapshot=4,
                # Normal context window and normal tool payload: retries are the only
                # anomalous dimension.
                context_window=NORMAL_CONTEXT_WINDOW,
            )
        )
        fleet.append(
            AgentProfile(
                name=INCIDENT_AGENTS["context_bloat"],
                runtime="openai",
                project=project_pool[-1],
                model=incident_models[1],
                session_id="sess-incident-context",
                # The *structural* signal — a context window materially larger than the
                # agent's own baseline — is the unambiguous form of this root cause, so
                # this profile is built to trip that rule rather than the fallback
                # prompt-token-ratio heuristic.
                #
                # The rule needs `window_context >= 1.5 * baseline_context`. Seeded
                # baseline rows report NORMAL_CONTEXT_WINDOW (200k), so the burst must
                # report >= 300k; 400k gives a comfortable 2.0x. (The earlier 240k was
                # 2x the 120k *absolute* floor but only 1.2x the 200k *baseline*, so the
                # structural rule never fired and classification fell through to the
                # prompt-ratio heuristic — which is inherently marginal because the
                # agent's own low-intensity burst minutes pollute the baseline.)
                # A realistic "context bloat" agent: the prompt per request grows ~4x
                # above baseline. The detector's prompt-ratio rule needs 3x, and with a
                # clean history baseline (see DEFAULT_BACKFILL_MINUTES) the measured ratio
                # clears it by a wide margin. An earlier value of 40x was a sledgehammer
                # used while the baseline was still contaminated; it made each burst row
                # cost >1M tokens, which starved the burst of rows under --target-tokens.
                input_scale=4.0,  # ~4x prompt growth per request
                # 1M is a realistic window for long-context models and clears the
                # structural rule's 1.5x-growth test against the clean 200k history
                # baseline by ~5x, so the classification never depends on a marginal ratio.
                context_window=CONTEXT_BLOAT_GROWTH_TARGET,
                requests_per_snapshot=1,
            )
        )
        fleet.append(
            AgentProfile(
                name=INCIDENT_AGENTS["tool_dump"],
                runtime="anthropic",
                project=project_pool[min(1, len(project_pool) - 1)],
                model=incident_models[2],
                session_id="sess-incident-tools",
                tool_bytes_per_request=TOOL_DUMP_MIN_BYTES_PER_REQUEST * 3,  # 600 KB/request
                requests_per_snapshot=2,
                # The tool-output signature is "huge payload at a *normal* context size".
                # Pinning the window equal to the baseline keeps the context-bloat rule
                # from matching first and stealing the classification.
                context_window=NORMAL_CONTEXT_WINDOW,
            )
        )
    assert_incident_profiles_are_isolated(fleet)
    return fleet


def _arrival_weight(mode: str, elapsed: float, duration: float, rng: random.Random) -> float:
    """Relative arrival rate multiplier for the current instant.

    ``steady`` is flat, ``burst`` is a sine wave with jitter, ``incident`` ramps up in the
    second half (an incident *starts*), and ``mixed`` keeps a calm first half then hands
    over to ``incident`` behaviour.
    """
    if duration <= 0:
        return 1.0
    progress = min(1.0, max(0.0, elapsed / duration))
    if mode == "steady":
        return 1.0
    if mode == "burst":
        wave = 0.5 + 0.5 * math.sin(progress * math.pi * 4)
        return 0.35 + 1.65 * wave
    if mode == "incident":
        return 0.6 + 1.8 * progress
    # mixed: calm until the halfway mark, then escalate.
    if progress < 0.5:
        return 0.8
    return 0.8 + 2.4 * ((progress - 0.5) * 2)


def _snapshot_for(
    profile: AgentProfile,
    *,
    ts: int,
    rng: random.Random,
    incident: bool,
    intensity: float = 1.0,
    baseline_shape: bool = False,
) -> SnapshotIn:
    """Build one snapshot for ``profile`` at ``ts`` (deterministic given ``rng``).

    ``intensity`` scales the *token volume* of an anomaly agent. A real incident is not
    just "the bad agent runs more often" — the per-request context, output and tool
    payload all balloon, which is what makes the per-minute burn rate jump by an order of
    magnitude and gives the detector a score it can actually classify.

    ``baseline_shape`` forces a normal-looking row even for an anomaly agent. Seeded
    baseline minutes use it so the agent's own history stays clean: the classifier decides
    context bloat by comparing the offending window against that history, so a polluted
    baseline would hide the very signal it is meant to measure.
    """
    requests = max(1, int(profile.requests_per_snapshot))
    # Base per-request prompt size: 400..9000 tokens, then scaled (context bloat x14).
    # Normal agents get a deliberately *narrow* prompt band so their per-minute series has
    # a low variance — a noisy baseline inflates the MAD-based sigma and hides the spike.
    is_anomaly = profile.name in INCIDENT_AGENTS.values()
    if is_anomaly:
        prompt = int(rng.randint(2000, 9000) * profile.input_scale)
        output = rng.randint(80, 2200)
        cache_write = rng.randint(0, 900)
    else:
        # Tight bands: a stable per-minute baseline is what makes a z-score detectable.
        prompt = rng.randint(1200, 2400)
        output = rng.randint(180, 320)
        cache_write = rng.randint(80, 200)
    cache_read = int(prompt * rng.uniform(1.0, 1.6))
    reasoning = rng.randint(0, 900) if ("reasoner" in profile.model or profile.model.startswith("o")) else 0

    if intensity != 1.0:
        # Incident volume ramp: everything that costs money grows with intensity.
        prompt = int(prompt * intensity)
        cache_read = int(cache_read * intensity)
        output = int(output * intensity)
        cache_write = int(cache_write * intensity)

    retries = 0
    if profile.retry_ratio > 0:
        # Guarantee the class signature: retries/requests >= 0.15.
        retries = max(1, int(round(requests * profile.retry_ratio * rng.uniform(0.9, 1.3))))
    # Normal agents never retry. A stray retry here would trip the classifier's first
    # rule (retry_storm) for every agent, masking the real root cause of each incident.

    tool_bytes = int(profile.tool_bytes_per_request * requests * intensity)
    if profile.tool_bytes_per_request == 0:
        tool_bytes = rng.randint(0, 120_000)

    context_window = profile.context_window
    if context_window is None:
        # Realistic spread for ordinary agents. Incident agents never reach this branch:
        # ``build_fleet`` pins an explicit window for every anomaly profile, and
        # ``assert_incident_profiles_are_isolated`` enforces it.
        context_window = rng.choice([128_000, NORMAL_CONTEXT_WINDOW, NORMAL_CONTEXT_WINDOW, 400_000, 1_000_000])

    if baseline_shape:
        # A seeded baseline row must look like ordinary traffic even for an anomaly agent:
        # normal context window, normal tool payload, no retries. Otherwise the agent's
        # own history carries the signature and the classifier cannot see the change.
        # These are *per-request* values; the SnapshotIn fields below multiply by
        # ``requests``, exactly like the normal path.
        context_window = NORMAL_CONTEXT_WINDOW
        tool_bytes = rng.randint(0, 40_000)
        retries = 0
        prompt = rng.randint(1200, 2400)
        cache_read = int(prompt * rng.uniform(1.0, 1.6))
        output = rng.randint(180, 320)
        cache_write = rng.randint(80, 200)
        reasoning = 0

    return SnapshotIn(
        event_id=None,  # assigned by the caller so ids are globally unique per run
        ts=ts,
        agent=profile.name,
        agent_version="sim-1.0",
        runtime=profile.runtime,
        project=profile.project,
        session_id=profile.session_id,
        model=profile.model,
        input_tokens=prompt * requests,
        output_tokens=output * requests,
        cache_read_tokens=cache_read * requests,
        cache_write_tokens=cache_write * requests,
        reasoning_tokens=reasoning * requests,
        requests=requests,
        retries=retries,
        tool_calls=rng.randint(0, 6) * requests,
        latency_ms=rng.randint(120, 9000),
        context_window=context_window,
        tool_result_bytes=tool_bytes,
        tags=["simulated", "incident" if incident else "normal"],
        meta={"sim": True, "mode": None},  # mode filled in by the caller
    )


def generate_baseline_minute(
    fleet: Sequence[AgentProfile],
    *,
    rng: random.Random,
    minute_ms: int,
    mode: str,
    event_prefix: str,
    event_counter: int,
    progress: float,
) -> list[SnapshotIn]:
    """Emit exactly one snapshot per fleet member for a single minute.

    The anomaly detector builds its baseline from a *per-minute* series and takes the p95
    over every minute of the observation window. A generator that only writes a handful
    of minutes leaves that series mostly gap-filled, which both starves the baseline and
    buries the spike below the percentile. Seeding one row per agent per minute guarantees
    the series is dense, so the incident spike lands on the p95 as intended.

    The seeded rows stay at *baseline volume* even during the spike phase: they exist to
    keep the series populated, not to carry the incident. Mixing the spike into the seed
    rows would raise the median along with the p95 and cancel the anomaly out.

    They also report a **normal** ``context_window`` and tool payload. The classifier's
    context-bloat rule requires the offending window's context window to have *grown*
    relative to the agent's baseline, so seeding the anomaly agents with their inflated
    values would make their own baseline look bloated and suppress the classification.
    """
    intensity = 1.0  # seeds are always baseline-shaped; the burst phase adds the spike

    batch: list[SnapshotIn] = []
    for offset, profile in enumerate(fleet):
        is_anomaly = profile.name in INCIDENT_AGENTS.values()
        snapshot = _snapshot_for(
            profile,
            ts=minute_ms + rng.randint(0, 59_000),
            rng=rng,
            incident=is_anomaly,
            intensity=intensity,
            baseline_shape=is_anomaly,
        )
        snapshot.event_id = f"{event_prefix}-{event_counter + offset:08d}"
        snapshot.meta = {"sim": True, "mode": mode, "baseline": True}
        batch.append(snapshot)
    return batch


def event_prefix_for(seed: int) -> str:
    """Deterministic event-id prefix for a run (keeps replays idempotent per seed)."""
    return f"sim{seed}"


def generate_incident_burst(
    fleet: Sequence[AgentProfile],
    *,
    rng: random.Random,
    window_start_ms: int,
    now_ms: int,
    mode: str,
    event_prefix: str,
    event_counter: int,
    rows: int = 220,
) -> list[SnapshotIn]:
    """Emit the incident burst: anomaly rows concentrated in the trailing minutes.

    This is the layer that makes an incident *detectable*. The detector takes the p95 of
    the per-minute token series over its lookback window and compares it with the median
    of the *history* window. The seeded baseline layer provides that clean history, so this
    layer only has to push a sustained run of trailing minutes well above it.

    The burst deliberately occupies the trailing :data:`BURST_WINDOW_MINUTES` (the
    detector's default lookback) rather than a percentage of the whole backfill: with a
    180-minute backfill, a "final 25%" burst would start 45 minutes before ``now`` and
    leak into the history window, contaminating the very baseline it is meant to exceed.
    """
    anomaly = [profile for profile in fleet if profile.name in INCIDENT_AGENTS.values()]
    if not anomaly:
        return []
    span_ms = max(60_000, now_ms - window_start_ms)
    burst_len_ms = min(span_ms, BURST_WINDOW_MINUTES * 60_000)
    burst_start = now_ms - burst_len_ms
    burst_span = max(60_000, burst_len_ms)

    burst: list[SnapshotIn] = []
    for offset in range(max(1, rows)):
        progress = offset / max(1, rows - 1)
        stamp = burst_start + int(burst_span * progress) + rng.randint(0, 59_000)
        stamp = min(stamp, now_ms - 1000)
        profile = anomaly[offset % len(anomaly)]
        snapshot = _snapshot_for(
            profile,
            ts=stamp,
            rng=rng,
            incident=True,
            # Peak intensity is deliberately moderate (5x, not 30x): the detector only
            # needs the p95 to clear 3x the baseline, and a runaway multiplier makes the
            # run burn hundreds of millions of tokens for no extra signal.
            intensity=1.0 + 4.0 * progress,
        )
        snapshot.event_id = f"{event_prefix}-{event_counter + offset:08d}"
        snapshot.meta = {"sim": True, "mode": mode, "burst": True}
        burst.append(snapshot)
    return burst


def generate_batch(
    fleet: Sequence[AgentProfile],
    *,
    rng: random.Random,
    count: int,
    ts: int,
    mode: str,
    event_prefix: str,
    event_counter: int,
    token_budget: int | None = None,
    progress: float = 1.0,
    ts_jitter_ms: int = 0,
    ts_span_ms: int = 0,
) -> list[SnapshotIn]:
    """Generate ``count`` snapshots spread across the fleet, stamped near ``ts``.

    ``token_budget`` caps the *total tokens* in the returned batch: generation stops as
    soon as adding the next snapshot would exceed the remaining budget. This is what makes
    ``--target-tokens`` precise instead of "approximately right", which matters because
    the flag exists to size a demo run (and a test assertion) deterministically.

    ``progress`` (0..1) drives the incident ramp: the anomaly agents stay calm for the
    first ~70% of the run and then spike, which is the shape the z-score detector needs.

    ``ts_span_ms`` spreads this batch's snapshots **evenly** across a time span starting
    at ``ts``. This is how the simulator guarantees dense per-minute coverage: a run that
    dumped each batch onto one millisecond would leave the detector's per-minute series
    almost entirely gap-filled, and a baseline built from a handful of minutes cannot
    separate an incident from ordinary noise.
    """
    incident_mode = mode in ("incident", "mixed")
    # Anomaly agents are always present so the raw evidence (retry ratios, context
    # windows, tool bytes) exists in the stream from the first batch — the detector
    # classifies from per-agent aggregates, so a class with zero rows can never be found.
    # Their *share* of traffic is what ramps up, which is what produces the quiet
    # baseline → spike contrast the z-score needs.
    incident_active = incident_mode and (mode != "mixed" or progress >= 0.5)
    incident_weight = 0.0
    if incident_active:
        # Burst rows carry the incident. The seeded baseline keeps every minute populated,
        # so this only has to concentrate *extra* anomaly traffic into the trailing
        # minutes: the p95 then rises while the median stays at baseline level.
        ramp = min(1.0, max(0.0, (progress - 0.55) / 0.45))
        incident_weight = 0.02 + 0.93 * ramp

    normal = [profile for profile in fleet if profile.name not in INCIDENT_AGENTS.values()]
    anomaly = [profile for profile in fleet if profile.name in INCIDENT_AGENTS.values()]

    batch: list[SnapshotIn] = []
    produced = 0
    # Volume multiplier for anomaly agents: stays at a calm 1x for the first ~60% of the
    # backfill window, then explodes to ~8x. The detector's fallback baseline is built
    # from the *first* active minutes of the observation window, so the spike has to be
    # concentrated in the trailing minutes — a smooth ramp would put the p95 inside the
    # baseline sample and flatten the ratio to ~2x, under the 3x gate.
    anomaly_intensity = 1.0
    if incident_active:
        # Burst-phase volume ramp, concentrated in the final ~30% of the time axis.
        spike = max(0.0, (progress - 0.70) / 0.30)
        anomaly_intensity = 1.0 + 30.0 * min(1.0, spike)

    for offset in range(count):
        # Profile assignment. The anomaly agents get a *deterministic, evenly spread* share
        # of every batch rather than an independent random draw per row. This is essential
        # for detectability: the detector takes the p95 over all 61 minutes of the
        # observation window, so an anomaly agent that only appears in 14 of them has its
        # spike diluted below the percentile. Giving every minute at least one anomaly
        # snapshot keeps the baseline populated and lets the trailing spike own the p95.
        n_anomaly = 0
        if anomaly:
            n_anomaly = max(len(anomaly), round(count * incident_weight)) if incident_active else 0
            n_anomaly = min(n_anomaly, count)
        if anomaly and offset < n_anomaly:
            profile = anomaly[offset % len(anomaly)]
        elif normal:
            profile = normal[(offset - n_anomaly) % len(normal)]
        else:
            profile = fleet[rng.randrange(len(fleet))]

        if ts_span_ms > 0:
            # Even spread across the slot, with sub-minute jitter so no two snapshots
            # share a millisecond (the event_id already guarantees uniqueness, this just
            # keeps the series realistic).
            step = ts_span_ms / max(1, count)
            stamp = ts + int(offset * step) + rng.randint(0, max(1, int(step)))
        elif ts_jitter_ms > 0:
            stamp = ts + rng.randint(-ts_jitter_ms, ts_jitter_ms)
        else:
            stamp = ts

        snapshot = _snapshot_for(
            profile,
            ts=stamp,
            rng=rng,
            incident=profile.name in INCIDENT_AGENTS.values(),
            intensity=anomaly_intensity if profile.name in INCIDENT_AGENTS.values() else 1.0,
        )
        snapshot.event_id = f"{event_prefix}-{event_counter + offset:08d}"
        snapshot.meta = {"sim": True, "mode": mode}
        if token_budget is not None:
            if batch and produced + snapshot.total_tokens > token_budget:
                break
        batch.append(snapshot)
        produced += snapshot.total_tokens
    return batch


def total_tokens_of(snapshots: Iterable[SnapshotIn]) -> int:
    """Sum ``total_tokens`` over snapshots (matches the ingest accounting)."""
    return sum(int(snap.total_tokens) for snap in snapshots)


def estimate_cost(snapshots: Iterable[SnapshotIn]) -> float:
    """Price snapshots with the shared catalog — the same maths the server applies."""
    total = 0.0
    for snap in snapshots:
        cost, _, _ = pricing.compute_cost(
            snap.model,
            input_tokens=snap.input_tokens,
            output_tokens=snap.output_tokens,
            cache_read_tokens=snap.cache_read_tokens,
            cache_write_tokens=snap.cache_write_tokens,
            reasoning_tokens=snap.reasoning_tokens,
            runtime=snap.runtime,
        )
        total += cost
    return round(total, 6)


# --------------------------------------------------------------------------------------
# Anomaly signature verification (used by --dry-run and the tests)
# --------------------------------------------------------------------------------------


def anomaly_signatures(snapshots: Sequence[SnapshotIn]) -> dict[str, dict[str, Any]]:
    """Measure the three incident signatures from a snapshot stream.

    Returns per-class evidence with the *aggregate* ratios the detector computes, so the
    tests (and a curious operator running ``--dry-run``) can verify the generator really
    produces detectable traffic instead of trusting the code comments.
    """
    def _collect(agent: str) -> list[SnapshotIn]:
        return [snap for snap in snapshots if snap.agent == agent]

    result: dict[str, dict[str, Any]] = {}

    retry_rows = _collect(INCIDENT_AGENTS["retry_storm"])
    requests = sum(int(snap.requests) for snap in retry_rows)
    retries = sum(int(snap.retries) for snap in retry_rows)
    result["retry_storm"] = {
        "agent": INCIDENT_AGENTS["retry_storm"],
        "snapshots": len(retry_rows),
        "requests": requests,
        "retries": retries,
        "ratio": round(retries / requests, 4) if requests else 0.0,
        "threshold": RETRY_STORM_MIN_RATIO,
        "detected": bool(requests) and (retries / requests) >= RETRY_STORM_MIN_RATIO,
    }

    context_rows = _collect(INCIDENT_AGENTS["context_bloat"])
    windows = [int(snap.context_window or 0) for snap in context_rows]
    mean_window = sum(windows) / len(windows) if windows else 0.0
    context_requests = sum(int(snap.requests) for snap in context_rows)
    input_per_request = (
        sum(int(snap.input_tokens) for snap in context_rows) / context_requests
        if context_requests
        else 0.0
    )
    result["context_bloat"] = {
        "agent": INCIDENT_AGENTS["context_bloat"],
        "snapshots": len(context_rows),
        "mean_context_window": round(mean_window, 1),
        "input_tokens_per_request": round(input_per_request, 1),
        "threshold": CONTEXT_BLOAT_MIN_WINDOW,
        "detected": bool(context_rows)
        and mean_window >= CONTEXT_BLOAT_MIN_WINDOW
        and input_per_request >= 3 * 1000,
    }

    tool_rows = _collect(INCIDENT_AGENTS["tool_dump"])
    tool_requests = sum(int(snap.requests) for snap in tool_rows)
    tool_bytes = sum(int(snap.tool_result_bytes) for snap in tool_rows)
    result["tool_dump"] = {
        "agent": INCIDENT_AGENTS["tool_dump"],
        "snapshots": len(tool_rows),
        "tool_result_bytes": tool_bytes,
        "requests": tool_requests,
        "bytes_per_request": round(tool_bytes / tool_requests, 1) if tool_requests else 0.0,
        "threshold": TOOL_DUMP_MIN_BYTES_PER_REQUEST,
        "detected": bool(tool_requests)
        and (tool_bytes / tool_requests) >= TOOL_DUMP_MIN_BYTES_PER_REQUEST,
    }
    return result


def all_signatures_present(snapshots: Sequence[SnapshotIn]) -> bool:
    """True when all three incident signatures are measurable in the stream."""
    signatures = anomaly_signatures(snapshots)
    return all(item["detected"] for item in signatures.values())


# --------------------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------------------


@dataclass(slots=True)
class SimStats:
    """Accumulated run statistics (the ``--report`` payload)."""

    mode: str = "steady"
    snapshots_generated: int = 0
    snapshots_sent: int = 0
    batches: int = 0
    accepted: int = 0
    duplicates: int = 0
    rejected: int = 0
    errors: list[str] = field(default_factory=list)
    tokens_generated: int = 0
    tokens_accepted: int = 0
    estimated_cost_usd: float = 0.0
    accepted_cost_usd: float = 0.0
    elapsed_s: float = 0.0
    target_reached: bool = False
    dry_run: bool = False
    server: str = ""
    unknown_models: list[str] = field(default_factory=list)

    # -- derived --------------------------------------------------------------------
    @property
    def snapshots_per_s(self) -> float:
        return self.snapshots_generated / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def tokens_per_s(self) -> float:
        return self.tokens_generated / self.elapsed_s if self.elapsed_s > 0 else 0.0

    @property
    def accepted_per_s(self) -> float:
        return self.accepted / self.elapsed_s if self.elapsed_s > 0 else 0.0

    def merge_ingest(self, payload: dict[str, Any]) -> None:
        """Accumulate one ``IngestResponse`` into the running totals."""
        self.accepted += int(payload.get("accepted") or 0)
        self.duplicates += int(payload.get("duplicates") or 0)
        self.rejected += int(payload.get("rejected") or 0)
        self.accepted_cost_usd += float(payload.get("cost_usd") or 0.0)
        for item in payload.get("errors") or []:
            if isinstance(item, dict):
                self.errors.append(str(item.get("error") or item))
        for model in payload.get("unknown_models") or []:
            if model not in self.unknown_models:
                self.unknown_models.append(str(model))

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "server": self.server,
            "dry_run": self.dry_run,
            "elapsed_s": round(self.elapsed_s, 4),
            "snapshots_generated": self.snapshots_generated,
            "snapshots_sent": self.snapshots_sent,
            "batches": self.batches,
            "accepted": self.accepted,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "errors": self.errors[:20],
            "tokens_generated": self.tokens_generated,
            "tokens_accepted": self.tokens_accepted,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "accepted_cost_usd": round(self.accepted_cost_usd, 6),
            "snapshots_per_s": round(self.snapshots_per_s, 2),
            "tokens_per_s": round(self.tokens_per_s, 2),
            "accepted_per_s": round(self.accepted_per_s, 2),
            "target_reached": self.target_reached,
            "unknown_models": self.unknown_models[:20],
        }

    def report_lines(self) -> list[str]:
        """Human readable ``--report`` block."""
        lines = [
            f"TokenLens simulate  mode={self.mode}  server={self.server}"
            + ("  [DRY RUN]" if self.dry_run else ""),
            f"  duration      {self.elapsed_s:.2f}s",
            f"  snapshots     {self.snapshots_generated:,} generated"
            f" / {self.snapshots_sent:,} sent in {self.batches:,} batches",
            f"  ingest        accepted {self.accepted:,}"
            f"  duplicates {self.duplicates:,}  rejected {self.rejected:,}",
            f"  tokens        {self.tokens_generated:,} generated",
            f"  cost          ~${self.estimated_cost_usd:,.6f} generated",
        ]
        # Throughput is only meaningful once the run took a measurable amount of time; a
        # dry run that finishes in microseconds would otherwise report absurd tok/s.
        if self.elapsed_s >= 0.01:
            lines.insert(
                4,
                f"  throughput    {self.snapshots_per_s:,.1f} snapshots/s"
                f"  ({self.tokens_per_s:,.0f} tok/s)",
            )
        else:
            lines.insert(4, "  throughput    n/a (dry run — no network I/O)")
        if not self.dry_run:
            lines.append(f"                ${self.accepted_cost_usd:,.6f} accepted by server")
        if self.target_reached:
            lines.append("  stop          --target-tokens reached")
        if self.errors:
            lines.append(f"  errors        {len(self.errors)} (first: {self.errors[0]})")
        if self.unknown_models:
            lines.append(f"  unknown models {', '.join(self.unknown_models[:5])}")
        return lines


# --------------------------------------------------------------------------------------
# Async runner
# --------------------------------------------------------------------------------------


class _HttpIngestor:
    """Batched, pooled HTTP writer for one simulation run."""

    def __init__(self, config: SimConfig) -> None:
        import httpx

        headers = {"Content-Type": "application/json", "User-Agent": "tokenlens-simulator/0.1"}
        if config.api_key:
            headers["X-API-Key"] = config.api_key
        limits = httpx.Limits(
            max_connections=max(4, config.concurrency * 2),
            max_keepalive_connections=max(4, config.concurrency),
        )
        self._client = httpx.AsyncClient(
            base_url=config.server,
            timeout=httpx.Timeout(config.timeout),
            headers=headers,
            limits=limits,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def post_batch(self, snapshots: Sequence[SnapshotIn]) -> dict[str, Any]:
        """POST one batch, returning the ``IngestResponse`` JSON."""
        payload = {"snapshots": [json.loads(snap.model_dump_json()) for snap in snapshots]}
        response = await self._client.post(INGEST_PATH, json=payload)
        response.raise_for_status()
        return response.json()

    async def __aenter__(self) -> "_HttpIngestor":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()


def build_plan(config: SimConfig, *, rng: random.Random | None = None) -> list[SnapshotIn]:
    """Build the complete deterministic snapshot stream for one run.

    Planning is a separate, pure phase on purpose. The anomaly detector reads a *per-minute*
    series and takes its p95, so the shape of the timeline has to be engineered rather than
    sampled: a run that lets random draws decide when traffic lands can never guarantee a
    detectable incident. Building the whole stream up front also makes ``--dry-run``
    byte-identical for a given seed, which is what the test suite asserts.

    The plan has three layers:

    1. **baseline minutes** (incident/mixed only) — one row per agent for every minute of
       the backfill window, so the detector's series is dense enough to have a median;
    2. **traffic rows** — the mode-shaped bulk of ordinary usage;
    3. **incident burst** — extra anomaly-agent rows concentrated in the trailing quarter,
       lifting the p95 while the baseline layer holds the median down.
    """
    config.validate()
    rng = rng or random.Random(config.seed)
    fleet = build_fleet(config, rng=rng)
    backfill_minutes = config.effective_backfill_minutes()
    backfill_ms = backfill_minutes * 60_000
    now_ms = timeutil.now_ms()
    if config.dry_run:
        # Anchor a dry run to the minute so the whole stream is reproducible per seed.
        now_ms = timeutil.bucket_start(now_ms, "minute")
    window_start_ms = now_ms - backfill_ms if backfill_ms > 0 else now_ms
    incident_mode = config.mode in ("incident", "mixed")
    prefix = event_prefix_for(config.seed)

    plan: list[SnapshotIn] = []
    counter = 0

    def _add(batch: Sequence[SnapshotIn]) -> None:
        nonlocal counter
        for snapshot in batch:
            snapshot.event_id = f"{prefix}-{counter:08d}"
            counter += 1
        plan.extend(batch)

    # -- layer 1: dense baseline minutes ------------------------------------------
    # The detector builds one per-minute series *per agent*, and it only needs enough
    # active minutes of history to trust a median (``MIN_HISTORY_MINUTES``, 10). Seeding
    # every fleet member every minute is therefore wasteful — at 180 minutes x 7 agents
    # that is 1260 rows and ~10M tokens, which starves the incident layers under a token
    # budget. Rotating one agent per minute keeps every agent's series populated across the
    # window at a seventh of the cost.
    if backfill_ms > 0 and incident_mode:
        minute = timeutil.bucket_start(window_start_ms, "minute")
        rotation = 0
        while minute < now_ms:
            profile = fleet[rotation % len(fleet)]
            rotation += 1
            _add(
                generate_baseline_minute(
                    [profile],
                    rng=rng,
                    minute_ms=minute,
                    mode=config.mode,
                    event_prefix="base",
                    event_counter=counter,
                    progress=0.0,
                )
            )
            minute += 60_000

    # -- layer 2: mode-shaped traffic ---------------------------------------------
    # ``_arrival_weight`` turns the mode into a per-instant rate multiplier, so ``burst``
    # genuinely arrives in waves and ``mixed`` escalates halfway instead of both modes
    # emitting the same flat stream.
    #
    # This layer deliberately uses **normal agents only**. An anomaly agent's signature
    # fields (``input_scale``, ``tool_bytes_per_request``) are applied unconditionally in
    # ``_snapshot_for`` — they are properties of the profile, not of the intensity ramp —
    # so letting one into the traffic layer would inflate its own calm minutes. The
    # detector builds its baseline from exactly those minutes, so the measured ratio would
    # collapse toward 1x and the incident would classify as ``unknown``. Baseline seeding
    # and the burst layer are the only places incident agents appear.
    traffic_rows = _traffic_row_count(config, backfill_minutes)
    normal_fleet = [profile for profile in fleet if profile.name not in INCIDENT_AGENTS.values()] or fleet
    for index in range(traffic_rows):
        progress = index / max(1, traffic_rows - 1)
        stamp = window_start_ms + int(backfill_ms * progress) if backfill_ms > 0 else now_ms
        weight = _arrival_weight(config.mode, progress * max(1.0, config.duration), max(1.0, config.duration), rng)
        # Rows per slot scale with the mode's arrival weight (1..3 rows).
        rows_here = max(1, int(round(weight)))
        for _ in range(rows_here):
            _add(
                generate_batch(
                    normal_fleet,
                    rng=rng,
                    count=1,
                    ts=stamp,
                    mode=config.mode,
                    event_prefix="flow",
                    event_counter=counter,
                    progress=progress,
                )
            )

    # -- layer 3: the incident burst ----------------------------------------------
    if incident_mode:
        # Size the burst so *each* anomaly agent covers the trailing window densely. Rows
        # are dealt round-robin across the three anomaly agents, so a flat row count gives
        # each agent only a third of them — at 60 rows that was 20 rows spread over 60
        # minutes, leaving each agent active in only ~5 minutes. A per-agent-per-minute
        # target keeps every anomaly agent present throughout the burst, which is what the
        # detector's p95 and its >=10-minute history requirement need.
        anomaly_agent_count = sum(
            1 for profile in fleet if profile.name in INCIDENT_AGENTS.values()
        ) or 1
        burst_minutes = min(
            BURST_WINDOW_MINUTES,
            max(1, int((now_ms - window_start_ms) / 60_000)),
        )
        # Two rows per agent per burst minute keeps every anomaly agent present throughout
        # the trailing window — which is what the detector's p95 and its >=10-minute
        # history requirement need. Density matters more than per-row volume here.
        burst_rows = anomaly_agent_count * burst_minutes * 2
        _add(
            generate_incident_burst(
                fleet,
                rng=rng,
                window_start_ms=window_start_ms,
                now_ms=now_ms,
                mode=config.mode,
                event_prefix="burst",
                event_counter=counter,
                rows=burst_rows,
            )
        )

    # -- token-target trim ----------------------------------------------------------
    # ``--target-tokens`` sizes the *whole* run, so every layer is scaled together rather
    # than exempting the baseline/burst layers (which let a 100k target balloon to 20M).
    #
    # The burst is trimmed by **row count**, not token cost. Burst rows are deliberately
    # enormous (inflated context, huge tool payloads), so a token-based share buys only a
    # handful of them and each anomaly agent ends up active in ~5 minutes — too sparse for
    # the detector's p95 or its >=10-minute history requirement. Density is what makes the
    # incident detectable, so it is preserved explicitly.
    if config.target_tokens and total_tokens_of(plan) > config.target_tokens:
        layers: dict[str, list[SnapshotIn]] = {"baseline": [], "flow": [], "burst": []}
        for snapshot in plan:
            meta = snapshot.meta or {}
            key = "baseline" if meta.get("baseline") else ("burst" if meta.get("burst") else "flow")
            layers[key].append(snapshot)
        if config.mode in ("incident", "mixed"):
            # Two layers must stay dense for the detector to work, and both are expensive:
            #   * baseline — a populated per-minute series for a clean history median;
            #   * burst    — a sustained run of trailing minutes above that median.
            # Budget them explicitly instead of letting one starve the other: reserve the
            # baseline first (it is cheap per row), then give the burst whatever is left up
            # to a ceiling, and let ordinary traffic take the remainder.
            baseline_rows: list[SnapshotIn] = []
            baseline_spent = 0
            baseline_budget = int(config.target_tokens * 0.30)
            for snapshot in layers["baseline"]:
                if baseline_spent >= baseline_budget:
                    break
                baseline_rows.append(snapshot)
                baseline_spent += snapshot.total_tokens

            agent_count = sum(
                1 for profile in fleet if profile.name in INCIDENT_AGENTS.values()
            ) or 1
            min_burst_rows = agent_count * 40
            burst_token_ceiling = int(config.target_tokens * 0.55)
            burst_rows_kept: list[SnapshotIn] = []
            burst_spent = 0
            for snapshot in layers["burst"]:
                if len(burst_rows_kept) >= min_burst_rows:
                    break
                if burst_spent >= burst_token_ceiling:
                    break
                burst_rows_kept.append(snapshot)
                burst_spent += snapshot.total_tokens

            kept: list[SnapshotIn] = list(baseline_rows) + list(burst_rows_kept)
            flow_budget = max(0, config.target_tokens - baseline_spent - burst_spent)
            flow_spent = 0
            for snapshot in layers["flow"]:
                if flow_spent >= flow_budget:
                    break
                kept.append(snapshot)
                flow_spent += snapshot.total_tokens
        else:
            kept = []
            spent = 0
            for snapshot in layers["flow"]:
                if spent >= config.target_tokens:
                    break
                kept.append(snapshot)
                spent += snapshot.total_tokens
        plan = kept

    plan.sort(key=lambda snapshot: int(snapshot.ts or 0))
    return plan


def _traffic_row_count(config: SimConfig, backfill_minutes: int) -> int:
    """How many mode-shaped traffic rows to emit before the incident burst."""
    if config.target_tokens:
        # A representative normal row is ~4-6k tokens; ask for enough rows to reach the
        # target, capped so a huge target does not build an unbounded plan in memory.
        return max(20, min(8000, int(config.target_tokens / 5_000)))
    if backfill_minutes:
        return max(60, backfill_minutes * 3)
    return max(20, int(config.batch_size * max(1.0, config.duration)))


def _chunk(items: Sequence[SnapshotIn], size: int) -> list[list[SnapshotIn]]:
    """Split the plan into request-sized batches (never above the ingest cap)."""
    size = max(1, min(MAX_BATCH, int(size)))
    return [list(items[index : index + size]) for index in range(0, len(items), size)]


async def run_simulation(config: SimConfig) -> SimStats:
    """Plan the stream, then send it (or validate it locally for ``--dry-run``)."""
    config.validate()
    stats = SimStats(mode=config.mode, dry_run=config.dry_run, server=config.server)
    plan = build_plan(config)
    stats.snapshots_generated = len(plan)
    stats.tokens_generated = total_tokens_of(plan)
    stats.estimated_cost_usd = estimate_cost(plan)

    ingestor: _HttpIngestor | None = None
    started = time.monotonic()
    try:
        if not config.dry_run:
            # Build the pooled client *before* starting the clock: connection-pool setup
            # can cost seconds on a cold process and must not eat the duration budget.
            ingestor = _HttpIngestor(config)
            started = time.monotonic()

        batches = _chunk(plan, config.batch_size)
        if config.dry_run:
            for batch in batches:
                for snapshot in batch:
                    # Validate through the real model path: a schema violation must surface
                    # here rather than as a 422 during a demo.
                    SnapshotIn.model_validate(json.loads(snapshot.model_dump_json()))
                stats.snapshots_sent += len(batch)
                stats.batches += 1
        else:
            assert ingestor is not None
            pending: list[asyncio.Task] = []
            for batch in batches:
                stats.snapshots_sent += len(batch)
                stats.batches += 1
                pending.append(asyncio.create_task(_send(ingestor, batch, stats)))
                if len(pending) >= config.concurrency:
                    await asyncio.gather(*pending)
                    pending = []
            if pending:
                await asyncio.gather(*pending)

        if config.replay and plan and ingestor is not None:
            # Replaying the first batch must count as duplicates, never re-price.
            try:
                stats.merge_ingest(await ingestor.post_batch(_chunk(plan, config.batch_size)[0]))
                stats.batches += 1
            except Exception as exc:  # pragma: no cover - network failure path
                stats.errors.append(f"replay failed: {exc}")
    finally:
        if ingestor is not None:
            await ingestor.close()
        stats.elapsed_s = max(0.0, time.monotonic() - started)

    if config.target_tokens:
        stats.target_reached = stats.tokens_generated >= config.target_tokens
    return stats


async def _send(ingestor: _HttpIngestor, batch: Sequence[SnapshotIn], stats: SimStats) -> None:
    """POST one batch and fold the response into ``stats`` (errors are recorded, not raised)."""
    try:
        payload = await ingestor.post_batch(batch)
        stats.merge_ingest(payload)
        stats.tokens_accepted += int(payload.get("total_tokens") or 0)
    except Exception as exc:  # noqa: BLE001 - one failed batch must not kill the run
        stats.errors.append(f"{exc.__class__.__name__}: {exc}")
        stats.rejected += len(batch)


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Argument parser shared by ``python tools/simulate.py`` and ``tokenlens simulate``."""
    parser = argparse.ArgumentParser(
        prog="simulate",
        description=(
            "TokenLens load generator: drives the public ingest API with realistic agent "
            "traffic. Use --mode incident to emit the three anomaly signatures the detector "
            "classifies, and --dry-run for a hermetic local run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
examples:
  python tools/simulate.py --mode burst --duration 30 --report
  python tools/simulate.py --mode incident --target-tokens 1000000 --report
  python tools/simulate.py --dry-run --mode incident --target-tokens 100000 --seed 7
  python tools/simulate.py --mode mixed --duration 20 --agents 8 --concurrency 8
""",
    )
    parser.add_argument("--server", default="http://127.0.0.1:8787", help="server base URL")
    parser.add_argument("--mode", choices=MODES, default="steady", help="workload shape (default steady)")
    parser.add_argument("--duration", type=float, default=10.0, help="seconds to run (default 10)")
    parser.add_argument("--agents", type=int, default=5, help="distinct agents (default 5)")
    parser.add_argument("--projects", type=int, default=4, help="distinct projects (default 4)")
    parser.add_argument("--sessions", type=int, default=20, help="concurrent sessions (default 20)")
    parser.add_argument("--target-tokens", type=int, default=0, help="stop once this many tokens were generated")
    parser.add_argument("--concurrency", type=int, default=4, help="in-flight batches (default 4)")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed for deterministic runs")
    parser.add_argument("--batch-size", type=int, default=100, help="snapshots per request (max 500)")
    parser.add_argument("--dry-run", action="store_true", help="generate and validate locally, no HTTP")
    parser.add_argument("--report", action="store_true", help="print a throughput report")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--api-key", default=None, help="X-API-Key when the server enables auth")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout seconds")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="re-send the first batch at the end to prove duplicate counting",
    )
    parser.add_argument(
        "--max-snapshots",
        type=int,
        default=0,
        help="hard cap on generated snapshots (0 = unbounded)",
    )
    parser.add_argument(
        "--backfill-minutes",
        type=int,
        default=None,
        help=(
            "spread generated timestamps over this many trailing minutes so the anomaly "
            "detector has a per-minute baseline (default: 60 for incident/mixed, 0 otherwise)"
        ),
    )
    return parser


def config_from_namespace(args: argparse.Namespace) -> SimConfig:
    """Convert an argparse namespace (from either entry point) into a :class:`SimConfig`."""
    return SimConfig(
        server=getattr(args, "server", "http://127.0.0.1:8787") or "http://127.0.0.1:8787",
        mode=getattr(args, "mode", "steady") or "steady",
        duration=float(getattr(args, "duration", 10.0) or 0.0),
        agents=int(getattr(args, "agents", 5) or 5),
        projects=int(getattr(args, "projects", 4) or 4),
        sessions=int(getattr(args, "sessions", 20) or 20),
        target_tokens=int(getattr(args, "target_tokens", 0) or 0),
        concurrency=int(getattr(args, "concurrency", 4) or 4),
        seed=int(getattr(args, "seed", 42) or 0),
        batch_size=int(getattr(args, "batch_size", 100) or 100),
        dry_run=bool(getattr(args, "dry_run", False)),
        report=bool(getattr(args, "report", False)),
        json_report=bool(getattr(args, "json", False)),
        api_key=getattr(args, "api_key", None),
        timeout=float(getattr(args, "timeout", 10.0) or 10.0),
        max_snapshots=int(getattr(args, "max_snapshots", 0) or 0),
        replay=bool(getattr(args, "replay", False)),
        backfill_minutes=getattr(args, "backfill_minutes", None),
    )


def argv_from_namespace(args: argparse.Namespace) -> list[str]:
    """Rebuild an argv list so ``tokenlens simulate`` can reuse this module's parser.

    Going through argv (instead of calling :func:`run_simulation` directly) keeps a single
    source of truth for the flag set and lets ``--help`` behave identically in both entry
    points.
    """
    argv: list[str] = ["--server", str(getattr(args, "server", "http://127.0.0.1:8787"))]
    for flag, dest in (
        ("--mode", "mode"),
        ("--duration", "duration"),
        ("--agents", "agents"),
        ("--projects", "projects"),
        ("--sessions", "sessions"),
        ("--target-tokens", "target_tokens"),
        ("--concurrency", "concurrency"),
        ("--seed", "seed"),
        ("--batch-size", "batch_size"),
        ("--timeout", "timeout"),
        ("--max-snapshots", "max_snapshots"),
        ("--backfill-minutes", "backfill_minutes"),
    ):
        value = getattr(args, dest, None)
        if value is not None:
            argv.extend([flag, str(value)])
    if getattr(args, "api_key", None):
        argv.extend(["--api-key", str(args.api_key)])
    for flag, dest in (("--dry-run", "dry_run"), ("--report", "report"), ("--json", "json"), ("--replay", "replay")):
        if getattr(args, dest, False):
            argv.append(flag)
    return argv


def format_report(stats: SimStats, *, as_json: bool = False) -> str:
    """Render the run statistics (``--report``/``--json``)."""
    if as_json:
        return json.dumps(stats.as_dict(), ensure_ascii=False, indent=2)
    return "\n".join(stats.report_lines())


def main(argv: Sequence[str] | None = None, *, out: Any = None) -> int:
    """Entry point for ``python tools/simulate.py`` and ``tokenlens simulate``."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    stream = out if out is not None else sys.stdout
    try:
        config = config_from_namespace(args).validate()
    except ValueError as exc:
        parser.error(str(exc))
        return 2  # pragma: no cover - parser.error raises SystemExit

    try:
        stats = asyncio.run(run_simulation(config))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        stream.write("interrupted\n")
        return 130
    except Exception as exc:  # noqa: BLE001 - report, do not traceback at the user
        stream.write(f"simulate failed: {exc.__class__.__name__}: {exc}\n")
        if not config.dry_run:
            stream.write(f"  hint: is the server running at {config.server}?\n")
        return 1

    if config.report or config.json_report or config.dry_run:
        stream.write(format_report(stats, as_json=config.json_report) + "\n")
        if config.dry_run and not config.json_report:
            # Extra evidence for a dry run: prove the incident signatures are present.
            if config.mode in ("incident", "mixed"):
                signatures = anomaly_signatures_for_run(config)
                for name, evidence in signatures.items():
                    marker = "OK " if evidence["detected"] else "MISS"
                    stream.write(f"  [{marker}] {name}: {_signature_summary(name, evidence)}\n")
    if stats.errors and not config.dry_run:
        return 1
    if not config.dry_run and stats.accepted == 0 and stats.snapshots_sent:
        return 1
    return 0


def anomaly_signatures_for_run(config: SimConfig) -> dict[str, dict[str, Any]]:
    """Re-generate a sample workload and report the anomaly signatures it contains.

    Used by ``--dry-run`` to print evidence, and by the tests to assert that
    ``--mode incident`` really produces detectable traffic.
    """
    rng = random.Random(config.seed)
    fleet = build_fleet(config, rng=rng)
    snapshots: list[SnapshotIn] = []
    remaining = max(200, min(2000, config.target_tokens // 100 if config.target_tokens else 500))
    counter = 0
    while remaining > 0:
        count = min(100, remaining)
        batch = generate_batch(
            fleet,
            rng=rng,
            count=count,
            ts=timeutil.now_ms(),
            mode=config.mode,
            event_prefix=f"sim{config.seed}",
            event_counter=counter,
        )
        if not batch:  # pragma: no cover - defensive
            break
        snapshots.extend(batch)
        counter += len(batch)
        remaining -= len(batch)
    return anomaly_signatures(snapshots)

def _signature_summary(name: str, evidence: dict[str, Any]) -> str:
    """One-line human summary of an anomaly signature."""
    if name == "retry_storm":
        return f"retries/requests={evidence['ratio']:.3f} (need >= {evidence['threshold']})"
    if name == "context_bloat":
        return (
            f"mean context_window={evidence['mean_context_window']:,.0f} "
            f"(need >= {evidence['threshold']:,}), "
            f"input/request={evidence['input_tokens_per_request']:,.0f}"
        )
    return (
        f"tool_result_bytes/request={evidence['bytes_per_request']:,.0f} "
        f"(need >= {evidence['threshold']:,})"
    )


if __name__ == "__main__":
    raise SystemExit(main())
