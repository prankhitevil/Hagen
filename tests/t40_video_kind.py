# -*- coding: utf-8 -*-
"""Проверка 40: тип записи решает разметку, документ и что хранить.

Раньше разметка говорящих включалась всегда: час лекции с одним голосом впустую
съедал одиннадцать минут процессора. Теперь тип записи («встреча», «лекция»,
«интервью», «только расшифровка») подставляет три настройки разом, и каждую
можно поменять руками.

Запускать из корня проекта:  .venv\\Scripts\\python.exe tests\\t40_video_kind.py
"""
import io
import shutil
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()

LINES = []
FAIL = []
MADE = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + ": " + repr(got)
        + ("" if ok else "  (ждали " + repr(want) + ")"))


from hagen import jobs, media, minutes, store  # noqa: E402

STATIC = PROJECT / "hagen" / "static"

# Остатки прошлых прогонов, если проверка когда-то упала посередине
for _old in store.list_all():
    if str(_old.get("title") or "").startswith(("t40_", "Проверка 40")):
        store.delete(_old["id"])


def wait_job(job_id, limit=180.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        job = jobs.get(job_id)
        if job and job.get("status") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.3)
    return {"status": "timeout"}


say("=== 1. Что подставляет каждый тип ===")
for kind, diarize, document, storage in (
        ("meeting", True, "meeting", "video"),
        ("lecture", False, "lecture", "none"),
        ("interview", True, "interview", "audio"),
        ("transcript", False, "", "none")):
    d = media.KIND_DEFAULTS[kind]
    check("%s: разметка" % kind, d["diarize"], diarize)
    check("%s: документ" % kind, d["document"], document)
    check("%s: хранить" % kind, d["store"], storage)

say("")
say("=== 2. Разбор настроек задания ===")
check("неизвестный тип — как встреча", media._kind({"video_kind": "ерунда"}), "meeting")
check("тип не задан — встреча", media._kind({}), "meeting")
check("лекция принята", media._kind({"video_kind": "lecture"}), "lecture")
check("хранение берётся из типа",
      media._store_mode({"video_kind": "lecture"}), "none")
check("но своё значение сильнее",
      media._store_mode({"video_kind": "lecture", "store_media": "video"}), "video")
check("старое задание с галочкой понимается",
      media._store_mode({"keep_video": True}), "video")
check("старое задание без галочки — только звук",
      media._store_mode({"keep_video": False}), "audio")
check("видео сохраняем только в режиме «видео»",
      media._keeps_video({"store_media": "video"}), True)
check("при «только скачать» видео сохраняем всегда",
      media._keeps_video({"store_media": "none", "video_only": True}), True)

say("")
say("=== 3. Разметка: тип и общая настройка вместе ===")
check("встреча + автоматически = размечаем",
      media._wants_diarize({"video_kind": "meeting", "diarize_auto": True}), True)
check("лекция не размечается, даже если автоматически включено",
      media._wants_diarize({"video_kind": "lecture", "diarize_auto": True}), False)
check("встреча без автоматической разметки не размечается",
      media._wants_diarize({"video_kind": "meeting", "diarize_auto": False}), False)
check("только расшифровка не размечается",
      media._wants_diarize({"video_kind": "transcript", "diarize_auto": True}), False)

say("")
say("=== 4. Документы разных жанров ===")
check("у встречи свой промпт",
      "краткое, но содержательное" in minutes.video_prompt("meeting"), True)
check("у лекции свой", "конспект" in minutes.video_prompt("lecture"), True)
check("у интервью свой", "выжимк" in minutes.video_prompt("interview"), True)
# В конспекте лекции не должно быть разделов совещания
lec = minutes.video_prompt("lecture")
check("в конспекте запрещены «задачи и договорённости»",
      "«Задачи и договорённости» быть НЕ должно" in lec, True)
check("в конспекте просят термины", "## Термины" in lec, True)
check("в выжимке просят главные мысли",
      "## Главные мысли" in minutes.video_prompt("interview"), True)
for kind in ("meeting", "lecture", "interview"):
    p = minutes.video_prompt(kind)
    check("%s: просим служебные строки" % kind, "НАЗВАНИЕ:" in p and "ПАПКА:" in p, True)
    check("%s: запрет на скриншоты" % kind, "скриншоты НЕ вставляй" in p, True)

