## Production and warehouse snapshots

The same authenticated `/api/v1/metrics` endpoint also exports the following **gauges**. No read reserves stock, accepts output, books filament or repairs a queue. The source is BamDude's existing Workshop readers and spool ledger; there is no second accounting database.

| Metric | Meaning and labels |
|---|---|
| `bamdude_stock_full_spools`, `bamdude_stock_full_grams` | Unused, unassigned stock eligible for the existing automatic stock selector (not every shelf spool). Grams are label weights. |
| `bamdude_inventory_spools{scope}` | Active/all spool records. |
| `bamdude_inventory_remaining_grams`, `bamdude_inventory_low_stock_spools` | Same estimates and threshold rules as Inventory; remaining is clamped per active spool. |
| `bamdude_inventory_overdrawn_spools` | Active records whose lifetime use exceeds label weight; investigate, do not assume physically empty. |
| `bamdude_assigned_spool_balance_grams`, `bamdude_assigned_spool_archived` | Per `printer_id`, `ams_id`, `tray_id`; signed remaining estimate and archived-assignment warning. |
| `bamdude_accounted_filament_window_grams{printer_id,kind,window}` | Booked signed grams during rolling `24h`, `7d`, `30d`, by **ledger settlement time**. `kind=print` includes failed attempts and legacy print outcomes; `service_adjustment` keeps runout/AMS corrections separate. Missing printer attribution is `unassigned`. Active slicer estimates are excluded. |
| `bamdude_pending_jobs{tier}` | `auto` awaiting distribution and `printer` awaiting dispatch; promoted AutoQueue rows are counted only in the printer tier. |
| `bamdude_printer_maintenance`, `bamdude_printer_queue_paused`, `bamdude_printer_auto_distribute_eligible`, `bamdude_printer_awaiting_plate_clear` | Independent stored gates, keyed by `printer_id`, including printers in Maintenance Mode. Not physical proof. Missing queue headers omit queue flags. |
| `bamdude_printer_telemetry_timestamp_known`, `bamdude_printer_telemetry_age_seconds` | Whether a last locally received MQTT timestamp is known and its age. Unknown age is omitted. A recent message does not prove every individual field is fresh. |
| `bamdude_running_order_archives{printer_id,order_id,line_id}` | Count of archives recorded as printing, using real foreign keys; missing links are `unknown`. Not proof that the printer is physically printing. |
| `bamdude_active_orders`, `bamdude_order_details_available` | Active order count and whether all detail fits the bounded reader. |
| `bamdude_order_line_units{order_id,line_id,unit,quantity}` | `ordered`, `ui_printed`, `ui_covered`, `ui_remaining`, `from_finished_stock`, `from_part_kits`, `held`, `issued`. Units are `product_units` or `parts`; do not sum unlike units. UI coverage includes unchecked outcomes, reservations and recounts. Receipts/stock are not additional production. |
| `bamdude_order_line_jobs{order_id,line_id,stage}` | Canonically attributed `running` archives and `queued` jobs from both tiers. |
| `bamdude_order_part_pieces{order_id,line_id,part_id,quantity}` | `ui_usable`, `ui_remaining`, signed `recount_adjustment`, `allocated_stock`, `checked_net`, `unchecked_net`. The last two partition the same canonical part awards **before recounts**; neither is a stock receipt. No filename inference or independent reallocation of checked prints. |
| `bamdude_order_part_quality_known` | Whether part QC attribution is known; unknown checked/unchecked quantities are `NaN`, not zero. |
| `bamdude_order_quality_coverage_complete`, `bamdude_order_unchecked_archives`, `bamdude_order_unattributed_archives`, `bamdude_order_recorded_defective_pieces` | QC/history coverage, unchecked and unattributed archive counts, and recorded defective part rows including failed attempts. Missing completed part rows make the defect quantity `NaN`. |
| `bamdude_order_can_complete` | Existing Workshop completion rule, not an automatic confirmation. |
| `bamdude_production_snapshot_timestamp_seconds` | UTC collection time. Multiple SQL reads are not an immutable production snapshot. |

Order detail covers **active orders only**, with no customer names, filenames or archive IDs in labels. Before hydration the reader checks aggregate limits: 50 orders, 1,000 lines, 10,000 live archives and 100,000 archive-part rows. If exceeded, `order_details_available=0` and **all order detail is omitted**, not reported as zero or silently truncated. Global stock/queue metrics remain available. IDs are current database IDs, not identities portable across restores. Open `/projects/{order_id}` for names and details.

All new values are accounting snapshots, and spool balances remain estimates. Corrections, deletion, aging windows and resets can decrease them. **Do not apply `rate()` or `increase()`**. Historical series begin only when Prometheus actually scrapes the endpoint; enabling it alone does not configure a scrape job. Rolling totals use existing retained ledger rows and do not reconstruct missing past usage.

For example:

```promql
# Booked print grams in the preceding 24 hours, excluding service corrections
sum(bamdude_accounted_filament_window_grams{kind="print",window="24h"})
# Free full-stock kilograms
bamdude_stock_full_grams / 1000
# Waiting work, without counting the router and executor twice
sum(bamdude_pending_jobs)
# Remaining product units by active order (do not mix parts-line units)
sum by (order_id) (bamdude_order_line_units{quantity="ui_remaining",unit="product_units"})
```
