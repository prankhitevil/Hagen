# -*- coding: utf-8 -*-
"""Проверка 39: удаление в трёх объёмах, остановка задач, кнопка «Обработать».

Служба поднимается тестовым клиентом внутри процесса (антивирус не даёт
порождать фоновые процессы). Заголовок Origin обязателен, иначе POST/DELETE
отвергаются защитой.

Запускать из корня проекта:  .venv\\Scripts\\python.exe tests\\t39_delete_and_queue.py
"""
import io
import json
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, expect as check, finish  # noqa: E402

isolate.voices()

MADE = []


from fastapi.testclient import TestClient  # noqa: E402

from hagen import config, jobs, obsidian, server, store  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
STATIC = PROJECT / "hagen" / "static"


def make_rec(title, with_media=True, with_assets=True):
    """Завести запись с файлами: дорожка звука, видео рядом, стенограмма."""
    meta = store.create(title=title, mode="offline", source="file",
                        source_name=title + ".mp4")
    rec_id = meta["id"]
    MADE.append(rec_id)
    if with_media:
        io.open(store.paths(rec_id)["file"], "wb").write(b"\0" * 4096)
    store.replace_segments(rec_id, [
        {"id": "s1", "track": "file", "start": 0.0, "end": 2.0,
         "text": "Добрый день, начнём с бюджета.", "speaker": "Иванов"},
        {"id": "s2", "track": "file", "start": 2.0, "end": 4.0,
         "text": "Смету подготовлю к четвергу.", "speaker": "Петрова"},
    ])
    patch = {"duration_s": 4.0, "tracks": ["file"], "status": "recorded"}
    if with_assets:
        folder = "t39-" + rec_id
        d = store.assets_dir(rec_id, folder)
        d.mkdir(parents=True, exist_ok=True)
        io.open(d / "video.mp4", "wb").write(b"\0" * 8192)
        io.open(d / "transcript.txt", "w", encoding="utf-8").write("текстовая копия")
        patch.update({"assets_folder": folder, "video_path": str(d / "video.mp4"),
                      "video_bytes": 8192})
    store.update(rec_id, patch)
    return rec_id


