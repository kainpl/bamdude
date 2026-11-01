"""Allowlisted cached runtime health, without diagnostic scans or provisioning."""

import platform
import time
from datetime import datetime, timezone
from typing import Literal

from pydantic import Field

from backend.app.core.config import APP_VERSION
from backend.app.mcp_server.contracts import Coverage, EmptyInput
from backend.app.services.farm_diagnostics.access import RECOVERY_READ, require
from backend.app.services.farm_diagnostics.contracts import Evidence, Finding, NextStep, ReadResult

_STATES = {"ready", "unavailable", "starting", "stopped", "stopping", "recovering", "degraded"}
_REASONS = {
    "not_started",
    "stopped",
    "starting",
    "startup_failed",
    "worker_restarting",
    "worker_unavailable",
    "worker_lost",
    "circuit_open",
    "ownership_uncertain",
    "recovery_required",
    "staging_full",
    "no_runtime",
    "bundle_missing",
    "bundle_mismatch",
    "runtime_missing",
    "broker_unavailable",
    "transport_unavailable",
}


class ComponentHealth(EmptyInput):
    state: Literal["ready", "unavailable", "starting", "stopped", "stopping", "recovering", "degraded", "unknown"]
    reason: str | None = None
    observed_at: datetime | None = None
    coverage: Coverage = Field(default_factory=Coverage)


def safe_health(value) -> ComponentHealth:
    raw = value.model_dump() if hasattr(value, "model_dump") else value
    state, reason = raw.get("state"), raw.get("reason")
    missing = []
    if state not in _STATES:
        state = "unknown"
        missing.append("unknown_worker_state")
    if reason is not None and reason not in _REASONS:
        reason = "unknown_reason"
        missing.append("worker_reason_not_allowlisted")
    return ComponentHealth(state=state, reason=reason, coverage=Coverage(complete=not missing, missing=missing))


async def system_health(access, args, request_id):
    from backend.app.core.database import get_pool_status
    from backend.app.services.analysis_runtime import get_analysis_health
    from backend.app.services.camera_runtime import camera_runtime_health
    from backend.app.services.library_file_runtime import get_library_file_health
    from backend.app.services.part_render_runtime import get_part_render_health
    from backend.app.services.preview_runtime import get_preview_health

    require(access, "bamdude_get_system_health")
    started = datetime.now(timezone.utc)
    components, evidence, findings = {}, [], []
    for name, read in (
        ("preview", get_preview_health),
        ("analysis", get_analysis_health),
        ("library_file", get_library_file_health),
        ("part_render", lambda: get_part_render_health(cached_only=True)),
        ("camera", camera_runtime_health),
    ):
        try:
            component = safe_health(read())
        except Exception:
            component = ComponentHealth(
                state="unknown",
                reason="source_unavailable",
                coverage=Coverage(complete=False, missing=["component_unavailable"]),
            )
        components[name] = component.model_dump(mode="json")
        eid = "worker:" + name
        evidence.append(
            Evidence(
                id=eid,
                source=name + "_cached_health",
                collected_at=datetime.now(timezone.utc),
                coverage=component.coverage,
            )
        )
        if component.state in {"unavailable", "stopped", "unknown"}:
            findings.append(
                Finding(
                    code="worker_unavailable",
                    severity="warning",
                    certainty="insufficient_evidence" if component.state == "unknown" else "observed",
                    evidence_ids=[eid],
                    explanation=f"{name} worker reports {component.state}.",
                    next_steps=[
                        NextStep(action_kind="open_ui", explanation="Review local service status.", ui_link="/system")
                    ],
                )
            )
    missing_sources = []
    try:
        pool = get_pool_status()
    except Exception:
        pool = {}
        missing_sources.append("database_pool_unavailable")
    pool_data = {
        key: pool.get(key) if type(pool.get(key)) is int else None
        for key in ("current_size", "checked_out", "checked_in", "overflow")
    }
    pool_data["dialect"] = pool.get("dialect") if pool.get("dialect") in {"sqlite", "postgresql"} else "unknown"
    pool_data["config"] = {
        key: value if type(value) in {int, float} else None
        for key, value in pool.get("config", {}).items()
        if key in {"pool_size", "max_overflow", "pool_timeout", "pool_recycle"}
    }
    try:
        import psutil

        uptime = max(0.0, time.time() - psutil.Process().create_time())
    except Exception:
        uptime = None
    observer = {"availability": "unavailable_for_actor"}
    if access.permits(*RECOVERY_READ):
        from backend.app.services.print_state_observer import print_state_observer

        try:
            summary = print_state_observer.public_summary(None)
            # The observer's public summary is already a cached, scalar projection.
            observer = {"availability": "available", **summary}
        except Exception:
            observer = {"availability": "source_unavailable"}
            missing_sources.append("print_state_observer_unavailable")
    finished = datetime.now(timezone.utc)
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data={
            "bamdude_version": APP_VERSION,
            "python_version": platform.python_version(),
            "process_uptime_seconds": uptime,
            "components": components,
            "database_pool": pool_data,
            "print_state_observer": observer,
        },
        evidence=evidence,
        findings=findings,
        coverage=Coverage(
            complete=not missing_sources and all(value["coverage"]["complete"] for value in components.values()),
            missing=sorted(
                set(missing_sources)
                | {reason for value in components.values() for reason in value["coverage"]["missing"]}
            ),
        ),
    )
