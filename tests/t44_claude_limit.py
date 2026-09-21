# -*- coding: utf-8 -*-
"""Проверка 44: Claude CLI — «модель думает» против «упёрлись в лимит».

Живой сбой 12.09: подписка упёрлась в лимит, CLI молча ждал его сброса, а
программа 600 с показывала замершие 85 % и потом писала «не ответил».

Теперь CLI запускается с потоком событий. Проверяем на подменённом CLI (тот
же протокол событий, что у claude 2.1.167, — сверено живым запуском):
  1. Лимит в rate_limit_event → честная ошибка за секунды, время сброса.
  2. Повтор из-за 429 → тоже лимит; перегрузка 529 → ждём, человеку пишем.
  3. Долгое раздумье → в задаче «Claude думает: N с», ответ принимается.
  4. Настоящая тишина → таймаут с пояснением, что лимит не сообщался.
  5. Старый CLI с голым текстом, огромная стенограмма, отмена задачи.
  6. Очередь задач: ошибка лимита кладёт в задачу пометку и путь повтора.
  7. Сквозь службу: протокол упёрся в лимит → «Собрать через облако»
     собирает его облачным движком только на этот раз, настройка не меняется.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t44_claude_limit.py
"""
import io
import sys
import textwrap
import time
from datetime import datetime
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


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


from hagen import config, jobs, minutes, store  # noqa: E402

FAKE = PROJECT / "tests" / "_fake_claude_cli.py"
FAKE.write_text(textwrap.dedent('''
    import json, sys, time
    scenario = sys.argv[1]
    data = sys.stdin.read()

    def out(obj):
        print(json.dumps(obj, ensure_ascii=False), flush=True)

    out({"type": "system", "subtype": "init", "model": "fake"})
    reset = int(time.time()) + 3600
    if scenario == "limit":
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
             "resetsAt": reset, "rateLimitType": "five_hour"}})
        time.sleep(60)
    elif scenario == "retry429":
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning",
             "resetsAt": reset, "rateLimitType": "seven_day"}})
        out({"type": "system", "subtype": "api_retry", "attempt": 1, "max_retries": 10,
             "retry_delay_ms": 60000, "error_status": 429, "error": "rate_limit"})
        time.sleep(60)
    elif scenario == "overload":
        for a in (1, 2):
            out({"type": "system", "subtype": "api_retry", "attempt": a, "max_retries": 10,
                 "retry_delay_ms": 500, "error_status": 529, "error": "overloaded"})
            time.sleep(0.6)
        out({"type": "result", "subtype": "success", "is_error": False, "result": "ГОТОВО"})
    elif scenario == "slow":
        out({"type": "system", "subtype": "thinking_tokens"})
        time.sleep(17)
        out({"type": "result", "subtype": "success", "is_error": False, "result": "ДОЛГО"})
    elif scenario == "silent":
        time.sleep(60)
    elif scenario == "error429":
        out({"type": "result", "subtype": "error_during_execution", "is_error": True,
             "api_error_status": 429, "result": "rate limited"})
    elif scenario == "plain":
        print("просто текст старого CLI")
    elif scenario == "size":
        out({"type": "result", "subtype": "success", "is_error": False,
             "result": "символов %d" % len(data)})
    elif scenario == "protocol":
        out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed",
             "resetsAt": reset, "rateLimitType": "five_hour"}})
        out({"type": "result", "subtype": "success", "is_error": False,
             "result": "# Протокол совещания\\nтест"})
'''), encoding="utf-8")

SCENARIO = {"name": "ok"}
_real_args = minutes._claude_args
minutes._claude_args = lambda exe, prompt, minimal=False, model="": [
    sys.executable, str(FAKE), SCENARIO["name"]]
minutes.resolve_claude_cli = lambda: sys.executable


class Handle:
    def __init__(self, cancel_after=None):
        self.notes = []
        self.t0 = time.time()
        self.cancel_after = cancel_after

    def log(self, msg):
        self.notes.append(str(msg))

    def progress(self, value, note=""):
        if note:
            self.notes.append(note)

    @property
    def cancelled(self):
        return self.cancel_after is not None and time.time() - self.t0 > self.cancel_after


