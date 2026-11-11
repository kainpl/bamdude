# External camera transport policy

Camera connections, snapshot decoding and FFmpeg remain owned by the supervised camera worker. The main server retains authorization, relay and the existing fan-out buffer. This policy does not add a second capture path or change who can read, replace or test a stored camera URL.

## HTTP destinations and redirects

Initial URLs and every redirect target are checked before the corresponding request. Cloud metadata spellings, link-local and unspecified addresses, ambiguous numeric IP spellings, malformed authorities and ports, control characters and backslash parser ambiguities are refused. Private LAN addresses, IPv6 ULA and loopback are intentional camera destinations, including local go2rtc. Use an ASCII DNS name or a supported IP literal; Unicode hostnames and IPv6 zone identifiers are refused.

HTTP follows at most three redirects under one header deadline. A finite capture also has one deadline spanning the redirects and body; live MJPEG instead retains its read-stall timeout after the headers. Relative redirects are supported. Loops, malformed locations and HTTPS-to-HTTP downgrades are refused. Basic credentials and cookies can continue on the same origin (scheme, host and effective port). The first origin change permanently switches that request chain to an anonymous session, including if it later returns to the original origin. Userinfo supplied by a redirect is ignored. An installation that previously relied on credentials crossing origins should configure the final camera endpoint directly.

This is a URL spelling policy. It does not pin DNS answers to the connected socket and does not prevent DNS rebinding. Do not treat configured camera endpoints as unrestricted untrusted URLs.

## Snapshot bodies and decoding

HTTP snapshot input and JPEG output are each limited to 10 MiB. Image dimensions are limited to 8192 on either edge and 16 million pixels. Supported still formats are JPEG, PNG, WebP and BMP; PNG/WebP/BMP become JPEG in the worker. Only one image/frame is accepted. Explicit HTML, SVG, JSON and other unsupported MIME types are rejected; missing or octet-stream MIME still needs supported magic and successful decoding. HTTP content compression is refused before reading the body to avoid automatic expansion. Invalid, oversized or unsupported responses yield no frame, with no raw-body fallback.

These are capture/parser ceilings, not a promise that every image below them is published. The existing live registry retains its stricter 2 MiB frame limit; the worker media channel retains its 10 MiB limit. Neither publication limit is raised by this work.

JPEG has a bounded fast path: supported magic, terminal EOI, dimensions, Pillow verification and actual decode are required, then the original bytes are preserved. Missing EOI and trailing data are refused. Pillow can accept some damaged/truncated entropy data when a terminal EOI is present; this is not a complete JPEG entropy/MCU syntax validator. The policy makes no such claim. WebP dimensions are checked from its fixed headers before native canvas allocation.

At most four whole HTTP snapshot operations and two native decoder futures are admitted per worker, without a waiting queue. Caller timeout or cancellation stops waiting for a native codec, but its decoder slot remains occupied until the codec really finishes. Native threads cannot be forcibly cancelled. Two wedged decoders can exhaust this capacity; a healthy worker heartbeat does not automatically detect or restart them. Explicit termination/recovery of the whole worker generation is the hard stop boundary. These limits bound admission and image size; they are not a hard per-image CPU-time guarantee or a total process memory quota.

## MJPEG, RTSP and cleanup

Live external MJPEG/RTSP rejects a frame over 10 MiB, including a started frame that never receives EOI. One-shot RTSP reads bounded stdout while the existing capped stderr drainer runs concurrently. Process termination/reaping, stdout-reader handoff and TLS-proxy cleanup remain with `CameraAttempt`, including early rejection and cancellation. The existing finite MJPEG warm-up/fallback behavior remains unchanged apart from the shared total deadline.

RTSP redirects happen inside FFmpeg. Initial URL validation, protocol restriction and reactive stderr handling do not prove that a same-protocol redirect cannot contact another destination, including metadata. This work does not supply RTSP pre-connect confinement. Validation uses synthetic images, mocked processes and loopback HTTP/TLS endpoints; it does not establish behavior on a physical camera or printer.
