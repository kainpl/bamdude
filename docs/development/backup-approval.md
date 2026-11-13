# Git backup approval and partial restore

Accepted and integrated on the fixes development branch after 0.7.3. Existing
schedules require the administrator save described below after upgrading.

Git backups may contain integration credentials and other private farm data.
An active administrator with Git backup permission must explicitly save the
configuration, and the target repository must be confirmed private. After this
upgrade, existing configurations need one administrator save before exporting
again. Their schedules remain enabled so the pause stays visible.

Settings → Backup shows the reason, the last attempt and the last successful
copy separately. A refused scheduled slot is recorded as **blocked**, rather
than a harmless no-change **skipped** run. Support diagnostics count blocks
separately from failures. Subscribed administrators receive one in-app notice
per continuous blocked period. External notification providers do not receive
this notice.

Approval belongs to the approving account's immutable identity. Disabling or
demoting that account pauses exports; restoring its access lets an unchanged
configuration resume. Password resets and signing out do not revoke an existing
configuration approval. Deleting and recreating the account requires another
administrator save, even if its name or numeric id is reused. A revoked browser
request cannot save a new approval after a slow repository check.

Each run checks current approval before capturing data and rechecks it with the
saved configuration before uploading snapshot bytes. Database/configuration
locks are released before provider calls. Restore closes admission and waits
for owned work; an unfinished drain refuses replacement.

Partial Git restore accepts only supported ordinary settings validated through
the current editor schema. Preview and apply use the same classifier; apply
reads current local state again. Unknown, malformed, masked, runtime, credential
and authentication-policy values are refused with reasons. Local destinations,
paths and selectors for retained credentials or private data remain unchanged.
Configure those through their own editor. The original browser authority is
checked again after remote reads before applying categories or sending printer
commands. Full ZIP backup retains its complete snapshot contract. Print
archives remain a history of individual physical prints.

Accepted UI limitations: preview estimates potential replacements even with
Overwrite off, so final counts and refusal reasons can differ after existing
rows are skipped. A blocked manual run can still show an English error toast
in the Ukrainian interface; the Settings approval warning is localized.
