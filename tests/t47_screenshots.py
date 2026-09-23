# -*- coding: utf-8 -*-
"""Проверка 47: снимки экрана со встречи — в заметку, в момент разговора.

Решения 13.09: модели снимки не показываются, лежат в сейфе рядом с
заметкой, удаляются вместе с ней.

Сейф на время проверки — временная папка: в настоящий сейф на Яндекс.Диске
тестовые картинки не попадают. Буфер обмена проверка использует по-настоящему
и после себя возвращает в него прежний текст.

  1. Снимок в буфере (как Win+Shift+S) попадает в запись с временем.
  2. То, что лежало в буфере ДО записи, не берётся.
  3. Файл в папке снимков, появившийся во время записи, берётся; старый — нет.
  4. Один снимок, пришедший и в буфер, и в папку, не дублируется.
  5. В заметке снимок стоит между репликами по времени, ссылкой Obsidian.
  6. «Удалить всё» убирает файлы снимков; «только видео и звук» — оставляет.
  7. Склейка записей сдвигает время снимков.
  8. Сквозь службу: запись с микрофона → снимок → стоп → заметка с картинкой.
  9. Папки снимков: список из настроек сильнее догадки, %ПЕРЕМЕННЫЕ% раскрываются,
     несуществующие пропускаются; догадка находит «Изображения», перенесённые
     в OneDrive; служба отдаёт странице и то, и другое.
"""
import io
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()


import win32clipboard  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from hagen import calls, config, obsidian, recordings, store  # noqa: E402
from hagen.platform.windows import shots  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t47_"))
VAULT = TMP / "vault"
SHOTS_IN = TMP / "screens"
VAULT.mkdir()
SHOTS_IN.mkdir()
OVERRIDES = {"vault_path": str(VAULT), "vault_subfolder": "Meetings",
             "screenshot_folders": [str(SHOTS_IN)], "screenshots_enabled": True}
_real_get = config.get
config.get = lambda k, d=None: OVERRIDES[k] if k in OVERRIDES else _real_get(k, d)


def picture(seed, size=(640, 360)):
    img = Image.new("RGB", size, (20 + seed * 40 % 200, 90, 160))
    d = ImageDraw.Draw(img)
    d.rectangle((40 + seed * 13, 40, 300 + seed * 7, 200), fill=(250, 250, 250))
    d.text((60, 80), "Снимок %d" % seed, fill=(0, 0, 0))
    return img


def to_clipboard(img):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "BMP")
    data = buf.getvalue()[14:]           # CF_DIB — BMP без файлового заголовка
    for _ in range(20):
        try:
            win32clipboard.OpenClipboard()
            break
        except Exception:
            time.sleep(0.1)
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, data)
    finally:
        win32clipboard.CloseClipboard()


