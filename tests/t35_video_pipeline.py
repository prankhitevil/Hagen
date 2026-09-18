# -*- coding: utf-8 -*-
"""Проверка 35: сквозной прогон раздела «Видео» без сети.

Через конвейер идут настоящие файлы: субтитры .vtt и английский wav. Проверяем,
что получается обычная запись с текстом, говорящими и правильными пометками, и
что тестовые записи после себя убираются.
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

from hagen import config, jobs, media, store  # noqa: E402

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


def wait_job(job_id, limit=600.0):
    """Дождаться задачи. Возвращает её итоговое состояние."""
    t0 = time.time()
    while time.time() - t0 < limit:
        job = jobs.get(job_id) if hasattr(jobs, "get") else None
        if job is None:
            for j in jobs.list_all(50):
                if j.get("id") == job_id:
                    job = j
                    break
        if job and job.get("status") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.5)
    return {"status": "timeout"}


VTT = """WEBVTT

00:00:01.000 --> 00:00:04.000
<v Иванов Иван>Добрый день, начнём с бюджета.

00:00:04.500 --> 00:00:08.000
<v Петрова Мария>Смету подготовлю к четвергу.

00:00:08.200 --> 00:00:11.000
<v Иванов Иван>Хорошо, тогда договорились.
"""

say("=== 1. Готовые субтитры вместо распознавания ===")
tmp_dir = PROJECT / "data" / "_uploads"
tmp_dir.mkdir(parents=True, exist_ok=True)
vtt = tmp_dir / "t35_meeting.vtt"
io.open(vtt, "w", encoding="utf-8", newline="\n").write(VTT)

res = media.submit_file(vtt, "t35_meeting.vtt",
                        {"make_summary": False, "diarize_auto": False,
                         "prefer_transcript": True, "keep_video": False})
rec_id = res["rec_id"]
MADE.append(rec_id)
job = wait_job(res["job_id"])
say("   задача: %s" % job.get("status"))
if job.get("status") != "done":
    say("   ошибка: %s" % (job.get("error") or "")[:300])
check("задача прошла", job.get("status"), "done")

segs = store.sorted_segments(rec_id)
meta = store.get(rec_id) or {}
check("реплик получено", len(segs), 3)
check("распознавание не запускалось", meta.get("transcript_source"), "subs")
check("имена говорящих взяты", sorted({s.get("speaker") for s in segs}),
      ["Иванов Иван", "Петрова Мария"])
check("имя не вклеено в текст", "Иванов" in (segs[0].get("text") or ""), False)
check("говорящие помечены готовыми", meta.get("diarize_status"), "subs")
check("этап текста отмечен", (meta.get("stages") or {}).get("text"), True)
check("раздел заметки — саммари", meta.get("doc_kind"), "summary")
say("   первая реплика: %r" % (segs[0].get("text") or "")[:60])

say("")
say("=== 2. Повторный прогон пропускает сделанное ===")
# Ровно то, ради чего пишется карта этапов: служба закрылась посреди работы,
# запустились снова — качать и распознавать заново не должны.
before = len(store.load_transcript(rec_id).get("segments") or [])
touched = []


def must_not_call(_handle):
    touched.append(1)
    raise AssertionError("источник вызван повторно, хотя этап уже сделан")


again = media.process(rec_id, must_not_call, {"make_summary": False,
                                              "diarize_auto": False}, None)
check("источник не трогали", touched, [])
check("реплики не переписаны",
      len(store.load_transcript(rec_id).get("segments") or []), before)
check("конвейер вернул результат", isinstance(again, dict), True)

say("")
say("=== 2б. Пропавший файл субтитров: понятная ошибка ===")
res2 = media.submit_file(tmp_dir / "нет-такого.vtt", "нет-такого.vtt",
                         {"make_summary": False, "diarize_auto": False})
MADE.append(res2["rec_id"])
job2 = wait_job(res2["job_id"])
check("задача честно упала", job2.get("status"), "error")
check("ошибка про файл, а не про звук",
      "не найден" in str(job2.get("error") or "").lower(), True)
say("   сообщение: %s" % str(job2.get("error") or "")[:120])

say("")
say("=== 3. Английский файл через конвейер ===")
wav = PROJECT / "tests" / "english.wav"
if not wav.exists():
    say("   пропущено: нет tests\\english.wav")
else:
    copy = tmp_dir / "t35_english.wav"
    shutil.copyfile(wav, copy)
    res3 = media.submit_file(copy, "t35_english.wav",
                             {"asr_lang": "en", "asr_files": "local",
                              "make_summary": False, "diarize_auto": False,
                              "prefer_transcript": False})
    rec3 = res3["rec_id"]
    MADE.append(rec3)
    t0 = time.time()
    job3 = wait_job(res3["job_id"])
    say("   задача: %s за %.1f c" % (job3.get("status"), time.time() - t0))
    if job3.get("status") != "done":
        say("   ошибка: %s" % (job3.get("error") or "")[:300])
    check("задача прошла", job3.get("status"), "done")
    segs3 = store.sorted_segments(rec3)
    meta3 = store.get(rec3) or {}
    text = " ".join(s.get("text") or "" for s in segs3)
    say("   распознано: %r" % text[:150])
    check("реплики есть", len(segs3) > 0, True)
    check("помечено как английское локальное", meta3.get("transcript_source"), "local_en")
    check("слова с отметками есть", bool((segs3[0].get("words") or [])), True)
    check("узнаны английские слова",
          any(w in text.lower() for w in ("budget", "morning", "thursday")), True)

say("")
say("=== 4. Только скачать: распознавания нет ===")
copy2 = tmp_dir / "t35_only.wav"
if wav.exists():
    shutil.copyfile(wav, copy2)
    res4 = media.submit_file(copy2, "t35_only.wav",
                             {"video_only": True, "keep_video": True,
                              "make_summary": False, "diarize_auto": False})
    rec4 = res4["rec_id"]
    MADE.append(rec4)
    job4 = wait_job(res4["job_id"])
    check("задача прошла", job4.get("status"), "done")
    meta4 = store.get(rec4) or {}
    check("текста нет", len(store.sorted_segments(rec4)), 0)
    check("этап текста пропущен", (meta4.get("stages") or {}).get("text"), "skip")
    check("файл сохранён рядом", bool(meta4.get("video_path")), True)
    kept = Path(str(meta4.get("video_path") or ""))
    check("и правда лежит на диске", kept.exists(), True)
    check("лежит вне сейфа",
          str(config.vault_root()).lower() in str(kept).lower(), False)

say("")
say("=== 5. Уборка ===")
for rid in MADE:
    m = store.get(rid) or {}
    folder = str(m.get("assets_folder") or "")
    if folder:
        shutil.rmtree(store.assets_dir(rid, folder), ignore_errors=True)
    store.delete(rid)
left = [r for r in MADE if store.get(r) is not None]
check("тестовые записи убраны", left, [])
for f in (vtt, tmp_dir / "t35_english.wav", tmp_dir / "t35_only.wav"):
    try:
        f.unlink(missing_ok=True)
    except OSError:
        pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t35_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
