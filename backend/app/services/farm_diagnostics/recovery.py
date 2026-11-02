"""Recovery information from the normal policy, with no preview/sign/apply path."""

from datetime import datetime, timezone
from urllib.parse import urlencode

from sqlalchemy import func, select

from backend.app.mcp_server.contracts import Coverage
from backend.app.models.archive import PrintArchive
from backend.app.models.print_queue import PrintQueueItem
from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.services import print_state_recovery_store as store
from backend.app.services.farm_diagnostics import paging
from backend.app.services.farm_diagnostics.access import DiagnosticError, printer_scope, require
from backend.app.services.farm_diagnostics.contracts import Evidence, ReadResult
from backend.app.services.print_state_recovery import ACTIONS
from backend.app.services.queue_sources import current_epoch

ISSUE_CODES = frozenset(
    {
        "terminal_archive_active_item",
        "active_item_missing_archive",
        "archive_owner_mismatch",
        "header_without_active_item",
        "multiple_active_items",
        "active_archive_without_current_run",
        "interrupted_dispatch",
        "terminal_runtime_context",
        "runtime_context_unresolved",
        "dispatch_no_progress",
        "runtime_phase_lingering",
        "queue_paused_by_repair",
    }
)
# Unknown future policy/episode reasons remain incomplete evidence, never raw prose.
REASONS = frozenset(
    {
        "live_unknown",
        "live_busy",
        "effects_pending",
        "identity_missing",
        "ambiguous_owner",
        "execution_unproven",
        "source_archive",
        "outcome_unknown",
        "invalid_run_times",
        "missing_run_times",
        "delete_has_dependencies",
        "estimated_time",
        "owner_alive",
        "owner_unknown",
        "archive_missing",
        "archive_foreign",
        "archive_not_terminal",
        "effects_unreadable",
        "swap_debt",
        "context_owner_active",
        "context_owner_unknown",
        "start_in_progress",
        "terminal_buffered",
        "finishing_in_progress",
        "activity_changed",
        "admission_unproven",
        "macro_selection_unproven",
        "swap_config_unproven",
        "timelapse_open",
        "timelapse_unknown",
        "queue_missing",
        "queue_busy",
        "printer_printing",
        "dispatch_claim_changed",
        "requires_operator_confirmation",
        "action_not_available",
        "unknown",
    }
)


def ui_path(pid, kind, target):
    # The application router lives at /system. Never derive a UI origin from Host
    # and never put credentials, actions, confirmations or a preview in this URL.
    return "/system?" + urlencode({"printer_id": pid, "kind": kind, "target_id": target}) + "#print-state"


def reasons(values):
    return sorted({v if isinstance(v, str) and v in REASONS else "unknown" for v in values})


def safe_issue(issue):
    if issue is None:
        return None
    out = {
        k: issue[k]
        for k in (
            "printer_id",
            "queue_id",
            "kind",
            "target_id",
            "item_id",
            "archive_id",
            "observed_at",
            "live_mode",
            "started_at",
            "completed_at",
            "observation",
            "queue_paused",
            "plate_gate",
            "attempts",
            "span_seconds",
            "active",
            "quiet_seconds",
            "age_seconds",
        )
        if k in issue
    }
    out["code"] = issue["code"] if issue.get("code") in ISSUE_CODES else "unknown"
    out["blocked_reasons"] = reasons(issue.get("blocked_reasons", []))
    out["policy_actions"] = [a for a in issue.get("actions", []) if a in ACTIONS]
    out["can_execute_via_mcp"] = False
    out["ui_path"] = ui_path(issue["printer_id"], issue["kind"], issue["target_id"])
    return out


async def target_printer(db, access, pid):
    found = await db.scalar(select(Printer.id).where(Printer.id == pid, printer_scope(access, Printer.id)).limit(1))
    if found is None:
        raise DiagnosticError("not_found")
    return found


async def unchanged(db, access, captured):
    if captured:
        fresh = (await store.read_facts(db, sorted({key[0] for key in captured}), list(captured)))[0]
        if set(fresh) != set(captured) or any(
            store.revision(fresh[key]) != store.revision(f) for key, f in captured.items()
        ):
            raise DiagnosticError("state_changed")
    if access.epoch != current_epoch():
        raise DiagnosticError("state_changed")


def result(access, request_id, started, data, *, next_cursor=None, truncated=False):
    finished = datetime.now(timezone.utc)
    coverage = Coverage(
        complete=False,
        truncated=truncated,
        missing=[
            "current_observations_are_not_a_transition_history",
            "operator_write_permissions_not_evaluated",
        ],
    )
    return ReadResult(
        request_id=request_id,
        collection_started_at=started,
        collection_finished_at=finished,
        data={**data, "can_execute_via_mcp": False, "write_permissions_evaluated": False},
        evidence=[
            Evidence(
                id="recovery:policy",
                source="normal_print_state_recovery_policy",
                collected_at=finished,
                coverage=coverage,
            )
        ],
        coverage=coverage,
        next_cursor=next_cursor,
    )


