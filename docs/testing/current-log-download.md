# Current log download validation

System Info lists `bamdude.log` before daily archives and downloads a full,
finite snapshot without enabling DEBUG or restarting the service.

Verified on Windows, 2026-09-13:

- Backend: 80 tests passed across `test_current_log_download.py`,
  `test_support_api.py`, `test_support_helpers.py`, and
  `test_support_bundle_fields.py`.
- Frontend: 51 tests passed across `LogArchivesPanel.test.tsx`,
  `SystemInfoPage.test.tsx`, and `api/client.test.ts`.
- `ruff check backend/`, `npm run lint`, and `npm run typecheck` passed.
  ESLint retains two existing hook warnings in `GcodeViewer` and `ModelViewer`.
- Documentation: bilingual MkDocs strict build passed.
- `npm run build` passed; the resulting `static/` bundle is included.

After integrating the concurrent spool-replacement change from
`feature/v0.5.6-fixes`, typechecking passed again, as did 89 frontend tests
(the three files above plus `AssignSpoolModal` and `FilamentHoverCard`) and
23 current-log/support API tests. The combined frontend bundle was rebuilt.

The tests cover an empty file and a UTF-8 log larger than the support-bundle
limit, copying in a worker thread, excluding concurrent appends, closing the
live handle before transfer (including a successful Windows rename), cleanup
on disconnect/cancellation, and early truncation during copying. Authentication,
path containment, archive compatibility, and the absence of a delete action
for the current row are covered as well.

The backend uses a temporary file, with bounded read/write chunks. Downloading
does not cap or sanitize the content. The browser uses the existing Blob-based
download flow. The log remains open briefly during local copying; no printer
or remote-farm test is needed for this filesystem/API change.
