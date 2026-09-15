# Status monitor design preview

The production monitor has authenticated and token-based TV access; the preview
below remains demonstration data only.

An isolated, interactive prototype for reviewing the **Printers** and **Queue**
monitor screens. It uses BamDude's actual `Card`, `Button`, `CardSizeSwitch`,
`Modal`, theme CSS, printer images and ETA comparators. The uniform monitor
tile is new; the production `PrinterCard` and `QueueCard` remain the source
for its information hierarchy rather than being mounted with live API hooks.

From `frontend/`:

```sh
npm exec vite -- --config vite.monitor.config.ts
```

Open <http://127.0.0.1:5186/monitor-mockup.html>.

- Both pages: 20 sample scenarios, a 50-printer wall, and location groups.
- Working search, attention/job ETA/queue ETA/name sorting, S/M/L/XL sizing,
  soft/strong tile colors, UK/EN, detail dialogs, fullscreen and popout.
- Click the connection indicator to preview stale data. All times are a fixed
  **2026-09-09 18:20** snapshot, not a live countdown.
- Queue pause and printer pause are separate states. Examples include a
  running print with a paused queue, a running print with no pending jobs,
  unknown durations, plate clearance, schedule/power/drying waits and upload.
- Page/view/sort/language/color are encoded in the URL; no app preferences,
  authentication or printer control APIs are used.

Build the standalone preview into `temp/monitor-mockup/`:

```sh
npm exec vite -- build --config vite.monitor.config.ts
```

Create one self-contained HTML file suitable for sending to someone else:

```sh
node scripts/export-monitor-mockup.mjs
```

The result is `temp/monitor-delivery/BamDude-monitor-mockup.html`. Open it
directly, including offline. Its JS, CSS, fonts, favicon and printer images
are embedded; there are no CDN or API dependencies.

This entry is intentionally separate from the production build and routes.
Attention priority, state colors and density are design proposals for review.
Small screens scroll; the 50-printer fit is intended for desktop monitors.