async def list_state_issues(db, access, args, request_id):
    require(access, "bamdude_list_state_issues")
    started = datetime.now(timezone.utc)
    if args.printer_id is not None:
        await target_printer(db, access, args.printer_id)
    filters = args.model_dump(mode="json", exclude={"cursor"})
    if args.cursor:
        position = paging.decode_position(args.cursor, access, "recovery_issues", filters)
        try:
            ceilings, inner = position["ceilings"], position["inner"]
            assert all(type(ceilings[n]) is int and ceilings[n] >= 0 for n in ("printer", "header", "item", "archive"))
            assert isinstance(inner, str) and len(inner) <= 160
            store.decode_cursor(inner)
        except (KeyError, ValueError, AssertionError, TypeError):
            raise DiagnosticError("invalid_cursor") from None
    else:
        visible = select(Printer.id).where(printer_scope(access, Printer.id))
        if args.printer_id is not None:
            visible = visible.where(Printer.id == args.printer_id)
        queues = select(PrinterQueue.id).where(PrinterQueue.printer_id.in_(visible))
        row = (
            await db.execute(
                select(
                    select(func.max(Printer.id)).where(Printer.id.in_(visible)).scalar_subquery().label("printer"),
                    select(func.max(PrinterQueue.id))
                    .where(PrinterQueue.id.in_(queues))
                    .scalar_subquery()
                    .label("header"),
                    select(func.max(PrintQueueItem.id))
                    .where(PrintQueueItem.queue_id.in_(queues))
                    .scalar_subquery()
                    .label("item"),
                    select(func.max(PrintArchive.id))
                    .where(PrintArchive.printer_id.in_(visible))
                    .scalar_subquery()
                    .label("archive"),
                )
            )
        ).one()
        ceilings, inner = {name: getattr(row, name) or 0 for name in ("printer", "header", "item", "archive")}, None
    captured = {}
    page, _ = await store.list_issues_page(
        db,
        access.printer_ids,
        printer_id=args.printer_id,
        cursor=inner,
        page_size=args.limit,
        ceilings=ceilings,
        capture=captured,
    )
    await unchanged(db, access, captured)
    cursor = (
        paging.encode_position(access, "recovery_issues", filters, {"ceilings": ceilings, "inner": page["next_cursor"]})
        if page["next_cursor"]
        else None
    )
    return result(
        access,
        request_id,
        started,
        {
            "issues": [safe_issue(i) for i in page["issues"]],
            "checked_at": page["checked_at"],
            "pagination_is_snapshot": False,
            "selection_basis": "normal_recovery_candidates_by_printer_kind_target",
            "stall_tracking": {
                k: v for k, v in page.get("stall_tracking", {}).items() if k in {"since_seconds", "evicted"}
            },
        },
        next_cursor=cursor,
        truncated=page["truncated"],
    )


async def explain_recovery(db, access, args, request_id):
    require(access, "bamdude_explain_recovery")
    started = datetime.now(timezone.utc)
    pid = await target_printer(db, access, args.printer_id)
    key = (pid, args.kind, args.target_id)
    if args.kind in {"episode", "repair"}:
        page, _ = await store.list_issues_page(db, access.printer_ids, printer_id=pid, target=key[1:], page_size=1)
        if not page["issues"]:
            raise DiagnosticError("not_found")
        issue = safe_issue(page["issues"][0])
        output = {
            "issue": issue,
            "requested_action": args.action,
            "policy_actions": [],
            "after": None,
            "before": None,
            "blockers": ["action_not_available"] if args.action else [],
            "archive_preserved": True,
            "requires_operator_confirmation": False,
            "requires_completed_confirmation": False,
        }
        await unchanged(db, access, {})
    else:
        captured = (await store.read_facts(db, [pid], [key]))[0]
        if key not in captured:
            raise DiagnosticError("not_found")
        output = store.explain_facts(key, captured[key], args.action)
        output["issue"] = safe_issue(output["issue"])
        output["blockers"] = reasons(output["blockers"])
        await unchanged(db, access, captured)
    output["ui_path"] = ui_path(pid, args.kind, args.target_id)
    output["execution"] = (
        "Open the normal UI, re-read the target and permissions, then request a fresh preview manually. Restart does not repair a durable queue claim."
    )
    return result(access, request_id, started, output)
