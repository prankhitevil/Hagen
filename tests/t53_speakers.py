# -*- coding: utf-8 -*-
"""Проверка 53: подписать говорящего, имена из Teams, несколько человек за устройством.

Решения 13.09.2026:
  * при подписи — проверка конфликтов с понятным выбором, молча ничего не решается:
    похожее имя, тот же человек у другого говорящего, голос похож на другого,
    у человека в базе другой голос (по умолчанию голос не запоминается);
  * переподписали говорящего — образец голоса уходит от прежнего человека;
  * имена из расшифровки Teams — сразу в базу; голоса учатся по звуку;
  * одно имя — несколько разных голосов (переговорка, общий компьютер):
    голос не учится, предлагается «Разделить голоса»;
  * «Разделить голоса» у любого говорящего, в том числе у «Я» («со мной в
    комнате были ещё люди»): реплики распознаются заново, голоса размечаются.

Нейросети подменены (разметка голосов, распознавание): проверяется логика.
База голосов — временный файл; записи создаются и удаляются проверкой.
"""
import io
import shutil
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from harness import LINES, FAIL, say, check, finish  # noqa: E402

MADE = []


from hagen import asr, audio_io, config, diarize, diarize_jobs, jobs, speakers, store, voices  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t53_"))
REAL_VOICES = voices.VOICES_PATH
voices.VOICES_PATH = TMP / "voices.json"
voices.reload()
OVR = {"voice_match_threshold": 0.7, "voice_suggest_threshold": 0.45, "voice_match_margin": 0.1,
       "owner_name": "Иван П.", "diarize_auto": True,
       # Разметка подменена в этом процессе, а помощник разметки — отдельная
       # программа: подмена туда не доходит. Считаем в самой программе.
       "processing_during_recording": "pause"}
_real_get = config.get
config.get = lambda k, d=None: OVR[k] if k in OVR else _real_get(k, d)
real_diarize, real_transcribe = diarize.diarize_pcm, asr.transcribe_spans

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 53"):
        store.delete(old["id"])

rng = np.random.default_rng(53)


def voice(base=None, noise=0.03):
    v = rng.normal(size=256) if base is None else np.asarray(base) + rng.normal(size=256) * noise
    return (v / np.linalg.norm(v) * 3.2).tolist()


IVAN, MARIA, ROOM_A, ROOM_B, OWNER, NEIGHBOUR = (voice() for _ in range(6))


def record(title, segs, embeddings=None, track="far", audio_s=None, **meta):
    rid = store.create(title=title, mode="online", source=meta.pop("source", "live"), category="Встречи")["id"]
    MADE.append(rid)
    store.replace_segments(rid, [store.make_segment(track if len(s) < 5 else s[4], s[0], s[1], s[2],
                                                    speaker=s[3][1], speaker_key=s[3][0]) for s in segs])
    if embeddings is not None:
        diarize.save_result(rid, {"turns": [], "embeddings": embeddings, "labels": list(embeddings)})
    if audio_s:
        audio_io.write_wav(store.track_path(rid, track), (rng.normal(size=int(audio_s * 16000)) * 0.05)
                           .astype(np.float32))
    if meta:
        store.update(rid, meta)
    return rid


def wait_job(job_id, limit=60.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        job = jobs.get(job_id)
        if job and job.get("status") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    return jobs.get(job_id) or {"status": "timeout"}


from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}

