"""Fold historical failure-reason spellings onto the one key vocabulary (upstream 5211fd45, #2974).

Three writers put three spellings of one cause into ``print_archives.failure_reason``:
the backend wrote English display labels ("Layer shift"), older archive editors
wrote the TRANSLATED label in whatever locale the user ran, and the stale /
reconnect paths wrote English sentences. The Failure Analysis widget groups on the
raw value, so one cause occupied several buckets — and a stored label has no key
for ``t()`` to resolve, so in Ukrainian one of them stayed English. The writers
now produce keys (``utils/failure_reasons``); this converts what is already stored.

The map is a FROZEN SNAPSHOT, deliberately not read from the locale files: it maps
what WAS written, and a translation reworded tomorrow must not stop it
recognising yesterday's rows. It is upstream's map — every label of all 14 of its
locales, since a Bambuddy database is imported as is — plus every en / uk label
this project's locales ever carried (read from their git history: no label maps
to two keys) and this project's own two sentences. A value outside the map is
left alone: guessing would be worse than one honest string in its own bucket.

Runs once (the ``_migrations`` ledger). The statement only matches labels and a
key is never a label, so running it again would change nothing either.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import bindparam, text

logger = logging.getLogger(__name__)

version = 186
name = "failure_reason_vocabulary"

LEGACY_FAILURE_REASON_LABELS: dict[str, str] = {
    "Adhesion failure": "adhesionFailure",
    "Agotamiento del filamento": "filamentRunout",
    "Alabeo": "warping",
    "Altro": "other",
    "Annullato dall'utente": "userCancelled",
    "Annulé par l'utilisateur": "userCancelled",
    "Aucune mise à jour d'état reçue": "noStatusUpdate",
    "Autre": "other",
    "Az ekstrüzyon": "underExtrusion",
    "Bico entupido": "cloggedNozzle",
    "Boquilla obstruida": "cloggedNozzle",
    "Buse bouchée": "cloggedNozzle",
    "Bükülme": "warping",
    "Cancelada por el usuario": "userCancelled",
    "Cancelado pelo usuário": "userCancelled",
    "Clogged nozzle": "cloggedNozzle",
    "Corte de corriente": "powerFailure",
    "Coupure courant": "powerFailure",
    "Deformazione": "warping",
    "Deslocamento de camada": "layerShift",
    "Desplazamiento de capa": "layerShift",
    "Diğer": "other",
    "Door gebruiker geannuleerd": "userCancelled",
    "Draadvorming": "stringing",
    "Durum güncellemesi alınmadı": "noStatusUpdate",
    "Décalage de couche": "layerShift",
    "Défaut d'adhésion": "adhesionFailure",
    "Empenamento": "warping",
    "Espagueti / Desprendido": "spaghettiDetached",
    "Fadenziehen": "stringing",
    "Falha de adesão": "adhesionFailure",
    "Falha de energia": "powerFailure",
    "Fallimento adesione": "adhesionFailure",
    "Fallo de adhesión": "adhesionFailure",
    "Filament aufgebraucht": "filamentRunout",
    "Filament bitti": "filamentRunout",
    "Filament fini": "filamentRunout",
    "Filament op": "filamentRunout",
    "Filament runout": "filamentRunout",
    "Filamento": "stringing",
    "Filamento esaurito": "filamentRunout",
    "Fim do filamento": "filamentRunout",
    "Fios": "stringing",
    "Geen statusupdate ontvangen": "noStatusUpdate",
    "Güç kesintisi": "powerFailure",
    "Haftungsfehler": "adhesionFailure",
    "Hechtingsprobleem": "adhesionFailure",
    "Hilos": "stringing",
    "Katman kayması": "layerShift",
    "Kein Statusupdate empfangen": "noStatusUpdate",
    "Kromtrekken": "warping",
    "Kullanıcı iptal etti": "userCancelled",
    "Laagverschuiving": "layerShift",
    "Layer shift": "layerShift",
    "Mancanza corrente": "powerFailure",
    "Nenhuma atualização de status recebida": "noStatusUpdate",
    "Nessun aggiornamento di stato ricevuto": "noStatusUpdate",
    "No se recibió actualización de estado": "noStatusUpdate",
    "No status update received": "noStatusUpdate",
    "Onderextrusie": "underExtrusion",
    "Other": "other",
    "Otro": "other",
    "Outcome uncertain after reconnect; inspect the printer and plate before resuming": "noStatusUpdate",
    "Outro": "other",
    "Overig": "other",
    "Power failure": "powerFailure",
    "Printer error": "printerError",
    "Schichtversatz": "layerShift",
    "Sonstiges": "other",
    "Sotto-estrusione": "underExtrusion",
    "Sous-extrusion": "underExtrusion",
    "Spagetti / Ayrılmış": "spaghettiDetached",
    "Spaghetti / Abgelöst": "spaghettiDetached",
    "Spaghetti / Destacado": "spaghettiDetached",
    "Spaghetti / Detached": "spaghettiDetached",
    "Spaghetti / Détaché": "spaghettiDetached",
    "Spaghetti / losgeraakt": "spaghettiDetached",
    "Spaghetti / staccato": "spaghettiDetached",
    "Spostamento layer": "layerShift",
    "Stale - print likely cancelled or failed without status update": "noStatusUpdate",
    "Stale - reconciled after reconnect, end time unknown": "noStatusUpdate",
    "Stopped by user (printer was offline)": "userCancelled",
    "Stringing": "stringing",
    "Stringing (Cheveux d'ange)": "stringing",
    "Stromausfall": "powerFailure",
    "Stroomuitval": "powerFailure",
    "Subextrusión": "underExtrusion",
    "Subextrusão": "underExtrusion",
    "Swap Mode error": "swapModeFailure",
    "Tıkalı nozul": "cloggedNozzle",
    "Ugello intasato": "cloggedNozzle",
    "Under-extrusion": "underExtrusion",
    "Unterextrusion": "underExtrusion",
    "User cancelled": "userCancelled",
    "Verformung": "warping",
    "Verstopfte Düse": "cloggedNozzle",
    "Verstopte nozzle": "cloggedNozzle",
    "Vom Benutzer abgebrochen": "userCancelled",
    "Warping": "warping",
    "Warping (Déformation)": "warping",
    "Yapışma başarısız": "adhesionFailure",
    "İplik oluşumu": "stringing",
    "Інше": "other",
    "Биття філаменту": "filamentRunout",
    "Викривлення": "warping",
    "Деформація": "warping",
    "Другое": "other",
    "Закончился филамент": "filamentRunout",
    "Закінчився філамент": "filamentRunout",
    "Засмічене сопло": "cloggedNozzle",
    "Засор сопла": "cloggedNozzle",
    "Збій живлення": "powerFailure",
    "Зсув шару": "layerShift",
    "Зсув шарів": "layerShift",
    "Користувач скасовано": "userCancelled",
    "Коробление": "warping",
    "Нанизування": "stringing",
    "Недоекструзія": "underExtrusion",
    "Недоэкструзия": "underExtrusion",
    "Нитки": "stringing",
    "Обновление статуса не получено": "noStatusUpdate",
    "Оновлення статусу не отримано": "noStatusUpdate",
    "Отменено пользователем": "userCancelled",
    "Плохая адгезия к столу": "adhesionFailure",
    "Помилка адгезії": "adhesionFailure",
    "Помилка принтера": "printerError",
    "Помилка свап-мода": "swapModeFailure",
    "Порушення адгезії": "adhesionFailure",
    "Підвидавлювання": "underExtrusion",
    "Сбой питания": "powerFailure",
    "Сдвиг слоёв": "layerShift",
    "Скасовано користувачем": "userCancelled",
    "Спагетти / отрыв детали": "spaghettiDetached",
    "Спагетті / Відр": "spaghettiDetached",
    "Спагеті / Відклеїлось": "spaghettiDetached",
    "Стрингинг": "stringing",
    "інше": "other",
    "その他": "other",
    "ステータス更新を受信できませんでした": "noStatusUpdate",
    "スパゲッティ / 剥離": "spaghettiDetached",
    "ノズル詰まり": "cloggedNozzle",
    "フィラメント切れ": "filamentRunout",
    "ユーザーによるキャンセル": "userCancelled",
    "レイヤーシフト": "layerShift",
    "使用者取消": "userCancelled",
    "其他": "other",
    "反り": "warping",
    "喷嘴堵塞": "cloggedNozzle",
    "噴嘴堵塞": "cloggedNozzle",
    "定着不良": "adhesionFailure",
    "层偏移": "layerShift",
    "層偏移": "layerShift",
    "押出不足": "underExtrusion",
    "拉丝": "stringing",
    "拉丝 / 脱落": "spaghettiDetached",
    "拉絲": "stringing",
    "拉絲 / 脫落": "spaghettiDetached",
    "挤出不足": "underExtrusion",
    "擠出不足": "underExtrusion",
    "断电": "powerFailure",
    "斷電": "powerFailure",
    "未收到状态更新": "noStatusUpdate",
    "未收到狀態更新": "noStatusUpdate",
    "用户取消": "userCancelled",
    "糸引き": "stringing",
    "翘曲": "warping",
    "翹曲": "warping",
    "耗材用完": "filamentRunout",
    "附着力失败": "adhesionFailure",
    "附著力失敗": "adhesionFailure",
    "電源障害": "powerFailure",
    "기타": "other",
    "노즐 막힘": "cloggedNozzle",
    "레이어 시프트": "layerShift",
    "사용자 취소": "userCancelled",
    "상태 업데이트를 받지 못함": "noStatusUpdate",
    "스트링": "stringing",
    "스파게티 / 분리": "spaghettiDetached",
    "압출 부족": "underExtrusion",
    "전원 실패": "powerFailure",
    "접착 실패": "adhesionFailure",
    "필라멘트 소진": "filamentRunout",
    "휨": "warping",
}


async def upgrade(conn):
    """No schema change — the conversion is data, in ``seed``."""


async def seed(session_factory):
    by_key: dict[str, list[str]] = defaultdict(list)
    for label, key in LEGACY_FAILURE_REASON_LABELS.items():
        if label != key:
            by_key[key].append(label)

    total = 0
    async with session_factory() as db:
        for key, labels in sorted(by_key.items()):
            result = await db.execute(
                text("UPDATE print_archives SET failure_reason = :key WHERE failure_reason IN :labels").bindparams(
                    bindparam("key"), bindparam("labels", expanding=True)
                ),
                {"key": key, "labels": labels},
            )
            total += result.rowcount or 0
        await db.commit()
    if total:
        logger.info("m186: converted %d failure_reason value(s) to the key vocabulary", total)
