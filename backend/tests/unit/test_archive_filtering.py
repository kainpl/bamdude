"""
Unit tests for archive filtering and timelapse snapshot-diff logic.

Tests:
1. Calibration print filtering - /usr/ prefix skips archive creation
2. Timelapse snapshot-diff - _list_timelapse_videos and _scan_for_timelapse_with_retries
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.app.services.bambu_ftp import LISTING_COMPLETE, DirectoryListing

# Patch paths for lazy imports inside functions
_FTP_MODULE = "backend.app.services.bambu_ftp"


class TestCalibrationPrintFiltering:
    """Test that internal printer files under /usr/ are not archived."""

    @pytest.mark.asyncio
    async def test_usr_prefix_skips_archive(self, capture_logs):
        """Calibration gcode (/usr/etc/print/auto_cali_for_user.gcode) should skip archiving."""
        with (
            patch("backend.app.main.async_session") as mock_session_maker,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.mqtt_relay") as mock_relay,
        ):
            mock_notif.on_print_start = AsyncMock()
            mock_plug.on_print_start = AsyncMock()
            mock_ws.send_print_start = AsyncMock()
            mock_relay.on_print_start = AsyncMock()
            mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

            # Mock printer with auto_archive enabled
            mock_printer = MagicMock()
            mock_printer.auto_archive = True
            mock_printer.id = 1
            # MagicMock conjures every attribute on demand and they are all
            # truthy, so on_print_start's `external_camera_enabled and
            # external_camera_url` gate passes and start_session mkdir's a real
            # timelapse_frames/ directory under the developer's live DATA_DIR.
            mock_printer.external_camera_enabled = False

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.execute = AsyncMock(
                return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_printer))
            )
            mock_session_maker.return_value = mock_session

            # Mock _send_print_start_notification
            with patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock) as mock_notif_send:
                from backend.app.main import on_print_start

                await on_print_start(
                    1,
                    {
                        "filename": "/usr/etc/print/auto_cali_for_user.gcode",
                        "subtask_name": "auto_cali_for_user",
                    },
                )

                # No "Print started": the event is the printer calibrating itself,
                # and its completion is silenced too (upstream 9c938843).
                mock_notif_send.assert_not_called()

        # Verify the skip was logged
        info_messages = [r.getMessage() for r in capture_logs.records if r.levelno >= 20]
        skip_msgs = [m for m in info_messages if "internal printer job" in str(m)]
        assert skip_msgs, "Should log that the internal printer job was skipped"

    @pytest.mark.asyncio
    async def test_usr_prefix_various_paths(self, capture_logs):
        """Various /usr/ paths should all be skipped."""
        test_paths = [
            "/usr/etc/print/auto_cali_for_user.gcode",
            "/usr/etc/print/some_other_calibration.gcode",
            "/usr/bin/firmware_test.gcode",
        ]

        for path in test_paths:
            with (
                patch("backend.app.main.async_session") as mock_session_maker,
                patch("backend.app.main.notification_service") as mock_notif,
                patch("backend.app.main.smart_plug_manager") as mock_plug,
                patch("backend.app.main.ws_manager") as mock_ws,
                patch("backend.app.main.printer_manager") as mock_pm,
                patch("backend.app.main.mqtt_relay") as mock_relay,
                patch("backend.app.main._send_print_start_notification", new_callable=AsyncMock),
            ):
                mock_notif.on_print_start = AsyncMock()
                mock_plug.on_print_start = AsyncMock()
                mock_ws.send_print_start = AsyncMock()
                mock_relay.on_print_start = AsyncMock()
                mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

                mock_printer = MagicMock()
                mock_printer.auto_archive = True
                mock_printer.id = 1
                # MagicMock conjures every attribute on demand and they are all
                # truthy, so on_print_start's `external_camera_enabled and
                # external_camera_url` gate passes and start_session mkdir's a real
                # timelapse_frames/ directory under the developer's live DATA_DIR.
                mock_printer.external_camera_enabled = False

                mock_session = AsyncMock()
                mock_session.__aenter__ = AsyncMock(return_value=mock_session)
                mock_session.__aexit__ = AsyncMock(return_value=False)
                mock_session.execute = AsyncMock(
                    return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_printer))
                )
                mock_session_maker.return_value = mock_session

                from backend.app.main import on_print_start

                await on_print_start(1, {"filename": path, "subtask_name": "test"})

            skip_msgs = [r for r in capture_logs.records if "internal printer job" in str(r.getMessage())]
            assert skip_msgs, f"Path {path} should be skipped"
            capture_logs.clear()

    @pytest.mark.asyncio
    async def test_normal_gcode_not_skipped(self, capture_logs):
        """User gcode files under /data/ should NOT be skipped."""
        with (
            patch("backend.app.main.async_session") as mock_session_maker,
            patch("backend.app.main.notification_service") as mock_notif,
            patch("backend.app.main.smart_plug_manager") as mock_plug,
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.printer_manager") as mock_pm,
            patch("backend.app.main.mqtt_relay") as mock_relay,
        ):
            mock_notif.on_print_start = AsyncMock()
            mock_plug.on_print_start = AsyncMock()
            mock_ws.send_print_start = AsyncMock()
            mock_relay.on_print_start = AsyncMock()
            mock_pm.get_printer = MagicMock(return_value=MagicMock(name="Test", serial_number="TEST123"))

            mock_printer = MagicMock()
            mock_printer.auto_archive = True
            mock_printer.id = 1
            # MagicMock conjures every attribute on demand and they are all
            # truthy, so on_print_start's `external_camera_enabled and
            # external_camera_url` gate passes and start_session mkdir's a real
            # timelapse_frames/ directory under the developer's live DATA_DIR.
            mock_printer.external_camera_enabled = False

            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.execute = AsyncMock(
                return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_printer))
            )
            mock_session_maker.return_value = mock_session

            from backend.app.main import on_print_start
            from backend.app.services.print_execution import ExecutionChanged

            # Isolate this filename-filter test before DB admission.
            decide = patch("backend.app.services.print_execution.decide_start", new_callable=AsyncMock)
            decide_mock = decide.start()
            decide_mock.side_effect = ExecutionChanged("unresolved test fixture")
            record = patch("backend.app.main._record_unproven_start", new_callable=AsyncMock)
            record.start()

            await on_print_start(
                1,
                {
                    "filename": "/data/Metadata/benchy.gcode.3mf",
                    "subtask_name": "benchy",
                },
            )

        decide.stop()
        record.stop()
        # Should NOT see "internal printer job" skip message
        skip_msgs = [r for r in capture_logs.records if "internal printer job" in str(r.getMessage())]
        assert not skip_msgs, "User gcode should not be skipped"


class TestListTimelapseVideos:
    """Test the _list_timelapse_videos helper function."""

    @pytest.mark.asyncio
    async def test_finds_video_files_in_timelapse_dir(self):
        """Should return MP4 and AVI files found in /timelapse directory."""
        mock_printer = MagicMock()
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X1C"

        mock_files = [
            {"name": "video1.mp4", "is_directory": False, "size": 1000, "path": "/timelapse/video1.mp4"},
            {"name": "video2.mp4", "is_directory": False, "size": 2000, "path": "/timelapse/video2.mp4"},
            {"name": "thumbs", "is_directory": True, "size": 0, "path": "/timelapse/thumbs"},
            {"name": "video3.avi", "is_directory": False, "size": 500, "path": "/timelapse/video3.avi"},
        ]

        with patch(f"{_FTP_MODULE}.list_directory_async", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = DirectoryListing("/timelapse", LISTING_COMPLETE, tuple(mock_files))

            from backend.app.main import _list_timelapse_videos

            videos, path = await _list_timelapse_videos(mock_printer)

        assert len(videos) == 3
        assert path == "/timelapse"
        assert all(f["name"].endswith((".mp4", ".avi")) for f in videos)

    @pytest.mark.asyncio
    async def test_tries_multiple_directories(self):
        """Should try /timelapse, /timelapse/video, /record, /recording."""
        mock_printer = MagicMock()
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "H2D"

        async def mock_list_files(ip, code, path, **kwargs):
            # ⚠️ answered=True on the empty ones: the printer replied and the
            # directory holds nothing, which is what keeps the walk going. An
            # unreachable printer answers False and stops it — see
            # TestFtpThatStopsAnswering.
            if path == "/record":
                return DirectoryListing(
                    path,
                    LISTING_COMPLETE,
                    ({"name": "clip.mp4", "is_directory": False, "size": 500, "path": "/record/clip.mp4"},),
                )
            return DirectoryListing(path, LISTING_COMPLETE)

        with patch(f"{_FTP_MODULE}.list_directory_async", side_effect=mock_list_files):
            from backend.app.main import _list_timelapse_videos

            mp4s, path = await _list_timelapse_videos(mock_printer)

        assert len(mp4s) == 1
        assert path == "/record"
        assert mp4s[0]["name"] == "clip.mp4"

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_files(self):
        """Should return ([], None) when no MP4 files exist."""
        mock_printer = MagicMock()
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X1C"

        with patch(f"{_FTP_MODULE}.list_directory_async", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = DirectoryListing("/timelapse", LISTING_COMPLETE)

            from backend.app.main import _list_timelapse_videos

            mp4s, path = await _list_timelapse_videos(mock_printer)

        assert mp4s == []
        assert path is None

    @pytest.mark.asyncio
    async def test_skips_directories(self):
        """Should filter out directory entries even if named .mp4."""
        mock_printer = MagicMock()
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X1C"

        mock_files = [
            {"name": "fake.mp4", "is_directory": True, "size": 0, "path": "/timelapse/fake.mp4"},
            {"name": "real.mp4", "is_directory": False, "size": 1000, "path": "/timelapse/real.mp4"},
        ]

        with patch(f"{_FTP_MODULE}.list_directory_async", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = DirectoryListing("/timelapse", LISTING_COMPLETE, tuple(mock_files))

            from backend.app.main import _list_timelapse_videos

            mp4s, path = await _list_timelapse_videos(mock_printer)

        assert len(mp4s) == 1
        assert mp4s[0]["name"] == "real.mp4"


class TestScanForTimelapseWithRetries:
    """Test the snapshot-diff timelapse scan logic."""

    def _make_mocks(self, archive_filename="benchy.gcode.3mf", timelapse_path=None):
        """Create standard mock archive and printer."""
        mock_archive = MagicMock()
        mock_archive.id = 1
        mock_archive.timelapse_path = timelapse_path
        mock_archive.printer_id = 1
        mock_archive.filename = archive_filename

        mock_printer = MagicMock()
        mock_printer.id = 1
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "X1C"
        # The scan claims the recording under the printer's immutable identity;
        # a printer without one is claimable by nobody (G10 C).
        mock_printer.connector_identity = "printer-identity"

        return mock_archive, mock_printer

    def _make_session_mock(self, mock_printer):
        """Create a mock async session that returns the given printer."""
        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_printer))
        )
        return mock_session

    @pytest.mark.asyncio
    async def test_detects_new_file_after_baseline(self):
        """Should detect a file that wasn't in the baseline snapshot."""
        mock_archive, mock_printer = self._make_mocks()

        baseline_files = [
            {"name": "old_video.mp4", "is_directory": False, "size": 1000, "path": "/timelapse/old_video.mp4"},
        ]
        new_files = baseline_files + [
            {"name": "new_video.mp4", "is_directory": False, "size": 2000, "path": "/timelapse/new_video.mp4"},
        ]

        call_count = 0

        async def mock_list_mp4s(printer, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return baseline_files, "/timelapse"
            return new_files, "/timelapse"

        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=mock_archive)
        mock_service.attach_timelapse = AsyncMock(return_value=True)
        mock_session = self._make_session_mock(mock_printer)

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", side_effect=mock_list_mp4s),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
            patch("backend.app.main.ArchiveService", return_value=mock_service),
            # ⚠️ Patch the seam the code actually uses. Stubbing
            # bambu_ftp.download_file_bytes_async no longer reaches it:
            # printer_files/ftp.py binds that name at import time, so a
            # module-attribute patch misses and the test reaches the real
            # network — which is a hang, not a failure.
            patch("backend.app.main.read_timelapse_video", new_callable=AsyncMock) as mock_download,
        ):
            mock_ws.send_archive_updated = AsyncMock()
            mock_download.return_value = b"fake video data"

            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(1, {"old_video.mp4"})

        # Should have attached the NEW file, not the old one
        mock_service.attach_timelapse.assert_called_once()
        attached_filename = mock_service.attach_timelapse.call_args[0][2]
        assert attached_filename == "new_video.mp4", f"Expected new_video.mp4, got {attached_filename}"

    @pytest.mark.asyncio
    async def test_ignores_old_files_with_wrong_mtime(self):
        """Should not pick old files even if they'd sort first by mtime."""
        mock_archive, mock_printer = self._make_mocks()

        # Both old files exist at baseline - neither should be picked
        baseline_files = [
            {"name": "old_video1.mp4", "is_directory": False, "size": 1000, "path": "/timelapse/old_video1.mp4"},
            {"name": "old_video2.mp4", "is_directory": False, "size": 2000, "path": "/timelapse/old_video2.mp4"},
        ]

        # Always return same files - no new file ever appears
        async def mock_list_mp4s(printer, **_kwargs):
            return baseline_files, "/timelapse"

        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=mock_archive)
        mock_service.attach_timelapse = AsyncMock(return_value=True)
        mock_session = self._make_session_mock(mock_printer)

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", side_effect=mock_list_mp4s),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
            patch("backend.app.main.ArchiveService", return_value=mock_service),
            # ⚠️ Patch the seam the code actually uses. Stubbing
            # bambu_ftp.download_file_bytes_async no longer reaches it:
            # printer_files/ftp.py binds that name at import time, so a
            # module-attribute patch misses and the test reaches the real
            # network — which is a hang, not a failure.
            patch("backend.app.main.read_timelapse_video", new_callable=AsyncMock) as mock_download,
        ):
            mock_ws.send_archive_updated = AsyncMock()
            mock_download.return_value = b"fake video data"

            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(1)

        # "benchy" not in "old_video1.mp4" or "old_video2.mp4" - no match at all
        mock_service.attach_timelapse.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_name_that_resembles_the_print_is_never_attached(self):
        """No new file appeared: a video whose name contains the print's name stays where it is (G10 C3)."""
        mock_archive, mock_printer = self._make_mocks()

        baseline_files = [
            {"name": "old_video.mp4", "is_directory": False, "size": 1000, "path": "/timelapse/old_video.mp4"},
            {
                "name": "benchy_20240101.mp4",
                "is_directory": False,
                "size": 2000,
                "path": "/timelapse/benchy_20240101.mp4",
            },
        ]

        async def mock_list_mp4s(printer, **_kwargs):
            return baseline_files, "/timelapse"

        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=mock_archive)
        mock_service.attach_timelapse = AsyncMock(return_value=True)
        mock_session = self._make_session_mock(mock_printer)

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", side_effect=mock_list_mp4s),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
            patch("backend.app.main.ArchiveService", return_value=mock_service),
            # ⚠️ Patch the seam the code actually uses. Stubbing
            # bambu_ftp.download_file_bytes_async no longer reaches it:
            # printer_files/ftp.py binds that name at import time, so a
            # module-attribute patch misses and the test reaches the real
            # network — which is a hang, not a failure.
            patch("backend.app.main.read_timelapse_video", new_callable=AsyncMock) as mock_download,
        ):
            mock_ws.send_archive_updated = AsyncMock()
            mock_download.return_value = b"fake video data"

            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(1, {"old_video.mp4", "benchy_20240101.mp4"})

        # "benchy" is in "benchy_20240101.mp4" — an earlier print of the same model, not this one's video.
        mock_service.attach_timelapse.assert_not_called()
        mock_download.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_when_archive_already_has_timelapse(self):
        """Should stop immediately if archive already has a timelapse."""
        mock_archive, _ = self._make_mocks(timelapse_path="/some/existing/timelapse.mp4")

        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=mock_archive)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", new_callable=AsyncMock) as mock_list,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("backend.app.main.ArchiveService", return_value=mock_service),
        ):
            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(1)

        # Should not have tried to list files or sleep
        mock_list.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_stops_when_archive_not_found(self):
        """Should stop immediately if archive doesn't exist."""
        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=None)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", new_callable=AsyncMock) as mock_list,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("backend.app.main.ArchiveService", return_value=mock_service),
        ):
            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(999)

        mock_list.assert_not_called()
        mock_sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_four_times(self):
        """Should retry with delays [5, 10, 20, 30]."""
        mock_archive, mock_printer = self._make_mocks(archive_filename="test.gcode.3mf")

        # Never find any files
        async def mock_list_mp4s(printer, **_kwargs):
            return [], None

        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=mock_archive)
        mock_session = self._make_session_mock(mock_printer)

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", side_effect=mock_list_mp4s),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("backend.app.main.ArchiveService", return_value=mock_service),
        ):
            mock_ws.send_archive_updated = AsyncMock()

            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(1, set())

        # Should have slept 4 times with delays [5, 10, 20, 30]
        assert mock_sleep.call_count == 4
        sleep_args = [call.args[0] for call in mock_sleep.call_args_list]
        assert sleep_args == [5, 10, 20, 30]