say("")
say("=== 5. Лекция через конвейер: разметки нет, конспект есть ===")
tmp_dir = PROJECT / "data" / "_uploads"
tmp_dir.mkdir(parents=True, exist_ok=True)
vtt = tmp_dir / "t40_lecture.vtt"
io.open(vtt, "w", encoding="utf-8", newline="\n").write(
    "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
    "Сегодня разберём, как устроен бюджетный процесс.\n\n"
    "00:00:05.500 --> 00:00:10.000\n"
    "Первый этап — планирование потребности.\n")

res = media.submit_file(vtt, "t40_lecture.vtt",
                        {"video_kind": "lecture", "prefer_transcript": True,
                         "make_summary": False, "diarize_auto": True})
rec = res["rec_id"]
MADE.append(rec)
job = wait_job(res["job_id"])
check("обработка прошла", job.get("status"), "done")
meta = store.get(rec) or {}
stages = meta.get("stages") or {}
check("тип записан в саму запись", meta.get("video_kind"), "lecture")
check("способ хранения записан", meta.get("store_media"), "none")
check("разметка пропущена", stages.get("diarize"), "skip")
check("задачи разметки не было",
      [j for j in jobs.for_recording(rec) if j.get("kind") == "diarize"], [])
segs = store.sorted_segments(rec)
# Соседние реплики одного голоса конвейер склеивает — считаем по тексту,
# а не по числу кусков.
text = " ".join(str(s.get("text") or "") for s in segs)
check("текст на месте", "бюджетный процесс" in text.lower(), True)
check("вторая фраза не потерялась", "планирование" in text.lower(), True)

say("")
say("=== 6. «Только расшифровка»: документа не делаем ===")
# Файл источника конвейер за собой убирает, поэтому пишем новый, а не копируем.
vtt2 = tmp_dir / "t40_plain.vtt"
io.open(vtt2, "w", encoding="utf-8", newline="\n").write(
    "WEBVTT\n\n00:00:01.000 --> 00:00:05.000\n"
    "Сегодня разберём, как устроен бюджетный процесс.\n")
res2 = media.submit_file(vtt2, "t40_plain.vtt",
                         {"video_kind": "transcript", "prefer_transcript": True,
                          "make_summary": True, "diarize_auto": True})
rec2 = res2["rec_id"]
MADE.append(rec2)
job2 = wait_job(res2["job_id"])
check("обработка прошла", job2.get("status"), "done")
st2 = (store.get(rec2) or {}).get("stages") or {}
check("документ пропущен", st2.get("summary"), "skip")
check("разметка пропущена", st2.get("diarize"), "skip")
check("задачи документа не было",
      [j for j in jobs.for_recording(rec2) if j.get("kind") == "summary"], [])

say("")
say("=== 7. Встреча: разметка ставится в очередь ===")
vtt3 = tmp_dir / "t40_meeting.vtt"
io.open(vtt3, "w", encoding="utf-8", newline="\n").write(
    "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nДобрый день, начнём.\n")
res3 = media.submit_file(vtt3, "t40_meeting.vtt",
                         {"video_kind": "meeting", "prefer_transcript": True,
                          "make_summary": False, "diarize_auto": True})
rec3 = res3["rec_id"]
MADE.append(rec3)
job3 = wait_job(res3["job_id"])
check("обработка прошла", job3.get("status"), "done")
st3 = (store.get(rec3) or {}).get("stages") or {}
# Разметка без звука не запустится (у записи только субтитры), но попытка
# постановки должна быть — тип этого требует. Проверяем, что этап не «skip».
check("разметку не пропустили по типу", st3.get("diarize") != "skip", True)

say("")
say("=== 8. Жанр документа выбирается по типу записи ===")
calls = []
real = minutes._run_engine


def fake(engine, prompt, text, timeout=None, role="strong", handle=None):
    calls.append(prompt)
    return ("НАЗВАНИЕ: Бюджетный процесс\nПАПКА: бюджетный процесс\n\n"
            "## О чём запись\n\nРазбирали устройство бюджета.\n\n"
            "## Планирование [00:00:05]\n\nПервый этап — потребность.\n")


minutes._run_engine = fake
try:
    out = minutes.generate_video_summary(rec)      # у записи тип «лекция»
finally:
    minutes._run_engine = real
