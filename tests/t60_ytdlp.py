# -*- coding: utf-8 -*-
"""Проверка 60: «Обновить yt-dlp» в настройках (16.09).

yt-dlp подключён библиотекой и едет вместе с переносимой папкой, поэтому со
временем устаревает: площадки меняют выдачу, и ссылки перестают открываться.
Кнопка в «Настройки → Тонкие настройки» ставит свежий, не заставляя лезть
в командную строку.

Что проверяем:
  1. версия читается;
  2. pip зовётся библиотекой, в своём процессе (Kaspersky не даёт порождать
     фоновые), старый yt_dlp выбрасывается из памяти, лишние лаунчеры .exe из
     .venv\\Scripts убираются, python.exe остаётся;
  3. неудача объясняется по-русски и говорит про интернет и корпоративную сеть;
  4. точки службы: версия, задача обновления, ответ об уже идущем обновлении;
  5. кнопка и подпись на вкладке «Тонкие настройки» подключены.

Настоящая установка из интернета не запускается: pip подменён.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t60_ytdlp.py
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

LINES = []
FAIL = []


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


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


import pip._internal.cli.main as pip_cli  # noqa: E402

from hagen import fetch  # noqa: E402

SCRIPTS = PROJECT / ".venv" / "Scripts"
STRAY = SCRIPTS / "t60-проверка.exe"
real_pip_main = pip_cli.main

say("=== 1. Версия ===")
ver = fetch.version()
check("версия yt-dlp читается", bool(ver) and ver[0].isdigit(), ver)

say("")
say("=== 2. Обновление: pip библиотекой, чистка, сброс модуля ===")
calls = []


def fake_pip(args):
    calls.append(list(args))
    print("Successfully installed yt-dlp")
    return 0


try:
    import yt_dlp  # noqa: F401  кладём модуль в память, чтобы проверить сброс

    was_module = sys.modules.get("yt_dlp")
    STRAY.write_bytes(b"MZ")          # лишний лаунчер, как после настоящего pip
    pip_cli.main = fake_pip
    notes = []
    res = fetch.update(note=notes.append)
    check("pip вызван библиотекой, без отдельного процесса", len(calls) == 1, calls)
    check("команда pip — обновление yt-dlp",
          calls and calls[0][:2] == ["install", "--upgrade"] and calls[0][-1] == "yt-dlp", calls)
    check("в ответе прежняя и новая версия", res.get("before") == ver and "after" in res, res)
    check("ход работы рассказывается словами", any("обновляю" in n for n in notes), notes)
    # Старый модуль выброшен из памяти, и версия прочитана уже заново: иначе
    # ссылки до перезапуска программы шли бы через прежний yt-dlp.
    check("yt_dlp загружен заново, а не остался прежним",
          sys.modules.get("yt_dlp") is not None and sys.modules.get("yt_dlp") is not was_module)
    check("лишний лаунчер .exe убран", not STRAY.exists())
    check("python.exe на месте", (SCRIPTS / "python.exe").exists())

    say("")
    say("=== 3. Неудача объясняется ===")

    def broken_pip(args):
        print("ERROR: Could not find a version that satisfies the requirement yt-dlp")
        return 1

    pip_cli.main = broken_pip
    try:
        fetch.update()
        check("ошибка поймана", False, "обновление не упало, хотя pip вернул 1")
    except fetch.FetchError as err:
        text = str(err)
        check("сказано про интернет и корпоративную сеть",
              "интернет" in text and "сеть" in text, text[:200])
        check("видно, что ответил pip", "Could not find a version" in text, text[:200])

    say("")
    say("=== 4. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import jobs, server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.get("/api/ytdlp")
        check("служба отдаёт версию", r.status_code == 200 and bool(r.json().get("version")), r.text[:120])
        pip_cli.main = fake_pip
        calls.clear()
        r = cli.post("/api/ytdlp/update", headers=ORIGIN)
        job_id = r.json().get("job_id")
        check("обновление поставлено задачей", r.status_code == 200 and bool(job_id), r.text[:200])
        job = {"status": "timeout"}
        t0 = time.time()
        while time.time() - t0 < 60:
            job = jobs.get(job_id) or job
            if job.get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(0.1)
        check("задача прошла", job.get("status") == "done", job.get("error"))
        check("задача назвалась по-человечески", "yt-dlp" in (job.get("title") or ""), job.get("title"))
        check("pip вызван из задачи", len(calls) == 1, calls)
finally:
    pip_cli.main = real_pip_main
    if STRAY.exists():
        STRAY.unlink()

say("")
say("=== 5. Кнопка в настройках ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
pane = html[html.find('id="t-advanced"'):]
check("кнопка «Обновить yt-dlp» на вкладке «Тонкие настройки»",
      'id="btn-ytdlp"' in pane and "Обновить yt-dlp" in pane)
check("рядом место для версии", 'id="ytdlp-state"' in pane)
check("объяснено, когда нажимать", "перестали открываться" in pane)
check("страница спрашивает версию и шлёт обновление",
      "'/api/ytdlp'" in js and "/api/ytdlp/update" in js)
check("кнопка подключена и блокируется на время работы",
      "$('btn-ytdlp').onclick = updateYtdlp" in js and "btn.disabled = true" in js)
check("после задачи кнопка возвращается", "j.kind === 'ytdlp'" in js)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t60_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
