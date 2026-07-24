# Add-on / module images — sourced from BambuStudio

These printer and accessory images are copied from **BambuStudio**
(`resources/images/`) and licensed under **AGPL-3.0**, the same terms as
BambuStudio itself. They are used read-only in the Printer Settings → Add-ons
tab to illustrate the printer body and its detected modules (AMS units,
filament buffer/hub, exhaust fan, etc.).

Renamed on copy from their BambuStudio names to a model/category scheme
(`printer_<model>`, `ams`, `ams_ht`, `filament_buffer_<model>`, `exhaust_fan`,
…). Re-sync = re-copy the matching files from a fresh BambuStudio checkout under
`temp/references/BambuStudio/resources/images/` and re-apply the same renames.

Source tag: BambuStudio v02.08.01.55. See the BambuStudio LICENSE for AGPL-3.0
terms. This mirroring mirrors the existing `backend/app/data/calib_assets/`
(AGPL calibration scaffolds) and `backend/app/data/printers/` (per-model config)
patterns.
