# -*- coding: utf-8 -*-
"""Проверка 87: пять розеток слоя платформы и их заглушки.

Обращения к системе сведены к пяти розеткам (`hagen/platform/base.py`). У
каждой две реализации: `windows/` — та, что работает, и `fake/` — заглушка для
проверок. Эта проверка сторожит договор между ними.

Что проверяем:
  1. обе реализации отдают все имена, которые обещает описание розетки;
  2. у одинаковых имён одинаковые аргументы — иначе заглушка однажды разойдётся
     с настоящей розеткой, и проверки на ней перестанут что-либо значить;
  3. `platform.use()` переключает реализацию и возвращает обратно;
  4. модуль розетки подключается при первом обращении, а не при импорте пакета:
     иначе pycaw, comtypes и win32com грузились бы при каждом запуске службы;
  5. заглушки и правда работают: звук пишется, клавиша нажимается, снимок
     доходит до логики, уведомление показывается;
  6. `hagen.platform` не заслоняет стандартный модуль `platform`.

Живого железа не нужно вовсе.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t87_platform_sockets.py
"""
import inspect
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

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


from hagen import platform  # noqa: E402
from hagen.platform import base  # noqa: E402

try:
    # Этот вопрос задаётся ПЕРВЫМ и только один раз: ниже проверка сама
    # подключит все розетки, и спрашивать станет поздно.
    say("=== 1. Тяжёлое не грузится при импорте ===")
    heavy = ("pycaw", "comtypes", "win32com", "windows_toasts", "pyaudiowpatch")
    loaded = [m for m in heavy if m in sys.modules]
    check("после импорта hagen.platform тяжёлого в памяти нет", not loaded, loaded)
    check("модуль розетки подключается вызовом, а не импортом",
          "hagen.platform.windows.audio" not in sys.modules)

    say("")
    say("=== 2. Имена на месте в обеих реализациях ===")
    for family in ("windows", "fake"):
        platform.use(family)
        for name, proto in sorted(base.SOCKETS.items()):
            mod = platform.socket(name)
            missing = [n for n in proto.__protocol_attrs__ if not hasattr(mod, n)]
            check("%s / %s" % (family, name), not missing, missing)
    platform.use(None)

    say("")
    say("=== 3. Заглушка не расходится с настоящей розеткой ===")
    for name, proto in sorted(base.SOCKETS.items()):
        platform.use("windows")
        real = platform.socket(name)
        platform.use("fake")
        stub = platform.socket(name)
        bad = []
        for attr in sorted(proto.__protocol_attrs__):
            a, b = getattr(real, attr, None), getattr(stub, attr, None)
            if not (callable(a) and callable(b)) or inspect.isclass(a) or inspect.isclass(b):
                continue
            try:
                pa = list(inspect.signature(a).parameters)
                pb = list(inspect.signature(b).parameters)
            except (TypeError, ValueError):
                continue
            if pa != pb:
                bad.append("%s: %s против %s" % (attr, pa, pb))
        check("аргументы совпадают: %s" % name, not bad, bad)
    platform.use(None)

    say("")
    say("=== 4. Переключение реализации ===")
    check("по умолчанию — Windows", platform.current() == "windows", platform.current())
    platform.use("fake")
    check("use(fake) переключил", platform.current() == "fake", platform.current())
    check("и розетка правда заглушечная",
          platform.audio().__name__.endswith("fake.audio"), platform.audio().__name__)
    platform.use(None)
    check("use(None) вернул обратно", platform.current() == "windows", platform.current())
    try:
        platform.socket("нет-такой")
        check("неизвестную розетку не находим", False)
    except KeyError as err:
        check("неизвестную розетку не находим", "нет-такой" in str(err), str(err))

    say("")
    say("=== 5. Заглушки работают ===")
    platform.use("fake")
    from hagen.platform import fake  # noqa: E402

    fake.reset()

    got = []
    rec = platform.audio().open_mic(None, got.append)
    rec.start()
    time.sleep(0.35)
    rec.stop()
    check("микрофон-заглушка отдал звук", len(got) >= 2, len(got))
    check("и сказал, какое устройство открыл", rec.device_name, rec.device_name)
    check("что открывали — записано", fake.audio.OPENED and fake.audio.OPENED[0]["kind"] == "mic",
          fake.audio.OPENED)

    fake.audio.BROKEN.add(fake.audio.MICS[0]["name"])
    try:
        platform.audio().open_mic(0, got.append).start()
        check("сломанное устройство честно не открывается", False)
    except RuntimeError as err:
        check("сломанное устройство честно не открывается", "не открылось" in str(err), str(err))
    fake.audio.reset()

    check("выключатель микрофона переключается",
          platform.audio().mic_toggle()["muted"] is True
          and platform.audio().mic_state()["muted"] is True)

    fake.desktop.MEETING = {"subject": "Планёрка", "attendees": ["Иван Петров", "Сергей Орлов"]}
    check("встреча отдаётся", platform.desktop().current_meeting()["subject"] == "Планёрка")
    check("имя записи берётся из темы",
          platform.desktop().suggest_title(platform.desktop().current_meeting()) == "Планёрка")
    check("число голосов — по приглашённым",
          platform.desktop().suggest_max_speakers(platform.desktop().current_meeting()) == 2)

    starts = []
    w = platform.desktop().watch_calls(on_call_start=starts.append, on_call_end=lambda i: None)
    w.start()
    w.fire_start({"process": "ms-teams.exe"})
    check("сторож звонков зовёт обработчик", starts and starts[0]["process"] == "ms-teams.exe",
          starts)
    w.stop()

    hk = platform.input().parse_hotkey("ctrl+shift+space")
    presses = []
    lis = platform.input().listen(hk, on_press=lambda: presses.append("нажали"))
    check("клавиша занялась", lis.start() is True, lis.error)
    lis.press()
    check("нажатие дошло до логики", presses == ["нажали"], presses)
    check("занятое сочетание второй раз не занять",
          platform.input().listen(hk, on_press=lambda: None).start() is False)
    lis.stop()
    platform.input().paste_text("текст из заглушки")
    check("вставка записана", fake.input.PASTED == ["текст из заглушки"], fake.input.PASTED)
    platform.input().click("start")
    check("щелчок записан", fake.input.CLICKS == ["start"], fake.input.CLICKS)

    shots = []
    sw = platform.system().watch_screenshots("rec1", position_s=lambda: 12.3,
                                             on_shot=shots.append)
    sw.start()
    sw.fire("экран.png")
    check("снимок дошёл до логики со временем",
          shots and shots[0] == {"file": "экран.png", "at_s": 12.3}, shots)
    sw.stop()
    check("сдвиг времени снимков считается",
          platform.system().shift_shots([{"at_s": 1.0}], 2.5)[0]["at_s"] == 3.5)

    gui = platform.shell()
    check("автозапуск переключается",
          gui.set_autostart(True)["enabled"] is True and gui.autostart_enabled() is True)
    gui.splash_show()
    gui.splash_close()
    check("заставку подняли и убрали",
          fake.shell.SPLASH == {"shown": 1, "closed": 1}, fake.shell.SPLASH)
    shell_obj = gui.app_shell(None, None)
    check("оболочка поднимается", shell_obj.start() is True and shell_obj.running)
    platform.use(None)

    say("")
    say("=== 6. Стандартный platform не заслонён ===")
    import platform as stdlib_platform  # noqa: E402

    check("import platform даёт стандартный модуль",
          hasattr(stdlib_platform, "system") and stdlib_platform.system() == "Windows",
          stdlib_platform.__name__)

finally:
    platform.use(None)
    say("")
    say("ИТОГО провалов: %d" % len(FAIL))
    for f in FAIL:
        say("   - " + f)
    io.open(PROJECT / "tests" / "t87_result.txt", "w", encoding="utf-8").write("\n".join(LINES))

sys.exit(1 if FAIL else 0)
