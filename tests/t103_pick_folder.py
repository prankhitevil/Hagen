# -*- coding: utf-8 -*-
"""Проверка 103: папка хранилища выбирается окном, как в Проводнике.

Раньше путь к хранилищу вписывали руками: в установщике — в консоли, в
настройках — в текстовое поле. Теперь и там и там открывается системное окно
выбора папки. Окно одно — розетка «Оболочки» `pick_folder`: ею пользуются и
установщик, и кнопка «Выбрать…» в настройках.

Что проверяем:
  1. заглушка розетки: отдаёт заданную папку и помнит, что спросили;
  2. Windows: где откроется окно (нет папки — ближайшая выше, чужой
     относительный путь — не трогаем), путь с обратными косыми, «передумал» —
     None, второе окно поверх первого не открывается. Настоящее окно не
     открываем: tkinter подменён;
  3. окно подключается чистым Python без библиотек программы — так его зовёт
     установщик, когда окружения ещё нет;
  4. маршрут: выбрали — путь, передумали — None, не открылось — понятная ошибка;
  5. страница: кнопка «Выбрать…» у папки заметок и общий обработчик; у папок
     снимков — та же кнопка, путь ложится строкой в список;
  6. установщик: выбранная папка ложится в настройки без вопроса в консоли;
     окно закрыли — спрашивает в консоли, как раньше.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t103_pick_folder.py
"""
import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen import platform  # noqa: E402
from hagen.platform import fake  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="hagen_t103_"))
try:
    say("=== 1. Заглушка ===")
    platform.use("fake")
    fake.reset()
    check("по умолчанию человек передумал", platform.shell().pick_folder("Папка", None) is None)
    fake.shell.PICKED = str(tmp)
    got = platform.shell().pick_folder("Папка заметок", r"C:\Users\tester\Obsidian")
    check("отдаёт заданную папку", got == str(tmp), got)
    check("помнит, что спросили",
          fake.shell.PICKS[-1] == ("Папка заметок", r"C:\Users\tester\Obsidian"), fake.shell.PICKS)
    fake.reset()
    check("reset всё обнулил", fake.shell.PICKED is None and not fake.shell.PICKS)
    platform.use(None)

    say("")
    say("=== 2. Windows: где открыть окно и что вернуть ===")
    import tkinter  # noqa: E402
    from tkinter import filedialog  # noqa: E402

    from hagen.platform.windows import app  # noqa: E402

    asked = {}
    answer = {"path": ""}

    class FakeTk:
        def withdraw(self):
            asked["withdrawn"] = True

        def attributes(self, *a):
            asked["attrs"] = a

        def destroy(self):
            asked["destroyed"] = True

    def fake_ask(**kw):
        asked.update(kw)
        return answer["path"]

    real_tk, real_ask = tkinter.Tk, filedialog.askdirectory
    tkinter.Tk, filedialog.askdirectory = FakeTk, fake_ask
    try:
        nested = tmp / "Obsidian" / "Встречи"
        (tmp / "Obsidian").mkdir()
        answer["path"] = (tmp / "Obsidian").as_posix()
        got = app.pick_folder("Папка заметок", str(nested))
        check("папки нет — окно открывается в ближайшей существующей выше",
              Path(asked.get("initialdir") or "") == tmp / "Obsidian", asked.get("initialdir"))
        check("путь — с обратными косыми, как в Windows", got == str(tmp / "Obsidian"), got)
        check("заголовок доходит до окна", asked.get("title") == "Папка заметок", asked.get("title"))
        check("окно поверх остальных, служебное окно Tk спрятано и убрано",
              asked.get("attrs") == ("-topmost", True) and asked.get("withdrawn") and asked.get("destroyed"),
              asked)
        asked.clear()
        answer["path"] = ""
        check("передумал — None", app.pick_folder("Папка", str(tmp)) is None)
        asked.clear()
        app.pick_folder("Папка", "Obsidian")
        check("относительный путь — окно там, где решит система", asked.get("initialdir") is None,
              asked.get("initialdir"))
        asked.clear()
        app.pick_folder("Папка", "")
        check("пусто — так же", asked.get("initialdir") is None, asked.get("initialdir"))
        app._picking.acquire()
        try:
            app.pick_folder("Папка", str(tmp))
            check("второе окно поверх первого не открывается", False)
        except RuntimeError as err:
            check("второе окно поверх первого не открывается", "уже открыто" in str(err), err)
        finally:
            app._picking.release()
        answer["path"] = str(tmp)
        check("после отказа замок отпущен", app.pick_folder("Папка", None) == str(tmp))
    finally:
        tkinter.Tk, filedialog.askdirectory = real_tk, real_ask

    say("")
    say("=== 3. Окно подключается без библиотек программы ===")
    base_py = PROJECT / "python" / "python.exe"
    if not base_py.exists():
        base_py = Path(sys.base_prefix) / "python.exe"
    code = ("import sys; sys.path.insert(0, %r); import tkinter.filedialog; "
            "from hagen import platform; "
            "print(platform.shell().pick_folder.__name__, "
            "sorted(m for m in sys.modules if m.split('.')[0] in "
            "('numpy', 'fastapi', 'torch', 'onnxruntime', 'pydantic')))" % str(PROJECT))
    res = subprocess.run([str(base_py), "-S", "-c", code], capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=60,
                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    check("чистый Python без site-packages находит розетку и tkinter",
          res.returncode == 0 and res.stdout.strip() == "pick_folder []",
          (res.stdout.strip() or res.stderr.strip()[-300:]))

    say("")
    say("=== 4. Маршрут ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    platform.use("fake")
    fake.reset()
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        fake.shell.PICKED = r"C:\Users\tester\Obsidian"
        r = cli.post("/api/pick-folder", json={"title": "Папка заметок / хранилище",
                                               "start": r"C:\Users\tester\Старое"}, headers=ORIGIN)
        check("выбрали — путь", r.status_code == 200 and r.json().get("path") == fake.shell.PICKED,
              (r.status_code, r.text[:200]))
        check("окну передали заголовок и где открыться",
              fake.shell.PICKS[-1] == ("Папка заметок / хранилище", r"C:\Users\tester\Старое"),
              fake.shell.PICKS)
        fake.shell.PICKED = None
        r = cli.post("/api/pick-folder", json={"start": ""}, headers=ORIGIN)
        check("передумали — path пустой, не ошибка",
              r.status_code == 200 and r.json().get("path") is None, (r.status_code, r.text[:200]))
        check("пустое «где» — None, заголовок по умолчанию",
              fake.shell.PICKS[-1] == ("Выбор папки", None), fake.shell.PICKS[-1])
        real_pick = fake.shell.pick_folder

        def broken(title, start=None):
            raise RuntimeError("нет tkinter")

        fake.shell.pick_folder = broken
        try:
            r = cli.post("/api/pick-folder", json={}, headers=ORIGIN)
        finally:
            fake.shell.pick_folder = real_pick
        detail = (r.json() if r.headers.get("content-type", "").startswith("application/json")
                  else {}).get("detail", "")
        check("не открылось — ошибка с причиной и подсказкой вписать руками",
              r.status_code == 500 and "нет tkinter" in detail and "вписать" in detail,
              (r.status_code, detail))
        settings_before = json.dumps(server.config.load(), sort_keys=True, ensure_ascii=False)
        fake.shell.PICKED = r"C:\Users\tester\Другое"
        cli.post("/api/pick-folder", json={}, headers=ORIGIN)
        check("маршрут сам настройки не меняет — сохраняет страница",
              json.dumps(server.config.load(), sort_keys=True, ensure_ascii=False) == settings_before)

        say("")
        say("=== 5. Страница ===")
        html = cli.get("/").text
        js = cli.get("/static/app.js").text
    platform.use(None)

    check("у папки заметок — кнопка «Выбрать…»",
          'data-for="set-vault"' in html and "js-pick-folder" in html)
    check("кнопка общая: обработчик один, зовёт маршрут и сохраняет поле",
          "function bindPickFolder" in js and "/api/pick-folder" in js
          and "applySettings(input)" in js)
    check("строка «папка на месте» перерисовывается после сохранения",
          js.count("paintVaultState()") >= 2)
    check("у папок снимков — та же кнопка, путь добавляется строкой",
          'data-for="set-shot-folders" data-add="line"' in html and "dataset.add === 'line'" in js)
    check("пустой список начинается с найденного программой, а не с одной папки",
          "input.dataset.seed" in js and "field.dataset.seed" in js)

    say("")
    say("=== 6. Установщик ===")
    import install  # noqa: E402

    platform.use("fake")
    fake.reset()
    real_project, real_ask = install.PROJECT, install.ask
    questions = []

    def fake_console(question, default=""):
        questions.append(question)
        return default

    install.PROJECT, install.ask = tmp, fake_console
    try:
        picked = str(tmp / "Хранилище")
        fake.shell.PICKED = picked
        install.configure()
        data = json.load(io.open(tmp / "settings.json", encoding="utf-8"))
        check("выбранная папка — в настройках", data.get("vault_path") == picked, data.get("vault_path"))
        check("путь в консоли не спрашивали",
              not any("хранилищ" in q.lower() for q in questions), questions)
        check("окно открылось с предложенной папкой",
              fake.shell.PICKS and fake.shell.PICKS[-1][1] == str(Path.home() / "Obsidian"),
              fake.shell.PICKS)
        (tmp / "settings.json").unlink()
        questions.clear()
        fake.shell.PICKED = None
        install.configure()
        data = json.load(io.open(tmp / "settings.json", encoding="utf-8"))
        check("окно закрыли — путь спрашивают в консоли, как раньше",
              any("хранилищ" in q.lower() for q in questions), questions)
        check("и Enter оставляет предложенную папку",
              data.get("vault_path") == str(Path.home() / "Obsidian"), data.get("vault_path"))
        real_pick = fake.shell.pick_folder
        fake.shell.pick_folder = lambda title, start=None: 1 / 0
        try:
            check("окно не открылось — установщик не падает, спросит в консоли",
                  install.pick_folder("Папка", str(tmp)) is None)
        finally:
            fake.shell.pick_folder = real_pick
    finally:
        install.PROJECT, install.ask = real_project, real_ask
        platform.use(None)
finally:
    import shutil  # noqa: E402

    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(finish("t103"))
