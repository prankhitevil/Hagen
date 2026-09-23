# -*- coding: utf-8 -*-
"""Проверка 38: обязательный ритуал — запись с микрофона, протокол, стенограмма.

Главная работа приложения — запись с микрофона. Проверять её после каждой правки
нужно целиком: 30 секунд с микрофона → «Сделать протокол» → «Сохранить
стенограмму». Служба поднимается тестовым клиентом внутри этого же процесса —
так проверка не зависит от антивируса, который не даёт порождать фоновые
процессы.

Протокол собирается ПО-НАСТОЯЩЕМУ, через Claude CLI: именно здесь ловится
поломка запуска CLI, из-за которой до модели доходила только первая строка
инструкции.

Запускать из корня проекта:  .venv\\Scripts\\python.exe tests\\t38_ritual.py
После себя тестовую запись убирает.
"""
import io
import sys
import time
import wave
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, expect as check  # noqa: E402

isolate.voices()

SECONDS = 30


from fastapi.testclient import TestClient  # noqa: E402

from hagen import jobs, store  # noqa: E402
from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
rec_id = ""


def wait_job(cli, job_id, limit=900.0):
    """Дождаться задачи. Состояние берём прямо из очереди — она в этом процессе."""
    t0 = time.time()
    last = ""
    while time.time() - t0 < limit:
        job = jobs.get(job_id) if hasattr(jobs, "get") else None
        if job is None:
            for j in jobs.list_all(50):
                if j.get("id") == job_id:
                    job = j
                    break
        if job:
            note = str(job.get("note") or job.get("status") or "")
            if note != last:
                say("      %3.0f c  %s" % (time.time() - t0, note[:90]))
                last = note
            if job.get("status") in ("done", "error", "cancelled"):
                return job
        time.sleep(0.5)
    return {"status": "timeout"}