class TestListTimelapseVideosAvi:
    """Test that _list_timelapse_videos finds AVI files (P1S format)."""

    @pytest.mark.asyncio
    async def test_finds_avi_files(self):
        """Should return AVI files alongside MP4 files."""
        mock_printer = MagicMock()
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "P1S"
        mock_printer.connector_identity = "printer-identity"

        mock_files = [
            {
                "name": "video_2026-02-17_10-00-00.avi",
                "is_directory": False,
                "size": 50000,
                "path": "/timelapse/video_2026-02-17_10-00-00.avi",
            },
        ]

        with patch(f"{_FTP_MODULE}.list_directory_async", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = DirectoryListing("/timelapse", LISTING_COMPLETE, tuple(mock_files))

            from backend.app.main import _list_timelapse_videos

            videos, path = await _list_timelapse_videos(mock_printer)

        assert len(videos) == 1
        assert videos[0]["name"].endswith(".avi")
        assert path == "/timelapse"

    @pytest.mark.asyncio
    async def test_finds_avi_case_insensitive(self):
        """Should match .AVI (uppercase) extensions."""
        mock_printer = MagicMock()
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "P1S"
        mock_printer.connector_identity = "printer-identity"

        mock_files = [
            {"name": "VIDEO.AVI", "is_directory": False, "size": 1000, "path": "/timelapse/VIDEO.AVI"},
        ]

        with patch(f"{_FTP_MODULE}.list_directory_async", new_callable=AsyncMock) as mock_list:
            mock_list.return_value = DirectoryListing("/timelapse", LISTING_COMPLETE, tuple(mock_files))

            from backend.app.main import _list_timelapse_videos

            videos, path = await _list_timelapse_videos(mock_printer)

        assert len(videos) == 1

    @pytest.mark.asyncio
    async def test_scan_detects_new_avi_file(self):
        """Snapshot-diff should detect new AVI files just like MP4."""
        mock_archive = MagicMock()
        mock_archive.id = 1
        mock_archive.timelapse_path = None
        mock_archive.printer_id = 1
        mock_archive.filename = "benchy.gcode.3mf"

        mock_printer = MagicMock()
        mock_printer.id = 1
        mock_printer.ip_address = "192.168.1.100"
        mock_printer.access_code = "12345678"
        mock_printer.model = "P1S"
        mock_printer.connector_identity = "printer-identity"

        baseline_files = []
        new_files = [
            {
                "name": "video_2026-02-17.avi",
                "is_directory": False,
                "size": 50000,
                "path": "/timelapse/video_2026-02-17.avi",
            },
        ]

        call_count = 0

        async def mock_list_videos(printer, **_kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return baseline_files, "/timelapse"
            return new_files, "/timelapse"

        mock_service = MagicMock()
        mock_service.get_archive = AsyncMock(return_value=mock_archive)
        mock_service.attach_timelapse = AsyncMock(return_value=True)

        mock_session = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=mock_printer))
        )

        with (
            patch("backend.app.main.async_session", return_value=mock_session),
            patch("backend.app.main._list_timelapse_videos", side_effect=mock_list_videos),
            patch("backend.app.main.ws_manager") as mock_ws,
            patch("backend.app.main.asyncio.sleep", new_callable=AsyncMock),
            patch("backend.app.main.ArchiveService", return_value=mock_service),
            # ⚠️ Patch the seam the code actually uses. Stubbing
            # bambu_ftp.download_file_bytes_async no longer reaches it:
            # printer_files/ftp.py binds that name at import time, so a
            # module-attribute patch misses and the test reaches the real
            # network — which is a hang, not a failure.
            patch("backend.app.main.read_timelapse_video", new_callable=AsyncMock) as mock_download,
        ):
            mock_ws.send_archive_updated = AsyncMock()
            mock_download.return_value = b"fake avi data"

            from backend.app.main import _scan_for_timelapse_with_retries

            await _scan_for_timelapse_with_retries(1, {f["name"] for f in baseline_files})

        mock_service.attach_timelapse.assert_called_once()
        attached_filename = mock_service.attach_timelapse.call_args[0][2]
        assert attached_filename == "video_2026-02-17.avi"


