"""The editor's ffmpeg command, its numbers and its progress (G10 E, M23, M26). No process is started here."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from backend.app.services import timelapse_editor
from backend.app.services.timelapse_editor import EditRefused, validated_request
from backend.app.services.timelapse_processor import (
    ENCODER_LOOKAHEAD,
    ENCODER_THREADS,
    TimelapseProcessor,
    ffprobe_beside,
    parse_progress_seconds,
)


@pytest.fixture
def processor():
    with patch("backend.app.services.timelapse_processor.get_ffmpeg_path", return_value="/usr/bin/ffmpeg"):
        return TimelapseProcessor(Path("/data/in.mp4"))


def _after(command: list[str], flag: str) -> str:
    return command[command.index(flag) + 1]


class TestCommand:
    def test_the_trim_is_an_input_option_so_a_slowed_video_keeps_its_new_length(self, processor):
        command = processor.edit_command(Path("/data/out.mp4"), trim_start=2, trim_end=7, speed=0.5)
        first_input = command.index("-i")
        assert command.index("-ss") < first_input and command.index("-t") < first_input
        assert _after(command, "-ss") == "2" and _after(command, "-t") == "5"
        assert command.count("-t") == 1

    def test_the_encoder_is_bounded_and_the_resolution_is_not_touched(self, processor):
        command = processor.edit_command(Path("/data/out.mp4"), speed=2.0)
        assert _after(command, "-threads") == str(ENCODER_THREADS)
        assert _after(command, "-x264-params") == f"rc-lookahead={ENCODER_LOOKAHEAD}"
        assert not any("scale=" in part for part in command)  # OD-6: no downscale

    def test_progress_goes_to_stdout_and_nothing_reads_stdin(self, processor):
        command = processor.edit_command(Path("/data/out.mp4"))
        assert _after(command, "-progress") == "pipe:1" and "-nostdin" in command and "-nostats" in command
        assert command[-1] == str(Path("/data/out.mp4"))

    def test_music_is_looped_mixed_at_its_volume_and_ends_with_the_video(self, processor):
        command = processor.edit_command(Path("/data/out.mp4"), audio_path=Path("/job/audio.mp3"), audio_volume=0.3)
        loop = command.index("-stream_loop")
        assert command[loop + 1 : loop + 4] == ["-1", "-i", str(Path("/job/audio.mp3"))]
        assert "[1:a]volume=0.3[a]" in _after(command, "-filter_complex")
        assert "-shortest" in command and "-an" not in command

    def test_mute_drops_every_sound_and_the_music_is_not_even_opened(self, processor):
        command = processor.edit_command(
            Path("/data/out.mp4"), audio_path=Path("/job/audio.mp3"), mute=True, source_has_audio=True
        )
        assert "-an" in command and command.count("-i") == 1
        assert "-filter_complex" not in command and "-c:a" not in command

    def test_a_source_without_sound_gets_no_audio_filter_at_another_speed(self, processor):
        command = processor.edit_command(Path("/data/out.mp4"), speed=2.0, source_has_audio=False)
        assert "-an" in command and "-af" not in command

    def test_a_source_with_sound_keeps_it_in_step_with_the_speed(self, processor):
        command = processor.edit_command(Path("/data/out.mp4"), speed=4.0, source_has_audio=True)
        assert _after(command, "-af") == "atempo=2.0,atempo=2.0000"


class TestNumbers:
    def _ok(self, **overrides):
        values = {
            "trim_start": 0,
            "trim_end": None,
            "speed": 1.0,
            "audio_volume": 1.0,
            "mute": False,
            "save_mode": "replace",
            "duration": 10.0,
        }
        values.update(overrides)
        return validated_request(**values)

    def test_a_plain_request_passes(self):
        request = self._ok(trim_start=1, trim_end=9, speed=2)
        assert (request.trim_start, request.trim_end, request.speed) == (1.0, 9.0, 2.0)

    def test_an_end_a_hair_past_the_probed_length_is_the_length(self):
        # The dialog reads the length from the browser's player, the server from ffprobe.
        assert self._ok(trim_end=10.3).trim_end == 10.0

    @pytest.mark.parametrize(
        "overrides",
        [
            {"speed": 0.2},
            {"speed": 4.5},
            {"speed": float("nan")},
            {"trim_start": float("inf")},
            {"trim_start": -0.1},
            {"trim_start": 10},
            {"trim_start": 5, "trim_end": 5},
            {"trim_end": 11},
            {"audio_volume": -0.1},
            {"audio_volume": 1.1},
            {"save_mode": "both"},
            {"trim_end": "soon"},
        ],
    )
    def test_what_is_not_finite_or_not_inside_the_video_is_refused(self, overrides):
        with pytest.raises(EditRefused) as refused:
            self._ok(**overrides)
        assert refused.value.status == 400 and refused.value.code == "edit_invalid"

    def test_a_video_whose_length_is_unknown_cannot_be_edited(self):
        with pytest.raises(EditRefused) as refused:
            self._ok(duration=0)
        assert refused.value.code == "video_unreadable"


class TestProgress:
    def test_only_the_output_time_lines_count(self):
        assert parse_progress_seconds("out_time_us=1500000") == 1.5
        assert parse_progress_seconds("out_time_ms=2000000") == 2.0  # microseconds, despite the name
        for line in ("frame=12", "progress=continue", "out_time_us=N/A", "out_time_us=-5", "", "out_time=00:00:01"):
            assert parse_progress_seconds(line) is None


class TestProbePath:
    @pytest.mark.parametrize(
        "ffmpeg, ffprobe",
        [
            ("ffmpeg", "ffprobe"),
            ("/usr/bin/ffmpeg", "/usr/bin/ffprobe"),
            ("C:/tools/ffmpeg-8.1-full_build/bin/ffmpeg.exe", "C:/tools/ffmpeg-8.1-full_build/bin/ffprobe.exe"),
            ("C:\\tools\\ffmpeg-8.1\\bin\\ffmpeg.exe", "C:\\tools\\ffmpeg-8.1\\bin\\ffprobe.exe"),
        ],
    )
    def test_only_the_files_name_changes(self, ffmpeg, ffprobe):
        assert ffprobe_beside(ffmpeg) == ffprobe


def test_a_staged_output_carries_its_jobs_own_name():
    assert timelapse_editor.staged_name("abc") == ".abc.editing.mp4"
