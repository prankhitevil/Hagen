# -*- coding: utf-8 -*-
"""Проверка 31: «Старт» при недоступном звуке — страница это переживает.

Человек нажал «Старт», а звук не открылся. Служба сама отменяет запись и
убирает пустую заметку. Страница должна это пережить:
  1. не мигать — экран записи остаётся на месте;
  2. не оставить в списке пустую запись;
  3. вернуть «Новую запись» и «Старт» в рабочее состояние, не зависнуть в
     режиме записи;
  4. сказать ОДНО понятное сообщение, а не стопку;
  5. обойтись без ошибок JavaScript.

Звук здесь — заглушка слоя платформы, и у неё «сломаны» все устройства: отказ
честный, без живого железа и без настоящего микрофона. Служба своя, на
свободном порту: запущенную рядом программу проверка не трогает. Настройки и
база голосов — временные. Страница — в настоящем WebView2, как у программы.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t31_ui_states.py
"""
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings(record_far=True, mode="online", mic_pill=False)

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


import uvicorn  # noqa: E402
import webview  # noqa: E402

from hagen import platform, server, store  # noqa: E402
from hagen.platform import fake  # noqa: E402

# Все устройства «сломаны»: ни микрофон, ни петля, ни запасной путь через
# ffmpeg не откроются — ровно тот отказ, который видит человек без звука.
platform.use("fake")
fake.reset()
fake.audio.BROKEN.update(d["name"] for d in fake.audio.MICS)
fake.audio.BROKEN.update(d["name"] for d in fake.audio.LOOPBACKS)

# Горячую клавишу диктовки служба занимает при старте; рабочее сочетание
# отбирать незачем, тем более что настоящий «Hagen» может быть запущен рядом.
server._start_dictation = lambda: None

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
server.set_actual_port(PORT)
srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT,
                                    log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            break
    time.sleep(0.1)

STATE_JS = """
JSON.stringify({
  recView: !document.getElementById('rec-view').classList.contains('hidden'),
  emptyState: !document.getElementById('empty-state').classList.contains('hidden'),
  btnNew: !document.getElementById('btn-new').disabled,
  btnStart: !document.getElementById('btn-start').disabled,
  recording: !!S.recordingId,
  starting: !!S.starting,
  errNotices: Array.from(document.querySelectorAll('#notices .notice.err')).map(n => n.textContent),
  recCount: document.querySelectorAll('#rec-list .rec-item').length
})
"""

result = {}
before_ids = {m["id"] for m in store.list_all()}
window = webview.create_window("t31", "http://127.0.0.1:%d/" % PORT, hidden=True,
                               width=1200, height=800)


def state():
    try:
        return json.loads(window.evaluate_js(STATE_JS))
    except Exception as err:
        return {"__error": str(err)[:200]}


def scenario():
    ready = False
    for _ in range(150):
        try:
            ready = window.evaluate_js(
                "typeof S !== 'undefined' && typeof startRecording === 'function'"
                " && !!document.getElementById('btn-start')")
        except Exception:
            ready = False
        if ready:
            break
        time.sleep(0.2)
    result["ready"] = bool(ready)
    try:
        if not ready:
            return
        window.evaluate_js("window.__t31errs = [];"
                           "window.addEventListener('error', (e) => window.__t31errs.push(e.message));")
        time.sleep(1.5)                      # список записей и настройки приходят следом
        result["before"] = state()

        # Как человек: «Новая запись», затем «Старт».
        window.evaluate_js("document.getElementById('btn-new').click()")
        time.sleep(0.5)
        window.evaluate_js("document.getElementById('btn-start').click()")

        # Служба открывает устройства в фоне, отказ приходит событием. Ждём,
        # пока страница выйдет из режима записи; сообщение ловим, пока оно
        # на экране (ошибка живёт восемь секунд).
        seen_err: list[str] = []
        t0 = time.time()
        while time.time() - t0 < 40:
            st = state()
            for n in st.get("errNotices") or []:
                if n not in seen_err:
                    seen_err.append(n)
            if seen_err and not st.get("recording") and not st.get("starting"):
                break
            time.sleep(0.3)
        result["seconds"] = round(time.time() - t0, 1)
        result["errors_seen"] = seen_err
        time.sleep(1.0)                      # «recordings» — следом за «capture»
        result["after"] = state()
        result["js_errors"] = window.evaluate_js("JSON.stringify(window.__t31errs)")
    except Exception as err:
        result["fatal"] = str(err)[:300]
    finally:
        window.destroy()


try:
    webview.start(scenario, gui="edgechromium", private_mode=True)
finally:
    srv.should_exit = True
    th.join(timeout=20)
    platform.use(None)

before = result.get("before") or {}
after = result.get("after") or {}
leftover = [m for m in store.list_all() if m["id"] not in before_ids]

say("=== «Старт» при недоступном звуке ===")
check("страница загрузилась", result.get("ready"), result.get("fatal", ""))
check("до старта кнопки рабочие", before.get("btnNew") and before.get("btnStart"), before)
check("служба ответила отказом, страница вышла из режима записи",
      not after.get("recording") and not after.get("starting"),
      "%s с, %s" % (result.get("seconds"), after))
check("экран записи не исчез и не мигнул пустым",
      after.get("recView") is True or after.get("emptyState") is True, after)
check("«Новая запись» снова рабочая", after.get("btnNew"), after)
check("«Старт» снова рабочий", after.get("btnStart"), after)
check("пустой записи в списке не осталось",
      after.get("recCount") == before.get("recCount"),
      "было %s, стало %s" % (before.get("recCount"), after.get("recCount")))
check("и в данных службы тоже", not leftover, [m["id"] for m in leftover])
errs = result.get("errors_seen") or []
check("сообщение об ошибке одно", len(errs) == 1, errs)
check("и оно про звук — то, что служба сказала",
      errs and errs[0] == server.NO_AUDIO_HINT, errs[:1])
check("ошибок JavaScript нет", result.get("js_errors") in ("[]", None), result.get("js_errors"))

# Если проверка упала посреди отказа, пустая запись могла остаться — убираем.
for m in leftover:
    try:
        store.delete(m["id"])
    except Exception:
        pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t31_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
