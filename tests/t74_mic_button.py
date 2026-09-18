# -*- coding: utf-8 -*-
"""Проверка 74: выключатель системного микрофона (решение 17.09).

«Крупная кнопка микрофона справа сверху, чтобы включать при необходимости» —
то же, что клавиша на ноутбуке, но видно состояние и можно нажать мышью.
Гасится микрофон Windows целиком: значит, и в записи будет тишина.

Настоящий микрофон машины проверка НЕ трогает: устройство подменяется
(человек может быть на звонке, а выключенный микрофон посреди совещания —
худшее, что может сделать проверка).

Что проверяем:
  1. состояние читается и переключается;
  2. `toggle` переключает в обе стороны;
  3. поломка COM не роняет программу, а честно отвечает «нечем управлять»;
  4. точки службы GET/POST и событие в окно;
  5. на странице есть кнопка, цвета и понятная подсказка;
  6. состояние обновляется само: событие, опрос, смена устройств.

Настройки и база голосов — временные (isolate).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t74_mic_button.py
"""
import io
import sys
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


from hagen.platform.windows import micmute  # noqa: E402


class FakeVolume:
    """Подставной регулятор: помнит, выключен ли микрофон."""

    def __init__(self, muted=False):
        self.muted = bool(muted)
        self.calls = 0

    def GetMute(self):
        return 1 if self.muted else 0

    def SetMute(self, value, _guid):
        self.calls += 1
        self.muted = bool(value)


FAKE = FakeVolume()
REAL_ENDPOINT = micmute._endpoint
REAL_THREAD = micmute._in_audio_thread

micmute._endpoint = lambda: (FAKE, "Подставной микрофон")
micmute._in_audio_thread = lambda fn, *args: fn(*args)   # без звукового потока

try:
    say("=== 1. Чтение и переключение ===")
    check("сначала включён", micmute.state() == {"available": True, "muted": False,
                                                 "device": "Подставной микрофон"},
          micmute.state())
    out = micmute.set_muted(True)
    check("выключился", out["muted"] is True and FAKE.muted is True, out)
    check("имя устройства возвращается", out["device"] == "Подставной микрофон", out)
    check("включился обратно", micmute.set_muted(False)["muted"] is False, FAKE.muted)

    say("")
    say("=== 2. Переключатель ===")
    check("toggle выключает", micmute.toggle()["muted"] is True, FAKE.muted)
    check("toggle включает", micmute.toggle()["muted"] is False, FAKE.muted)

    say("")
    say("=== 3. Когда управлять нечем ===")
    micmute._endpoint = lambda: (_ for _ in ()).throw(OSError("нет такого устройства"))
    bad = micmute.state()
    check("состояние: честное «нечем»", bad["available"] is False and "why" in bad, bad)
    check("переключение не падает", micmute.set_muted(True)["available"] is False)
    check("toggle не падает", micmute.toggle()["available"] is False)
    micmute._endpoint = lambda: (FAKE, "Подставной микрофон")

    say("")
    say("=== 4. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.get("/api/mic", headers=ORIGIN)
        check("GET отдаёт состояние", r.status_code == 200 and r.json()["muted"] is False, r.text)
        r2 = cli.post("/api/mic", json={"muted": True}, headers=ORIGIN)
        check("POST выключает", r2.status_code == 200 and r2.json()["muted"] is True, r2.text)
        r3 = cli.post("/api/mic", json={"toggle": True}, headers=ORIGIN)
        check("POST переключает обратно", r3.json()["muted"] is False, r3.text)
        r4 = cli.post("/api/mic", json={}, headers=ORIGIN)
        check("пустое тело — тоже переключение", r4.json()["muted"] is True, r4.text)
        micmute.set_muted(False)
    # Маршруты микрофона переехали в роутер.
    api = io.open(PROJECT / "hagen" / "api" / "tools.py", encoding="utf-8").read()
    check("о переключении узнают все окна", '{"type": "mic", "mic": out}' in api)

    say("")
    say("=== 5. Страница ===")
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    check("кнопка есть", 'id="btn-micmute"' in html)
    check("кнопка со значком микрофона", "ic-mic" in html)
    # Этап 6, п. 6.9: выключатель переехал в шапку боковой панели. Круглая
    # кнопка поверх содержимого закрывала таймер записи, а заливкой --rec на
    # экране помечены только «Старт» и «Стоп» — выключатель красный значком.
    head = html.split('class="side-head"', 1)[1].split('class="mode-row"', 1)[0]
    check("кнопка в шапке боковой панели", 'id="btn-micmute"' in head)
    check("включён — красный значок, без заливки", ".mic-btn{position:relative;color:var(--rec-ink)" in css)
    check("выключен — серый с перечёркиванием",
          ".mic-btn.off{color:var(--muted)" in css and ".mic-btn.off .mic-slash" in css)
    check("подсказка объясняет последствие",
          "вас не слышат и собеседники" in js and "Hagen слушает" in js)
    check("нажатие переключает", "$('btn-micmute').onclick = toggleMic" in js)

    say("")
    say("=== 6. Кнопка не врёт ===")
    check("событие от службы обновляет кнопку", "case 'mic':" in js)
    check("состояние спрашивается при открытии окна", "loadMic();" in js)
    check("и опрашивается дальше", js.count("loadMic()") >= 3, js.count("loadMic()"))
    check("смена устройств тоже обновляет", "devicechange" in js and "loadMic()" in js)
    check("пока управлять нечем — кнопка спрятана", "m.available === false" in js)

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))
finally:
    micmute._endpoint = REAL_ENDPOINT
    micmute._in_audio_thread = REAL_THREAD

say("")
say("Всего замечаний: %d" % len(FAIL))
for name in FAIL:
    say("   — " + name)
io.open(PROJECT / "tests" / "t74_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
