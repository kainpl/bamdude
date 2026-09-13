# Час роботи камер і походження кадру

[English](camera-observability.md)

Статус камери доповнено необов'язковими `telemetry` (поточний або останній live
producer) та `last_snapshot` (останнє рішення HTTP snapshot). Чинні поля, права
та клієнти сумісні. Читання `GET /api/v1/printers/{id}/camera/status` не відкриває
камеру й не запускає FFprobe.

| Поле | Значення |
| --- | --- |
| `session_id`, `attempt_id` | Одна сесія live producer і одна фізична спроба захоплення/підключення. Reconnect отримує новий суфікс attempt. |
| `first_frame_ms` | Monotonic час від початку спроби до першого кадру з повними JPEG-маркерами, до cleanup. Це не час HTTP-відповіді й не перевірка декодування JPEG. |
| `caller_wait_ms` | Повний час очікування конкретного caller, включно зі спільним producer та його cleanup. Є в capture result, snapshot evidence і першокадровій діагностиці. |
| `cleanup_ms` | Виміряний час закриття ресурсів власником. Зараз вимірюється для `CameraAttempt` і live chamber; інші шляхи можуть повертати null. |
| `frame_age_ms` | Вік останнього побаченого кадру, не телеметрії принтера. |
| `attempts_total`, `reconnects_total` | Спроби producer та повторні підключення. Плановий HTTP snapshot poll — спроба, але не reconnect. |
| `consecutive_failures` | Built-in RTSP обнуляє після чинного стабільного вікна; external reconnect рахує послідовні спроби без кадрів, snapshot polling — невдалі опитування. Це різні політики. |
| `output_frames`, `output_fps` | Кадри, опубліковані producer. FPS — до 64 вимірів за останні 10 секунд. Це не FPS джерела. |
| `subscriber_dropped_frames` | JPEG, відкинуті через заповнені локальні черги повільних глядачів, сума за глядачами. Це не втрати пакетів мережі. |
| `source_codec`, `source_resolution` | Необов'язкові спостереження з уже наявного bounded FFmpeg stderr drain. Без другого reader або probe; null означає невідоме. |
| `started_at`, `active`, `end_reason` | UTC для зіставлення логів, активність producer та фіксована причина, наприклад `client_disconnected`, `connect_failed`, `retry_exhausted`, `cleanup_failed`. |

`last_snapshot.frame_source`: `own_capture`, `shared_capture`, `live_buffer`,
`snapshot_cache` або null при невдачі. Cache/buffer не отримують вигаданий час
підключення. Shared callers зберігають attempt ID і first-frame time producer,
але мають власний wait time. Якщо після невдалого leader follower захопив кадр
сам, це його власна спроба. При timeout caller до завершення producer дані
завершеної спроби відсутні. Внутрішній bytes-only API захоплення сумісний.

Діагностика додає таймінги першокадрового етапу; чинний `duration_ms` лишається
повним часом етапу. Наявний UI може ігнорувати додаткові JSON-поля; нового
дашборда в цій зміні немає.

Після cleanup кожен producer пише один INFO-запис `Camera session completed`
у backend log з тими самими таймінгами й лічильниками. Він потрапляє до
завантажених логів; окремого вкладення support bundle не додано.
Модуль метрик не зберігає JPEG, URL, access codes і не логує кожен кадр.
У RAM залишаються активні producer, до 128 завершених identities і до 128
snapshot-рішень. Видалення принтера прибирає його асоціацію, перезапуск очищає
всі виміри. Відсутній запис означає невідоме, а не успіх.

Для підтримки збережіть статус під час проблеми та поточний backend log.
Діагностику запускайте доречно: без live producer вона може захоплювати кадр.
Перед порівнянням тривалостей звіряйте `attempt_id`. Відкидання кадрів через чергу
глядача саме по собі не доводить збій Wi-Fi чи причину затримки API/WebSocket.

## Експериментальний worker runtime

`CAMERA_RUNTIME=worker` — opt-in, за замовченням лишається `inline`. Він до
початку camera work запускає один supervised child. One-shot built-in/external
capture і live MJPEG, RTSP та snapshot зовнішніх камер ідуть через його
автентифікований loopback JPEG relay, а цей процес зберігає чинний browser
fan-out. Для кожного worker live lease є один producer на стабільний printer
identity без секретів і bounded черги лише останнього кадру; втрата media socket
звільняє producer.

Режим fail-closed. Помилка bootstrap/containment worker не повертає inline
transport. Built-in Bambu chamber/RTSPS і external live sources використовують
worker-owned decoded JPEG leases. Virtual Printer camera passthrough — окремий
raw TCP lease, тому він зберігає байти як є й не перетинається з JPEG relay того
самого джерела. Спочатку перевіряйте налаштування на підтримуваному хості:
поточна валідація не замінює physical-farm або Linux-service acceptance run.

[Перевірки й синтетичний baseline](testing/camera-observability.md).
