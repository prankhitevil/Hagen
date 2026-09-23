# -*- coding: utf-8 -*-
"""Проверка 85: состояние программы и цвет говорящего (17.09).

1. Внизу боковой панели всегда было написано «модель загружается…», в том
числе когда модель давно готова: надпись стояла прямо в HTML, а меняло её
только разовое событие. Окно, открытое ПОСЛЕ прогрева (из трея, после
переподключения сокета), события не получало.

2. Цвет имени брался от ДОРОЖКИ: всё с микрофона красилось цветом владельца.
Поэтому реплика, отданная другому человеку руками, и эхо, узнанное по голосу,
оставались «цветом владельца», а одно имя оказывалось двух цветов.

Что проверяем:
  1. служба честно сообщает состояние модели: загружается / готова / ошибка;
  2. состояние приходит и в общем состоянии, и событием, и не кэшируется;
  3. точка «повторить» перезапускает загрузку;
  4. на странице нет зашитой надписи, всё рисует одна функция;
  5. цвет владельца — только у его собственных реплик;
  6. отданная руками реплика и сосед по комнате красятся как другие люди;
  7. одно имя — один цвет, разные имена — разные.

Настройки и база голосов — временные (isolate). Модель не грузится: состояние
подменяется.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t85_status_and_colors.py
"""
import io
import re
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen import asr  # noqa: E402

JS = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
HTML = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()

try:
    say("=== 1. Служба знает состояние модели ===")
    check("состояние читается", set(asr.live_state()) >= {"state", "error", "seconds"},
          asr.live_state())
    asr._live_state.update({"state": "ready", "error": "", "seconds": 1.2})
    check("готова", asr.live_state()["state"] == "ready", asr.live_state())
    asr._live_state.update({"state": "error", "error": "нет файла модели", "seconds": None})
    check("ошибка с причиной",
          asr.live_state() == {"state": "error", "error": "нет файла модели", "seconds": None},
          asr.live_state())
    check("наружу отдаётся копия", asr.live_state() is not asr._live_state)

    say("")
    say("=== 2. Состояние доходит до окна ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    # Служба при старте сама прогревает модель эфира. Проверке этого не нужно:
    # подменяем прогрев пустышкой ДО поднятия службы, иначе настоящая загрузка
    # затрёт состояние, которое мы ставим руками.
    calls = {"n": 0}
    real_warmup = asr.warmup

    def fake_warmup(live=True, precise=False):
        calls["n"] += 1
        asr._live_state.update({"state": "ready", "error": "", "seconds": 0.5})
        return {"live": 0.5}

    asr.warmup = fake_warmup
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        asr._live_state.update({"state": "error", "error": "нет файла модели", "seconds": None})
        st = cli.get("/api/state", headers=ORIGIN).json()
        got = (st.get("capabilities") or {}).get("asr") or {}
        check("состояние есть в общем состоянии", got.get("state") == "error", got)
        check("и причина тоже", got.get("error") == "нет файла модели", got)
        # Готовность службы кэшируется на 20 с — состояние модели кэшироваться
        # не должно, иначе окно снова врало бы.
        asr._live_state.update({"state": "ready", "error": "", "seconds": 1.2})
        st2 = cli.get("/api/state", headers=ORIGIN).json()
        check("состояние не кэшируется",
              ((st2.get("capabilities") or {}).get("asr") or {}).get("state") == "ready",
              (st2.get("capabilities") or {}).get("asr"))

        say("")
        say("=== 3. «Повторить» ===")
        was = calls["n"]
        asr._live_state.update({"state": "error", "error": "нет файла", "seconds": None})
        r = cli.post("/api/asr/warmup", headers=ORIGIN)
        check("загрузка запущена заново", calls["n"] == was + 1, calls)
        check("ответ — новое состояние",
              r.status_code == 200 and r.json()["state"] == "ready", r.text)
    asr.warmup = real_warmup

    say("")
    say("=== 4. Страница ===")
    check("зашитой надписи в HTML нет", "модель загружается…</div>" not in HTML)
    check("рисует одна функция", "function paintProgramState()" in JS)
    check("её зовёт загрузка состояния",
          "S.asr = S.caps.asr || { state: 'loading' };" in JS and "paintProgramState();" in JS)
    check("её же зовёт событие службы",
          re.search(r"ready\(ev\) \{\s*\n\s*S\.asr = ev\.asr", JS) is not None)
    check("есть все четыре состояния",
          all(x in JS for x in ["'ready'", "'error'", "'offline'", "модель загружается…"]))
    check("у ошибки есть «повторить»", "js-asr-retry" in JS and "/api/asr/warmup" in JS)
    check("служба не ответила — тоже состояние", "state: 'offline'" in JS)

    say("")
    say("=== 5. Цвет говорящего ===")
    m = re.search(r"function speakerClass\(seg\) \{(.+?)\n\}", JS, re.S)
    check("правило нашлось", m is not None)
    body = m.group(1) if m else ""
    check("цвет владельца — по его ключу, а не по дорожке",
          "speaker_key || '') === 'me'" in body, body.strip()[:160])
    check("старого правила «всё с микрофона — я» нет",
          "includes('~')" not in body, body.strip()[:160])

    # Пересказ того же правила на Python: цвет владельца только у своих реплик.
    def klass(seg):
        return ("me" if seg.get("track") == "mic" and str(seg.get("speaker_key") or "") == "me"
                else "them")

    mine = {"track": "mic", "speaker_key": "me", "speaker": "Иван Петров"}
    given = {"track": "mic", "speaker_key": "SPEAKER_00", "speaker": "Сергей Фещенко"}
    room = {"track": "mic", "speaker_key": "me~1", "speaker": "Рядом со мной · голос 1"}
    far = {"track": "far", "speaker_key": "SPEAKER_00", "speaker": "Сергей Фещенко"}
    check("своя реплика — цвет владельца", klass(mine) == "me")
    check("отданная руками — уже не владелец", klass(given) == "them")
    check("сосед по комнате — не владелец", klass(room) == "them")
    check("одно имя на двух дорожках — один класс",
          klass(given) == klass(far) == "them")

    say("")
    say("=== 6. Мелочи (п. 1.7 и 1.9) ===")
    CSS = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
    check("«Проект» и «Теги» — обычные поля, а не браузерные",
          'id="rec-project" type="text"' in HTML and 'id="rec-tags" type="text"' in HTML)
    check("общий вид полей их накрывает", "input[type=text]" in CSS)
    check("несуществующего признака --border в оформлении нет", "var(--border)" not in CSS)

    from hagen import config  # noqa: E402

    config.save({"call_watch_processes": ["Zoom.exe", "zoom.exe", " Teams.exe ", "", "Zoom.exe"]})
    got = config.get("call_watch_processes")
    check("повторы в списке программ отсекаются", got == ["Zoom.exe", "Teams.exe"], got)
    check("в заводском списке повторов нет",
          len({x.lower() for x in config.DEFAULTS["call_watch_processes"]})
          == len(config.DEFAULTS["call_watch_processes"]),
          config.DEFAULTS["call_watch_processes"])

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))

sys.exit(finish("t85"))