class TestConvertTimelapseToMp4:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("exit_code", [0, 1])
    async def test_conversion_keeps_source_on_failure_and_updates_only_after_success(
        self, db_session, test_engine, archive_factory, printer_factory, monkeypatch, tmp_path, exit_code
    ):
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from backend.app.core.config import settings
        from backend.app.services import archive_media, queue_sources
        from backend.app.services.archive import _convert_timelapse_to_mp4

        printer = await printer_factory()
        archive = await archive_factory(printer.id, file_path="")
        archive_id, identity = archive.id, archive.recovery_identity
        monkeypatch.setattr(settings, "base_dir", tmp_path)
        # The conversion converts the video the archive SHOWS (G10 C): the AVI is
        # published first, as the attach publishes it.
        staged_avi = tmp_path / ".upload.part"
        staged_avi.write_bytes(b"avi")
        published = await archive_media.publish_primary(
            db_session,
            archive_id,
            staged=staged_avi,
            directory=tmp_path,
            filename="video.avi",
            base_dir=tmp_path,
            origin="upload",
            expected_identity=identity,
            expected_epoch=queue_sources.current_epoch(),
            expected_token="none:0",
        )
        assert published.ok
        source = tmp_path / "video.avi"
        output = source.with_suffix(".mp4")
        monkeypatch.setattr(
            "backend.app.core.database.async_session", async_sessionmaker(test_engine, expire_on_commit=False)
        )
        monkeypatch.setattr("backend.app.services.camera.get_ffmpeg_path", lambda: "ffmpeg")
        process = AsyncMock()
        process.communicate.return_value = (b"", b"failure" if exit_code else b"")
        process.returncode = exit_code
        staged = []

        async def run(*args, **_kwargs):
            # ffmpeg writes to the staged name it was given — never to the final one (G10 C).
            from pathlib import Path

            staged.append(Path(args[-1]))
            if not exit_code:
                staged[-1].write_bytes(b"mp4")
            return process

        execute = AsyncMock(side_effect=run)
        monkeypatch.setattr("asyncio.create_subprocess_exec", execute)
        await _convert_timelapse_to_mp4(archive_id, source, expected_identity=identity)
        db_session.expire_all()
        await db_session.refresh(archive)
        assert source.exists() == bool(exit_code)
        assert output.exists() == (not exit_code)
        assert archive.timelapse_path == ("video.avi" if exit_code else "video.mp4")
        assert "-threads" in execute.call_args.args
        assert staged[0] != output and staged[0].name.startswith(".")
        assert not staged[0].exists()  # published, or removed on failure: never left behind

    @pytest.mark.asyncio
    async def test_skips_when_no_ffmpeg(self, tmp_path, monkeypatch):
        from backend.app.services.archive import _convert_timelapse_to_mp4

        source = tmp_path / "video.avi"
        source.write_bytes(b"avi")
        monkeypatch.setattr("backend.app.services.camera.get_ffmpeg_path", lambda: None)
        await _convert_timelapse_to_mp4(1, source)
        assert source.exists()


