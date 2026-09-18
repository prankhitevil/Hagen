# -*- coding: utf-8 -*-
"""Проверка 36: маршруты службы отвечают. Без запуска отдельного процесса.

Служба поднимается внутри этого же процесса тестовым клиентом: так проверка не
зависит от антивируса, который иногда не даёт порождать фоновые процессы.
Заголовок Origin обязателен — без него защита отвергает любой POST.
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()

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


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + ": " + repr(got)
        + ("" if ok else "  (ждали " + repr(want) + ")"))


from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}

# base_url задаёт заголовок Host: служба намеренно отвечает только своему же
# адресу, и по умолчанию тестовый клиент присылает чужой Host: testserver.
with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    say("=== 1. Страница и статика ===")
    page = cli.get("/")
    check("страница отдаётся", page.status_code, 200)
    html = page.text if page.status_code == 200 else ""
    check("кнопка «Видео» в разметке", 'data-mode="video"' in html, True)
    check("video.js подключён раньше app.js",
          "video.js" in html and html.index("video.js") < html.index("app.js"), True)
    check("video.js отдаётся", cli.get("/static/video.js").status_code, 200)
    check("переключателя «Быстро/Точнее» нет", "set-quality" in html, False)

    say("")
    say("=== 2. Состояние и сервисы ===")
    st = cli.get("/api/state").json()
    check("состояние отдаётся", isinstance(st.get("recordings"), list), True)
    caps = st.get("capabilities") or {}   # именно так поле зовётся в ответе
    # Число сервисов растёт, когда в список добавляют новый (17.09 добавлены
    # российские агрегаторы, китайские модели напрямую, Groq и Gemini).
    # Проверяем не точное число, а что список непустой и в нём есть тот,
    # на котором держится распознавание.
    ids = [p.get("id") for p in (caps.get("providers") or [])]
    check("сервисы в списке есть", len(ids) >= 5, True)
    check("polza на месте", "polza" in ids, True)
    say("   подсказка моделей: " + str(caps.get("minutes_models"))[:110])
    keys_leaked = [p for p in (caps.get("providers") or []) if "api_key" in p]
    check("ключей наружу нет", keys_leaked, [])
    pr = cli.get("/api/providers").json()
    check("выбранный сервис", pr.get("selected"), "anthropic")
    check("сервис для распознавания", pr.get("asr_selected"), "polza")

    say("")
    say("=== 3. Настройки раздела «Видео» ===")
    o = cli.get("/api/media/options").json()
    say("   папка видео     : %s" % o.get("assets_dir"))
    say("   английский      : %s — %s" % ((o.get("english") or {}).get("ready"),
                                          (o.get("english") or {}).get("why")))
    say("   облако          : %s — %s" % ((o.get("cloud") or {}).get("ready"),
                                          (o.get("cloud") or {}).get("why")))
    say("   сайты с паролем : %s — %s" % ((o.get("webauth") or {}).get("ready"),
                                          (o.get("webauth") or {}).get("why")))
    check("английский готов", (o.get("english") or {}).get("ready"), True)
    check("папка видео вне сейфа", "Yandex" in str(o.get("assets_dir")), False)
    check("этапы конвейера", o.get("stages"),
          ["media", "text", "diarize", "summary", "note"])
    check("субтитры .vtt и .srt", sorted(o.get("transcript_exts") or []),
          [".srt", ".vtt"])

    say("")
    say("=== 4. Защита и понятные отказы ===")
    check("POST без Origin отвергается",
          cli.post("/api/media/probe", json={"url": "https://ya.ru"}).status_code, 403)
    bad = cli.post("/api/media/probe", json={"url": "не ссылка"}, headers=ORIGIN)
    check("мусор вместо ссылки — 400", bad.status_code, 400)
    check("объяснение по-русски", "ссылк" in bad.json().get("detail", "").lower(), True)
    bad2 = cli.post("/api/media/source", json={"kind": "неизвестный"}, headers=ORIGIN)
    check("неизвестный источник — 400", bad2.status_code, 400)
    nosuch = cli.post("/api/media/20260101-000000-0000/summary", headers=ORIGIN)
    check("саммари для несуществующей записи — 404", nosuch.status_code, 404)

    say("")
    say("=== 5. Добавление сервиса: проверка адреса ===")
    for url, why in (("http://polza.ai/api/v1", "не https"),
                     ("https://127.0.0.1/v1", "свой компьютер"),
                     ("https://192.168.0.2/v1", "домашняя сеть")):
        r = cli.post("/api/providers", json={"title": "т", "base_url": url},
                     headers=ORIGIN)
        check("отклонён адрес (%s)" % why, r.status_code, 400)
    r = cli.delete("/api/providers/anthropic", headers=ORIGIN)
    check("встроенный сервис не удаляется", r.status_code, 400)

    say("")
    say("=== 6. Старых путей больше нет ===")
    check("/api/upload убран", cli.post("/api/upload", headers=ORIGIN).status_code, 404)
    check("/api/fetch убран",
          cli.post("/api/fetch", json={"url": "https://ya.ru"}, headers=ORIGIN).status_code,
          404)
    check("новый путь загрузки на месте",
          cli.post("/api/media/upload", headers=ORIGIN).status_code in (400, 422), True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t36_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
