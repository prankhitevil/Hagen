# -*- coding: utf-8 -*-
"""Проверка 86: заставка при запуске.

От щелчка по ярлыку до окна проходит около двух секунд: поднимается служба и
греется быстрая модель. Всё это время экран пустой, и человек щёлкает по ярлыку
второй раз (замечено 18.09). Заставка показывает картинку.

Проверяем: картинка лежит в папке программы и читается; окно поднимается,
видно на экране и закрывается по команде; запуск встраивает её до создания
окна и закрывает перед ним; при старте сразу в трей заставки нет.

Окно создаётся внутри этого процесса — отдельных процессов не заводим
(Kaspersky), как и в остальных проверках окон.
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.settings()

LINES = []
FAIL = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


from hagen.platform.windows import splash  # noqa: E402

say("=== 1. Картинка на месте ===")
check("файл заставки лежит в папке программы", splash.IMAGE.exists(), splash.IMAGE.name)
size = splash.IMAGE.stat().st_size if splash.IMAGE.exists() else 0
check("файл не пустой", size > 10000, "%d байт" % size)

say("")
say("=== 2. Окно поднимается и закрывается ===")
s = splash.Splash()
started = s.start()
check("окно создалось", started, s.error or "")
if started:
    import ctypes

    user32 = ctypes.windll.user32
    check("окно видно на экране", bool(user32.IsWindowVisible(s.hwnd)))
    # Заставка не должна отбирать фокус: иначе она перехватит набор текста
    # у того, кто в этот момент что-то печатает.
    style = user32.GetWindowLongW(s.hwnd, -20)      # GWL_EXSTYLE
    check("фокус не отбирает", bool(style & 0x08000000))   # WS_EX_NOACTIVATE
    check("поверх других окон", bool(style & 0x00000008))  # WS_EX_TOPMOST
    hwnd = s.hwnd
    s.close(wait=False)          # без выдержки: её проверяет следующий раздел
    for _ in range(40):
        if not user32.IsWindow(hwnd):
            break
        time.sleep(0.05)
    check("закрылась по команде", not user32.IsWindow(hwnd))

say("")
say("=== 3. Запуск не ждёт заставку ===")
s2 = splash.Splash()
if s2.start():
    import ctypes

    user32 = ctypes.windll.user32
    topmost = lambda: bool(user32.GetWindowLongW(s2.hwnd, -20) & 0x00000008)
    check("пока грузимся — поверх всех окон", topmost())
    t0 = time.time()
    s2.close()                      # как будто окно программы готово сразу
    check("close возвращается мгновенно", time.time() - t0 < 0.3,
          "%.2f с" % (time.time() - t0))
    time.sleep(0.4)
    check("уходит с переднего плана сразу", not topmost())
    check("но ещё видна: короткий запуск не превращается во вспышку",
          bool(user32.IsWindow(s2.hwnd)))
    time.sleep(splash.MIN_SECONDS)
    check("гаснет сама через %.0f с" % splash.MIN_SECONDS,
          not user32.IsWindow(s2.hwnd))

say("")
say("=== 4. Нет картинки — нет и беды ===")
gone = splash.Splash(PROJECT / "hagen" / "icons" / "нет-такого-файла.png")
check("без файла заставка просто не показывается", gone.start() is False)
check("сказано, почему", "нет" in (gone.error or ""), gone.error)

say("")
say("=== 5. Встроена в запуск ===")
run = io.open(PROJECT / "run.py", encoding="utf-8").read()
check("запуск показывает заставку", "splash_show()" in run)
check("закрывается перед созданием окна",
      run.index("splash_close()") < run.index("webview.create_window("))
check("при старте в трей и подготовке моделей заставки нет",
      "if args.app and not args.tray and not args.prepare:" in run)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for name in FAIL:
    say("   - " + name)
io.open(PROJECT / "tests" / "t86_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
