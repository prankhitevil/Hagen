# -*- coding: utf-8 -*-
"""Проверка 72: отсев эха колонок ПО ГОЛОСУ (17.09).

Сверка текстов спотыкается там, где дорожки расслышали по-разному: у
собеседника «неделя 2665», в микрофоне «недели две, 665» — звуки те же, слова
разные, и эхо проходит в стенограмму. Голос в эту ловушку не попадает.

Судим своими силами записи, без базы голосов: реплики микрофона, во время
которых собеседники молчали, — это заведомо владелец, они и дают образец его
голоса. Реплика, которая ближе к голосу собеседника, — эхо.

Отпечатки здесь подставные (модель не грузится): проверяется логика решения,
а не качество модели.

Что проверяем:
  1. реплика, похожая на собеседника, метится эхом;
  2. своя речь, попавшая на чужую фразу по времени, остаётся;
  3. без образцов своего голоса решение не принимается;
  4. запас (`echo_voice_margin`) работает: спорное не метится;
  5. решение человека (`echo_locked`) и выключенная настройка не трогаются;
  6. короткий кусок без отпечатка не метится;
  7. настоящий счёт отпечатков подключён к разметке говорящих.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t72_echo_voice.py
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
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


import numpy as np  # noqa: E402

from hagen import audio_io, config, diarize, echo, store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 72"):
        store.delete(old["id"])

SR = 16000
ME = [1.0, 0.0, 0.0]          # голос владельца
THEM = [0.0, 1.0, 0.0]        # голос собеседника
MIDDLE = [0.69, 0.72, 0.0]    # спорный: к собеседнику ближе, но чуть-чуть


def seg(track, start, end, text, **extra):
    s = store.make_segment(track, start, end, text)
    s.update(extra)
    return s


def record(title, segments, embeddings=None, seconds=60):
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    store.replace_segments(rid, segments)
    audio_io.write_wav(store.track_path(rid, store.TRACK_MIC),
                       np.zeros(SR * seconds, dtype=np.float32), SR)
    diarize.save_result(rid, {"key_embeddings": embeddings
                              if embeddings is not None else {"SPEAKER_00": THEM}})
    return rid


def fake_embed(by_start):
    """Подставной счёт отпечатков: вектор выбирается по началу куска."""
    def embed(pcm, spans, sr=SR):
        return [by_start.get(round(float(s["start"]), 1)) for s in spans]
    return embed


def echoes(rid):
    return [s["text"] for s in store.sorted_segments(rid, include_echo=True) if s.get("echo")]


try:
    config.save({"echo_filter": True, "echo_by_voice": True, "echo_voice_margin": 0.05})

    say("=== 1. Эхо по голосу и своя речь рядом ===")
    rid = record("Проверка 72 голос", [
        seg("far", 10.0, 20.0, "неделя 2665 крахмала 120 тонн уехало"),
        seg("mic", 11.0, 19.0, "недели две 665 крахмалы 120 тонн уехало"),   # эхо
        seg("mic", 12.0, 18.0, "да я как раз про это и говорю"),             # своя речь
        seg("mic", 30.0, 34.0, "Наташ подскажи по отгрузкам"),               # образец
        seg("mic", 40.0, 44.0, "и ещё по вагонам вопрос"),                   # образец
    ])
    n = echo.mark_by_voice(rid, embed_fn=fake_embed({
        30.0: ME, 40.0: ME, 11.0: THEM, 12.0: ME}))
    check("помечена одна реплика", n == 1, n)
    check("помечено именно эхо", echoes(rid) == ["недели две 665 крахмалы 120 тонн уехало"],
          echoes(rid))
    check("сказано, как узнали",
          [s.get("echo_by") for s in store.sorted_segments(rid, include_echo=True)
           if s.get("echo")] == ["голос"])
    check("своя речь поверх чужой фразы осталась",
          "да я как раз про это и говорю" in [s["text"] for s in store.sorted_segments(rid)])
    check("повторный проход ничего не добавляет",
          echo.mark_by_voice(rid, embed_fn=fake_embed({30.0: ME, 40.0: ME, 12.0: ME})) == 0)

    say("")
    say("=== 1а. Настоящий случай: «Control Unior» против «контроль у него» ===")
    # Запись 20260917-105826-9344, 02:11. Совпадение по
    # тексту 40 % — сверка текстов бессильна, решает голос.
    rid_c = record("Проверка 72 контроль", [
        seg("far", 130.5, 132.2, "Да, собственно, Control Unior."),
        seg("mic", 130.5, 132.1, "Да, собственно, контроль у него."),
        seg("mic", 150.0, 154.0, "своя реплика раз"),
        seg("mic", 160.0, 164.0, "своя реплика два"),
    ])
    n_c = echo.mark_by_voice(rid_c, embed_fn=fake_embed({
        150.0: ME, 160.0: ME, 130.5: THEM}))
    check("по голосу узнано", n_c == 1, n_c)
    check("помечено именно эхо", echoes(rid_c) == ["Да, собственно, контроль у него."],
          echoes(rid_c))

    say("")
    say("=== 2. Образцов своего голоса не хватает ===")
    rid2 = record("Проверка 72 мало образцов", [
        seg("far", 10.0, 20.0, "неделя 2665 крахмала"),
        seg("mic", 11.0, 19.0, "недели две 665 крахмалы"),
        seg("mic", 30.0, 34.0, "единственная своя реплика"),
    ])
    check("без двух чистых реплик не судим",
          echo.mark_by_voice(rid2, embed_fn=fake_embed({30.0: ME, 11.0: THEM})) == 0)
    check("стенограмма цела", len(store.sorted_segments(rid2)) == 3)

    say("")
    say("=== 3. Запас при спорном сходстве ===")
    rid3 = record("Проверка 72 запас", [
        seg("far", 10.0, 20.0, "неделя 2665 крахмала"),
        seg("mic", 11.0, 19.0, "недели две 665 крахмалы"),
        seg("mic", 30.0, 34.0, "своя реплика раз"),
        seg("mic", 40.0, 44.0, "своя реплика два"),
    ])
    pick = fake_embed({30.0: ME, 40.0: ME, 11.0: MIDDLE})
    check("спорное не метится", echo.mark_by_voice(rid3, embed_fn=pick) == 0)
    config.save({"echo_voice_margin": 0.0})
    check("без запаса метится", echo.mark_by_voice(rid3, embed_fn=pick) == 1)
    config.save({"echo_voice_margin": 0.05})

    say("")
    say("=== 4. Решение человека и выключенная настройка ===")
    rid4 = record("Проверка 72 решение", [
        seg("far", 10.0, 20.0, "неделя 2665 крахмала"),
        seg("mic", 11.0, 19.0, "недели две 665 крахмалы", echo_locked=True),
        seg("mic", 30.0, 34.0, "своя реплика раз"),
        seg("mic", 40.0, 44.0, "своя реплика два"),
    ])
    picked = fake_embed({30.0: ME, 40.0: ME, 11.0: THEM})
    check("подтверждённое человеком не трогаем", echo.mark_by_voice(rid4, embed_fn=picked) == 0)
    config.save({"echo_by_voice": False})
    rid5 = record("Проверка 72 выключено", [
        seg("far", 10.0, 20.0, "неделя 2665 крахмала"),
        seg("mic", 11.0, 19.0, "недели две 665 крахмалы"),
        seg("mic", 30.0, 34.0, "своя реплика раз"),
        seg("mic", 40.0, 44.0, "своя реплика два"),
    ])
    check("выключенная настройка ничего не метит", echo.mark_by_voice(rid5, embed_fn=picked) == 0)
    config.save({"echo_by_voice": True})

    say("")
    say("=== 5. Отпечаток не посчитался ===")
    rid6 = record("Проверка 72 без отпечатка", [
        seg("far", 10.0, 20.0, "неделя 2665 крахмала"),
        seg("mic", 11.0, 19.0, "недели две 665 крахмалы"),
        seg("mic", 30.0, 34.0, "своя реплика раз"),
        seg("mic", 40.0, 44.0, "своя реплика два"),
    ])
    check("без отпечатка реплика не метится",
          echo.mark_by_voice(rid6, embed_fn=fake_embed({30.0: ME, 40.0: ME})) == 0)
    check("без отпечатков собеседников не метится",
          echo.mark_by_voice(record("Проверка 72 без чужих", [
              seg("far", 10.0, 20.0, "неделя 2665 крахмала"),
              seg("mic", 11.0, 19.0, "недели две 665 крахмалы"),
              seg("mic", 30.0, 34.0, "своя реплика раз"),
              seg("mic", 40.0, 44.0, "своя реплика два"),
          ], embeddings={}), embed_fn=picked) == 0)

    say("")
    say("=== 6. Короткие куски и подключение к разметке ===")
    check("короче полсекунды отпечаток не считаем", diarize.MIN_EMB_S >= 0.5, diarize.MIN_EMB_S)
    src = io.open(PROJECT / "hagen" / "diarize.py", encoding="utf-8").read()
    engines = [io.open(PROJECT / "hagen" / name, encoding="utf-8").read()
               for name in ("diar_pyannote.py", "diar_onnx.py")]
    check("счёт отпечатков берёт модель разметки",
          'def embed_spans' in src and 'eng.embed(' in src
          and all('def embed(' in e for e in engines))
    srv = io.open(PROJECT / "hagen" / "server.py", encoding="utf-8").read()
    # Разметка говорящих переехала в роутер.
    spk = io.open(PROJECT / "hagen" / "api" / "speakers.py", encoding="utf-8").read()
    check("отсев по голосу подключён к разметке говорящих", "mark_by_voice(rec_id)" in spk)
    check("неудача отсева не роняет разметку",
          "отсев эха по голосу не отработал" in spk)

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))
finally:
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass

say("")
say("Всего замечаний: %d" % len(FAIL))
for name in FAIL:
    say("   — " + name)
io.open(PROJECT / "tests" / "t72_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
