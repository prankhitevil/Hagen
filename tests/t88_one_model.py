# -*- coding: utf-8 -*-
"""Проверка 88: выбор моделей распознавания параметрами (решение 21.09).

В «Настройки → Модели» человек выбирает: одна модель на всё или две; какая
для звонков и какая для голосового ввода; движок точной модели (onnx-asr или
torch) и веса onnx-asr (сжатые или полные). Программа сама считает, что
скачать, что не нужно, и умеет «Сбросить всё».

Что проверяем — распознаватели подставные, поиск речи и дорожки эфира настоящие:
  1. Умолчания — рекомендованное (одна точная, onnx-asr, полные веса); перенос
     прежнего выбора стенда (asr_preset) и установки без стенда («как было»).
  2. Роли: звонки, голосовой ввод и файлы идут своими движками; torch — с
     закреплёнными потоками; выбор держится до перезапуска.
  3. Замена недостающего движка первым готовым, причина видна; отказ точной
     посреди записи — фраза распознана быстрой.
  4. Сторож простоя и черновик; «Перечитать точнее» — только при быстрой в
     звонках.
  5. Запись на настоящей речи: время слов, замены словаря, сводка в карточке.
  6. Разметка говорящих делит реплику эфира по словам.
  7. Части моделей НА ВРЕМЕННЫХ ПАПКАХ: чего не хватает, что не нужно,
     «Оставить», «Удалить» (занятое — после перезапуска, пока чего-то не
     хватает — отказ), общие файлы onnx-asr, удаление не выходит за папки
     моделей, отложенное удаление при запуске, «Сбросить всё».
  8. Диктовка — моделью голосового ввода; --prepare; время слов onnx-asr.
  9. Страница и служба: поля во вкладке «Модели», маршруты /api/asr/models*.

Настоящие settings.json, база голосов и папки моделей не трогаются.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t88_one_model.py
"""
import io
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

import numpy as np  # noqa: E402

from hagen import asr, audio_io, config, live, needs, store  # noqa: E402

LINES = []
FAIL = []
SR = asr.SR


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


# ------------------------------------------------ подставные распознаватели
calls = []
ready_state = {"fast": True, "torch": True, "ox_int8": True, "ox_fp32": True}
state = {"fails": False}
WORDS = ["смета", "подрядчик", "срок"]


def _words_result(pcm):
    dur = len(pcm) / float(SR)
    step = dur / len(WORDS)
    words = [asr.Word(w, i * step + 0.05, (i + 1) * step - 0.05) for i, w in enumerate(WORDS)]
    return asr.Result(" ".join(WORDS), words)


def fake_onnx(pcm, name):
    calls.append(("fast", name, None))
    return asr.Result("быстрая модель")


def fake_torch(pcm, name, word_timestamps=True, threads=0):
    calls.append(("torch", name, threads))
    if state["fails"]:
        raise RuntimeError("не хватило памяти")
    return _words_result(pcm) if word_timestamps else asr.Result("точная без слов")


def fake_ox(pcm, quant):
    calls.append(("ox", quant, None))
    if state["fails"]:
        raise RuntimeError("не хватило памяти")
    return _words_result(pcm)


def fake_ready(engine):
    return (True, "") if ready_state[engine] else (False, "файлов нет")


REAL = {"onnx": asr._transcribe_onnx, "torch": asr._transcribe_torch, "ox": asr._transcribe_ox,
        "engine_ready": asr.engine_ready, "export": asr.export_onnx, "install": needs.install,
        "dirs": (asr.CKPT_DIR, asr.ONNX_DIR, asr.EN_DIR, asr.OX_DIR)}
asr._transcribe_onnx = fake_onnx
asr._transcribe_torch = fake_torch
asr._transcribe_ox = fake_ox
asr.engine_ready = fake_ready