def run(name, timeout=120, handle=None, text="стенограмма"):
    SCENARIO["name"] = name
    t0 = time.time()
    try:
        res = minutes.run_claude_cli("инструкция", text, timeout=timeout, handle=handle)
        return res, None, time.time() - t0
    except Exception as err:
        return None, err, time.time() - t0


say("=== 1–2. Лимит подписки ===")
res, err, dt = run("limit")
check("лимит в событии — ошибка лимита", isinstance(err, minutes.ClaudeLimitError), repr(err))
check("сказано за секунды, а не через таймаут", dt < 10, "%.1f c" % dt)
if isinstance(err, minutes.ClaudeLimitError):
    want = datetime.fromtimestamp(err.resets_at).strftime("%H:%M") if err.resets_at else "?"
    check("в тексте время сброса", want in str(err), str(err))
    check("назван пятичасовой лимит", "пятичасовой" in str(err), str(err))
    check("пометка для окна", err.job_extra.get("error_kind") == "claude_limit", err.job_extra)

res, err, dt = run("retry429")
check("повтор из-за 429 — это лимит", isinstance(err, minutes.ClaudeLimitError), repr(err))
check("время сброса взято из предыдущего события",
      isinstance(err, minutes.ClaudeLimitError) and bool(err.resets_at))
check("и тоже за секунды", dt < 10, "%.1f c" % dt)

res, err, dt = run("error429")
check("ошибка результата с кодом 429 — лимит", isinstance(err, minutes.ClaudeLimitError), repr(err))

h = Handle()
res, err, dt = run("overload", handle=h)
check("перегрузка серверов — ждём и получаем ответ", res == "ГОТОВО" and err is None, (res, err))
check("человеку сказано, что идёт повтор", any("повторяю" in n for n in h.notes), h.notes)

say("")
say("=== 3–4. Думает или молчит ===")
h = Handle()
res, err, dt = run("slow", handle=h)
check("долгое раздумье — ответ принят", res == "ДОЛГО", (res, err))
check("в задаче видно «Claude думает: N с»", any(n.startswith("Claude думает") for n in h.notes),
      h.notes)

res, err, dt = run("silent", timeout=30)
check("полная тишина — таймаут", isinstance(err, RuntimeError)
      and not isinstance(err, minutes.ClaudeLimitError), repr(err))
check("в тексте: лимит не сообщался", err is not None and "Лимит подписки не сообщался" in str(err),
      str(err))
check("таймаут соблюдён (30–40 с)", 30 <= dt <= 40, "%.1f c" % dt)

say("")
say("=== 5. Старый CLI, большой текст, отмена ===")
res, err, dt = run("plain")
check("голый текст старого CLI принят", res == "просто текст старого CLI", (res, err))
big = "реплика совещания " * 20000
res, err, dt = run("size", text=big)
check("стенограмма 360 тысяч символов доходит целиком", res == "символов %d" % len(big), (res, err))
h = Handle(cancel_after=1.0)
res, err, dt = run("silent", handle=h)
check("отмена задачи обрывает ожидание", err is not None and "отменена" in str(err) and dt < 6,
      "%s, %.1f c" % (err, dt))

say("")
say("=== 6. Очередь задач ===")


def failing(handle):
    raise minutes.ClaudeLimitError(time.time() + 600, "five_hour")


jid = jobs.submit("minutes", failing, "Проверка 44", rec_id=None,
                  extra={"retry": {"url": "/api/x", "body": {"template": "protocol"}}})
for _ in range(50):
    j = jobs.get(jid)
    if j and j["status"] in ("done", "error"):
        break
    time.sleep(0.1)
j = jobs.get(jid) or {}
check("задача упала с пометкой лимита", j.get("status") == "error" and j.get("error_kind") == "claude_limit",
      (j.get("status"), j.get("error_kind")))
check("в задаче время сброса", bool(j.get("resets_at")))
check("путь повтора сохранён", (j.get("retry") or {}).get("url") == "/api/x", j.get("retry"))