with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:

    say("=== 1. Удалить только видео и звук ===")
    rec = make_rec("Проверка 39 — только медиа")
    assets = store.assets_dir(rec, (store.get(rec) or {}).get("assets_folder"))
    res = cli.request("DELETE", "/api/recordings/%s?scope=media" % rec, headers=ORIGIN)
    check("маршрут ответил", res.status_code, 200)
    body = res.json() if res.status_code == 200 else {}
    say("   освободилось: %s байт, файлов %s" % (body.get("freed_bytes"), body.get("files")))
    check("запись осталась", store.get(rec) is not None, True)
    check("звук удалён", store.paths(rec)["file"].exists(), False)
    check("видео удалено", (assets / "video.mp4").exists(), False)
    check("текстовая копия не тронута", (assets / "transcript.txt").exists(), True)
    check("стенограмма цела", len(store.sorted_segments(rec)), 2)
    m = store.get(rec) or {}
    check("метка «медиа удалено» стоит", m.get("media_removed"), True)
    check("дорожки сняты", m.get("tracks"), [])
    check("путь к видео снят", m.get("video_path"), None)
    check("освобождённое посчитано", body.get("freed_bytes") >= 4096 + 8192, True)

    say("")
    say("=== 2. По оставшейся стенограмме можно сделать документ ===")
    got = cli.get("/api/recordings/%s" % rec).json()
    check("запись отдаётся", len(got.get("segments") or []), 2)
    # Протокол и саммари ставятся в очередь — саму модель здесь не зовём,
    # проверяем, что маршруты принимают запись без звука.
    made = cli.post("/api/media/%s/summary" % rec, headers=ORIGIN, json={})
    check("саммари принято в работу", made.status_code, 200)
    job_id = (made.json() or {}).get("job_id")
    check("остановка сработала", jobs.cancel(job_id), True)
    # Разметка и перечитывание без звука должны честно отказать
    dia = cli.post("/api/recordings/%s/diarize" % rec, headers=ORIGIN, json={})
    check("разметка отказала", dia.status_code, 400)
    say("   ответ: %s" % str((dia.json() or {}).get("detail"))[:70])
    ret = cli.post("/api/recordings/%s/retranscribe" % rec, headers=ORIGIN, json={})
    check("перечитать отказало", ret.status_code, 400)

    say("")
    say("=== 2б. Старая запись: видео по прямому пути, без папки ===")
    # У записей, сделанных до появления папок ассетов, assets_folder пустой,
    # а video_path есть. Такое видео тоже обязано удаляться.
    old = make_rec("Проверка 39 — старая запись", with_assets=False)
    old_dir = store.assets_root() / ("t39-старая-" + old)
    old_dir.mkdir(parents=True, exist_ok=True)
    old_video = old_dir / "video.mp4"
    io.open(old_video, "wb").write(b"\0" * 2048)
    store.update(old, {"video_path": str(old_video), "assets_folder": ""})
    res = cli.request("DELETE", "/api/recordings/%s?scope=media" % old, headers=ORIGIN)
    check("маршрут ответил", res.status_code, 200)
    check("видео по прямому пути удалено", old_video.exists(), False)
    check("метка поставлена", (store.get(old) or {}).get("media_removed"), True)

    # Чужой файл вне хранилища тяжёлых не трогаем
    alien = make_rec("Проверка 39 — чужое видео", with_assets=False)
    alien_video = PROJECT / "data" / "_uploads" / "t39_чужое.mp4"
    alien_video.parent.mkdir(parents=True, exist_ok=True)
    io.open(alien_video, "wb").write(b"\0" * 1024)
    store.update(alien, {"video_path": str(alien_video), "assets_folder": ""})
    cli.request("DELETE", "/api/recordings/%s?scope=media" % alien, headers=ORIGIN)
    check("чужой файл цел", alien_video.exists(), True)
    alien_video.unlink(missing_ok=True)
    try:
        old_dir.rmdir()
    except OSError:
        pass

    say("")
    say("=== 3. Удалить из истории: файлы снаружи остаются ===")
    rec2 = make_rec("Проверка 39 — из истории")
    assets2 = store.assets_dir(rec2, (store.get(rec2) or {}).get("assets_folder"))
    note2 = config.vault_root() / "Встречи" / "t39-из-истории.md"
    note2.parent.mkdir(parents=True, exist_ok=True)
    io.open(note2, "w", encoding="utf-8").write("# заметка\n")
    store.update(rec2, {"vault_path": str(note2)})
    res = cli.request("DELETE", "/api/recordings/%s?scope=history" % rec2, headers=ORIGIN)
    check("маршрут ответил", res.status_code, 200)
    check("записи в списке нет", store.get(rec2), None)
    check("видео осталось", (assets2 / "video.mp4").exists(), True)
    check("заметка осталась", note2.exists(), True)

    say("")
    say("=== 4. Удалить всё ===")
    rec3 = make_rec("Проверка 39 — всё")
    assets3 = store.assets_dir(rec3, (store.get(rec3) or {}).get("assets_folder"))
    note3 = config.vault_root() / "Встречи" / "t39-всё.md"
    note3.parent.mkdir(parents=True, exist_ok=True)
    io.open(note3, "w", encoding="utf-8").write("# заметка\n")
    store.update(rec3, {"vault_path": str(note3)})
    res = cli.request("DELETE", "/api/recordings/%s?scope=all" % rec3, headers=ORIGIN)
    check("маршрут ответил", res.status_code, 200)
    check("запись удалена", store.get(rec3), None)
    check("папка записи убрана", store.rec_dir(rec3).exists(), False)
    check("видео и папка рядом убраны", assets3.exists(), False)
    check("заметка убрана", note3.exists(), False)
    check("в ответе сказано про заметку", (res.json() or {}).get("note_deleted"), True)

    say("")
    say("=== 5. Неизвестный объём удаления отвергается ===")
    rec4 = make_rec("Проверка 39 — чужой объём")
    bad = cli.request("DELETE", "/api/recordings/%s?scope=всё-подряд" % rec4, headers=ORIGIN)
    check("маршрут отказал", bad.status_code, 400)
    check("запись на месте", store.get(rec4) is not None, True)
    gone = cli.request("DELETE", "/api/recordings/20200101-000000-0000?scope=all",
                       headers=ORIGIN)
    check("несуществующая запись — 404", gone.status_code, 404)

