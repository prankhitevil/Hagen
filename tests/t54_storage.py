# -*- coding: utf-8 -*-
"""Проверка 54: место на диске и срок хранения звука звонков.

Решения 13.09.2026: звук хранится бессрочно, в настройках видно, где
он лежит и сколько занимает; параметр «удалять звук звонков старше N дней».
Удаляется только звук — стенограмма, документы и заметка остаются. Видеозаписи
срок не трогает (там решает «что хранить»), как и идущую запись, занятые
задачи и записи, где ждут решения «разделить голоса».
"""
import io
import os
import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()
# И настоящие настройки: проверка ждёт заводской срок хранения звука (0), а на
# машине, где срок задан, краснела не по делу.
isolate.settings()

LINES = []
FAIL = []
MADE = []


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


from hagen import audio_io, config, storage, store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 54"):
        store.delete(old["id"])

DAY = 86400
NOW = time.time()


def rec(title, source="live", age_days=0.0, tracks=("mic", "far"), **meta):
    rid = store.create(title="Проверка 54 — " + title, mode="online", source=source, category="Встречи")["id"]
    MADE.append(rid)
    store.replace_segments(rid, [store.make_segment("mic", 0, 2, "Текст остаётся.")])
    for tr in tracks:
        p = store.track_path(rid, tr)
        audio_io.write_wav(p, np.zeros(16000 * 2, dtype=np.float32))
        stamp = NOW - age_days * DAY
        os.utime(p, (stamp, stamp))
    meta.setdefault("status", "recorded")        # новая запись по умолчанию «пишется»
    store.update(rid, meta)
    return rid


OVR = {}
_real_get = config.get
config.get = lambda k, d=None: OVR[k] if k in OVR else _real_get(k, d)

try:
    say("=== 1. Что попадает под срок ===")
    old_call = rec("старый звонок", age_days=40)
    fresh_call = rec("свежий звонок", age_days=5)
    old_video = rec("старое видео", source="file", age_days=90, tracks=("file",))
    recording = rec("идёт запись", age_days=40, status="recording")
    pending = rec("ждёт разделения", age_days=40,
                  speakers={"sub1": {"name": "Переговорная", "multi_voice": {"voices": 2}}})
    busy = rec("занята задачей", age_days=40)
    due = [d["id"] for d in storage.expired(30, busy=lambda r: r == busy, now=NOW)]
    check("старый звонок — под сроком", old_call in due, due)
    check("свежий звонок — нет", fresh_call not in due)
    check("видеозапись — нет (там решает «что хранить»)", old_video not in due)
    check("идущая запись — нет", recording not in due)
    check("ждёт решения «разделить голоса» — нет", pending not in due)
    check("запись, занятая задачей, — нет", busy not in due)
    check("срок 0 — ничего не удаляется", storage.expired(0, now=NOW) == [])

    say("")
    say("=== 2. Удаление по сроку ===")
    res = storage.purge(30, busy=lambda r: r == busy, now=NOW)
    check("удалён звук только старого звонка", res["removed"] == [old_call] and res["freed_bytes"] > 0, res)
    m = store.get(old_call) or {}
    check("звук ушёл, запись помечена «звук удалён»", m.get("media_removed") is True
          and not store.track_path(old_call, "mic").exists())
    check("стенограмма осталась", [s["text"] for s in store.sorted_segments(old_call)] == ["Текст остаётся."])
    check("у свежего звонка звук на месте", store.track_path(fresh_call, "mic").exists())
    check("повторная проверка ничего не трогает", storage.purge(30, busy=lambda r: r == busy, now=NOW)["removed"] == [])

    say("")
    say("=== 3. Сколько места и где ===")
    use = storage.audio_usage()
    check("путь к звуку — папка данных программы", use["path"] == str(config.DATA_DIR))
    check("звонки и видео посчитаны отдельно", use["calls_bytes"] > 0 and use["video_audio_bytes"] > 0, use)

    say("")
    say("=== 4. Сквозь службу ===")
    from fastapi.testclient import TestClient

    from hagen import server

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        st = cli.get("/api/storage").json()
        check("по умолчанию срок 0 и под срок ничего не попадает", st["retention_days"] == 0 and st["due"]["records"] == 0,
              st.get("due"))
        OVR["audio_retention_days"] = 30
        st = cli.get("/api/storage").json()
        check("срок 30 дней: видно, сколько удалится", st["retention_days"] == 30 and st["due"]["records"] >= 2, st["due"])
        check("размеры звука и видео отданы", st["audio"]["bytes"] > 0 and "path" in st["videos"])
        r = cli.post("/api/storage/reveal", headers=ORIGIN, json={"what": "куда-нибудь"})
        check("открыть можно только свои папки", r.status_code == 400, r.status_code)
        server._busy_record = (lambda real: (lambda r: r == busy or real(r)))(server._busy_record)
        r = cli.post("/api/storage/purge", headers=ORIGIN)
        removed = r.json().get("removed") or []
        # «идёт запись» служба при запуске закрывает как прерванную — её здесь не проверяем
        check("«удалить сейчас» — ждущая разделения и занятая задачей не тронуты",
              pending not in removed and busy not in removed and fresh_call not in removed, removed)
        html = cli.get("/").text
        check("в настройках блок «Звук записей» и поле срока", 'id="set-retention"' in html and 'id="storage-audio"' in html)
finally:
    config.get = _real_get
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t54_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
