"""Per-printer-model FTP tuning knobs.

Mirrors the shape of :mod:`backend.app.services.camera_profiles` — a
small registry of per-model overrides so quirky firmwares can be
tuned without sprinkling ``if model == "X":`` branches through
``bambu_ftp.py``. Adding a new model's quirk is a config edit (an
entry in ``_PROFILES`` plus the alias for its internal SSDP code if
needed), not another hard-coded branch.

The default profile matches the historical pre-fix behaviour, so
every model that doesn't have an entry here keeps its existing FTP
behaviour byte-for-byte.

Currently only the TLS-version cap lives here — see ``cap_tls_v1_2``
below, and its note on what has been measured since it was added. The A1
data-channel-plaintext quirk still lives in :class:`BambuFTPClient`
via ``A1_MODELS`` / ``skip_session_reuse``; folding that into a
profile field is a future cleanup, not load-bearing for this fix.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FTPProfile:
    """Tuning knobs for one printer model's FTP path.

    All defaults reflect the historical behaviour. Models with quirky
    firmware override individual fields rather than re-defining the
    whole profile.
    """

    # Pin the SSL context's ``maximum_version`` to TLS 1.2.
    #
    # ``ssl.create_default_context()`` negotiates TLS 1.3 when both peers
    # offer it (any Python this project runs, 3.12 included). The cap was
    # added for P2S firmware 01.02.00.00 (#1401): a 426 "Failure reading
    # network stream" part-way through an upload, read as the printer's
    # vsFTPd not tolerating TLS 1.3's asynchronous session tickets on the
    # data channel. Capping made session resumption synchronous and the
    # reporter's uploads completed.
    #
    # ⚠️ Measured since (upstream #2780, audit D5): the cap only bites on a
    # printer that OFFERS 1.3, and none measured does — an X1C and an H2D
    # probed on :990, then a 9-printer farm (six P2S, two X1C, an H2D), all
    # refuse 1.3 and complete only on 1.2. A cap is not needed to reach a
    # 1.2-only peer either: an uncapped client negotiates 1.2. And a
    # version mismatch reports ``TLSV1_ALERT_PROTOCOL_VERSION``, never
    # ``WRONG_VERSION_NUMBER`` — that one comes from bytes that are not a
    # TLS record at all (a cleartext answer). Both measurements are pinned
    # by ``tests/unit/services/test_ftp_cleartext_probe.py``. So the entries
    # below are kept as tuning slots and as a record of what each reporter
    # saw, not because the mechanism is understood.
    #
    # **Defaults to False** — only applied to printer models where a
    # reporter has confirmed the symptom. Existing P1S / X1C / H2D
    # installs that work fine today stay on the negotiated TLS 1.3.
    # This is deliberately conservative; flipping a printer to the
    # capped path is a config edit when a new model surfaces the
    # same bug.
    cap_tls_v1_2: bool = False


# ---------------------------------------------------------------------------
# Profile registry
# ---------------------------------------------------------------------------

# Default profile = historical behaviour. Used for every model that
# doesn't have an entry in ``_PROFILES``.
DEFAULT_PROFILE = FTPProfile()

# Per-model overrides. Keys are uppercase display names (e.g. "P2S")
# AFTER alias normalisation, so internal SSDP codes ("N7") resolve via
# ``_MODEL_ALIASES`` below.
_PROFILES: dict[str, FTPProfile] = {
    # P2S firmware 01.02.00.00 (#1401, reporter @iitazz): a 426 truncation
    # part-way through a transfer — the only symptom here a TLS 1.3
    # session-ticket problem could explain, though measured P2S units refuse
    # 1.3 on :990 (see the field's note). The reporter confirmed the fix.
    "P2S": FTPProfile(
        cap_tls_v1_2=True,
    ),
    # X2D firmware 01.01.00.00 (#1638, reporter @vasmarfas): the handshake
    # failed with `[SSL: WRONG_VERSION_NUMBER]` and the cap was added on the
    # reading that a TLS-1.3 ClientHello caused it. ⚠️ It cannot have: that
    # error means the printer answered in cleartext (see the field's note),
    # so the cap is not what changed the outcome. RE-TEST WANTED — kept
    # because the reporter saw the symptom clear and the entry costs nothing
    # on a printer that does not offer 1.3.
    "X2D": FTPProfile(
        cap_tls_v1_2=True,
    ),
    # H2C firmware 01.02.00.00 (#2582): intermittent failures to fetch the
    # 3MF, capped on the belief that it was the P2S's session-reuse variant.
    # ⚠️ Unconfirmed on the same grounds as the X2D entry — measured printers
    # never negotiate 1.3 here, so RE-TEST WANTED. There is no second chance
    # on this model: the prot_p → prot_c fallback in ``bambu_ftp.py`` is
    # A1-only.
    "H2C": FTPProfile(
        cap_tls_v1_2=True,
    ),
}

# SSDP internal codes that should resolve to a display-name profile.
# Mirrors the same map in :mod:`camera_profiles`.
_MODEL_ALIASES: dict[str, str] = {
    "N7": "P2S",  # P2S internal SSDP code
    "N6": "X2D",  # X2D internal SSDP code
    "O1C": "H2C",  # H2C internal SSDP code
    "O1C2": "H2C",  # H2C dual-nozzle variant SSDP code
}


def get_ftp_profile(model: str | None) -> FTPProfile:
    """Return the :class:`FTPProfile` for *model*, or the default.

    ``model`` can be either a display name (e.g. ``"P2S"``) or an
    internal SSDP code (e.g. ``"N7"``). Unknown / missing models fall
    back to :data:`DEFAULT_PROFILE` so the FTP path is never blocked
    on a missing entry.
    """
    if not model:
        return DEFAULT_PROFILE
    key = model.upper().strip()
    key = _MODEL_ALIASES.get(key, key)
    return _PROFILES.get(key, DEFAULT_PROFILE)