say("")
say("=== 6. Заметку вне хранилища не трогаем ===")
rec5 = make_rec("Проверка 39 — чужой путь")
stranger = PROJECT / "data" / "_uploads" / "t39_чужая.md"
stranger.parent.mkdir(parents=True, exist_ok=True)
io.open(stranger, "w", encoding="utf-8").write("# чужой файл\n")
store.update(rec5, {"vault_path": str(stranger)})
out = obsidian.delete_note(rec5)
check("удаление отклонено", out.get("deleted"), False)
check("причина названа", out.get("why"), "путь вне хранилища")
check("чужой файл цел", stranger.exists(), True)
stranger.unlink(missing_ok=True)

say("")
say("=== 7. Удалённая запись не воскресает ===")
# Отменённая задача договаривает свой шаг уже после удаления. Раньше mkdir
# поднимал папку заново, и на диске оставался невидимый мусор.
rec6 = make_rec("Проверка 39 — гонка", with_assets=False)
store.delete(rec6)
check("папки нет", store.rec_dir(rec6).exists(), False)
store.update(rec6, {"title": "поздняя правка"})
store.save_transcript(rec6, {"segments": [{"id": "x", "text": "поздняя реплика"}]})
store.append_segment(rec6, {"id": "y", "text": "ещё одна"})
check("папка не воскресла", store.rec_dir(rec6).exists(), False)
check("записи по-прежнему нет", store.get(rec6), None)

say("")
say("=== 8. Остановка задачи ===")
started = []


def slow(handle):
    """Задача, которая слушается остановки."""
    started.append(1)
    for _ in range(200):
        if handle.cancelled:
            raise RuntimeError("Задача отменена")
        time.sleep(0.05)
    return {"finished": True}


job_id = jobs.submit("test", slow, "Проверка 39: долгая задача")
t0 = time.time()
while not started and time.time() - t0 < 10:
    time.sleep(0.05)
check("задача пошла", bool(started), True)
check("остановка принята", jobs.cancel(job_id), True)
t0 = time.time()
while time.time() - t0 < 15:
    j = jobs.get(job_id) or {}
    if j.get("status") not in ("running", "queued"):
        break
    time.sleep(0.1)
j = jobs.get(job_id) or {}
check("задача остановлена, а не упала", j.get("status"), "cancelled")
check("ошибки не показываем", j.get("error"), None)
say("   подпись: %r" % j.get("note"))

# Та, что ещё не начиналась, снимается из очереди
queued = jobs.submit("test", lambda h: {"ok": True}, "Проверка 39: из очереди")
jobs.cancel(queued)
t0 = time.time()
while time.time() - t0 < 10:
    j2 = jobs.get(queued) or {}
    if j2.get("status") not in ("running", "queued"):
        break
    time.sleep(0.1)
check("снята до запуска", (jobs.get(queued) or {}).get("status"), "cancelled")
check("уже завершённую не остановить", jobs.cancel(queued), False)

say("")
say("=== 8б. Служба говорит странице, что отмена запрошена ===")
slow2 = []


def slow_job(handle):
    slow2.append(1)
    for _ in range(200):
        if handle.cancelled:
            raise RuntimeError("Задача отменена")
        time.sleep(0.05)
    return {}


jid = jobs.submit("test", slow_job, "Проверка 39: признак отмены")
t0 = time.time()
while not slow2 and time.time() - t0 < 10:
    time.sleep(0.05)
jobs.cancel(jid)
pub = jobs.get(jid) or {}
check("признак отмены виден странице", pub.get("cancel_requested"), True)
t0 = time.time()
while time.time() - t0 < 15 and (jobs.get(jid) or {}).get("status") == "running":
    time.sleep(0.1)

say("")
say("=== 8в. Документы: протокол и саммари в разных файлах ===")
doc = make_rec("Проверка 39 — документы", with_assets=False)
from hagen import minutes  # noqa: E402

minutes._save_result(doc, "protocol", "# Протокол совещания\nтекст протокола", "тест")
minutes._save_result(doc, "video_summary", "## О чём запись\nтекст саммари", "тест")
check("протокол на месте", "текст протокола" in
      io.open(store.paths(doc)["minutes"], "r", encoding="utf-8").read(), True)
check("саммари отдельным файлом",
      (store.rec_dir(doc) / "summary.md").exists(), True)
