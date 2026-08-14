"""The support bundle describes the process it runs in (upstream #2734).

A bundle carried everything except BamDude's own footprint, so "memory climbs
over days until the OOM killer fires" arrived with nothing to act on — the
numbers that name the mechanism only exist while it is happening, and by then
the container has been restarted.

Two properties matter more than the figures themselves and are pinned here:
the collector must produce *something* on a host where psutil cannot answer,
because the bundle is how somebody reports a problem in the first place; and it
must never record a child's command line, because an ffmpeg argv carries the
camera URL and with it the camera's password.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from backend.app.api.routes import support as support_routes


class TestCollectsSomethingUseful:
    def test_the_figures_that_separate_the_mechanisms_are_present(self):
        info = support_routes._collect_process_info()
        assert info["available"] is True
        # RSS against VMS is the pair that distinguishes address space from a
        # growing heap — either alone supports the wrong conclusion.
        for key in ("rss_bytes", "vms_bytes", "num_threads", "uptime_seconds"):
            assert key in info, key
        assert info["rss_formatted"].endswith(("B", "KB", "MB", "GB"))

    def test_the_heap_census_names_what_the_heap_is_full_of(self):
        """⚠️ Runs against the real process, so the RSS gate applies to it too.

        Asserting the census unconditionally passed alone and failed in the full
        suite, where the pytest process is past the 2 GB limit by the time this
        file is reached — the collector was doing exactly what
        ``TestHeapCensusGate`` pins, and the assertion did not know about it.
        Either outcome is correct; what must never happen is the census
        vanishing without saying so.
        """
        info = support_routes._collect_process_info()
        if "gc_census" in info:
            assert "skipped" in info["gc_census"]
            return
        assert info["gc_tracked_objects"] > 0
        assert len(info["gc_top_types"]) <= 15
        # Descending, so the top of the list is the thing worth looking at.
        counts = list(info["gc_top_types"].values())
        assert counts == sorted(counts, reverse=True)


class TestDegradesRatherThanFails:
    def test_a_host_where_psutil_cannot_answer_still_yields_a_section(self):
        # Hardened kernels and restricted containers raise here. Refusing to
        # produce a bundle on exactly those hosts would be the worst possible
        # trade: the bundle is the reporting mechanism.
        with patch.object(support_routes.psutil, "Process", side_effect=OSError("denied")):
            assert support_routes._collect_process_info() == {"available": False}

    def test_one_unavailable_metric_does_not_take_the_others_with_it(self):
        proc = MagicMock()
        proc.memory_info.side_effect = OSError("denied")
        proc.num_threads.return_value = 12
        proc.create_time.return_value = 0.0
        proc.open_files.side_effect = OSError("denied")
        proc.net_connections.return_value = []
        proc.children.return_value = []
        with patch.object(support_routes.psutil, "Process", return_value=proc):
            info = support_routes._collect_process_info()
        assert info["available"] is True
        assert "rss_bytes" not in info
        assert info["num_threads"] == 12  # collected despite its neighbours failing


class TestChildrenCarryNoCredentials:
    def _child(self, name: str) -> MagicMock:
        c = MagicMock()
        c.name.return_value = name
        return c

    def test_children_are_counted_by_executable_name_only(self):
        proc = MagicMock()
        proc.memory_info.return_value = MagicMock(rss=1024, vms=2048)
        proc.children.return_value = [self._child("ffmpeg"), self._child("ffmpeg"), self._child("sh")]
        proc.net_connections.return_value = []
        proc.open_files.return_value = []
        proc.create_time.return_value = 0.0
        with patch.object(support_routes.psutil, "Process", return_value=proc):
            info = support_routes._collect_process_info()
        assert info["children_total"] == 3
        assert info["children_by_name"] == {"ffmpeg": 2, "sh": 1}
        # The whole point: nothing anywhere in the section is an argv. An ffmpeg
        # command line contains rtsp://bblp:<access code>@… — the same secret
        # the log sanitiser exists to strip.
        assert "cmdline" not in repr(info)

    def test_a_child_that_will_not_name_itself_is_still_counted(self):
        # A process that exits between children() and name() raises. Dropping it
        # would under-count exactly the churn a leak produces.
        bad = MagicMock()
        bad.name.side_effect = OSError("gone")
        proc = MagicMock()
        proc.memory_info.return_value = MagicMock(rss=1024, vms=2048)
        proc.children.return_value = [bad, self._child("ffmpeg")]
        proc.net_connections.return_value = []
        proc.open_files.return_value = []
        proc.create_time.return_value = 0.0
        with patch.object(support_routes.psutil, "Process", return_value=proc):
            info = support_routes._collect_process_info()
        assert info["children_total"] == 2
        assert info["children_by_name"]["<unknown>"] == 1


class TestHeapCensusGate:
    def test_a_process_over_the_limit_says_why_it_skipped(self):
        """The census must not be the allocation that tips the host over.

        Silently omitting it would read as "the heap is fine"; the reason is
        recorded instead, and everything that actually separates the mechanisms
        is still collected.
        """
        proc = MagicMock()
        huge = support_routes._GC_CENSUS_RSS_LIMIT + 1
        proc.memory_info.return_value = MagicMock(rss=huge, vms=huge)
        proc.num_threads.return_value = 40
        proc.create_time.return_value = 0.0
        proc.open_files.return_value = []
        proc.net_connections.return_value = []
        proc.children.return_value = []
        with patch.object(support_routes.psutil, "Process", return_value=proc):
            info = support_routes._collect_process_info()
        assert "gc_tracked_objects" not in info
        assert "skipped" in info["gc_census"]
        assert info["num_threads"] == 40  # the useful part survives the skip


def test_the_issue_pack_shares_the_collector_it_is_wired_into():
    """Both artefacts go through ``_collect_support_info`` — the one place they do
    not diverge — so wiring the section there covers the downloaded ZIP and the
    GitHub issue pack at once.

    Asserted rather than assumed. The pack is the only artefact that reaches us
    from a user without a GitHub account, and it is otherwise strictly poorer
    than the ZIP (no per-printer push-status dumps, and logs only if the client
    chose to send them) — so a section that quietly landed in one and not the
    other would be missing exactly where it is needed most.
    """
    from backend.app.api.routes.bug_report import _collect_support_info as pack_collector

    assert pack_collector is support_routes._collect_support_info