class TestAttachTimelapseBackgroundConversion:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("extension", ["mp4", "avi"])
    async def test_conversion_only_for_non_mp4(
        self, db_session, archive_factory, printer_factory, monkeypatch, tmp_path, extension
    ):
        from backend.app.core.config import settings
        from backend.app.services.archive import ArchiveService

        printer = await printer_factory()
        archive = await archive_factory(printer.id, file_path="")
        monkeypatch.setattr(settings, "base_dir", tmp_path)
        monkeypatch.setattr(settings, "archive_dir", tmp_path / "archive")
        tasks = []

        def spawn(coro, **kwargs):
            tasks.append(kwargs["name"])
            coro.close()

        monkeypatch.setattr("backend.app.services.archive.spawn_background_task", spawn)
        assert await ArchiveService(db_session).attach_timelapse(archive.id, b"video", f"video.{extension}")
        assert tasks == ([f"timelapse-convert-{archive.id}"] if extension == "avi" else [])


class TestDeleteTimelapse:
    """Test DELETE /archives/{id}/timelapse endpoint."""

    @pytest.mark.asyncio
    async def test_delete_timelapse_removes_file_and_clears_db(
        self, db_session, archive_factory, printer_factory, monkeypatch, tmp_path
    ):
        """Deleting a timelapse clears the pointer and its registry row, then removes the file (G10 C)."""
        from sqlalchemy import select

        from backend.app.api.routes import archives as routes
        from backend.app.core.config import settings
        from backend.app.models.archive_media_asset import ArchiveMediaAsset

        timelapse_dir = tmp_path / "archives" / "1"
        timelapse_dir.mkdir(parents=True)
        timelapse_file = timelapse_dir / "timelapse.mp4"
        timelapse_file.write_bytes(b"fake video data")
        monkeypatch.setattr(settings, "base_dir", tmp_path)
        announced = AsyncMock()
        monkeypatch.setattr(routes.ws_manager, "send_archive_updated", announced)

        printer = await printer_factory()
        archive = await archive_factory(printer.id, file_path="", timelapse_path="archives/1/timelapse.mp4")
        archive_id = archive.id

        # auth_result=(None, True) → the auth-disabled / can_modify_all path.
        result = await routes.delete_timelapse(archive_id=archive_id, db=db_session, auth_result=(None, True))

        assert result == {"status": "deleted"}
        db_session.expire_all()
        assert (await db_session.get(type(archive), archive_id)).timelapse_path is None
        # Only the removal's mark is left: the "no video" that follows has its own revision (G10 C).
        assert [row.role for row in (await db_session.execute(select(ArchiveMediaAsset))).scalars()] == ["retired"]
        assert not timelapse_file.exists()
        announced.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delete_timelapse_404_when_no_timelapse(self):
        """Should return 404 when archive has no timelapse attached."""
        from fastapi import HTTPException

        from backend.app.api.routes.archives import delete_timelapse

        mock_archive = MagicMock()
        mock_archive.timelapse_path = None
        mock_archive.deleted_at = None

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_archive
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await delete_timelapse(archive_id=1, db=mock_db, auth_result=(None, True))

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_delete_timelapse_404_when_archive_not_found(self):
        """Should return 404 when archive doesn't exist."""
        from fastapi import HTTPException

        from backend.app.api.routes.archives import delete_timelapse

        mock_db = AsyncMock()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        mock_db.execute = AsyncMock(return_value=mock_result)

        with pytest.raises(HTTPException) as exc_info:
            await delete_timelapse(archive_id=999, db=mock_db, auth_result=(None, True))

        assert exc_info.value.status_code == 404