# Показывается последний собранный документ (13.09: у записи их может быть несколько)
check("показывается последний собранный — саммари",
      "саммари" in minutes.load_minutes(doc), True)
store.update(doc, {"last_document": "protocol"})
check("а если последним был протокол — протокол", "протокола" in minutes.load_minutes(doc), True)
check("в списке документов оба", [d["key"] for d in minutes.stored_documents(doc)],
      ["protocol", "meeting"])

# Документ удалённой записи никуда не пишется
store.delete(doc)
try:
    minutes._save_result(doc, "protocol", "поздний протокол", "тест")
    FAIL.append("документ записался в удалённую запись")
    say("   ПЛОХО документ записался в удалённую запись")
except RuntimeError as err:
    say("   ok    отказ сохранять в удалённую запись: %r" % str(err)[:60])
check("папка не воскресла", store.rec_dir(doc).exists(), False)

say("")
say("=== 9. Интерфейс: обработка по кнопке, без «Старта» у видео ===")
html = io.open(STATIC / "index.html", "r", encoding="utf-8").read()
appjs = io.open(STATIC / "app.js", "r", encoding="utf-8").read()
videojs = io.open(STATIC / "video.js", "r", encoding="utf-8").read()
css = io.open(STATIC / "app.css", "r", encoding="utf-8").read()

check("кнопка «Обработать» есть", 'id="btn-process"' in html, True)
check("список выбранных файлов есть", 'id="v-files"' in html, True)
check("перетаскивание больше не запускает обработку",
      "window.videoUpload" in appjs, False)
check("бросок кладёт файлы в список", "window.videoPickFiles" in appjs, True)
check("video.js отдаёт videoPickFiles", "window.videoPickFiles = vPickFiles" in videojs, True)
check("выбор файла тоже только кладёт", "inp.onchange = () => vPickFiles" in videojs, True)
check("кнопка гаснет на время записи", "proc.disabled" in videojs, True)

check("блок живой записи помечен", 'id="live-controls"' in html, True)
check("поле режима помечено", 'id="rec-mode-field"' in html, True)
check("одна вкладка «+ Документ»", 'id="btn-add-doc"' in html and 'id="btn-summary"' not in html, True)
# После этапа 6 блока записи нет и у готовой записи: он нужен только новой и
# идущей (п. 6.1). Решение одно, в paintRecShell.
check("блок прячется у видеозаписи и у готовой записи",
      "isVideoRec(m) && !S.recordingId && S.mode === 'video'" in appjs
      and "function paintRecShell()" in appjs, True)
# Самое важное: «Стоп» обязан работать, какая бы запись ни была открыта
check("«Стоп» считается по факту записи",
      "$('btn-stop').disabled = !S.recordingId || S.starting" in appjs, True)
check("значок записи тоже по факту записи",
      "!(S.recordingId && (!m || m.id === S.recordingId))" in appjs, True)
check("пустое событие записи не ломает страницу",
      "if (!m || !m.id) return;" in appjs, True)    # обработчик в таблице EVENT_HANDLERS
check("настройки видео не сбрасываются", "const first = V.opts === null" in videojs, True)
check("убранный файл не уходит в работу",
      "убран из списка — пропущен" in videojs, True)

check("диалог удаления есть", 'id="dlg-delete"' in html, True)
for scope in ("all", "media", "history"):
    check("ответ «%s» в диалоге" % scope, 'data-scope="%s"' % scope in html, True)
check("старого вопроса нет", "вместе с аудио" in appjs, False)
check("кнопка остановки задачи есть", "js-stop-job" in appjs, True)
check("стили списка файлов на месте", ".file-item{" in css, True)
check("стиль выключенного пункта на месте", ".tpl.off{" in css, True)

say("")
say("=== 10. Уборка ===")
for rid in list(MADE):
    m = store.get(rid) or {}
    folder = str(m.get("assets_folder") or "")
    if folder:
        import shutil

        shutil.rmtree(store.assets_dir(rid, folder), ignore_errors=True)
    store.delete(rid)
left = [r for r in MADE if store.get(r) is not None]
check("тестовые записи убраны", left, [])
for n in (config.vault_root() / "Встречи" / "t39-из-истории.md",
          config.vault_root() / "Встречи" / "t39-всё.md"):
    try:
        n.unlink(missing_ok=True)
    except OSError:
        pass

sys.exit(finish("t39"))
