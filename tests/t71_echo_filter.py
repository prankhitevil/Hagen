# -*- coding: utf-8 -*-
"""Проверка 71: отсев эха колонок из дорожки микрофона (17.09).

Владелец слушал звонок через динамики ноутбука (у Jabra своё подавление эха,
у Realtek его нет). Микрофон услышал собеседников, и их слова попали в
стенограмму как его реплики. Фильтр эха их пропустил, потому что сверял
реплику микрофона с ОДНОЙ фразой собеседников, а микрофон слепил в одну
реплику то, что у собеседников разрезано на две.

Случаи взяты с его записи 17.09.

Что проверяем:
  1. простое эхо отсеивается, своя речь остаётся;
  2. эхо, накрывшее две фразы собеседников, тоже отсеивается (новое);
  3. короткие реплики строже: «да, хорошо» человек мог сказать и сам;
  4. далёкое по времени совпадение не считается эхом;
  5. подтверждённое человеком (`echo_locked`) фильтр не трогает;
  6. выключенный фильтр не метит ничего;
  7. полный проход по записи метит и снимает пометки.

База голосов и настройки — временные (isolate), записи проверка удаляет за собой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t71_echo_filter.py
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


from hagen import config, echo, store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 71"):
        store.delete(old["id"])

# Настоящий случай 17.09: собеседник сказал длинную фразу, микрофон разрезал
# её иначе и приписал владельцу.
FAR_A = "Готовы её привести оперативно"
FAR_B = "Мы сейчас проверяем всё ли там действительно кондиция что входит в комплект"
MIC_BOTH = ("Готовы её привести оперативно Мы сейчас проверяем всё ли там "
            "действительно кондиция что входит в комплект")


def seg(track, start, end, text, **extra):
    s = store.make_segment(track, start, end, text)
    s.update(extra)
    return s


def record(title, segments):
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    store.replace_segments(rid, segments)
    return rid


def marks(rid):
    return [(s["text"][:24], bool(s.get("echo"))) for s in
            store.sorted_segments(rid, include_echo=True)]


try:
    config.save({"echo_filter": True, "echo_match_threshold": 0.6})

    say("=== 1. Простое эхо и своя речь ===")
    far = seg("far", 10.0, 14.0, FAR_A)
    mine = seg("mic", 20.0, 24.0, "Алексей проверит это сегодня и вернётся с ответом")
    ech = seg("mic", 10.6, 14.4, "Готовы её привести оперативно")
    check("эхо узнано", echo.echo_source(ech, [far]) is not None)
    check("своя речь не тронута", echo.echo_source(mine, [far]) is None)
    check("на эхо указана его фраза", echo.echo_source(ech, [far])["id"] == far["id"])

    say("")
    say("=== 2. Эхо, накрывшее две фразы собеседников (случай 17.09) ===")
    far_a = seg("far", 10.0, 13.0, FAR_A)
    far_b = seg("far", 13.0, 19.0, FAR_B)
    both = seg("mic", 10.5, 19.6, MIC_BOTH)
    one = echo.echo_source(both, [far_a])
    check("по одной фразе не узнаётся — так и было", one is None)
    check("по двум фразам узнаётся", echo.echo_source(both, [far_a, far_b]) is not None)
    check("указана та фраза, что совпала больше",
          echo.echo_source(both, [far_a, far_b])["id"] == far_b["id"],
          echo.echo_source(both, [far_a, far_b])["text"][:30])

    say("")
    say("=== 2а. Предел сверки текстов: дорожки расслышали по-разному ===")
    # Настоящие случаи с записи 20260917-105826-9344.
    # Звуки те же, слова разные — совпадение 33–40 %, и текстом это не поймать
    # никогда. Такое закрывает только сверка по голосу (t72): проверка стоит
    # здесь, чтобы предел был записан, а не открывался заново.
    far_c = seg("far", 130.5, 132.2, "Да, собственно, Control Unior.")
    mic_c = seg("mic", 130.5, 132.1, "Да, собственно, контроль у него.")
    check("текстом не ловится — и это ожидаемо", echo.echo_source(mic_c, [far_c]) is None,
          "%.0f%%" % (100 * echo._contained(echo._words(mic_c["text"]),
                                            echo._words(far_c["text"]))))
    check("порог не подкрутили тихо", echo._threshold() >= 0.5, echo._threshold())

    say("")
    say("=== 3. Короткие реплики строже ===")
    far_s = seg("far", 30.0, 32.0, "Да хорошо договорились")
    short_same = seg("mic", 30.4, 32.2, "Да хорошо договорились")
    short_own = seg("mic", 30.4, 32.2, "Да поехали")
    check("дословно повторённое коротко — эхо", echo.echo_source(short_same, [far_s]) is not None)
    check("своё короткое «да поехали» осталось", echo.echo_source(short_own, [far_s]) is None)

    say("")
    say("=== 4. Далёкое по времени не эхо ===")
    late = seg("mic", 40.0, 44.0, FAR_A)
    check("через полминуты — не эхо", echo.echo_source(late, [far]) is None)

    say("")
    say("=== 5. Полный проход по записи ===")
    rid = record("Проверка 71 проход", [
        seg("far", 10.0, 13.0, FAR_A),
        seg("far", 13.0, 19.0, FAR_B),
        seg("mic", 10.5, 19.6, MIC_BOTH),
        seg("mic", 30.0, 34.0, "Наташ подскажи пожалуйста по отгрузкам"),
    ])
    n = echo.mark(rid)
    check("помечена одна реплика", n == 1, n)
    check("помечено именно эхо",
          marks(rid) == [(FAR_A[:24], False), (MIC_BOTH[:24], True),
                         (FAR_B[:24], False), ("Наташ подскажи пожалуйста по отгрузкам"[:24], False)],
          marks(rid))
    check("эхо не показывается", len(store.sorted_segments(rid)) == 3,
          [s["text"][:20] for s in store.sorted_segments(rid)])
    check("сказано, эхом чего это было",
          any(s.get("echo_of") for s in store.sorted_segments(rid, include_echo=True)))
    check("повторный проход не метит заново", echo.mark(rid) == 0)

    say("")
    say("=== 6. Решение человека и выключенный фильтр ===")
    rid2 = record("Проверка 71 решение", [
        seg("far", 10.0, 13.0, FAR_A),
        seg("far", 13.0, 19.0, FAR_B),
        seg("mic", 10.5, 19.6, MIC_BOTH, echo_locked=True),
    ])
    check("подтверждённое человеком не метится", echo.mark(rid2) == 0)
    check("реплика осталась видимой", len(store.sorted_segments(rid2)) == 3)

    config.save({"echo_filter": False})
    rid3 = record("Проверка 71 выключено", [
        seg("far", 10.0, 13.0, FAR_A),
        seg("far", 13.0, 19.0, FAR_B),
        seg("mic", 10.5, 19.6, MIC_BOTH),
    ])
    check("выключенный фильтр ничего не метит", echo.mark(rid3) == 0)
    check("при выключенном фильтре видно всё", len(store.sorted_segments(rid3)) == 3)
    config.save({"echo_filter": True})

    say("")
    say("=== 7. Снятая пометка возвращает реплику ===")
    segs = store.sorted_segments(rid, include_echo=True)
    for s in segs:
        if s.get("echo"):
            s["text"] = "Наташ это уже моя собственная фраза про отгрузки"
    store.replace_segments(rid, segs)
    echo.mark(rid)
    check("правленая реплика перестала быть эхом",
          len(store.sorted_segments(rid)) == 4,
          [s["text"][:20] for s in store.sorted_segments(rid)])

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
io.open(PROJECT / "tests" / "t71_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