def pick(**choice):
    """Выбрать модели и «перезапустить программу»."""
    config.save(dict(asr.RECOMMENDED, **dict({"asr_calls": "fast", "asr_voice": "precise",
                                              "asr_reread": True}, **choice)))
    asr._live_route = None
    return asr.live_route()


speech = np.random.uniform(-0.1, 0.1, SR).astype(np.float32)
tmp_models = None

try:
    say("=== 1. Умолчания и перенос прежнего выбора ===")
    want = asr.chosen()
    check("по умолчанию — одна точная модель на onnx-asr, полные веса",
          want == {"live": "ox_fp32", "voice": "ox_fp32", "files": "ox_fp32",
                   "reread": False, "drafts": False}, want)
    check("бегущий текст по умолчанию выключен", config.DEFAULTS.get("live_draft") is False)
    check("рекомендованное = умолчания", all(config.DEFAULTS[k] == v
                                             for k, v in asr.RECOMMENDED.items()))

    real_path = config.SETTINGS_PATH
    tmpdir = Path(tempfile.mkdtemp(prefix="t88_settings_"))
    try:
        config.SETTINGS_PATH = tmpdir / "settings.json"
        cases = {"current": ("fast", "torch", "torch", True, True),
                 "current_nodraft": ("fast", "torch", "torch", True, False),
                 "one_torch": ("torch", "torch", "torch", False, False),
                 "one_torch_2": ("torch", "torch", "torch", False, False),
                 "one_ox_fp32": ("ox_fp32", "ox_fp32", "ox_fp32", False, False),
                 "one_ox_int8": ("ox_int8", "ox_int8", "ox_int8", False, False),
                 None: ("fast", "torch", "torch", True, True)}
        for preset, (lv, vc, fl, rr, dr) in cases.items():
            raw = {"live_model": "v3_e2e_ctc", "owner_name": "Я"}
            if preset:
                raw["asr_preset"] = preset
            config.SETTINGS_PATH.write_text(json.dumps(raw), encoding="utf-8")
            config._cache = None
            got = asr.chosen()
            check("перенос: %s" % (preset or "установка без стенда — «как было»"),
                  (got["live"], got["voice"], got["files"], got["reread"], got["drafts"])
                  == (lv, vc, fl, rr, dr), got)
        raw = {"live_model": "v3_e2e_ctc", "asr_preset": "current", "asr_count": 1,
               "asr_single": "precise", "asr_engine": "torch"}
        config.SETTINGS_PATH.write_text(json.dumps(raw), encoding="utf-8")
        config._cache = None
        check("новые параметры в файле главнее прежнего asr_preset",
              asr.chosen()["live"] == "torch", asr.chosen())
        # Хватает одного нового ключа: иначе заданный явно движок затирался бы
        # переносом «как было».
        raw = {"live_model": "v3_e2e_ctc", "asr_engine": "onnx_asr", "asr_weights": "fp32"}
        config.SETTINGS_PATH.write_text(json.dumps(raw), encoding="utf-8")
        config._cache = None
        got = asr.chosen()
        check("один новый ключ — переноса нет, остальное по умолчанию",
              (got["live"], got["voice"], got["files"]) == ("ox_fp32",) * 3, got)
    finally:
        config.SETTINGS_PATH = real_path
        config._cache = None
        shutil.rmtree(tmpdir, ignore_errors=True)

    say("")
    say("=== 2. Роли идут своими движками ===")
    pick()
    calls.clear()
    res = asr.transcribe_live(speech)
    asr.transcribe_precise(speech, role="voice")
    asr.transcribe_precise(speech, role="files")
    check("одна точная onnx-asr: звонки, ввод и файлы — onnx-asr, со словами",
          calls == [("ox", None, None)] * 3 and len(res.words) == 3, calls)
    pick(asr_engine="onnx_asr", asr_weights="int8")
    calls.clear()
    asr.transcribe_live(speech)
    check("сжатые веса — onnx-asr со сжатием", calls == [("ox", "int8", None)], calls)
    pick(asr_engine="torch")
    calls.clear()
    asr.transcribe_live(speech)
    asr.transcribe_precise(speech, role="voice")
    check("одна точная torch: потоки закреплены", calls == [("torch", "v3_e2e_rnnt",
                                                           asr._threads())] * 2, calls)
    pick(asr_single="fast")
    calls.clear()
    res = asr.transcribe_live(speech)
    asr.transcribe_precise(speech, role="voice")
    asr.transcribe_precise(speech, role="files")
    check("одна быстрая: всё быстрой, слов нет",
          [c[0] for c in calls] == ["fast"] * 3 and res.words == [], calls)
    check("…и файлы на быстрой — предупреждение в «Моделях»",
          any("времени слов" in n for n in needs.models_state()["notes"]))
    route = pick(asr_count=2, asr_calls="fast", asr_voice="precise", asr_engine="torch")
    calls.clear()
    asr.transcribe_live(speech)
    asr.transcribe_precise(speech, role="voice")
    asr.transcribe_precise(speech, role="files")
    check("две модели: звонки быстрой, ввод и файлы точной",
          [c[0] for c in calls] == ["fast", "torch", "torch"], calls)
    check("…и «Перечитать точнее» есть", route["reread"] is True, route)
    route = pick(asr_count=2, asr_calls="precise", asr_voice="fast", asr_engine="onnx_asr")
    calls.clear()
    asr.transcribe_live(speech)
    asr.transcribe_precise(speech, role="voice")
    asr.transcribe_precise(speech, role="files")
    check("две модели: звонки точной, ввод быстрой, файлы точной",
          [c[0] for c in calls] == ["ox", "fast", "ox"], calls)
    check("…звонки уже точные — «Перечитать точнее» нет", route["reread"] is False, route)
    config.save({"asr_count": 1, "asr_single": "fast"})
    check("выбрали другое — до перезапуска работает прежнее",
          asr.live_route()["live"] == "ox_fp32", asr.live_route())

    say("")
    say("=== 3. Замена недостающего и отказ посреди записи ===")
    ready_state["ox_fp32"] = False
    route = pick()
    check("нет полных весов — роли идут быстрой, причина названа",
          route["live"] == route["voice"] == route["files"] == "fast"
          and "файлов нет" in route["why"], route)
    st = asr.live_state()
    check("в состоянии окна видно, почему", "Не всё скачано" in (st.get("note") or ""), st)
    ready_state["fast"] = False
    route = pick()
    check("нет и быстрой — берётся torch", route["live"] == "torch", route)
    ready_state.update({"fast": True, "ox_fp32": True})
    pick()
    state["fails"] = True
    calls.clear()
    res = asr.transcribe_live(speech)
    check("точная отказала посреди записи — фраза распознана быстрой",
          res.text == "быстрая модель" and asr.live_route()["live"] == "fast", calls)
    check("в состоянии видно, почему", "не сработала" in (asr.live_state().get("note") or ""))
    calls.clear()
    asr.transcribe_live(speech)
    check("точную на каждой фразе заново не пробует", [c[0] for c in calls] == ["fast"], calls)
    state["fails"] = False

    say("")
    say("=== 4. Сторож простоя, черновик, перечитка ===")
    asr._torch_cache.clear()
    asr._torch_used.clear()
    pick(asr_engine="torch")
    for name in ("v3_e2e_rnnt", "другая"):
        asr._torch_cache[name] = object()
        asr._torch_used[name] = time.time() - 10 * 3600
    got = asr.release_idle(minutes=30)
    check("torch-модель эфира не выгружена, остальное — как раньше",
          got == ["другая"] and "v3_e2e_rnnt" in asr._torch_cache, got)
    pick(asr_count=2, asr_calls="fast", asr_engine="torch")
    asr._torch_used["v3_e2e_rnnt"] = time.time() - 10 * 3600
    got = asr.release_idle(minutes=30)
    check("звонки на быстрой — torch выгружается после простоя", got == ["v3_e2e_rnnt"], got)
    asr._torch_cache.clear()
    asr._torch_used.clear()
    check("черновик выключен, пока не включён галочкой", live.drafts_enabled() is False)
    pick(asr_count=2, asr_calls="fast", live_draft=True)
    check("галочка включает черновик у быстрой в звонках", live.drafts_enabled() is True)
    pick(live_draft=True)
    check("у точной в звонках черновика нет и с галочкой", live.drafts_enabled() is False)
    pick(asr_count=2, asr_calls="fast", asr_reread=False)
    check("«Перечитать точнее» можно выключить", asr.live_route()["reread"] is False)
    pick()
    check("одна модель — «Перечитать точнее» нет, и окно это знает",
          asr.live_state().get("reread") is False)

    say("")
    say("=== 5. Запись на настоящей речи ===")
    pcm, _sr = audio_io.read_wav(PROJECT / "tests" / "meeting.wav")
    pcm = np.asarray(pcm, dtype=np.float32).reshape(-1)
    live.DRAFT_EVERY_S = 0.0
    config.save({"replacements": [{"from": "смета", "to": "Смета-2026", "where": "both"}]})

    def record(title):
        rid = store.create(title="Проверка 88 — " + title, mode="online", source="live")["id"]
        events = []
        sess = live.LiveSession(rid, mode="online", on_event=events.append)
        step = int(0.1 * SR)
        for i in range(0, pcm.size, step):
            sess.feed(store.TRACK_FAR, pcm[i:i + step])
        sess.stop()
        segs = [e["segment"] for e in events if e.get("type") == "segment"]
        drafts = [d for e in events if e.get("type") == "draft"
                  for d in (e.get("drafts") or {}).values() if d]
        meta = store.get(rid) or {}
        store.delete(rid)
        return segs, drafts, meta

    pick()
    segs, drafts, meta = record("одна точная")
    check("реплики есть, черновиков нет", len(segs) >= 2 and not drafts, (len(segs), drafts[:2]))
    check("у каждой реплики — время слов от начала записи",
          segs and all(len(s.get("words") or []) == 3
                       and abs(s["words"][0]["start"] - (s["start"] + 0.05)) < 0.01 for s in segs))
    check("замена словаря — и в тексте, и в словах",
          all(s["text"].startswith("Смета-2026") and s["words"][0]["text"] == "Смета-2026"
              for s in segs))
    stand = meta.get("asr_stand") or {}
    check("в карточке записи — чем шёл эфир, процессор, задержка фраз",
          stand.get("title") == "Точная (onnx-asr, полные веса)" and "delay_median_s" in stand
          and "процессор" in (stand.get("text") or ""), stand)
    pick(asr_count=2, asr_calls="fast", live_draft=True)
    segs, drafts, meta = record("быстрая с черновиком")
    old_keys = set(store.make_segment(store.TRACK_FAR, 0.0, 1.0, "x")) | {"reason"}
    check("быстрая: черновик есть, у реплик прежние поля без words",
          drafts and segs and all(set(s) == old_keys for s in segs),
          [sorted(set(s) - old_keys) for s in segs])
    check("быстрая: в карточке — «Быстрая, с бегущим текстом»",
          (meta.get("asr_stand") or {}).get("title") == "Быстрая, с бегущим текстом",
          meta.get("asr_stand"))
    live.DRAFT_EVERY_S = 1.5
    config.save({"replacements": []})

    say("")
    say("=== 6. Разметка делит реплику эфира по словам ===")
    from hagen import diarize  # noqa: E402

    seg = store.make_segment(store.TRACK_FAR, 10.0, 16.0, "первая половина фразы вторая половина")
    seg["words"] = [{"text": "первая", "start": 10.1, "end": 10.9},
                    {"text": "половина", "start": 11.0, "end": 11.9},
                    {"text": "фразы", "start": 12.0, "end": 12.6},
                    {"text": "вторая", "start": 13.4, "end": 14.2},
                    {"text": "половина", "start": 14.3, "end": 15.8}]
    turns = [{"start": 9.9, "end": 12.8, "speaker": "SPEAKER_00"},
             {"start": 13.2, "end": 16.1, "speaker": "SPEAKER_01"}]
    out = diarize.relabel([seg], turns, track=store.TRACK_FAR)
    check("со словами — две реплики, по голосу на каждую",
          len(out) == 2 and out[0]["speaker_key"] != out[1]["speaker_key"],
          [(s["text"], s.get("speaker_key")) for s in out])

    say("")
    say("=== 7. Части моделей — на временных папках ===")
    asr.engine_ready = REAL["engine_ready"]      # готовность — по настоящим файлам
    tmp_models = Path(tempfile.mkdtemp(prefix="t88_models_"))
    asr.CKPT_DIR = tmp_models / "gigaam"
    asr.ONNX_DIR = tmp_models / "onnx"
    asr.EN_DIR = tmp_models / "onnx-asr"
    asr.OX_DIR = asr.EN_DIR / "gigaam-v3"
    roots = [asr.CKPT_DIR, asr.ONNX_DIR, asr.EN_DIR, asr.OX_DIR]
    if not all(str(r).startswith(str(tmp_models)) for r in roots):
        raise SystemExit("папки моделей не временные — останавливаюсь, ничего не трогаю")
    for r in roots:
        r.mkdir(parents=True, exist_ok=True)

    def put(key):
        for p in needs.part_files(key):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 2**20)          # 1 МБ на файл

    stranger = [asr.ONNX_DIR / "чужой.onnx", asr.OX_DIR / "README.md", tmp_models / "рядом.txt"]
    for p in stranger:
        p.write_text("не трогать", encoding="utf-8")
    for key in needs.MODEL_PARTS:
        put(key)
    check("все части на месте", all(needs.on_disk(k) for k in needs.MODEL_PARTS),
          {k: needs.on_disk(k) for k in needs.MODEL_PARTS})

    pick()
    st = needs.models_state()
    check("одна точная onnx-asr: скачивать нечего", st["missing"] == [] and st["download_mb"] == 0,
          st["missing"])
    check("не нужно: быстрая, torch, сжатые веса, неиспользуемые; английская не в счёт",
          [u["key"] for u in st["unneeded"]] == ["fast", "precise", "precise_ox_int8", "unused"],
          [u["key"] for u in st["unneeded"]])
    check("вес ненужного посчитан", st["unneeded_mb"] == sum(u["disk_mb"] for u in st["unneeded"])
          and st["unneeded_mb"] > 0, st["unneeded_mb"])

    needs.keep_unneeded()
    check("«Оставить» — больше не предлагается", needs.models_state()["unneeded"] == [])
    config.save({"asr_keep_parts": []})

    asr._live_route = dict(asr.live_route(), live="fast")    # программа держит быструю
    res = needs.delete_unneeded()
    check("«Удалить»: занятое программой — после перезапуска",
          res["later"] == ["fast"] and set(res["removed"]) == {"precise", "precise_ox_int8",
                                                              "unused"}, res)
    check("файлы удалены, занятое пока лежит",
          needs.on_disk("fast") and not needs.on_disk("precise")
          and not needs.on_disk("precise_ox_int8") and not needs.on_disk("unused"))
    check("общие файлы onnx-asr остались — полные веса живы",
          needs.ready("precise_ox_fp32") and (asr.OX_DIR / "config.json").exists())
    check("чужие файлы в папках моделей и рядом целы", all(p.exists() for p in stranger))
    check("отложенное записано", config.get("asr_pending_delete") == ["fast"])

    asr._live_route = None                          # «перезапуск»
    done = needs.cleanup_pending()
    check("при запуске отложенное удалено", done == ["fast"] and not needs.on_disk("fast")
          and config.get("asr_pending_delete") == [], done)

    pick(asr_engine="torch")
    try:
        needs.delete_unneeded()
        check("пока выбору не хватает — удалять отказывается", False, "удалила")
    except needs.NeedError as err:
        check("пока выбору не хватает — удалять отказывается", "Сначала скачайте" in str(err),
              str(err))
    check("…и ничего не тронуто", needs.ready("precise_ox_fp32"))

    put("precise_ox_int8")
    needs.remove("precise_ox_fp32")
    check("удалили полные при живых сжатых — общие файлы на месте",
          not needs.on_disk("precise_ox_fp32") and (asr.OX_DIR / "config.json").exists())
    needs.remove("precise_ox_int8")
    check("удалили последний вариант — ушли и общие", not (asr.OX_DIR / "config.json").exists())

    real_files = needs.part_files
    needs.part_files = lambda key: [tmp_models / "рядом.txt"]
    try:
        needs.remove("unused")
        check("файл вне папок моделей не удаляется", False, "удалила")
    except needs.NeedError:
        check("файл вне папок моделей не удаляется", (tmp_models / "рядом.txt").exists())
    finally:
        needs.part_files = real_files

    for key in needs.MODEL_PARTS:
        put(key)
    pick(asr_count=2, asr_calls="fast", asr_engine="torch")
    res = needs.reset_models()
    check("«Сбросить всё»: остались только полные веса onnx-asr",
          [k for k in needs.MODEL_PARTS if needs.on_disk(k)] in (["precise_ox_fp32"],
                                                                 ["fast", "precise", "precise_ox_fp32"]),
          ([k for k in needs.MODEL_PARTS if needs.on_disk(k)], res))
    check("…занятое работавшей программой — после перезапуска",
          set(res["later"]) == {"fast", "precise"}, res)
    check("…и настройки рекомендованные",
          all(config.get(k) == v for k, v in asr.RECOMMENDED.items()))
    asr._live_route = None
    needs.cleanup_pending()
    check("…после перезапуска осталась только одна модель",
          [k for k in needs.MODEL_PARTS if needs.on_disk(k)] == ["precise_ox_fp32"],
          [k for k in needs.MODEL_PARTS if needs.on_disk(k)])
    for key in needs.MODEL_PARTS:
        put(key)
    needs.remove("precise_ox_int8")
    needs.remove("precise_ox_fp32")
    config.save({"asr_count": 2})

    def broken(note=None):
        raise needs.NeedError("нет интернета")

    real_install = needs.PARTS["precise_ox_fp32"]["install"]
    needs.PARTS["precise_ox_fp32"]["install"] = broken
    try:
        needs.reset_models()
        check("сброс без сети и без точной — отказ", False, "сбросила")
    except needs.NeedError:
        check("сброс без сети и без точной — отказ, ничего не тронуто",
              needs.on_disk("fast") and needs.on_disk("english") and config.get("asr_count") == 2)
    finally:
        needs.PARTS["precise_ox_fp32"]["install"] = real_install

    say("")
    say("=== 8. Диктовка, --prepare, время слов onnx-asr ===")
    asr.engine_ready = fake_ready
    from hagen import dictate  # noqa: E402

    pick(asr_count=2, asr_voice="fast")
    check("голосовой ввод — своя часть", asr.part_for("voice") == "fast"
          and asr.part_for("files") == "precise_ox_fp32")
    for key in needs.MODEL_PARTS:
        put(key)
    calls.clear()
    dictate.recognise(np.random.uniform(-0.1, 0.1, SR * 2).astype(np.float32))
    check("диктовка идёт моделью голосового ввода", [c[0] for c in calls] == ["fast"], calls)

    installed = []
    needs.install = lambda key, note=None: installed.append(key) or {"ready": True}
    for choice, want_parts in (({}, ["precise_ox_fp32"]),
                               ({"asr_count": 2, "asr_engine": "torch"}, ["fast", "precise"])):
        installed.clear()
        pick(**choice)
        ok = asr.prepare_all()
        check("--prepare готовит нужное выбору: %s" % want_parts,
              installed == want_parts and all(o for _, o in ok), (installed, ok))
    needs.install = REAL["install"]
    run_py = io.open(PROJECT / "run.py", encoding="utf-8").read()
    check("run.py --prepare зовёт asr.prepare_all", "asr.prepare_all()" in run_py)

    tight = asr._words_from_tokens([" да", "вай", " по", "ехали"], [0.0, 0.12, 2.0, 2.2], 3.0,
                                   frame_s=0.04, tight=True)
    check("слово перед паузой кончается на своём кусочке, а не после паузы",
          abs(tight[0].end - 0.16) < 1e-6, [(w.text, w.start, w.end) for w in tight])

    say("")
    say("=== 9. Страница и служба ===")
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    pane = html[html.find('id="t-minutes"'):]
    pane = pane[:pane.find('class="tabpane', 20)]
    ids = ["set-asr-count", "set-asr-single", "set-asr-calls", "set-asr-voice", "set-asr-reread",
           "set-live-draft", "set-asr-engine", "set-asr-weights", "btn-asr-download",
           "btn-asr-delete", "btn-asr-keep", "btn-asr-reset", "asr-models-state"]
    check("поля и кнопки — во вкладке «Модели»", all('id="%s"' % i in pane for i in ids),
          [i for i in ids if 'id="%s"' % i not in pane])
    check("подсказка: срабатывает после перезапуска", "после перезапуска" in pane)
    check("подсказка про память честная: «меньше», а не «не занимает»",
          "меньше памяти" in pane and "не занимает оперативку" not in pane)
    check("старого списка вариантов нет", "set-asr-preset" not in html and "asr_preset" not in js)
    check("страница берёт состояние у службы и сохраняет выбор",
          "/api/asr/models" in js and "collectAsrModels()" in js)
    check("«Перечитать точнее» прячется, когда её нет",
          "S.asr.reread === false" in js)
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    asr.engine_ready = REAL["engine_ready"]
    pick()
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        origin = {"Origin": "http://127.0.0.1:8787"}
        body = cli.get("/api/asr/models").json()
        check("служба отдаёт выбор, недостающее и ненужное",
              body.get("title", "").startswith("Одна модель") and "unneeded" in body
              and "missing" in body, str(body)[:200])
        r = cli.post("/api/asr/models/cleanup", json={"action": "keep"}, headers=origin)
        check("«Оставить» через службу", r.status_code == 200
              and r.json()["state"]["unneeded"] == [], r.text[:200])
        config.save({"asr_keep_parts": []})
        r = cli.post("/api/asr/models/cleanup", json={"action": "delete"}, headers=origin)
        check("«Удалить» через службу", r.status_code == 200
              and not needs.on_disk("precise") and needs.ready("precise_ox_fp32"), r.text[:200])
        check("старого маршрута вариантов нет", cli.get("/api/asr/presets").status_code == 404)
finally:
    asr._transcribe_onnx = REAL["onnx"]
    asr._transcribe_torch = REAL["torch"]
    asr._transcribe_ox = REAL["ox"]
    asr.engine_ready = REAL["engine_ready"]
    asr.export_onnx = REAL["export"]
    needs.install = REAL["install"]
    asr.CKPT_DIR, asr.ONNX_DIR, asr.EN_DIR, asr.OX_DIR = REAL["dirs"]
    live.DRAFT_EVERY_S = 1.5
    asr._live_route = None
    if tmp_models is not None:
        shutil.rmtree(tmp_models, ignore_errors=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t88_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
