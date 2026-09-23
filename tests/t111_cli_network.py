# -*- coding: utf-8 -*-
"""Проверка 111: сеть для Claude CLI — защита от запуска с «не того» адреса (23.09).

Серверы Anthropic отказывают запросам из части стран, а запрос с такого
адреса всё равно уносит токен аккаунта и стенограмму. Программа решает, каким
путём пойдёт CLI, до его запуска.

Ни одного запроса в настоящую сеть: порты — свои слушающие сокеты на
localhost, CLI — подставной, проба выхода — подменена. Что проверяем:
  1. порт слушает / не слушает; разбор адреса прокси;
  2. выбор порта: «auto» берёт первый слушающий HTTP-порт, явный адрес —
     как написан, мусор — ошибка;
  3. окружение через прокси: унаследованные переменные сняты, свои стоят;
     телеметрия и автообновление CLI выключены всегда;
  4. адреса для yt-dlp берутся из того же списка портов;
  5. режим «порт»: порта нет — CLI НЕ запускается, есть — идёт через него;
     явный адрес, который не слушает, — отказ с адресом;
  6. режим «проба»: отказ Anthropic — CLI не запускается, «пускает» — идёт как есть;
  7. режим «без защиты» — запускается всегда;
  8. кнопка «Проверить» подчиняется тем же правилам;
  9. разбор ответа Cloudflare и вердикт по коду Anthropic;
 10. отчёт для окна: порты, путь, проба только по просьбе и не через
     мёртвый порт;
 11. маршрут POST /api/engines/claude_cli/network, GET не принимается;
 12. умолчания и страница: кнопка в полях CLI, окно, настройки уходят.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t111_cli_network.py
"""
import io
import os
import socket
import sys
import textwrap
import threading
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.settings()


from hagen import config, fetch, minutes, vpn  # noqa: E402

# Свой слушающий порт и заведомо закрытый (занят и тут же отпущен).
SRV = socket.socket()
SRV.bind(("127.0.0.1", 0))
SRV.listen(5)
OPEN = SRV.getsockname()[1]


def _accept_all() -> None:
    # Соединения надо принимать и закрывать: иначе очередь ожидания (5)
    # заполняется, и настоящий слушающий порт начинает выглядеть закрытым.
    while True:
        try:
            conn, _ = SRV.accept()
            conn.close()
        except OSError:
            return


threading.Thread(target=_accept_all, daemon=True).start()
_tmp = socket.socket()
_tmp.bind(("127.0.0.1", 0))
CLOSED = _tmp.getsockname()[1]
_tmp.close()

FAKE = PROJECT / "tests" / "_fake_claude_net.py"
RUNS = PROJECT / "tests" / "_fake_claude_net.runs"
FAKE.write_text(textwrap.dedent('''
    import json, os, sys
    mode, runs = sys.argv[1], sys.argv[2]
    def out(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    if mode == "auth":
        out({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"})
        sys.exit(0)
    sys.stdin.read()
    with open(runs, "a", encoding="utf-8") as fh:
        fh.write("run\\n")
    out({"type": "result", "subtype": "success", "is_error": False,
         "result": "proxy=%s flag=%s" % (os.environ.get("HTTPS_PROXY", ""),
                                         os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", ""))})
'''), encoding="utf-8")

_real = (minutes.resolve_claude_cli, minutes._auth_status_args, minutes._claude_args,
         vpn.PORTS, vpn.probe_egress)
minutes.resolve_claude_cli = lambda: sys.executable
minutes._auth_status_args = lambda exe: [sys.executable, str(FAKE), "auth", str(RUNS)]
minutes._claude_args = lambda exe, prompt, minimal=False, model="": [
    sys.executable, str(FAKE), "run", str(RUNS)]

PROBE = {"calls": 0, "answer": {"ip": "203.0.113.5", "country": "NL", "anthropic": "open",
                                "status": 401, "error": ""}}


def fake_probe(proxy=None, as_is=False):
    PROBE["calls"] += 1
    PROBE["last"] = (proxy, as_is)
    return dict(PROBE["answer"])


vpn.probe_egress = fake_probe


def run():
    """Запустить подставной CLI. Возвращает (ответ или текст ошибки, было ли запусков)."""
    RUNS.unlink(missing_ok=True)
    try:
        out = minutes.run_claude_cli("инструкция", "стенограмма", timeout=30)
    except RuntimeError as err:
        out = "ОШИБКА: %s" % err
    runs = len(RUNS.read_text(encoding="utf-8").split()) if RUNS.exists() else 0
    return out, runs