check("документ собран", bool(out.get("markdown")), True)
check("жанр взят из записи", out.get("document"), "lecture")
check("модели ушёл промпт конспекта",
      "конспект" in (calls[0] if calls else ""), True)
check("конспект лёг своим файлом (13.09: у каждого документа свой)",
      (store.rec_dir(rec) / "conspect.md").exists(), True)

calls.clear()
minutes._run_engine = fake
try:
    out2 = minutes.generate_video_summary(rec, document="interview")
finally:
    minutes._run_engine = real
check("жанр можно задать явно", out2.get("document"), "interview")
check("модели ушёл промпт выжимки",
      "выжимк" in (calls[0] if calls else ""), True)

say("")
say("=== 9. Интерфейс: селектор типа и выбор хранения ===")
html = io.open(STATIC / "index.html", "r", encoding="utf-8").read()
videojs = io.open(STATIC / "video.js", "r", encoding="utf-8").read()
appjs = io.open(STATIC / "app.js", "r", encoding="utf-8").read()

check("селектор типа есть", 'id="v-kind"' in html, True)
for kind in ("meeting", "lecture", "interview", "transcript"):
    check("тип «%s» в списке" % kind, 'value="%s"' % kind in html, True)
check("выбор хранения есть", 'id="v-store"' in html, True)
for mode in ("none", "audio", "video"):
    check("вариант хранения «%s»" % mode, 'value="%s"' % mode in html, True)
check("старой галочки «сохранять видео» больше нет", 'id="v-keep"' in html, False)
check("подсказка по типу есть", 'id="v-kind-note"' in html, True)
check("тип уходит в задание", "video_kind: $('v-kind').value" in videojs, True)
check("хранение уходит в задание", "store_media: $('v-store').value" in videojs, True)
check("тип подставляется по источнику", "V_SRC_KIND" in videojs, True)
check("свой выбор типа запоминается", "kindTouched" in videojs, True)
check("в окне документа тип записи сопоставлен с видом документа",
      "KIND_DOC" in appjs and 'id="doc-video-kind"' in html, True)

say("")
say("=== 9б. Пути в карточке записи и «открыть папку» ===")
from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    # заведём запись с видео и заметкой, чтобы было что показывать
    pm = store.create(title="Проверка 40 — пути", mode="offline", source="file")
    pid = pm["id"]
    MADE.append(pid)
    folder = "t40-пути-" + pid
    d = store.assets_dir(pid, folder)
    d.mkdir(parents=True, exist_ok=True)
    io.open(d / "video.mp4", "wb").write(b"\0" * 512)
    io.open(d / "transcript.txt", "w", encoding="utf-8").write("текст")
    store.update(pid, {"assets_folder": folder, "video_path": str(d / "video.mp4")})

    got = cli.get("/api/recordings/%s" % pid).json()
    keys = [p.get("key") for p in (got.get("paths") or [])]
    say("   показано: %s" % keys)
    check("видео в списке", "video" in keys, True)
    check("текстовая копия в списке", "transcript" in keys, True)
    check("папка записи в списке", "assets" in keys, True)
    check("заметки нет — и не показана", "note" in keys, False)

    for p in (got.get("paths") or []):
        check("путь %s существует" % p["key"], Path(p["path"]).exists(), True)

    # чужое открыть нельзя: ключа нет в списке — маршрут отказывает
    bad = cli.post("/api/recordings/%s/reveal" % pid, headers=ORIGIN,
                   json={"what": "C:\\Windows"})
    check("чужой путь открыть нельзя", bad.status_code, 404)
    gone = cli.post("/api/recordings/%s/reveal" % pid, headers=ORIGIN,
                    json={"what": "note"})
    check("несуществующий файл открыть нельзя", gone.status_code, 404)

check("кнопка «Открыть папку» есть", "js-reveal" in appjs, True)
check("блок путей в разметке", 'id="paths-box"' in html, True)

say("")
say("=== 10. Уборка ===")
for rid in list(MADE):
    m = store.get(rid) or {}
    folder = str(m.get("assets_folder") or "")
    if folder:
        shutil.rmtree(store.assets_dir(rid, folder), ignore_errors=True)
    store.delete(rid)
check("тестовые записи убраны", [r for r in MADE if store.get(r) is not None], [])
for f in (vtt, vtt2, vtt3):
    try:
        f.unlink(missing_ok=True)
    except OSError:
        pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t40_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
