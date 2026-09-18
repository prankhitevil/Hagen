# -*- coding: utf-8 -*-
"""Проверка 8: сквозной путь файла через HTTP API службы.

Готовый видеофайл → распознавание → разметка говорящих → имя говорящему и его
голос в базу → протокол → заметка в Obsidian → удаление. Всё теми же
запросами, что делает страница, и в том же порядке.

Живой путь — запись с микрофона — проверяет «ритуал» (t38). Здесь путь файла:
перетащили видео, получили заметку.

Чего проверка НЕ трогает:
  * запущенную рядом программу — служба своя, внутри процесса проверки;
  * настоящие настройки и базу голосов — они временные;
  * настоящий сейф Obsidian — заметка пишется во временную папку;
  * облако и Claude — вместо модели подмена: проверяется путь промпта, файлов
    и заметки, а не качество протокола. Настоящий вызов модели — в t38.

Нужны точная модель распознавания и модель разметки говорящих: без разметки
проверка честно говорит, что этот шаг пропущен.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t8_e2e.py
"""
import io
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

def _hf_token() -> str:
    """Токен Hugging Face — единственное, что берётся из настоящих настроек.

    Без него разметка говорящих не запускается вовсе, а проверять путь файла без
    разметки — проверять половину. Ни ключей сервисов, ни путей, ни устройств
    отсюда не читается; токен никуда не печатается.
    """
    try:
        import json

        data = json.loads((PROJECT / "settings.json").read_text(encoding="utf-8"))
        return str(data.get("hf_token") or "").strip()
    except Exception:
        return ""


VAULT = Path(tempfile.mkdtemp(prefix="t8_vault_"))
isolate.voices()
isolate.settings(vault_path=str(VAULT), vault_subfolder="Meetings", vault_confirmed=True,
                 minutes_engine="claude_cli", owner_name="Я", smart_folder_name=False,
                 hf_token=_hf_token())

LINES = []
FAIL = []


def say(msg=""):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name
        + (": " + str(detail)[:300] if detail != "" else ""))


from fastapi.testclient import TestClient  # noqa: E402

from hagen import minutes, server, store, voices  # noqa: E402

PROTOCOL = "ПРОТОКОЛ-ПРОВЕРКИ-8"


def fake_engine(engine, prompt, text, timeout=None, role="strong", handle=None):
    """Подмена модели: ответ как у настоящего Claude, по форме протокола."""
    return ("# Протокол совещания\n\n## Участники\n\n- Иван Петров\n\n"
            "## Принятые решения\n\n%s\n" % PROTOCOL)


real_engine = minutes._run_engine
minutes._run_engine = fake_engine
server._start_dictation = lambda: None

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
VIDEO = PROJECT / "tests" / "meeting_video.mp4"


def wait_job(cli, job_id, label, limit=1800):
    """Дождаться задачи. Возвращает её последнее состояние."""
    t0 = time.time()
    last = ""
    job = None
    while time.time() - t0 < limit:
        jobs = cli.get("/api/jobs").json()
        job = next((j for j in jobs if j.get("id") == job_id), None)
        if job is None:
            say("   %s: задача исчезла" % label)
            return None
        line = "%s %3d%%" % (job["status"], round((job.get("progress") or 0) * 100))
        if line != last:
            say("   %-9s %s %s" % (label, line, (job.get("note") or "")[:60]))
            last = line
        if job["status"] in ("done", "error", "cancelled"):
            say("   %s: %s за %.1f с" % (label, job["status"], time.time() - t0))
            if job.get("error"):
                say("   ошибка: %s" % str(job["error"])[:200])
            return job
        time.sleep(1.0)
    say("   %s: не дождались" % label)
    return job