with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    say("=== 1. Микрофон на месте ===")
    dev = cli.get("/api/devices").json()
    mics = dev.get("mics") or dev.get("inputs") or []
    say("   микрофонов найдено: %d" % len(mics))
    if mics:
        say("   выбран: %s" % (mics[0].get("name") if isinstance(mics[0], dict) else mics[0]))
    check("микрофон есть", len(mics) > 0, True)

    say("")
    say("=== 2. Запись %d секунд ===" % SECONDS)
    started = cli.post("/api/recordings", headers=ORIGIN,
                       json={"title": "Проверка 38 — ритуал", "mode": "offline"})
    check("запись началась", started.status_code, 200)
    if started.status_code == 200:
        rec_id = str(((started.json().get("meta") or {}).get("id")) or "")
    say("   запись: %s" % rec_id)

    t0 = time.time()
    while time.time() - t0 < SECONDS:
        time.sleep(1.0)
        if int(time.time() - t0) % 10 == 0:
            say("      идёт %2.0f с" % (time.time() - t0))

    stopped = cli.post("/api/recordings/%s/stop" % rec_id, headers=ORIGIN, json={})
    check("остановилась", stopped.status_code, 200)

    say("")
    say("=== 3. Расшифровка ===")
    deadline = time.time() + 300
    stopped_at = time.time()
    segs = []
    while time.time() < deadline:
        meta = store.get(rec_id) or {}
        segs = store.sorted_segments(rec_id)
        if meta.get("status") in ("ready", "done", "recorded") and segs:
            break
        # Живая запись дописывает последнюю фразу ещё до ответа на «Стоп»:
        # если после остановки реплик нет и через 20 с, их не будет вовсе —
        # в комнате было тихо. Раньше тут ждали все 300 с.
        if meta.get("status") == "recorded" and time.time() - stopped_at > 20:
            break
        if meta.get("status") == "error":
            break
        time.sleep(1.0)
    meta = store.get(rec_id) or {}
    say("   состояние: %s" % meta.get("status"))
    say("   длительность: %s с" % meta.get("duration_s"))
    say("   реплик: %d" % len(segs))
    for s in segs[:3]:
        say("      | %s: %s" % (s.get("speaker") or "?", (s.get("text") or "")[:70]))
    check("запись сохранена", bool(meta), True)
    check("длительность похожа на 30 с",
          20 <= float(meta.get("duration_s") or 0) <= 45, True)

    # Звук проверяем по самой дорожке, а не по тексту: в пустой комнате речи
    # нет, и распознавать нечего — это не поломка. Проверка «микрофон пишет»
    # обязана отличаться от проверки «речь распознана».
    track = store.paths(rec_id)["mic"]
    peak = 0.0
    if track.exists():
        with wave.open(str(track), "rb") as wf:
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
        pcm = pcm.astype(np.float32) / 32768.0
        peak = float(np.max(np.abs(pcm))) if pcm.size else 0.0
        say("   дорожка: %.1f МБ, пик %.3f" % (track.stat().st_size / 1048576, peak))
    check("дорожка записана", track.exists(), True)
    check("микрофон слышит звук", peak > 0.002, True)

    spoken = len(segs) > 0
    if spoken:
        check("речь распознана", True, True)
    else:
        # Чтобы два оставшихся шага ритуала всё-таки проверились, подставляем
        # известный текст. В отчёте это сказано прямо — выдавать за
        # распознавание нельзя.
        say("   ВНИМАНИЕ: речи в комнате не было, распознавать нечего.")
        say("   Подставляю известный текст, чтобы проверить протокол и выгрузку.")
        store.replace_segments(rec_id, [
            {"id": "r1", "track": "mic", "start": 0.0, "end": 6.0, "speaker": "Я",
             "text": "Проверяю диктофон. Смету по бюджету подготовлю к четвергу."},
            {"id": "r2", "track": "mic", "start": 6.0, "end": 12.0, "speaker": "Я",
             "text": "Тендер по закупкам переносим на май, ответственный — я."},
        ])
        segs = store.sorted_segments(rec_id)

    say("")
    say("=== 4. «Сделать протокол» — настоящий вызов модели ===")
    if not segs:
        say("   пропущено: текста нет, протокол собирать не из чего")
        FAIL.append("протокол не проверен: нет текста")
    else:
        made = cli.post("/api/recordings/%s/minutes" % rec_id, headers=ORIGIN,
                        json={"template": "protocol"})
        check("задача принята", made.status_code, 200)
        job = wait_job(cli, made.json().get("job_id"))
        say("   итог: %s" % job.get("status"))
        if job.get("error"):
            say("   ошибка: %s" % str(job.get("error"))[:300])
        check("протокол собран", job.get("status"), "done")

        got = cli.get("/api/recordings/%s/minutes" % rec_id)
        check("протокол отдаётся", got.status_code, 200)
        md = got.json().get("markdown") if got.status_code == 200 else ""
        say("   первая строка: %r" % (md.splitlines() or [""])[0])
        # Разделы заданы инструкцией. Если до модели дошла только первая строка
        # промпта — их не будет: ровно так ломался запуск через батник.
        check("заголовок как в инструкции",
              md.strip().startswith("# Протокол совещания"), True)
        for head in ("## Участники", "## Обсуждённые вопросы", "## Принятые решения",
                     "## Задачи", "## Открытые вопросы"):
            check("есть раздел %s" % head, head in md, True)
        check("таблица задач на месте", "| Задача |" in md, True)

    say("")
    say("=== 5. «Сохранить заметку» ===")
    saved = cli.post("/api/recordings/%s/save" % rec_id, headers=ORIGIN, json={})
    check("сохранение прошло", saved.status_code, 200)
    if saved.status_code == 200:
        path = Path(str(saved.json().get("path") or ""))
        say("   заметка: %s" % path)
        check("файл заметки есть", path.exists(), True)
        if path.exists():
            text = io.open(path, "r", encoding="utf-8").read()
            check("в заметке есть стенограмма", "\n# Стенограмма\n" in text, True)
            check("в заметке есть протокол", "\n# Протокол\n" in text, True)
            prot = text.split("\n# Протокол\n", 1)[1].split("\n# Стенограмма\n", 1)[0]
            check("разделы протокола — подразделы ## внутри «# Протокол» (формат 15.09)",
                  "\n## Обсуждённые вопросы" in prot and "\n## Задачи" in prot, True)
            check("в протоколе нет названия, даты и участников — они в свойствах",
                  "Протокол совещания" not in prot and "## Участники" not in prot
                  and not prot.lstrip().startswith("Дата"), True)
            check("«О записи» первым разделом", text.index("\n# О записи\n") < text.index("\n# Протокол\n"), True)

say("")
say("=== 6. Уборка ===")
if rec_id:
    m = store.get(rec_id) or {}
    vault = str(m.get("vault_path") or "")
    if vault:
        try:
            Path(vault).unlink(missing_ok=True)
            say("   заметка убрана из хранилища")
        except OSError as err:
            say("   заметку убрать не вышло: %s" % err)
    store.delete(rec_id)
    check("тестовая запись убрана", store.get(rec_id), None)

say("")
if not spoken:
    say("ОГОВОРКА: речь с микрофона не звучала — шаг «распознано» этим прогоном")
    say("не проверен. Запись, протокол и выгрузка проверены полностью.")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t38_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
