# -*- coding: utf-8 -*-
"""Проверка 9: память на голоса — на настоящих моделях.

Сценарий: в первой записи человек подписал говорящего «Иван Петров» и
попросил запомнить голос. Во второй записи тот же голос должен подписаться
сам либо прийти подсказкой с процентом совпадения. «Нет, это другой человек»
подсказку убирает.

Логику базы голосов на подделках проверяют t52 и t53; здесь — что она
работает на настоящих отпечатках pyannote, от загрузки файла до подсказки.

Чего проверка НЕ трогает: запущенную рядом программу (служба своя, внутри
процесса) и настоящую базу голосов (она временная — поэтому и чистить её перед
проверкой не нужно). Из настоящих настроек берётся один токен Hugging Face:
без него разметка не запускается.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t9_voices.py
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402


def _hf_token() -> str:
    """Токен Hugging Face — единственное, что берётся из настоящих настроек."""
    try:
        import json

        data = json.loads((PROJECT / "settings.json").read_text(encoding="utf-8"))
        return str(data.get("hf_token") or "").strip()
    except Exception:
        return ""


isolate.voices()
isolate.settings(hf_token=_hf_token(), diarize_auto=True, call_watch_enabled=False,
                 mic_pill=False, screenshots_enabled=False)

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

from hagen import server, store, voices  # noqa: E402

server._start_dictation = lambda: None
ORIGIN = {"Origin": "http://127.0.0.1:8787"}
WAV = PROJECT / "tests" / "meeting.wav"
NAME = "Иван Петров"


def wait_queue(cli, label, limit=1800):
    """Дождаться, пока опустеет очередь: после файла служба сама размечает голоса."""
    t0 = time.time()
    last = ""
    while time.time() - t0 < limit:
        act = [j for j in cli.get("/api/jobs").json() if j["status"] in ("queued", "running")]
        if not act:
            return True
        j = act[0]
        line = "%s %s %d%%" % (j["kind"], j["status"], round((j.get("progress") or 0) * 100))
        if line != last:
            say("   [%s] %s %s" % (label, line, (j.get("note") or "")[:50]))
            last = line
        time.sleep(1.0)
    say("   [%s] не дождались очереди" % label)
    return False


def upload(cli):
    with open(WAV, "rb") as fh:
        r = cli.post("/api/media/upload", headers=ORIGIN,
                     files={"file": (WAV.name, fh, "audio/wav")})
    return r.json().get("rec_id") if r.status_code == 200 else None


def speakers_of(cli, rec_id):
    """{ключ говорящего: имя, число реплик, подсказка}."""
    d = cli.get("/api/recordings/%s" % rec_id).json()
    by: dict = {}
    for s in d.get("segments") or []:
        k = s.get("speaker_key") or "?"
        row = by.setdefault(k, {"name": s.get("speaker"), "n": 0, "sugg": s.get("suggestion")})
        row["n"] += 1
    return d.get("meta") or {}, by


made = []
try:
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        caps = (cli.get("/api/state").json().get("capabilities") or {})
        check("разметка говорящих доступна", caps.get("diarize"), caps.get("diarize_note"))
        check("база голосов пуста — она временная", voices.list_people() == [],
              [p.get("name") for p in voices.list_people()])

        say("")
        say("=== 1. Первая запись ===")
        rec1 = upload(cli)
        check("файл принят", bool(rec1))
        made.append(rec1)
        wait_queue(cli, "запись 1")
        meta1, by1 = speakers_of(cli, rec1)
        check("голоса размечены", meta1.get("diarized"), meta1.get("diarized"))
        cand = sorted(((k, v) for k, v in by1.items() if k not in ("me", "far", "file", "?")),
                      key=lambda kv: -kv[1]["n"])
        check("отдельные говорящие найдены", len(cand) >= 2, list(by1))

        if cand:
            key1 = cand[0][0]
            say("")
            say("=== 2. «%s» — %s, запомнить голос ===" % (key1, NAME))
            r = cli.post("/api/recordings/%s/speaker" % rec1, headers=ORIGIN,
                         json={"speaker_key": key1, "name": NAME,
                               "apply_all": True, "remember": True})
            check("имя принято", r.status_code == 200, r.text[:200])
            person = next((p for p in voices.list_people() if p.get("name") == NAME), None)
            check("голос в базе", bool(person and person.get("has_voice")), person)

            say("")
            say("=== 3. Вторая запись, тот же звук ===")
            rec2 = upload(cli)
            made.append(rec2)
            wait_queue(cli, "запись 2")
            _meta2, by2 = speakers_of(cli, rec2)
            auto = [k for k, v in by2.items() if v["name"] == NAME]
            sugg = [k for k, v in by2.items()
                    if (v.get("sugg") or {}).get("name") == NAME and v["name"] != NAME]
            for k, v in by2.items():
                mark = ("  <- подписан сам" if k in auto else
                        "  <- подсказка %d%%" % round((v["sugg"] or {}).get("score", 0) * 100)
                        if k in sugg else "")
                say("      %-12s %-14s реплик %-3d%s" % (k, v["name"], v["n"], mark))
            check("тот же голос узнан: подписан сам или предложен", bool(auto or sugg),
                  {k: (v["name"], v.get("sugg")) for k, v in by2.items()})
            check("узнан ровно один говорящий, а не все подряд", len(auto) + len(sugg) == 1,
                  auto + sugg)

            # На одном и том же звуке сходство почти полное, и голос подписывается
            # уверенно. Чтобы проверить подсказку и отказ от неё, поднимаем порог
            # уверенности выше единицы: подписать сам голос теперь не может,
            # остаётся только предложить.
            say("")
            say("=== 4. Подсказка вместо подписи и «Нет, это другой человек» ===")
            from hagen import config  # noqa: E402

            config.save({"voice_match_threshold": 1.01})
            rec3 = upload(cli)
            made.append(rec3)
            wait_queue(cli, "запись 3")
            _meta3, by3 = speakers_of(cli, rec3)
            sugg3 = [k for k, v in by3.items() if (v.get("sugg") or {}).get("name") == NAME]
            named3 = [k for k, v in by3.items() if v["name"] == NAME]
            check("при высоком пороге голос не подписан молча", not named3, named3)
            check("а предложен подсказкой с процентом", len(sugg3) == 1,
                  {k: v.get("sugg") for k, v in by3.items()})
            if sugg3:
                score = (by3[sugg3[0]].get("sugg") or {}).get("score", 0)
                say("   подсказка: %s, %d%%" % (NAME, round(score * 100)))
                r = cli.post("/api/recordings/%s/speaker/reject" % rec3, headers=ORIGIN,
                             json={"speaker_key": sugg3[0]})
                check("отказ принят", r.status_code == 200, r.text[:200])
                _m, by4 = speakers_of(cli, rec3)
                check("подсказка убрана", not (by4.get(sugg3[0]) or {}).get("sugg"),
                      (by4.get(sugg3[0]) or {}).get("sugg"))
                check("и имя не подставилось", (by4.get(sugg3[0]) or {}).get("name") != NAME,
                      (by4.get(sugg3[0]) or {}).get("name"))

        say("")
        say("=== 5. Уборка ===")
        for rid in [m for m in made if m]:
            r = cli.delete("/api/recordings/%s?scope=all" % rid, headers=ORIGIN)
            check("запись %s удалена" % rid, r.status_code == 200, r.text[:120])
        made = []
except Exception:
    import traceback

    check("путь без сбоев", False, traceback.format_exc()[-600:])
finally:
    for rid in [m for m in made if m]:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t9_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
