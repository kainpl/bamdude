"""Bounded, credential-safe ffmpeg diagnostics shared by camera captures and streams.

Adapted from Bambuddy #2968 (d4477e9b). The route's original banner filter
also needs to cover one-shot RTSP and external USB/RTSP cameras: keeping the
first characters only logs the build banner and loses the diagnosis at the end.
Keep this module independent of routes and services so both can use it.
"""

from __future__ import annotations

from backend.app.core.logging_filters import redact_url_credentials

# What ffmpeg prints before it has anything to say. Every line of the banner is
# either the version line or an indented continuation, and a real diagnostic is
# never indented this way, so the match is on the exact prefixes rather than on
# indentation alone -- ``  Duration: ...`` and ``    Stream #0:0 ...`` are
# indented too and are worth keeping.
_BANNER_PREFIXES = (
    "ffmpeg version ",
    "ffprobe version ",
    "  built with ",
    "  configuration:",
    "  libavutil ",
    "  libavcodec ",
    "  libavformat ",
    "  libavdevice ",
    "  libavfilter ",
    "  libswscale ",
    "  libswresample ",
    "  libpostproc ",
)

# How much of the tail to keep. ffmpeg's diagnosis is the last thing it writes,
# and ten lines is enough to carry the error plus the input analysis that
# explains it without letting a chatty decoder rotate the log file.
_MAX_LINES = 10

# And a ceiling on the whole thing. Ten lines is only a bound on the log record
# if the lines are a sane length, and ffmpeg quotes what the peer sent it back
# at us -- a printer's RTSP response is not something Bambuddy controls. Well
# above any real diagnosis, so this only ever trims a line that was already not
# going to be read.
_MAX_CHARACTERS = 2000

# What to log when the summary is empty. A failure whose stderr held nothing but
# the banner still deserves a line saying so -- ``failed: `` with an empty tail
# reads like a truncation bug rather than a printer that closed the connection.
NO_FFMPEG_OUTPUT = "no diagnostic output"


def summarize_ffmpeg_stderr(text: str | bytes | None) -> str:
    """Strip ffmpeg's boilerplate banner and keep the last lines that matter.

    Accepts raw ``bytes`` as well as ``str`` and decodes with ``errors=
    "replace"``: ffmpeg copies fragments of the stream into its error messages,
    so a bare ``.decode()`` at the call site can raise ``UnicodeDecodeError``
    while reporting an unrelated failure. Losing the diagnosis to a second
    exception is the one outcome worse than logging the banner.

    Returns ``""`` when there is nothing left after the banner, which is the
    signal the streaming endpoint uses to stay quiet. One-shot callers that log
    unconditionally should fall back to :data:`NO_FFMPEG_OUTPUT`.
    """
    if not text:
        return ""
    if isinstance(text, (bytes, bytearray)):
        text = text.decode(errors="replace")
    # Redaction runs on the whole string before anything is dropped: a
    # credentialed URL that straddles the cut would otherwise leave its tail in
    # the log with no ``@`` left for the pattern to anchor on.
    text = redact_url_credentials(text) or ""
    meaningful = [line for line in text.splitlines() if line.strip() and not line.startswith(_BANNER_PREFIXES)]
    summary = "\n".join(meaningful[-_MAX_LINES:])
    if len(summary) > _MAX_CHARACTERS:
        # From the end, for the same reason the whole module exists.
        summary = "..." + summary[-_MAX_CHARACTERS:]
    return summary