def clipboard_text():
    try:
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_UNICODETEXT):
                return win32clipboard.GetClipboardData(win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass
    return None


def restore_text(text):
    try:
        win32clipboard.OpenClipboard()
        try:
            win32clipboard.EmptyClipboard()
            if text is not None:
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
        finally:
            win32clipboard.CloseClipboard()
    except Exception:
        pass


saved_clip = clipboard_text()

# Остатки прошлых прогонов, если проверка когда-то упала посередине
for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 47"):
        store.delete(old["id"])

try:
    say("=== 0. По умолчанию буфер обмена не смотрим (решение 13.09) ===")
    meta = store.create(title="Проверка 47", mode="online", source="live",
                        category="Встречи")
    rid = meta["id"]
    check("по умолчанию снимки только из папки", config.get("screenshots_from_clipboard") in (False, None),
          config.get("screenshots_from_clipboard"))
    w0 = shots.ScreenshotWatcher(rid, position_s=lambda: 5.0, on_shot=lambda s: None,
                                 folder=lambda: obsidian.shots_dir(store.get(rid) or {}))
    w0.start()
    time.sleep(1.2)
    to_clipboard(picture(7))
    time.sleep(2.2)
    w0.stop()
    check("картинка в буфере во время записи не взята", w0.count == 0, w0.count)

    say("")
    say("=== 1–4. Наблюдатель (с включённым буфером — запасной режим) ===")
    OVERRIDES["screenshots_from_clipboard"] = True
    old_file = SHOTS_IN / "старый.png"
    picture(9).save(old_file)
    to_clipboard(picture(8))                 # лежит в буфере ДО записи
    pos = {"s": 12.0}
    got = []
    w = shots.ScreenshotWatcher(rid, position_s=lambda: pos["s"], on_shot=got.append,
                                folder=lambda: obsidian.shots_dir(store.get(rid) or {}))
    w.start()
    time.sleep(2.2)
    check("старый снимок из буфера и старый файл не взяты", w.count == 0, w.count)

    to_clipboard(picture(1))
    time.sleep(2.2)
    check("снимок из буфера взят", w.count == 1, w.count)
    s = (store.get(rid) or {}).get("screenshots") or []
    check("время снимка — позиция в записи", s and s[0]["at_s"] == 12.0, s and s[0]["at_s"])
    check("файл лежит в сейфе рядом с заметкой, в «Скриншоты»",
          s and Path(s[0]["path"]).exists()
          and Path(s[0]["path"]).parent == VAULT / "Meetings" / "Встречи" / "Скриншоты",
          s and s[0]["path"])

    pos["s"] = 65.0
    img2 = picture(2)
    to_clipboard(img2)
    time.sleep(0.3)
    img2.save(SHOTS_IN / "Снимок экрана 2.png")   # тот же снимок и в папке — как в Windows 11
    time.sleep(5.0)
    check("снимок, пришедший и в буфер, и в папку, не задвоен", w.count == 2, w.count)

    pos["s"] = 130.0
    picture(3).save(SHOTS_IN / "Снимок экрана 3.png")
    time.sleep(5.0)
    w.stop()
    check("новый файл из папки снимков взят", w.count == 3, w.count)
    s = (store.get(rid) or {}).get("screenshots") or []
    check("источники записаны", [x["source"] for x in s] == ["clipboard", "clipboard", "folder"],
          [x["source"] for x in s])

    say("")
    say("=== 5. Заметка ===")
    store.replace_segments(rid, [
        store.make_segment("mic", 0.0, 5.0, "Начинаем."),
        store.make_segment("far", 20.0, 25.0, "Смотрите график."),
        store.make_segment("mic", 70.0, 75.0, "Понял."),
    ])
    res = obsidian.save_note(rid)
    text = Path(res["path"]).read_text(encoding="utf-8")
    names = [x["file"] for x in s]
    i_first = text.find("Начинаем.")
    i_shot1 = text.find("![[%s|700]]" % names[0])
    i_graph = text.find("Смотрите график.")
    i_shot2 = text.find("![[%s|700]]" % names[1])
    i_ok = text.find("Понял.")
    i_shot3 = text.find("![[%s|700]]" % names[2])
    check("все три снимка в заметке", min(i_shot1, i_shot2, i_shot3) > 0, (i_shot1, i_shot2, i_shot3))
    check("снимок на 12 с — между репликами 0 с и 20 с", i_first < i_shot1 < i_graph)
    check("снимок на 65 с — между 20 с и 70 с", i_graph < i_shot2 < i_ok)
    check("снимок на 130 с — после последней реплики", i_ok < i_shot3)
    check("подпись со временем", "🖼 **[00:01:05] Снимок экрана**" in text)
    minutes_prompts = io.open(PROJECT / "hagen" / "minutes.py", encoding="utf-8").read()
    for_model = __import__("hagen.minutes", fromlist=["x"]).build_transcript_text(rid)
    check("модели — только время и имя снимка, без картинки и без вставки",
          "[00:00:12] 🖼 Снимок экрана: %s" % names[0] in for_model and "![[" not in for_model,
          for_model[:300])

    say("")
    say("=== 6–7. Склейка и удаление ===")
    dst = store.create(title="Проверка 47 — прошлая", mode="online", source="live",
                       category="Встречи")["id"]
    from hagen import audio_io
    import numpy as np

    audio_io.write_wav(store.track_path(dst, "mic"), np.zeros(100 * 16000, dtype=np.float32))
    audio_io.write_wav(store.track_path(rid, "mic"), np.zeros(10 * 16000, dtype=np.float32))
    calls.merge_recordings(rid, dst)
    moved = (store.get(dst) or {}).get("screenshots") or []
    check("склейка сдвинула время снимков на 100 с", [x["at_s"] for x in moved] == [112.0, 165.0, 230.0],
          [x["at_s"] for x in moved])
    files = [Path(x["path"]) for x in moved]

    from fastapi.testclient import TestClient

    from hagen import server

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.delete("/api/recordings/%s?scope=media" % dst, headers=ORIGIN)
        check("«только видео и звук» снимки оставляет", r.status_code == 200 and all(f.exists() for f in files))
        store.update(dst, {"media_removed": False})
        r = cli.delete("/api/recordings/%s?scope=all" % dst, headers=ORIGIN)
        check("«удалить всё» убирает снимки", r.status_code == 200 and not any(f.exists() for f in files),
              r.json() if r.status_code == 200 else r.text)
        check("служба сказала, сколько снимков убрано", (r.json() or {}).get("shots_deleted") == 3, r.json())

        say("")
        say("=== 8. Сквозь службу: запись → снимок → заметка ===")
        OVERRIDES["mode"] = "offline"
        OVERRIDES.pop("screenshots_from_clipboard", None)   # обычный режим: только папка
        started = cli.post("/api/recordings", headers=ORIGIN, json={"title": "Проверка 47 — живая",
                                                                   "mode": "offline"})
        rec = started.json()["meta"]["id"]
        for _ in range(60):
            if recordings.watching_shots(rec):
                break
            time.sleep(0.2)
        check("со стартом записи включилось наблюдение за снимками", recordings.watching_shots(rec))
        time.sleep(3.0)
        picture(5).save(SHOTS_IN / "Снимок экрана 5.png")   # как Win+Shift+S в «Снимки экрана»
        time.sleep(3.0)
        cli.post("/api/recordings/%s/stop" % rec, headers=ORIGIN, json={})
        check("после стопа наблюдение выключено", not recordings.watching_shots(rec))
        m = store.get(rec) or {}
        sh = m.get("screenshots") or []
        check("снимок попал в живую запись", len(sh) == 1, len(sh))
        check("время снимка правдоподобно (2–9 с)", sh and 2.0 <= sh[0]["at_s"] <= 9.0, sh and sh[0]["at_s"])
        r = cli.get("/api/recordings/%s" % rec)
        keys = [p["key"] for p in r.json().get("paths") or []]
        check("в карточке есть «Снимки экрана»", "shots" in keys, keys)
        saved = cli.post("/api/recordings/%s/save" % rec, headers=ORIGIN, json={})
        note = Path(saved.json().get("path") or "")
        body = note.read_text(encoding="utf-8") if str(note) != "." and note.is_file() else ""
        check("в заметке живой записи картинка", sh and ("![[%s|700]]" % sh[0]["file"]) in body)
        cli.delete("/api/recordings/%s?scope=all" % rec, headers=ORIGIN)
        check("живая запись и её снимок убраны", store.get(rec) is None and sh and not Path(sh[0]["path"]).exists())

    say("")
    say("=== 9. Папки снимков: настройка и догадка ===")
    import os

    os.environ["T47_ROOT"] = str(TMP)
    OVERRIDES["screenshot_folders"] = [r"%T47_ROOT%\screens", str(TMP / "нет такой"), "  "]
    check("список из настроек: переменная раскрыта, несуществующая пропущена",
          shots.screenshot_folders() == [SHOTS_IN], shots.screenshot_folders())
    # «Изображения» перенесены (как в OneDrive), своей записи у «Снимков экрана» нет
    moved = TMP / "OneDrive - Contoso" / "Pictures"
    (moved / "Screenshots").mkdir(parents=True)
    _real_shell = shots._shell_folder
    shots._shell_folder = lambda name: moved if name == "My Pictures" else None
    try:
        found = shots.screenshot_folders(auto=True)
        check("догадка находит «Screenshots» в перенесённых «Изображениях»",
              moved / "Screenshots" in found, found)
        check("догадка не смотрит в настройку", SHOTS_IN not in found, found)
        check("пока список задан, догадка не действует",
              shots.screenshot_folders() == [SHOTS_IN], shots.screenshot_folders())
        OVERRIDES["screenshot_folders"] = []
        check("пустой список — действует догадка", moved / "Screenshots" in shots.screenshot_folders(),
              shots.screenshot_folders())
        OVERRIDES["screenshot_folders"] = [str(SHOTS_IN)]
        with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
            r = cli.get("/api/screenshots/folders")
            body = r.json() if r.status_code == 200 else {}
            check("служба отдаёт, за какими папками следим", body.get("active") == [str(SHOTS_IN)], body)
            check("служба отдаёт, что нашлось бы само",
                  str(moved / "Screenshots") in (body.get("auto") or []), body)
    finally:
        shots._shell_folder = _real_shell
        os.environ.pop("T47_ROOT", None)
finally:
    restore_text(saved_clip)
    config.get = _real_get
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t47"))