say("")
say("=== 7. Сквозь службу ===")
from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
meta = store.create(title="Проверка 44 — лимит", mode="online", source="live")
rid = meta["id"]
store.replace_segments(rid, [store.make_segment("mic", 0, 5, "Смету подготовлю к четвергу.")])
events = []
real_publish = server.hub.publish


def spy(payload):
    events.append(payload)
    real_publish(payload)


server.hub.publish = spy


def wait(job_id, limit=60):
    for _ in range(limit * 10):
        j = jobs.get(job_id)
        if j and j["status"] in ("done", "error", "cancelled"):
            return j
        time.sleep(0.1)
    return jobs.get(job_id) or {}


engine_before = config.get("minutes_engine")
with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    SCENARIO["name"] = "limit"
    r = cli.post("/api/recordings/%s/minutes" % rid, headers=ORIGIN, json={"template": "protocol"})
    j = wait(r.json()["job_id"])
    check("протокол упёрся в лимит", j.get("status") == "error" and j.get("error_kind") == "claude_limit",
          (j.get("status"), j.get("error_kind"), j.get("note")))
    retry = j.get("retry") or {}
    check("у задачи путь повтора на тот же протокол",
          retry.get("url") == "/api/recordings/%s/minutes" % rid
          and (retry.get("body") or {}).get("template") == "protocol", retry)
    job_events = [e["job"] for e in events if e.get("type") == "job" and e["job"]["id"] == j["id"]]
    check("окно получило пометку лимита в событии",
          any(e.get("error_kind") == "claude_limit" and e.get("retry") for e in job_events))
    check("окно получило сообщение об ошибке",
          any(e.get("type") == "notice" and e.get("level") == "err" and "лимит" in (e.get("text") or "")
              for e in events))

    # «Собрать через облако»: облачный вызов подменён, в сеть ничего не уходит
    sent = []

    def fake_cloud(info, prompt, text, key, model):
        sent.append((info.get("service"), model, len(text)))
        return "# Протокол совещания\n\nСобрано облаком.\n\n## Участники\nЯ"

    minutes.CALLS = {k: fake_cloud for k in minutes.CALLS}
    minutes._call_openai = fake_cloud
    config.save({"api_base_url": "https://api.example.com/v1",
                 "api_keys": {"docs": "test-key-подлиннее"}})
    minutes._model_for = lambda provider, role="strong": "test-model"
    body = dict(retry.get("body") or {}, engine="api")
    r = cli.post(retry.get("url"), headers=ORIGIN, json=body)
    j2 = wait(r.json()["job_id"])
    check("повтор через облако собрал протокол", j2.get("status") == "done", (j2.get("status"), j2.get("note")))
    check("текст ушёл в облачный движок ровно один раз", len(sent) == 1, sent)
    md = (store.rec_dir(rid) / "minutes.md").read_text(encoding="utf-8") if (store.rec_dir(rid) / "minutes.md").exists() else ""
    check("документ помечен движком api", "движок: api" in md, md[-80:])
    check("общая настройка движка не поменялась", config.get("minutes_engine") == engine_before,
          config.get("minutes_engine"))

    # Саммари видео — тот же путь
    SCENARIO["name"] = "limit"
    store.update(rid, {"source": "file", "video_kind": "meeting"})
    r = cli.post("/api/media/%s/summary" % rid, headers=ORIGIN, json={})
    j3 = wait(r.json()["job_id"])
    check("саммари упёрлось в лимит", j3.get("error_kind") == "claude_limit", j3.get("note"))
    check("у саммари путь повтора", (j3.get("retry") or {}).get("url") == "/api/media/%s/summary" % rid,
          j3.get("retry"))
    r = cli.post("/api/media/%s/summary" % rid, headers=ORIGIN, json={"engine": "bogus"})
    check("неизвестный движок отвергнут", r.status_code == 400, r.status_code)

    note = (store.get(rid) or {}).get("vault_path")
    if note:
        Path(note).unlink(missing_ok=True)
    cli.delete("/api/recordings/%s?scope=all" % rid, headers=ORIGIN)

server.hub.publish = real_publish
minutes._claude_args = _real_args
FAKE.unlink(missing_ok=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t44_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
