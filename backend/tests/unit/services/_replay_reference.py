"""A slow, plain reference for ``rebalance_replay.replay`` — the differential tests compare the two.

Same rules, none of the speed: no heap (the next event is found by scanning), no per-printer
job lists or cursors (every wave sorts the pending jobs afresh and walks all of them), no lazy
re-sort. A difference between the two is a bug in the fast one's bookkeeping.
"""

from math import inf

from backend.app.services.farm_forecast import _admit
from backend.app.services.rebalance_replay import Replay, jumps, router_key


def reference_replay(lanes, jobs, *, sjf=False, stagger=None, operator_hours=None) -> Replay:
    out = Replay()
    lanes = sorted(lanes, key=lambda lane: lane.printer_id)
    by_pid = {lane.printer_id: lane for lane in lanes}
    fit = {
        job.job_id: {pid: (exact, colour) for pid, exact, colour in job.fits if pid in by_pid and by_pid[pid].routable}
        for job in jobs
    }
    by_id = {job.job_id: job for job in jobs}
    jumped = {job.job_id: job.been_jumped for job in jobs}
    pending = [job.job_id for job in jobs]
    tainted: dict = {}
    group_taint: dict = {}
    windows = {pid: float(end) for pid, end in stagger.live.items()} if stagger else {}
    peers = {}
    if stagger is not None:
        for lane in lanes:
            mine = set(stagger.resolver.groups_for(lane.printer_id))
            peers[lane.printer_id] = {
                o.printer_id
                for o in lanes
                if o.printer_id != lane.printer_id and mine & set(stagger.resolver.groups_for(o.printer_id))
            }
    # state per lane
    queue = {lane.printer_id: [] for lane in lanes}
    ready_at = {}
    initially_free = set()
    events = []  # (t, phase, pid)

    def taint(pid, at):
        tainted[pid] = min(tainted.get(pid, inf), at)

    def freeze(pid, at):
        taint(pid, at)
        if any(not run["started"] for run in queue[pid]) or any(pid in fit[jid] for jid in pending):
            for other in peers.get(pid, ()):
                taint(other, at)
        for run in queue[pid]:
            if run["job"] is not None:
                out.unknown.add(run["job"])
        queue[pid] = []

    def service(lane, finish, auto_eject):
        gap = 0.0 if auto_eject else lane.gap
        if operator_hours is not None and lane.requires_operator and not auto_eject:
            return operator_hours.service_finish(finish, gap)
        return finish + gap

    for lane in lanes:
        pid = lane.printer_id
        held = [h for h in lane.held if h.seconds is None or h.seconds > 0]
        queue[pid] = [
            {
                "seconds": h.seconds,
                "prep": 0.0 if h.started else h.prep,
                "auto": h.auto_eject,
                "started": h.started,
                "job": None,
                "blocked": h.blocked,
            }
            for h in held
        ]
        if held:
            if held[0].started:
                events.append((0.0, 1, pid))
            elif lane.hold_unknown:
                freeze(pid, 0.0)
            else:
                events.append((lane.waiting, 1, pid))
        elif lane.routable:
            ready_at[pid] = inf if lane.hold_unknown else lane.waiting
            initially_free.add(pid)
            events.append((0.0, 0, pid))

    def is_ready(pid, t):
        if t == 0.0 and pid in initially_free:
            return by_pid[pid].ready
        return ready_at.get(pid, inf) <= t

    def start(pid, t):
        lane = by_pid[pid]
        if not queue[pid]:
            return
        run = queue[pid][0]
        if not run["started"] and (lane.stuck or run["blocked"]):
            return
        if not run["started"] and lane.hold_unknown:
            freeze(pid, t)
            return
        if not run["started"] and stagger is not None:
            admit = _admit(t, lane, windows, stagger)
            if admit > t:
                events.append((admit, 1, pid))
                return
            windows[pid] = (
                t + (run["prep"] if stagger.wait_for_bed else 0.0) + (lane.interval or stagger.interval_seconds)
            )
        queue[pid].pop(0)
        doubtful = tainted.get(pid, inf) <= t
        if doubtful and not run["started"]:
            for other in peers.get(pid, ()):
                taint(other, t)
        if run["seconds"] is None:
            if run["job"] is not None:
                out.unknown.add(run["job"])
            freeze(pid, t)
            return
        finish = t + run["prep"] + run["seconds"]
        if run["job"] is not None:
            if doubtful:
                out.unknown.add(run["job"])
            elif run["job"] not in out.unknown:
                out.finish[run["job"]] = finish
        ready = service(lane, finish, run["auto"])
        if run["started"]:
            ready = max(ready, lane.waiting)
        if queue[pid]:
            events.append((ready, 1, pid))
        elif lane.routable:
            ready_at[pid] = inf if lane.hold_unknown else ready
            events.append((finish, 0, pid))

    while events:
        t = min(e[0] for e in events)
        frees = sorted(e for e in events if e[0] == t and e[1] == 0)
        for e in frees:
            events.remove(e)
        free = {pid for _t, _p, pid in frees}
        if free:
            order = sorted(pending, key=lambda jid: router_key(by_id[jid], sjf=sjf, jumped=jumped[jid]))
            for jid in order:
                if not free:
                    break
                options = [pid for pid in free if pid in fit[jid]]
                if not options:
                    continue
                pid = max(options, key=lambda p: (is_ready(p, t), fit[jid][p][0], fit[jid][p][1], -p))
                job = by_id[jid]
                pending.remove(jid)
                free.discard(pid)
                out.lane_of[jid] = pid
                out.routed_at[jid] = t
                out.ready_when_routed[jid] = is_ready(pid, t)
                doubtful = any(tainted.get(p, inf) <= t for p in fit[jid]) or (
                    sjf and job.group is not None and group_taint.get(job.group, inf) <= t
                )
                if doubtful:
                    out.unknown.add(jid)
                    taint(pid, t)
                    if sjf and job.group is not None:
                        group_taint[job.group] = min(group_taint.get(job.group, inf), t)
                queue[pid].append(
                    {
                        "seconds": job.seconds,
                        "prep": job.prep,
                        "auto": job.auto_eject,
                        "started": False,
                        "job": jid,
                        "blocked": False,
                    }
                )
                if by_pid[pid].stuck:
                    pass
                elif ready_at[pid] == inf:
                    freeze(pid, t)
                else:
                    events.append((max(t, ready_at[pid]), 1, pid))
                if sjf and job.seconds is not None and job.group is not None:
                    for other in pending:
                        if jumps(job, by_id[other], peer_jumped=jumped[other]):
                            jumped[other] = True
        starts = sorted(e for e in events if e[0] == t and e[1] == 1)
        for e in starts:
            events.remove(e)
            start(e[2], t)

    for jid in pending:
        if any(pid in tainted for pid in fit[jid]):
            out.unknown.add(jid)
    for jid in out.unknown:
        out.finish.pop(jid, None)
    return out
