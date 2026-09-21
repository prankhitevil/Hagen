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
import isolate  # noqa: E402  настоящие база голосов и настройки не трогаются

isolate.voices()
# Служба при запуске переносит старые настройки и пишет их в файл: на
# настоящем settings.json проверка переписала бы настройки человека.
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
    # Сервисы — два подключения «адрес, ключ, модель» (решение 21.09).
    conns = caps.get("connections") or {}
    check("подключения: документы и распознавание", sorted(conns), ["asr", "docs"])
    check("распознавание по умолчанию — polza",
          (conns.get("asr") or {}).get("base_url"), "https://polza.ai/api/v1")
    check("у документов адреса по умолчанию нет", (conns.get("docs") or {}).get("base_url"), "")
    say("   подсказка моделей: " + str(caps.get("minutes_models"))[:110])
    check("ключей наружу нет", [r for r, c in conns.items() if "key" in c], [])
    pr = cli.get("/api/connections").json()
    check("маршрут подключений", sorted(pr.get("connections") or {}), ["asr", "docs"])
    check("в нём подходящие адреса для подсказки",
          any("routerai.ru" in s.get("base_url", "") for s in pr.get("suggested") or []), True)

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
    say("=== 5. Адрес сервиса: проверка ===")
    for url, why in (("http://polza.ai/api/v1", "не https"),
                     ("https://127.0.0.1/v1", "свой компьютер"),
                     ("https://192.168.0.2/v1", "домашняя сеть")):
        cli.post("/api/settings", json={"api_base_url": url,
                                        "api_keys": {"docs": "ключ-подлиннее-восьми"}},
                 headers=ORIGIN)
        docs = cli.get("/api/connections").json()["connections"]["docs"]
        check("отклонён адрес (%s)" % why, "не годится" in docs.get("problem", ""), True)
    r = cli.post("/api/models?role=docs&refresh=true", headers=ORIGIN)
    check("по такому адресу список моделей не спрашивается", r.status_code, 502)
    check("неизвестное подключение — 400",
          cli.post("/api/models?role=нет", headers=ORIGIN).status_code, 400)
    check("готовность обновилась сразу после сохранения",
          "не годится" in str(cli.get("/api/state").json()["capabilities"]["engines"][1]["reason"]),
          True)

    say("")
    say("=== 6. Старых путей больше нет ===")
    check("/api/upload убран", cli.post("/api/upload", headers=ORIGIN).status_code, 404)
    check("/api/fetch убран",
          cli.post("/api/fetch", json={"url": "https://ya.ru"}, headers=ORIGIN).status_code,
          404)
    check("новый путь загрузки на месте",
          cli.post("/api/media/upload", headers=ORIGIN).status_code in (400, 422), True)
    check("/api/providers убран (список сервисов стал полями)",
          cli.get("/api/providers").status_code, 404)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t36_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
