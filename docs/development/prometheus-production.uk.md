## Знімки виробництва та складу

Той самий захищений `/api/v1/metrics` експортує **gauge**, прочитані з наявного обліку Workshop і журналу катушок. Запит нічого не резервує, не приймає випуск, не списує пластик і не виправляє чергу. Окремої бази обліку немає.

| Метрика | Значення та мітки |
|---|---|
| `bamdude_stock_full_spools`, `bamdude_stock_full_grams` | Невикористані вільні котушки, придатні для штатного автовибору зі складу; це не всі котушки на полиці. Грами — вага за етикеткою. |
| `bamdude_inventory_spools{scope}` | Активні/усі записи котушок. |
| `bamdude_inventory_remaining_grams`, `bamdude_inventory_low_stock_spools` | Ті самі оцінки залишку й пороги, що в Inventory. Від'ємний залишок кожної активної котушки обмежено нулем. |
| `bamdude_inventory_overdrawn_spools` | Активні котушки зі списанням понад вагу за етикеткою. Це не доказ фізично порожньої котушки. |
| `bamdude_assigned_spool_balance_grams`, `bamdude_assigned_spool_archived` | За `printer_id`, `ams_id`, `tray_id`: оцінка залишку зі знаком та ознака архівної призначеної котушки. |
| `bamdude_accounted_filament_window_grams{printer_id,kind,window}` | Проведені грами зі знаком за рухомі `24h`, `7d`, `30d`, за **часом запису в журнал**. `kind=print` включає невдалі спроби та старі типи результатів; `service_adjustment` відокремлює поправки runout/AMS. Невідомий принтер — `unassigned`. Оцінок активної нарізки тут немає. |
| `bamdude_pending_jobs{tier}` | `auto` — ще не розподілені; `printer` — очікують запуску. Переданий запис AutoQueue вдруге не рахується. |
| `bamdude_printer_maintenance`, `bamdude_printer_queue_paused`, `bamdude_printer_auto_distribute_eligible`, `bamdude_printer_awaiting_plate_clear` | Незалежні записані ознаки за `printer_id`, включно з режимом обслуговування. Не є фізичним підтвердженням. Якщо заголовка черги немає, її ознаки пропущено. |
| `bamdude_printer_telemetry_timestamp_known`, `bamdude_printer_telemetry_age_seconds` | Чи відомий час останнього локально отриманого MQTT-повідомлення та його вік. Невідомий вік пропущено. Нове повідомлення не доводить свіжість кожного поля. |
| `bamdude_running_order_archives{printer_id,order_id,line_id}` | Архіви зі станом друку за справжніми зовнішніми ключами; відсутній зв'язок — `unknown`. Не доказ фізичного друку. |
| `bamdude_active_orders`, `bamdude_order_details_available` | Кількість активних замовлень і доступність усієї деталізації в межах лімітів. |
| `bamdude_order_line_units{order_id,line_id,unit,quantity}` | `ordered`, `ui_printed`, `ui_covered`, `ui_remaining`, `from_finished_stock`, `from_part_kits`, `held`, `issued`. Одиниці — `product_units` або `parts`; різні одиниці не підсумовувати. Покриття UI включає неперевірені результати, резерви та перерахунок. Приймання/склад не є додатковим виробництвом. |
| `bamdude_order_line_jobs{order_id,line_id,stage}` | Штатно віднесені до позиції архіви `running` та завдання `queued` обох черг. |
| `bamdude_order_part_pieces{order_id,line_id,part_id,quantity}` | `ui_usable`, `ui_remaining`, поправка зі знаком `recount_adjustment`, `allocated_stock`, `checked_net`, `unchecked_net`. Останні два — розподіл тих самих штатно зарахованих деталей **до поправок перерахунку**, не складські приймання. Назви файлів не визначають замовлення. |
| `bamdude_order_part_quality_known` | Чи відомий розподіл за перевіркою; невідомі перевірені/неперевірені кількості мають `NaN`, а не нуль. |
| `bamdude_order_quality_coverage_complete`, `bamdude_order_unchecked_archives`, `bamdude_order_unattributed_archives`, `bamdude_order_recorded_defective_pieces` | Повнота історії, неперевірені й нерозподілені архіви, записаний брак деталей включно з невдалими спробами. Якщо у завершених архівів немає рядків деталей, брак — `NaN`. |
| `bamdude_order_can_complete` | Штатна умова завершення Workshop; запит нічого не підтверджує. |
| `bamdude_production_snapshot_timestamp_seconds` | Час збору UTC. Кілька SQL-запитів не є незмінним знімком виробництва. |

Деталізація охоплює **лише активні замовлення**. У мітках немає замовників, назв файлів та ID архівів. Перед завантаженням перевіряються сумарні межі: 50 замовлень, 1 000 позицій, 10 000 живих архівів, 100 000 рядків деталей архівів. При перевищенні `order_details_available=0`, **уся деталізація замовлень пропускається**, а не перетворюється на нулі чи непомітно обрізається. Метрики складу й черг залишаються. ID стосуються поточної БД, не переносяться між відновленнями. Назви й подробиці — у `/projects/{order_id}`.

Усі ці значення — знімки обліку, залишки котушок — оцінки. Поправки, видалення, скидання та старіння вікон можуть їх зменшувати. **Не застосовуйте `rate()` або `increase()`**. Історія Prometheus починається лише після реального опитування; увімкнення endpoint не створює scrape job. Рухомі суми читають збережений журнал, не відновлюють відсутню історію.

```promql
# Списані на друк грами за попередні 24 години, без службових поправок
sum(bamdude_accounted_filament_window_grams{kind="print",window="24h"})
# Вільні повні котушки в кілограмах
bamdude_stock_full_grams / 1000
# Очікують у двох чергах без дублювання
sum(bamdude_pending_jobs)
# Залишок виробів за активними замовленнями, без позицій окремих деталей
sum by (order_id) (bamdude_order_line_units{quantity="ui_remaining",unit="product_units"})
```
