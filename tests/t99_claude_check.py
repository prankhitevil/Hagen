# -*- coding: utf-8 -*-
"""Проверка 99: кнопка «Проверить» у Claude CLI.

Найти claude.exe мало: вход может быть не выполнен, а антивирус — не пускать
CLI в сеть, и человек узнал бы об этом только на первом документе. Проверка
спрашивает CLI о входе (лимит не тратит) и делает короткий настоящий запрос.

На подменённом CLI (ответ «auth status --json» сверен с claude 2.1.187):
  1. CLI не найден → сказано, как поставить и как войти.
  2. Вход по подписке, ответ пришёл → «Работает», подписка и путь к файлу.
  3. Вход не выполнен → пробный запрос НЕ делается, сказано «claude auth login».
  4. Старый CLI без команды auth → решает пробный запрос.
  5. Лимит подписки в пробном запросе → вход и связь есть, документы — нет.
  6. Вход не через claude.ai → предупреждение, что оплата пойдёт не по подписке.
  7. Пробный запрос упал → текст ошибки CLI.
  8. Маршрут POST /api/engines/claude_cli/check; строка «готов» в настройках
     больше не обещает того, чего не проверяли.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t99_claude_check.py
"""
import io
import sys
import textwrap
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящие settings.json не читаются

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
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


from hagen import minutes  # noqa: E402

FAKE = PROJECT / "tests" / "_fake_claude_check.py"
RUNS = PROJECT / "tests" / "_fake_claude_check.runs"
FAKE.write_text(textwrap.dedent('''
    import json, sys, time
    mode, scenario, runs = sys.argv[1], sys.argv[2], sys.argv[3]

    def out(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    if mode == "auth":
        if scenario == "old":
            sys.stderr.write("error: unknown command 'auth'\\n")
            sys.exit(1)
        if scenario == "logged_out":
            out({"loggedIn": False})
            sys.exit(1)
        method = "api_key" if scenario == "apikey" else "claude.ai"
        out({"loggedIn": True, "authMethod": method, "apiProvider": "firstParty",
             "email": "tester@example.com", "subscriptionType": "max"})
        sys.exit(0)

    # пробный запрос: отметить, что он был, и ответить потоком событий
    with open(runs, "a", encoding="utf-8") as fh:
        fh.write(scenario + "\\n")
    sys.stdin.read()
    out({"type": "system", "subtype": "init", "model": "fake"})
    if scenario == "limit":
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
             "resetsAt": int(time.time()) + 3600, "rateLimitType": "five_hour"}})
        time.sleep(60)
    elif scenario == "fail":
        sys.stderr.write("Self-signed certificate detected\\n")
        sys.exit(1)
    else:
        out({"type": "result", "subtype": "success", "is_error": False, "result": "готов"})
'''), encoding="utf-8")

SCENARIO = {"name": "ok"}
_real = (minutes.resolve_claude_cli, minutes._auth_status_args, minutes._claude_args)
minutes.resolve_claude_cli = lambda: sys.executable
minutes._auth_status_args = lambda exe: [sys.executable, str(FAKE), "auth",
                                         SCENARIO["name"], str(RUNS)]
minutes._claude_args = lambda exe, prompt, minimal=False, model="": [
    sys.executable, str(FAKE), "run", SCENARIO["name"], str(RUNS)]


def probe(name):
    """Проверка по сценарию. Возвращает (итог, сколько раз был пробный запрос, секунды)."""
    SCENARIO["name"] = name
    RUNS.unlink(missing_ok=True)
    t0 = time.time()
    res = minutes.check_claude_cli(timeout=30)
    runs = len(RUNS.read_text(encoding="utf-8").split()) if RUNS.exists() else 0
    return res, runs, time.time() - t0