try:
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        def name_it(rid, key, name, decision=None, remember=True):
            return cli.post("/api/recordings/%s/speaker" % rid, headers=ORIGIN,
                            json={"speaker_key": key, "name": name, "decision": decision or {},
                                  "remember": remember})

        def speaker_names(rid):
            return {s["speaker_key"]: s["speaker"] for s in store.sorted_segments(rid)}

        say("=== 1. Новый человек и голос с происхождением ===")
        r1 = record("Проверка 53 — первая", [(0, 5, "Привет.", ("SPEAKER_00", "Спикер 1")),
                                              (6, 9, "Здравствуйте.", ("SPEAKER_01", "Спикер 2"))],
                    embeddings={"SPEAKER_00": IVAN, "SPEAKER_01": MARIA})
        r = name_it(r1, "SPEAKER_00", "Иван Петров")
        check("подписан без вопросов", r.status_code == 200 and r.json()["voice_saved"], r.text[:200])
        ivan = voices.resolve("Иван Петров")
        check("в базе с голосом", ivan and ivan["has_voice"])
        check("образец знает запись и говорящего", voices.samples_from(r1, "SPEAKER_00")[0]["id"] == ivan["id"])
        r = name_it(r1, "SPEAKER_01", "Мария Сидорова")
        maria = voices.resolve("Мария Сидорова")

        say("")
        say("=== 2. Порядок слов — тот же человек ===")
        r2 = record("Проверка 53 — вторая", [(0, 5, "Добрый день.", ("SPEAKER_00", "Спикер 1")),
                                              (6, 9, "Коллеги.", ("SPEAKER_01", "Спикер 2")),
                                              (10, 12, "Да.", ("SPEAKER_02", "Спикер 3"))],
                    embeddings={"SPEAKER_00": voice(IVAN), "SPEAKER_01": voice(MARIA), "SPEAKER_02": voice()})
        r = name_it(r2, "SPEAKER_00", "петров иван")
        check("«петров иван» — без вопроса, к Ивану Петрову",
              r.status_code == 200 and r.json()["person"]["id"] == ivan["id"], r.text[:200])
        check("подпись — каноническим именем", speaker_names(r2)["SPEAKER_00"] == "Иван Петров")
        check("голос добавился", voices.get_person(ivan["id"])["count"] == 2)

        say("")
        say("=== 3. Похожее имя — вопрос «это он?» ===")
        r = name_it(r2, "SPEAKER_01", "Sidorova Maria")
        c = r.json().get("conflict") or {}
        check("латиница — 409 similar_name", r.status_code == 409 and c.get("kind") == "similar_name", r.text[:200])
        check("кнопки: «это Мария» и «другой человек»",
              [x["id"] for x in c.get("choices") or []] == ["alias:%s" % maria["id"], "new"], c.get("choices"))
        r = name_it(r2, "SPEAKER_01", "Sidorova Maria", {"alias_of": maria["id"]})
        check("«это она» — вариант имени запомнен", r.status_code == 200
              and "Sidorova Maria" in voices.get_person(maria["id"])["aliases"], r.text[:200])

        say("")
        say("=== 4. У человека в базе другой голос ===")
        r = name_it(r2, "SPEAKER_02", "Иван Петров")
        c = r.json().get("conflict") or {}
        check("409 voice_differs", r.status_code == 409 and c.get("kind") == "voice_differs", r.text[:300])
        check("по умолчанию — не запоминать", [x["id"] for x in c.get("choices") or [] if x.get("primary")] == ["no"])
        # сначала «тот же человек у другого говорящего»: Иван уже SPEAKER_00 этой записи
        r = name_it(r2, "SPEAKER_02", "Иван Петров", {"voice": "no"})
        c = r.json().get("conflict") or {}
        check("тот же человек у другого говорящего — 409 same_in_record",
              r.status_code == 409 and c.get("kind") == "same_in_record", r.text[:300])
        r = name_it(r2, "SPEAKER_02", "Иван Петров", {"voice": "no", "namesake": True})
        check("тёзка — отдельный человек «Иван Петров 2»", r.status_code == 200
              and r.json()["person"]["name"] == "Иван Петров 2" and not r.json()["voice_saved"], r.text[:300])

        say("")
        say("=== 5. Голос похож на другого человека ===")
        r3 = record("Проверка 53 — третья", [(0, 5, "Слушаю.", ("SPEAKER_00", "Спикер 1")),
                                              (6, 9, "Итак.", ("SPEAKER_01", "Спикер 2"))],
                    embeddings={"SPEAKER_00": voice(MARIA), "SPEAKER_01": voice(IVAN)})
        r = name_it(r3, "SPEAKER_00", "Иван Петров")
        c = r.json().get("conflict") or {}
        check("409 voice_is_other с Марией", r.status_code == 409 and c.get("kind") == "voice_is_other"
              and (c.get("other") or {}).get("person_id") == maria["id"], r.text[:300])
        r = name_it(r3, "SPEAKER_00", "Иван Петров", {"voice": "use:%s" % maria["id"]})
        check("«это Мария» — подписано Марией", r.status_code == 200
              and speaker_names(r3)["SPEAKER_00"] == "Мария Сидорова", r.text[:200])

        say("")
        say("=== 6. Переподписали — голос уходит от прежнего ===")
        before = voices.get_person(maria["id"])["count"]
        r = name_it(r3, "SPEAKER_00", "Ольга Новикова", {"voice": "keep"})
        check("подписано новым именем", r.status_code == 200 and speaker_names(r3)["SPEAKER_00"] == "Ольга Новикова",
              r.text[:300])
        check("у Марии этот образец убран", voices.get_person(maria["id"])["count"] == before - 1
              and "Мария Сидорова" in r.json()["removed_from"], r.json().get("removed_from"))

        say("")
        say("=== 7. Один человек разметкой разделён надвое — объединить ===")
        r = name_it(r3, "SPEAKER_01", "Мария Сидорова", {"voice": "no"})
        r4 = record("Проверка 53 — четвёртая", [(0, 5, "Раз.", ("SPEAKER_00", "Спикер 1")),
                                                 (6, 9, "Два.", ("SPEAKER_01", "Спикер 2"))],
                    embeddings={"SPEAKER_00": voice(IVAN), "SPEAKER_01": voice(IVAN)})
        name_it(r4, "SPEAKER_00", "Иван Петров")
        r = name_it(r4, "SPEAKER_01", "Иван Петров")
        c = r.json().get("conflict") or {}
        check("409 same_in_record, «объединить» — главная кнопка", r.status_code == 409
              and c.get("kind") == "same_in_record" and c["choices"][0]["id"] == "merge", r.text[:300])
        r = name_it(r4, "SPEAKER_01", "Иван Петров", {"merge": True})
        check("объединено: у записи один говорящий", r.status_code == 200
              and set(speaker_names(r4)) == {"SPEAKER_00"}, speaker_names(r4))

        say("")
        say("=== 8. Поиск для поля имени ===")
        s = cli.get("/api/recordings/%s/speaker/search" % r2, params={"key": "SPEAKER_01", "q": "сид"}).json()
        check("по части фамилии", [p["name"] for p in s["people"]] == ["Мария Сидорова"], s["people"])
        s = cli.get("/api/recordings/%s/speaker/search" % r2, params={"key": "SPEAKER_01", "q": ""}).json()
        check("без текста сверху — похожая по голосу", s["people"][0]["name"] == "Мария Сидорова"
              and s["people"][0]["voice"] > 0.9, [(p["name"], p["voice"]) for p in s["people"][:3]])

        say("")
        say("=== 9. Общее устройство ===")
        r5 = record("Проверка 53 — пятая", [(0, 5, "Мы из переговорки.", ("SPEAKER_00", "Спикер 1"))],
                    embeddings={"SPEAKER_00": voice()})
        r = name_it(r5, "SPEAKER_00", "Переговорная 3")
        check("подписано, голос не запомнен", r.status_code == 200 and not r.json()["voice_saved"]
              and voices.resolve("Переговорная 3")["kind"] == "shared", r.text[:200])

        say("")
        say("=== 10. Имена из Teams и обучение голосам ===")
        segs = []
        t = 0.0
        for i in range(12):                       # Иван: 12 реплик по 4 с = 48 с
            segs.append((t, t + 4, "Иван говорит %d." % i, ("sub1", "Петров Иван"), "file"))
            t += 5
        for i in range(12):                       # переговорка: два голоса попеременно
            segs.append((t, t + 4, "Переговорка %d." % i, ("sub2", "Переговорная 3"), "file"))
            t += 5
        segs.append((t, t + 3, "Коротко.", ("sub3", "Мария Сидорова"), "file"))
        t += 4
        rt = record("Проверка 53 — Teams", segs, track="file", audio_s=t + 2, source="file",
                    transcript_source="subs", store_media="none")
        sp = {"sub1": {"name": "Петров Иван", "confirmed": True}, "sub2": {"name": "Переговорная 3", "confirmed": True},
              "sub3": {"name": "Мария Сидорова", "confirmed": True}}
        store.update(rt, {"speakers": sp, "diarize_status": "subs", "diarized": True})
        speakers.link_names(rt)
        linked = (store.get(rt) or {})["speakers"]
        check("имена связаны с базой (порядок слов не важен)", linked["sub1"]["person_id"] == ivan["id"]
              and linked["sub3"]["person_id"] == maria["id"], linked)
        new_teams = speakers.link_names(rt)

        def fake_diarize_full(pcm, sr=16000, **kw):
            turns = []
            for s in store.sorted_segments(rt):
                k, a, b = s["speaker_key"], s["start"], s["end"]
                if k == "sub1":
                    turns.append({"start": a, "end": b, "speaker": "A"})
                elif k == "sub2":
                    turns.append({"start": a, "end": b, "speaker": "B" if int(a) % 10 == 0 else "C"})
                else:
                    turns.append({"start": a, "end": b, "speaker": "D"})
            return {"turns": turns, "exclusive_turns": turns, "labels": ["A", "B", "C", "D"],
                    "embeddings": {"A": voice(IVAN), "B": ROOM_A, "C": ROOM_B, "D": voice(MARIA)},
                    "duration_s": pcm.shape[0] / 16000.0, "rtf": 0.1}

        diarize.diarize_pcm = fake_diarize_full
        before = voices.get_person(ivan["id"])["count"]
        job = wait_job(diarize_jobs.queue_diarize(rt))
        diarize.diarize_pcm = real_diarize
        meta = store.get(rt) or {}
        rep = {x["key"]: x["status"] for x in meta.get("voices_learned") or []}
        check("разметка по записи Teams прошла", job.get("status") == "done", job.get("error"))
        check("голос Ивана выучен из расшифровки", rep.get("sub1") == "added"
              and voices.get_person(ivan["id"])["count"] == before + 1, rep)
        check("под «Переговорная 3» два голоса — не учим, предлагаем разделить",
              rep.get("sub2") == "multi" and meta["speakers"]["sub2"].get("multi_voice", {}).get("voices") == 2, rep)
        check("у Марии речи мало — голос не взят", rep.get("sub3") == "little", rep)
        check("реплики Teams не переподписаны", speaker_names(rt)["sub1"] == "Петров Иван")
        check("звук оставлен, пока не решено про разделение",
              store.track_path(rt, "file").exists() and not meta.get("media_removed"))

        say("")
        say("=== 11. Разделить голоса переговорки ===")
        calls = {"asr": 0}

        def fake_transcribe(pcm, spans, precise=True, words=True, progress=None, lang="ru"):
            calls["asr"] += 1
            n = pcm.shape[0] / 16000.0
            return [{"start": 0.0, "end": n, "text": "распознано заново",
                     "words": [{"text": "распознано", "start": 0.1, "end": n / 2},
                               {"text": "заново", "start": n / 2, "end": n - 0.1}]}]

        def fake_diarize_part(pcm, sr=16000, **kw):
            turns = []
            for s in store.sorted_segments(rt):
                if s["speaker_key"] == "sub2":
                    lab = "X" if int(s["start"]) % 10 == 0 else "Y"
                    turns.append({"start": s["start"], "end": s["end"], "speaker": lab})
            return {"turns": turns, "exclusive_turns": turns, "labels": ["X", "Y"],
                    "embeddings": {"X": voice(ROOM_A), "Y": voice(MARIA)}}

        diarize.diarize_pcm, asr.transcribe_spans = fake_diarize_part, fake_transcribe
        r = cli.post("/api/recordings/%s/speaker/split" % rt, headers=ORIGIN, json={"speaker_key": "sub2"})
        job = wait_job(r.json().get("job_id"))
        diarize.diarize_pcm, asr.transcribe_spans = real_diarize, real_transcribe
        names = speaker_names(rt)
        check("разделение прошло", job.get("status") == "done", job.get("error"))
        check("реплики без слов распознаны заново", calls["asr"] == 12, calls)
        check("два голоса: «Переговорная 3 · голос 1» и узнанная по голосу Мария",
              names.get("sub2~1") == "Переговорная 3 · голос 1" and "sub2~2" in names, names)
        meta = store.get(rt) or {}
        e2 = meta["speakers"].get("sub2~2") or {}
        check("голос из базы узнан (Мария)", e2.get("name") == "Мария Сидорова"
              or (e2.get("suggestion") or {}).get("name") == "Мария Сидорова", e2)
        check("предложение разделить снято", not speakers.pending_split(meta))
        check("хранить звук не просили — после решения он убран", meta.get("media_removed") is True, meta.get("store_media"))
        r = name_it(rt, "sub2~1", "Олег Кузнецов")
        check("голос из разделения подписывается и запоминается", r.status_code == 200 and r.json()["voice_saved"],
              r.text[:200])
        r = cli.post("/api/recordings/%s/speaker/unsplit" % rt, headers=ORIGIN, json={"speaker_key": "sub2"})
        names = speaker_names(rt)
        check("отмена разделения вернула «Переговорная 3»", r.status_code == 200
              and names.get("sub2") == "Переговорная 3" and "sub2~1" not in names, names)
        check("образец отменённого голоса убран из базы", not voices.samples_from(rt, "sub2~1"))

        say("")
        say("=== 12. «Со мной в комнате были ещё люди» ===")
        owner = voices.owner_person()
        voices.add_sample(None, OWNER, person_id=owner["id"], rec_id="old", speaker_key="me")
        mic = []
        for i in range(10):
            mic.append((i * 5.0, i * 5.0 + 4, "Реплика %d." % i, ("me", "Я"), "mic"))
        rm = record("Проверка 53 — кабинет", mic, track="mic", audio_s=52)

        def fake_diarize_mic(pcm, sr=16000, **kw):
            turns = [{"start": i * 5.0, "end": i * 5.0 + 4, "speaker": "P" if i % 3 else "Q"} for i in range(10)]
            return {"turns": turns, "exclusive_turns": turns, "labels": ["P", "Q"],
                    "embeddings": {"P": voice(NEIGHBOUR), "Q": voice(OWNER)}}

        diarize.diarize_pcm, asr.transcribe_spans = fake_diarize_mic, fake_transcribe
        r = cli.post("/api/recordings/%s/room" % rm, headers=ORIGIN, json={"on": True})
        job = wait_job(r.json().get("job_id"))
        diarize.diarize_pcm, asr.transcribe_spans = real_diarize, real_transcribe
        segs = store.sorted_segments(rm)
        mine = [s for s in segs if s["speaker_key"] == "me"]
        other = [s for s in segs if s["speaker_key"] == "me~1"]
        check("разделение моей дорожки прошло", job.get("status") == "done", job.get("error"))
        check("мой голос узнан по образцу, хотя он реже", len(mine) == 4 and all(s["speaker"] == "Я" for s in mine),
              [(s["speaker_key"], s["speaker"]) for s in segs])
        check("сосед — «Рядом со мной · голос 1»", len(other) == 6 and other[0]["speaker"] == "Рядом со мной · голос 1")
        s = cli.get("/api/recordings/%s/speaker/search" % rm, params={"key": "me~1", "q": ""}).json()
        check("для голоса с микрофона окно предложит «Это я»", s["is_mic"] is True)
        r = name_it(rm, "me~1", "", {"me": True}, remember=False)
        check("«Это я» возвращает реплики владельцу", r.status_code == 200
              and all(x["speaker_key"] == "me" for x in store.sorted_segments(rm)), r.text[:200])
        r = cli.post("/api/recordings/%s/room" % rm, headers=ORIGIN, json={"on": False})
        check("переключатель выключен — вся дорожка снова «Я»", r.status_code == 200
              and {x["speaker"] for x in store.sorted_segments(rm)} == {"Я"})

        say("")
        say("=== 12б. «Мой голос»: образец решает, «меня в записи не было» (15.09) ===")

        def mic_diarize(voice_map):
            """Подставная разметка микрофона: voice_map — метка → отпечаток."""
            labs = list(voice_map)

            def fake(pcm, sr=16000, **kw):
                turns = [] if not labs else [
                    {"start": i * 5.0, "end": i * 5.0 + 4, "speaker": labs[i % len(labs)]} for i in range(10)]
                return {"turns": turns, "exclusive_turns": turns, "labels": labs,
                        "embeddings": {k: v for k, v in voice_map.items()}}
            return fake

        def run_mic(rid, fake, endpoint="room", body=None):
            diarize.diarize_pcm, asr.transcribe_spans = fake, fake_transcribe
            try:
                resp = cli.post("/api/recordings/%s/%s" % (rid, endpoint), headers=ORIGIN,
                                json=body or {"on": True})
                jid = resp.json().get("job_id")
                return resp, (wait_job(jid) if jid else {"status": "no job", "error": resp.text[:200]})
            finally:
                diarize.diarize_pcm, asr.transcribe_spans = real_diarize, real_transcribe

        def mic_speakers(rid):
            return sorted({(x["speaker_key"], x["speaker"]) for x in store.sorted_segments(rid)})

        # 1. образец есть, два голоса, ни один не владелец — «Я» никому
        m1 = record("Проверка 53 — чужие голоса", mic, track="mic", audio_s=52)
        _, job = run_mic(m1, mic_diarize({"P": voice(NEIGHBOUR), "R": voice(ROOM_B)}))
        who = mic_speakers(m1)
        check("два чужих голоса: «Я» не поставлено никому", job.get("status") == "done"
              and all(k != "me" for k, _ in who) and len({k for k, _ in who}) == 2, who)
        note = ((store.get(m1) or {}).get("splits") or {}).get("me", {}).get("owner_note", "")
        check("в пояснении сказано, что вашего голоса нет", "не поставлено никому" in note, note)

        # 2. образец есть, один голос и он чужой — видео с телефона (запись 9:14)
        m2 = record("Проверка 53 — видео с телефона", mic, track="mic", audio_s=52)
        _, job = run_mic(m2, mic_diarize({"V": voice(NEIGHBOUR)}))
        who = mic_speakers(m2)
        check("один чужой голос: все реплики — «Рядом со мной · голос 1», не «Я»",
              job.get("status") == "done" and who == [("me~1", "Рядом со мной · голос 1")], who)

        # 3. образец есть, один голос и он владелец — делить нечего, всё «Я»
        m3 = record("Проверка 53 — я один", mic, track="mic", audio_s=52)
        _, job = run_mic(m3, mic_diarize({"O": voice(OWNER)}))
        check("один голос и это вы — всё осталось «Я»", job.get("status") == "done"
              and mic_speakers(m3) == [("me", "Я")], mic_speakers(m3))

        # 4. «меня в записи не было» сильнее образца
        m4 = record("Проверка 53 — меня не было", mic, track="mic", audio_s=52)
        resp, job = run_mic(m4, mic_diarize({"O": voice(OWNER), "P": voice(NEIGHBOUR)}),
                            endpoint="owner_absent")
        who = mic_speakers(m4)
        check("«меня не было»: даже похожий на образец голос не «Я»", resp.status_code == 200
              and job.get("status") == "done" and all(k != "me" for k, _ in who), who)
        check("отметка сохранена в записи", (store.get(m4) or {}).get("owner_absent") is True)

        # 5. «меня не было», а модель голосов не услышала вовсе — один чужой голос
        m5 = record("Проверка 53 — голосов не слышно", mic, track="mic", audio_s=52)
        resp, job = run_mic(m5, mic_diarize({}), endpoint="owner_absent")
        who = mic_speakers(m5)
        check("голосов не нашлось, но вас не было — все реплики чужие",
              job.get("status") == "done" and who == [("me~1", "Рядом со мной · голос 1")], (who, job.get("error")))

        # 6. без отметки и без голосов — ничего не трогаем
        m6 = record("Проверка 53 — тишина", mic, track="mic", audio_s=52)
        _, job = run_mic(m6, mic_diarize({}))
        check("голосов не нашлось и отметки нет — всё осталось «Я»", mic_speakers(m6) == [("me", "Я")],
              mic_speakers(m6))

        # 7. снять «меня не было» — все реплики снова ваши
        r = cli.post("/api/recordings/%s/owner_absent" % m4, headers=ORIGIN, json={"on": False})
        check("сняли отметку — вся дорожка снова «Я», отметка снята", r.status_code == 200
              and mic_speakers(m4) == [("me", "Я")] and not (store.get(m4) or {}).get("owner_absent"),
              mic_speakers(m4))

        # 8. образца нет — прежнее правило «самый частый голос — ваш»
        voices.remove_samples_from("old", "me")
        m8 = record("Проверка 53 — без образца", mic, track="mic", audio_s=52)
        _, job = run_mic(m8, mic_diarize({"P": voice(NEIGHBOUR), "R": voice(ROOM_B)}))
        mine8 = [x for x in store.sorted_segments(m8) if x["speaker_key"] == "me"]
        check("образца нет — «Я» у самого частого голоса, как раньше",
              job.get("status") == "done" and len(mine8) == 5, mic_speakers(m8))

        # 9. догадку «самый частый голос — ваш» можно подтвердить кнопкой (15.09)
        sm = ((store.get(m8) or {}).get("splits") or {}).get("me") or {}
        check("догадка отмечена — в плашке будет кнопка «Да, это мой голос»", sm.get("owner_guess") is True, sm)
        cand = ((diarize.load_result(m8) or {}).get("key_embeddings") or {}).get(speakers.OWNER_CANDIDATE)
        check("отпечаток угаданного голоса сохранён в разметке записи, но не в базе",
              bool(cand) and not voices.raw_people().get(owner["id"], {}).get("samples"))
        check("угаданный голос не ищется среди людей как отдельный говорящий",
              speakers.OWNER_CANDIDATE not in ((store.get(m8) or {}).get("speakers") or {}))
        r = cli.post("/api/recordings/%s/owner_voice" % m8, headers=ORIGIN)
        rec_owner = voices.raw_people().get(owner["id"]) or {}
        check("«Да, это мой голос» — образец владельца сохранён", r.status_code == 200
              and r.json().get("voice_saved") is True and len(rec_owner.get("samples") or []) == 1, r.text[:200])
        sm = ((store.get(m8) or {}).get("splits") or {}).get("me") or {}
        check("после подтверждения кнопка уходит, в плашке «ваш голос запомнен»",
              sm.get("owner_guess") is False and "запомнен" in sm.get("owner_note", ""), sm)
        r = cli.post("/api/recordings/%s/owner_voice" % m8, headers=ORIGIN)
        check("второй раз подтверждать нечего — понятный отказ", r.status_code == 400, r.text[:200])
        r = cli.post("/api/recordings/%s/owner_voice" % m1, headers=ORIGIN)
        check("в записи без догадки подтверждать нечего", r.status_code == 400, r.text[:200])
        # тот же голос в новом разделении — уже по образцу, не по догадке
        cli.post("/api/recordings/%s/room" % m8, headers=ORIGIN, json={"on": False})
        _, job = run_mic(m8, mic_diarize({"P": voice(NEIGHBOUR), "R": voice(ROOM_B)}))
        sm = ((store.get(m8) or {}).get("splits") or {}).get("me") or {}
        check("повторное разделение: «Я» узнано по запомненному образцу",
              job.get("status") == "done" and "по образцу" in sm.get("owner_note", "")
              and sm.get("owner_guess") is False, sm.get("owner_note"))
        check("образец из подтверждения пережил повторное разделение",
              len((voices.raw_people().get(owner["id"]) or {}).get("samples") or []) == 1)
        voices.remove_samples_from(m8, "me")
        voices.add_sample(None, OWNER, person_id=owner["id"], rec_id="old", speaker_key="me")

        # 10. «Перечитать точнее» не теряет разделение микрофона (15.09)
        def fake_reread(pcm, spans, precise=True, words=True, progress=None, lang="ru"):
            return [{"start": i * 5.0, "end": i * 5.0 + 4, "text": "перечитано %d" % i,
                     "words": [{"text": "перечитано", "start": i * 5.0 + 0.1, "end": i * 5.0 + 2},
                               {"text": str(i), "start": i * 5.0 + 2, "end": i * 5.0 + 3.9}]} for i in range(10)]

        def reread(rid, voice_map):
            diarize.diarize_pcm, asr.transcribe_spans = mic_diarize(voice_map), fake_reread
            try:
                resp = cli.post("/api/recordings/%s/retranscribe" % rid, headers=ORIGIN, json={})
                job = wait_job(resp.json().get("job_id"))
                t0 = time.time()
                while time.time() - t0 < 60:
                    busy = jobs.busy_with("split", rid) or jobs.busy_with("diarize", rid)
                    if not busy and ((store.get(rid) or {}).get("splits") or {}).get("me"):
                        break
                    time.sleep(0.1)
                return job
            finally:
                diarize.diarize_pcm, asr.transcribe_spans = real_diarize, real_transcribe

        m10 = record("Проверка 53 — перечитать с разделением", mic, track="mic", audio_s=52)
        run_mic(m10, mic_diarize({"Q": voice(OWNER), "P": voice(NEIGHBOUR)}))
        check("до перечитывания микрофон разделён", bool(((store.get(m10) or {}).get("splits") or {}).get("me")))
        job = reread(m10, {"Q": voice(OWNER), "P": voice(NEIGHBOUR)})
        after = store.get(m10) or {}
        keys = {x["speaker_key"] for x in store.sorted_segments(m10)}
        check("перечитано", job.get("status") == "done" and (job.get("result") or {}).get("mic_resplit") is True,
              (job.get("status"), job.get("result"), job.get("error")))
        check("после «Перечитать точнее» микрофон снова разделён: «Я» и сосед",
              "me" in keys and "me~1" in keys and after.get("room_shared") is True, (keys, after.get("room_shared")))
        check("«Я» после перечитывания — по образцу",
              "по образцу" in ((after.get("splits") or {}).get("me") or {}).get("owner_note", ""),
              ((after.get("splits") or {}).get("me") or {}).get("owner_note"))

        m11 = record("Проверка 53 — перечитать, меня не было", mic, track="mic", audio_s=52)
        run_mic(m11, mic_diarize({"Q": voice(OWNER), "P": voice(NEIGHBOUR)}), endpoint="owner_absent")
        job = reread(m11, {"Q": voice(OWNER), "P": voice(NEIGHBOUR)})
        after = store.get(m11) or {}
        keys = {x["speaker_key"] for x in store.sorted_segments(m11)}
        check("после перечитывания «меня не было» в силе: «Я» никому",
              job.get("status") == "done" and after.get("owner_absent") is True and "me" not in keys, keys)

        m12 = record("Проверка 53 — перечитать без разделения", mic, track="mic", audio_s=52)
        job = reread(m12, {"Q": voice(OWNER), "P": voice(NEIGHBOUR)})
        check("микрофон не был разделён — после перечитывания и не делится",
              (job.get("result") or {}).get("mic_resplit") is False
              and {x["speaker_key"] for x in store.sorted_segments(m12)} == {"me"},
              {x["speaker_key"] for x in store.sorted_segments(m12)})
        # 11. «Добавить образец» узнанным неуверенно (ниже 80 %): собеседникам и «Я» (15.09)
        def blend(base, w):
            """Отпечаток с похожестью примерно w на base."""
            a = np.asarray(base, dtype=float)
            a = a / np.linalg.norm(a)
            o = rng.normal(size=256)
            o = o - o.dot(a) * a
            o = o / np.linalg.norm(o)
            v = w * a + np.sqrt(max(0.0, 1 - w * w)) * o
            return (v * 3.2).tolist()

        ivan_before = voices.get_person(ivan["id"])["count"]
        # отдельный человек для «узнан уверенно»: одного человека программа
        # отдаёт только одному говорящему записи
        OLGA = voice()
        olga = voices.add_sample("Ольга Проверкина", OLGA, origin="manual")
        f1 = record("Проверка 53 — узнан неуверенно", [(0, 5, "Здравствуйте.", ("SPEAKER_00", "Спикер 1")),
                                                        (6, 9, "Коллеги.", ("SPEAKER_01", "Спикер 2"))])
        voices.apply_to_meta(f1, {"SPEAKER_00": blend(IVAN, 0.74), "SPEAKER_01": voice(OLGA)})
        sp = (store.get(f1) or {}).get("speakers") or {}
        e0, e1 = sp.get("SPEAKER_00") or {}, sp.get("SPEAKER_01") or {}
        check("узнан неуверенно (~74 %) — имя встало и предложено добавить образец",
              e0.get("name") == "Иван Петров" and e0.get("sample_offer") is True, e0)
        check("узнан уверенно (~99 %) — добавлять не предлагается",
              e1.get("name") == "Ольга Проверкина" and e1.get("sample_offer") is False, e1)
        diarize.save_result(f1, {"turns": [], "labels": ["SPEAKER_00", "SPEAKER_01"],
                                 "embeddings": {"SPEAKER_00": blend(IVAN, 0.74), "SPEAKER_01": voice(OLGA)}})
        r = cli.post("/api/recordings/%s/speaker/add_sample" % f1, headers=ORIGIN, json={"speaker_key": "SPEAKER_00"})
        check("«Добавить образец» — у Ивана стало на образец больше", r.status_code == 200
              and voices.get_person(ivan["id"])["count"] == ivan_before + 1, r.text[:200])
        check("после добавления кнопка уходит",
              not (((store.get(f1) or {}).get("speakers") or {}).get("SPEAKER_00") or {}).get("sample_offer"))
        r = cli.post("/api/recordings/%s/speaker/add_sample" % f1, headers=ORIGIN, json={"speaker_key": "SPEAKER_00"})
        check("второй раз добавлять нечего — понятный отказ", r.status_code == 400, r.text[:200])
        r = cli.post("/api/recordings/%s/speaker/add_sample" % f1, headers=ORIGIN, json={"speaker_key": "SPEAKER_01"})
        check("уверенно узнанному образец кнопкой не добавляется", r.status_code == 400, r.text[:200])
        voices.remove_samples_from(f1, "SPEAKER_00")
        pid = (olga.get("person") or {}).get("id") or (voices.resolve("Ольга Проверкина") or {}).get("id")
        if pid:
            voices.delete_person(pid)

        f2 = record("Проверка 53 — узнан неуверенно, подписали сами", [(0, 5, "Добрый день.", ("SPEAKER_00", "Спикер 1"))])
        voices.apply_to_meta(f2, {"SPEAKER_00": blend(IVAN, 0.74)})
        diarize.save_result(f2, {"turns": [], "labels": ["SPEAKER_00"], "embeddings": {"SPEAKER_00": blend(IVAN, 0.74)}})
        name_it(f2, "SPEAKER_00", "Иван Петров", {"person_id": ivan["id"]})
        check("подписали вручную — «добавить образец» больше не предлагается",
              not (((store.get(f2) or {}).get("speakers") or {}).get("SPEAKER_00") or {}).get("sample_offer"))
        voices.remove_samples_from(f2, "SPEAKER_00")

        owner_before = len((voices.raw_people().get(owner["id"]) or {}).get("samples") or [])
        m13 = record("Проверка 53 — мой голос неуверенно", mic, track="mic", audio_s=52)
        _, job = run_mic(m13, mic_diarize({"Q": blend(OWNER, 0.62), "P": voice(NEIGHBOUR)}))
        sm = ((store.get(m13) or {}).get("splits") or {}).get("me") or {}
        check("«Я» узнано по образцу неуверенно — кнопка «Добавить и этот образец»",
              job.get("status") == "done" and "по образцу" in sm.get("owner_note", "")
              and sm.get("owner_add") is True and sm.get("owner_guess") is False, sm)
        r = cli.post("/api/recordings/%s/speaker/add_sample" % m13, headers=ORIGIN, json={"speaker_key": "me"})
        sm = ((store.get(m13) or {}).get("splits") or {}).get("me") or {}
        check("добавлен ещё один образец вашего голоса", r.status_code == 200
              and len((voices.raw_people().get(owner["id"]) or {}).get("samples") or []) == owner_before + 1
              and sm.get("owner_add") is False and "образец добавлен" in sm.get("owner_note", ""), (r.text[:120], sm))
        voices.remove_samples_from(m13, "me")
        m14 = record("Проверка 53 — мой голос уверенно", mic, track="mic", audio_s=52)
        _, job = run_mic(m14, mic_diarize({"Q": voice(OWNER), "P": voice(NEIGHBOUR)}))
        sm = ((store.get(m14) or {}).get("splits") or {}).get("me") or {}
        check("«Я» узнано уверенно (99 %) — добавлять не предлагается", sm.get("owner_add") is False, sm)


        say("")
        say("=== 13. «Настройки → Голоса» ===")
        v = cli.get("/api/voices").json()
        card = next(p for p in v["people"] if p["id"] == ivan["id"])
        check("в карточке образцы с названиями записей", any(x["rec_title"] == "Проверка 53 — первая"
                                                             for x in card["samples"]), card["samples"])
        check("«Иван Петров» и «Иван Петров 2» в возможных дублях",
              any({d["a"]["name"], d["b"]["name"]} == {"Иван Петров", "Иван Петров 2"} for d in v["duplicates"]))
        twin = voices.resolve("Иван Петров 2")
        r = cli.patch("/api/voices/%s" % twin["id"], headers=ORIGIN, json={"name": "Петров Иван"})
        check("переименование в занятое имя — 409 с тем, кем занято", r.status_code == 409
              and r.json()["other"]["id"] == ivan["id"], r.text[:200])
        r = cli.post("/api/voices/%s/merge" % twin["id"], headers=ORIGIN, json={"into": ivan["id"]})
        check("объединение из настроек", r.status_code == 200 and voices.get_person(twin["id"]) is None)
        check("запись, где был тёзка, перевязана на Ивана", speaker_names(r2).get("SPEAKER_02") == "Иван Петров",
              speaker_names(r2))
        r = cli.post("/api/voices/%s/kind" % maria["id"], headers=ORIGIN, json={"kind": "shared"})
        check("отметка «общее устройство»", r.status_code == 200 and voices.get_person(maria["id"])["kind"] == "shared")
finally:
    diarize.diarize_pcm, asr.transcribe_spans = real_diarize, real_transcribe
    config.get = _real_get
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass
    # настоящую базу не перечитываем — см. t52: чтение перевело бы её в новый формат
    voices.VOICES_PATH = REAL_VOICES
    voices._cache, voices._cache_stamp = None, None
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t53"))