try:
    say("=== 1. Порт слушает / не слушает; разбор адреса ===")
    check("свой порт слушает", vpn.listening(OPEN) is True, OPEN)
    check("закрытый порт не слушает", vpn.listening(CLOSED) is False, CLOSED)
    check("адрес хост:порт", vpn.parse_proxy("127.0.0.1:12334") == ("127.0.0.1", 12334))
    check("адрес с http://", vpn.parse_proxy("http://127.0.0.1:10809/") == ("127.0.0.1", 10809))
    check("мусор не разобран", vpn.parse_proxy("socks5://x") is None and vpn.parse_proxy("") is None)
    check("порт за пределами", vpn.parse_proxy("127.0.0.1:70000") is None)

    say("")
    say("=== 2. Выбор порта ===")
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True, note="проба"),
                 vpn.Port(OPEN, "Тестовый", http=True, socks=True, note="проба"))
    got = vpn.find_cli_proxy("auto")
    check("auto — первый слушающий HTTP-порт",
          got["listening"] and got["url"] == "http://127.0.0.1:%d" % OPEN
          and got["client"] == "Тестовый", got)
    vpn.PORTS = (vpn.Port(OPEN, "Носочный", http=False, socks=True, note="только SOCKS"),
                 vpn.Port(CLOSED, "Закрытый", http=True, socks=False, note="проба"))
    got = vpn.find_cli_proxy("auto")
    check("порт только под SOCKS для CLI не берётся", not got["url"] and not got["listening"], got)
    got = vpn.find_cli_proxy("127.0.0.1:%d" % OPEN)
    check("явный адрес — как написан", got["url"] == "http://127.0.0.1:%d" % OPEN
          and got["listening"] and "Носочный" in got["client"], got)
    got = vpn.find_cli_proxy("127.0.0.1:%d" % CLOSED)
    check("явный закрытый — не слушает", got["url"] and not got["listening"], got)
    got = vpn.find_cli_proxy("абракадабра")
    check("мусор в настройке — ошибка", got.get("error") and not got["listening"], got)

    say("")
    say("=== 3. Окружение ===")
    base = {"PATH": "x", "http_proxy": "http://old:1", "HTTPS_PROXY": "http://old:2",
            "ALL_PROXY": "socks5://old:3", "NO_PROXY": "что-то"}
    env = vpn.env_through(base, "http://127.0.0.1:%d" % OPEN)
    check("унаследованные прокси сняты", "http_proxy" not in env and "ALL_PROXY" not in env, env)
    check("свой прокси стоит в обеих переменных",
          env["HTTPS_PROXY"] == env["HTTP_PROXY"] == "http://127.0.0.1:%d" % OPEN, env)
    check("localhost остаётся прямым", "127.0.0.1" in env["NO_PROXY"], env["NO_PROXY"])
    check("остальное окружение на месте", env["PATH"] == "x")
    check("исходный словарь не тронут", base["http_proxy"] == "http://old:1")
    os.environ["CLAUDECODE"] = "1"
    env = minutes._cli_env()
    os.environ.pop("CLAUDECODE", None)
    check("телеметрия и автообновление CLI выключены",
          env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1")
    check("метка вложенной сессии снята", "CLAUDECODE" not in env)

    say("")
    say("=== 4. Адреса для yt-dlp — из того же списка ===")
    vpn.PORTS = (vpn.Port(12334, "Hiddify", http=True, socks=True),
                 vpn.Port(10809, "Happ", http=True, socks=False),
                 vpn.Port(10808, "Happ", http=False, socks=True))
    check("смешанный — SOCKS, чисто HTTP — http, чисто SOCKS — SOCKS",
          vpn.yt_proxies() == ["socks5://127.0.0.1:12334", "http://127.0.0.1:10809",
                               "socks5://127.0.0.1:10808"], vpn.yt_proxies())
    cands = fetch.proxy_candidates("auto")
    check("yt-dlp: сначала напрямую, потом порты", cands[0] is None and cands[1:] == vpn.yt_proxies(),
          cands)
    check("пусто в настройке — только напрямую", fetch.proxy_candidates("") == [None])

    say("")
    say("=== 5. Режим «порт» ===")
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True),)
    isolate.settings(claude_cli_guard="port", claude_cli_proxy="auto")
    out, runs = run()
    check("порта нет — CLI не запущен", runs == 0, runs)
    check("сказано, что VPN не найден и что делать",
          "не найден локальный порт" in out and "Сеть для CLI" in out, out)
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True),
                 vpn.Port(OPEN, "Тестовый", http=True, socks=True))
    out, runs = run()
    check("порт есть — CLI запущен один раз", runs == 1, runs)
    check("и пошёл через этот порт", ("proxy=http://127.0.0.1:%d" % OPEN) in out, out)
    check("телеметрия выключена и в настоящем запуске", "flag=1" in out, out)
    isolate.settings(claude_cli_guard="port", claude_cli_proxy="127.0.0.1:%d" % CLOSED)
    out, runs = run()
    check("явный адрес не слушает — отказ с адресом",
          runs == 0 and ("127.0.0.1:%d" % CLOSED) in out, out)
    isolate.settings(claude_cli_guard="port", claude_cli_proxy="мусор")
    out, runs = run()
    check("мусор в настройке — отказ, не запуск", runs == 0 and "не разобран" in out, out)
    check("проба выхода в режиме «порт» не делалась", PROBE["calls"] == 0, PROBE["calls"])

    say("")
    say("=== 6. Режим «проба» ===")
    isolate.settings(claude_cli_guard="probe")
    PROBE["answer"].update(anthropic="blocked", status=403, ip="198.51.100.7", country="RU")
    out, runs = run()
    check("отказ по региону — CLI не запущен", runs == 0, runs)
    check("сказано, откуда и что делать", "198.51.100.7" in out and "отказывают" in out
          and "Сеть для CLI" in out, out)
    check("проба шла «как есть», без прокси", PROBE.get("last") == (None, True), PROBE.get("last"))
    PROBE["answer"].update(anthropic="unknown", status=None, error="сети нет")
    out, runs = run()
    check("ответа нет — тоже не запущен, причина в тексте", runs == 0 and "сети нет" in out, out)
    PROBE["answer"].update(anthropic="open", status=401, error="")
    out, runs = run()
    # «Как есть» — с теми переменными прокси, что есть у самой программы
    # (на машине с VPN в режиме системного прокси они не пустые).
    inherited = "proxy=%s " % os.environ.get("HTTPS_PROXY", "")
    check("пускает — CLI запущен как есть", runs == 1 and inherited in out, (inherited, out))

    say("")
    say("=== 7. Без защиты ===")
    isolate.settings(claude_cli_guard="off")
    calls_before = PROBE["calls"]
    out, runs = run()
    check("запущен как есть, пробы не было",
          runs == 1 and PROBE["calls"] == calls_before and inherited in out, (runs, out))

    say("")
    say("=== 8. Кнопка «Проверить» подчиняется тем же правилам ===")
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True),)
    isolate.settings(claude_cli_guard="port", claude_cli_proxy="auto")
    RUNS.unlink(missing_ok=True)
    res = minutes.check_claude_cli(timeout=30)
    check("вход виден, запроса не было, причина — сеть",
          res["logged_in"] is True and not res["ok"] and not RUNS.exists()
          and "не найден локальный порт" in res["text"], res)

    say("")
    say("=== 9. Разбор ответа Cloudflare и вердикт ===")
    tr = vpn.parse_trace("fl=1f2\nh=www.cloudflare.com\nip=203.0.113.5\nts=1.2\nloc=NL\n")
    check("адрес и страна", tr.get("ip") == "203.0.113.5" and tr.get("loc") == "NL", tr)
    check("401 — пускает", vpn.anthropic_verdict(401) == "open")
    check("403 — отказ по региону", vpn.anthropic_verdict(403) == "blocked")
    check("остальное — не понять", vpn.anthropic_verdict(None) == "unknown"
          and vpn.anthropic_verdict(500) == "unknown")

    say("")
    say("=== 10. Отчёт для окна ===")
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True, note="проба"),
                 vpn.Port(OPEN, "Тестовый", http=True, socks=True, note="проба"))
    calls_before = PROBE["calls"]
    rep = minutes.network_report()
    check("режим и порты", rep["mode"] == "port" and len(rep["ports"]) == 2, rep)
    check("у каждого порта — кто и слушает ли",
          [p["listening"] for p in rep["ports"]] == [False, True]
          and rep["ports"][1]["client"] == "Тестовый", rep["ports"])
    check("путь назван, всё хорошо", rep["ok"] and "Тестовый" in rep["route"], rep["route"])
    check("без просьбы пробы нет", "probe" not in rep and PROBE["calls"] == calls_before, rep.keys())
    rep = minutes.network_report(probe=True)
    check("по просьбе — проба через выбранный порт",
          rep.get("probe", {}).get("anthropic") == "open"
          and PROBE["last"] == ("http://127.0.0.1:%d" % OPEN, False), (rep.get("probe"), PROBE["last"]))
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True, note="проба"),)
    calls_before = PROBE["calls"]
    rep = minutes.network_report(probe=True)
    check("порта нет — отчёт честный, пробы через мёртвый порт нет",
          not rep["ok"] and "не слушает" in rep["route"] and PROBE["calls"] == calls_before
          and "наружу ничего не ушло" in rep["probe"]["error"], rep)
    isolate.settings(claude_cli_guard="probe")
    rep = minutes.network_report(probe=True)
    check("в режиме «проба» — как есть", "как есть" in rep["route"] and PROBE["last"] == (None, True), rep)
    isolate.settings(claude_cli_guard="off")
    rep = minutes.network_report()
    check("без защиты — так и сказано", rep["ok"] and "без защиты" in rep["route"], rep["route"])

    say("")
    say("=== 11. Маршрут ===")
    from fastapi import FastAPI  # noqa: E402
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen.api import dictation  # noqa: E402

    isolate.settings(claude_cli_guard="port", claude_cli_proxy="auto")
    app = FastAPI()
    app.include_router(dictation.router)
    with TestClient(app) as cli:
        r = cli.post("/api/engines/claude_cli/network", json={})
        body = r.json() if r.status_code == 200 else {}
        check("POST отдаёт отчёт", r.status_code == 200 and "ports" in body and body.get("mode") == "port",
              (r.status_code, body.get("route")))
        calls_before = PROBE["calls"]
        r = cli.post("/api/engines/claude_cli/network", json={"probe": True})
        check("с probe — проба (или честный отказ без неё)",
              r.status_code == 200 and "probe" in r.json(), r.status_code)
        r = cli.get("/api/engines/claude_cli/network")
        check("GET не принимается", r.status_code == 405, r.status_code)

    say("")
    say("=== 12. Умолчания и страница ===")
    check("по умолчанию — только через порт, auto",
          config.DEFAULTS["claude_cli_guard"] == "port" and config.DEFAULTS["claude_cli_proxy"] == "auto")
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    cli_block = html[html.index('id="cli-fields"'):html.index('id="api-fields"')]
    check("кнопка «Сеть для CLI» — в полях подписки, рядом с моделью",
          'id="btn-cli-network"' in cli_block and cli_block.index('id="set-cli-model"')
          < cli_block.index('id="btn-cli-network"') < cli_block.index('id="set-cli-timeout"'))
    dlg = html[html.index('id="dlg-cli-network"'):html.index('id="dlg-merge"')]
    check("окно с тремя режимами и полем порта",
          dlg.count('data-mode="') == 3 and 'id="set-cli-proxy"' in dlg
          and 'id="btn-net-probe"' in dlg, dlg.count('data-mode="'))
    check("мини-руководство в окне", "Как это работает" in html)
    check("настройки уходят вместе с остальными",
          "claude_cli_guard:" in js and "claude_cli_proxy:" in js and "bindNetDialog()" in js)

    say("")
    say("=== 13. Сводка «Готов к записи»: состояние порта без сети ===")
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True),
                 vpn.Port(OPEN, "Тестовый", http=True, socks=True))
    calls_before = PROBE["calls"]
    check("порт слушает — назван номер",
          minutes.cli_network_state() == "порт VPN %d слушает" % OPEN, minutes.cli_network_state())
    vpn.PORTS = (vpn.Port(CLOSED, "Закрытый", http=True, socks=True),)
    check("порта нет — «VPN выключен»", minutes.cli_network_state() == "VPN выключен",
          minutes.cli_network_state())
    isolate.settings(claude_cli_guard="off")
    check("без защиты — так и сказано", "без защиты" in minutes.cli_network_state())
    isolate.settings(claude_cli_guard="probe")
    check("проба — так и сказано", "проба" in minutes.cli_network_state())
    check("сводка в сеть не ходит", PROBE["calls"] == calls_before)
    check("служба отдаёт строку в сводку, страница её показывает",
          'caps["claude_cli_net"]' in io.open(PROJECT / "hagen" / "server.py", encoding="utf-8").read()
          and "c.claude_cli_net" in js)
finally:
    (minutes.resolve_claude_cli, minutes._auth_status_args, minutes._claude_args,
     vpn.PORTS, vpn.probe_egress) = _real
    SRV.close()
    FAKE.unlink(missing_ok=True)
    RUNS.unlink(missing_ok=True)

sys.exit(finish("t111"))