try:
    say("=== 1. CLI не найден ===")
    minutes.resolve_claude_cli = lambda: None
    res, runs, _ = probe("ok")
    check("не найден", res["found"] is False and res["ok"] is False, res)
    check("сказано, как поставить", "npm i -g @anthropic-ai/claude-code" in res["text"], res["text"])
    check("и как войти", "claude auth login" in res["text"], res["text"])
    check("пробного запроса не было", runs == 0, runs)
    check("ошибка документа говорит то же самое",
          minutes.available_engines()[0]["reason"] == res["text"])
    check("в настройках не обещано несуществующее поле пути",
          "укажите путь" not in res["text"], res["text"])
    minutes.resolve_claude_cli = lambda: sys.executable

    say("")
    say("=== 2. Вход по подписке, ответ пришёл ===")
    res, runs, dt = probe("ok")
    check("работает", res["ok"] and res["answered"] and res["logged_in"] is True, res)
    check("подписка названа", res["subscription"] == "max" and "подписка max" in res["text"],
          res["text"])
    check("путь к файлу в тексте", sys.executable in res["text"], res["text"])
    check("время ответа посчитано", isinstance(res["seconds"], float), res["seconds"])
    check("пробный запрос был ровно один", runs == 1, runs)
    check("почта аккаунта наружу не отдаётся",
          "tester@example.com" not in str(res), res)

    say("")
    say("=== 3. Вход не выполнен ===")
    res, runs, dt = probe("logged_out")
    check("не работает", res["ok"] is False and res["logged_in"] is False, res)
    check("сказано «claude auth login»", "claude auth login" in res["text"], res["text"])
    check("пробный запрос не делался", runs == 0, runs)
    check("ответ быстрый", dt < 15, "%.1f c" % dt)

    say("")
    say("=== 4. Старый CLI без команды auth ===")
    res, runs, _ = probe("old")
    check("вход неизвестен, решил пробный запрос",
          res["logged_in"] is None and res["ok"] is True and runs == 1, res)
    check("в тексте: способ входа неизвестен", "не сообщил" in res["text"], res["text"])

    say("")
    say("=== 5. Лимит подписки ===")
    res, runs, dt = probe("limit")
    check("не работает", res["ok"] is False and res["answered"] is False, res)
    check("сказано, что вход и связь есть", "связь есть" in res["text"], res["text"])
    check("назван лимит", "пятичасовой лимит" in res["text"], res["text"])
    check("за секунды, а не через таймаут", dt < 15, "%.1f c" % dt)

    say("")
    say("=== 6. Вход не через claude.ai ===")
    res, runs, _ = probe("apikey")
    check("работает", res["ok"] is True, res)
    check("предупреждение об оплате", "не по подписке" in res["text"], res["text"])

    say("")
    say("=== 7. Пробный запрос упал ===")
    res, runs, _ = probe("fail")
    check("не работает", res["ok"] is False and res["logged_in"] is True, res)
    check("в тексте ошибка CLI", "пробный запрос не прошёл" in res["text"]
          and "Self-signed" in res["text"], res["text"])

    say("")
    say("=== 8. Маршрут и строка в настройках ===")
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen.api import dictation  # noqa: E402

    app = FastAPI()
    app.include_router(dictation.router)
    SCENARIO["name"] = "ok"
    with TestClient(app) as cli:
        r = cli.post("/api/engines/claude_cli/check")
        body = r.json() if r.status_code == 200 else {}
        check("POST отвечает итогом проверки", r.status_code == 200 and body.get("ok") is True,
              (r.status_code, body.get("text")))
        r = cli.get("/api/engines/claude_cli/check")
        check("GET не принимается", r.status_code == 405, r.status_code)
    cli_engine = minutes.available_engines()[0]
    check("найденный CLI не назван «готовым»",
          "Готов" not in cli_engine["reason"] and "Проверить" in cli_engine.get("state", ""),
          cli_engine)
finally:
    (minutes.resolve_claude_cli, minutes._auth_status_args, minutes._claude_args) = _real
    FAKE.unlink(missing_ok=True)
    RUNS.unlink(missing_ok=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t99_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