rec_id = None
try:
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        say("=== 0. Служба ===")
        st = cli.get("/api/state").json()
        caps = st.get("capabilities") or {}
        before = {m["id"] for m in cli.get("/api/recordings").json()}
        say("   разметка говорящих: %s (%s)" % (caps.get("diarize"), caps.get("diarize_note")))

        say("")
        say("=== 1. Готовый видеофайл, как перетаскиванием ===")
        with open(VIDEO, "rb") as fh:
            r = cli.post("/api/media/upload", headers=ORIGIN,
                         files={"file": (VIDEO.name, fh, "video/mp4")})
        check("файл принят", r.status_code == 200, r.text[:200])
        up = r.json() if r.status_code == 200 else {}
        rec_id = up.get("rec_id")
        check("заведена запись и задача", bool(rec_id and up.get("job_id")), up)
        job = wait_job(cli, up.get("job_id"), "файл") if rec_id else None
        check("файл распознан", bool(job) and job["status"] == "done", (job or {}).get("error", ""))

        data = cli.get("/api/recordings/%s" % rec_id).json() if rec_id else {}
        segs = data.get("segments") or []
        words = sum(len((s.get("text") or "").split()) for s in segs)
        check("в стенограмме есть речь", len(segs) > 0 and words >= 10,
              "%d реплик, %d слов" % (len(segs), words))

        say("")
        say("=== 2. Разметка говорящих ===")
        if not caps.get("diarize"):
            say("   ОГОВОРКА: разметка недоступна (%s) — шаги 2 и 3 не проверены"
                % caps.get("diarize_note"))
        else:
            # После распознавания файла разметка запускается сама («авторазметка»).
            # Тогда ждём ту, что уже идёт, как ждёт и страница, — вторую не заводим.
            running = [j for j in cli.get("/api/jobs").json()
                       if j.get("rec_id") == rec_id and j.get("kind") == "diarize"
                       and j.get("status") in ("queued", "running", "done")]
            if running:
                job_id = running[0]["id"]
                say("   разметка уже запущена службой сама")
            else:
                r = cli.post("/api/recordings/%s/diarize" % rec_id, headers=ORIGIN,
                             json={"min_speakers": 2, "max_speakers": 4})
                job_id = r.json().get("job_id") if r.status_code == 200 else None
            check("разметка идёт", job_id is not None)
            job = wait_job(cli, job_id, "разметка") if job_id else None
            check("разметка закончилась", bool(job) and job["status"] == "done",
                  (job or {}).get("error", ""))
            data = cli.get("/api/recordings/%s" % rec_id).json()
            segs = data.get("segments") or []
            keys = sorted({s.get("speaker_key") for s in segs if s.get("speaker_key")})
            check("говорящих больше одного", len(keys) >= 2, keys)

            say("")
            say("=== 3. Имя говорящему и его голос в базу ===")
            key = next((k for k in keys if k not in ("me", "far", "file")), None)
            check("есть кому дать имя", key is not None, keys)
            if key:
                r = cli.post("/api/recordings/%s/speaker" % rec_id, headers=ORIGIN,
                             json={"speaker_key": key, "name": "Иван Петров",
                                   "apply_all": True, "remember": True})
                check("имя принято", r.status_code == 200, r.text[:200])
                data = cli.get("/api/recordings/%s" % rec_id).json()
                named = [s for s in data.get("segments") or [] if s.get("speaker") == "Иван Петров"]
                check("реплики подписаны именем", len(named) > 0, len(named))
                people = [p.get("name") for p in voices.list_people()]
                check("голос запомнен — во временной базе", "Иван Петров" in people, people)

        say("")
        say("=== 4. Протокол ===")
        r = cli.post("/api/recordings/%s/minutes" % rec_id, headers=ORIGIN,
                     json={"template": "protocol"})
        check("протокол принят в работу", r.status_code == 200, r.text[:200])
        job = wait_job(cli, r.json().get("job_id"), "протокол", limit=300) if r.status_code == 200 else None
        check("протокол собран", bool(job) and job["status"] == "done", (job or {}).get("error", ""))
        md = cli.get("/api/recordings/%s/minutes" % rec_id).json().get("markdown") or ""
        check("в протоколе ответ модели", PROTOCOL in md, md[:120])

        say("")
        say("=== 5. Заметка в Obsidian ===")
        r = cli.post("/api/recordings/%s/save" % rec_id, headers=ORIGIN)
        check("заметка сохранена", r.status_code == 200, r.text[:200])
        path = Path((r.json() if r.status_code == 200 else {}).get("path") or "")
        check("заметка — во временном сейфе, а не в настоящем",
              path.exists() and VAULT in path.parents, str(path))
        note = io.open(path, encoding="utf-8").read() if path.exists() else ""
        check("в заметке протокол", PROTOCOL in note)
        check("в заметке стенограмма", any((s.get("text") or "")[:20] in note
                                          for s in segs if (s.get("text") or "").strip()))

        say("")
        say("=== 6. Удаление ===")
        r = cli.delete("/api/recordings/%s?scope=all" % rec_id, headers=ORIGIN)
        check("запись удалена", r.status_code == 200, r.text[:200])
        check("из списка ушла", rec_id not in {m["id"] for m in cli.get("/api/recordings").json()})
        check("заметка убрана из сейфа", not path.exists(), str(path))
        after = {m["id"] for m in cli.get("/api/recordings").json()}
        check("чужие записи не тронуты", before <= after, sorted(before - after)[:3])
        rec_id = None
except Exception:
    import traceback

    check("сквозной путь без сбоев", False, traceback.format_exc()[-600:])
finally:
    minutes._run_engine = real_engine
    if rec_id:
        # Проверка упала на полпути — запись за собой всё равно убираем.
        try:
            store.delete(rec_id)
        except Exception:
            pass
    shutil.rmtree(VAULT, ignore_errors=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t8_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
