# -*- coding: utf-8 -*-
"""Чужое приватное не трогать: поиск обращений к `_имени` другого модуля.

Правило простое: если ядру понадобилось `_имя` из соседнего модуля, это имя
становится частью его двери, а не импортируется в обход. На 22.09
таких мест было тридцать — от `voices._people()` до `server._calls` из слоя
платформы. Здесь — простой разбор: атрибуты вида `модуль._имя` и импорты
`from .модуль import _имя`, где модуль — не `self` и не собственный.

Запуск из корня проекта:
    .venv\\Scripts\\python.exe tools\\check_private.py hagen run.py

Ложные срабатывания: переменная с именем модуля (`store = ...`) — их не
отличить без вывода типов; таких мест в программе нет.

Код выхода 1, если что-то нашлось: годится для прогона перед выпуском.
"""
from __future__ import annotations

import ast
import io
import os
import sys

#: Имена, у которых `_поле` — не приватное соседа, а своё: self, cls, объекты.
SKIP = {"self", "cls", "np", "os", "sys", "re", "ctypes", "win32gui", "win32con",
        "win32api", "pythoncom", "wintypes", "user32", "kernel32", "shell32"}


def module_names(tree: ast.AST) -> set[str]:
    """Свои модули, импортированные в файле: `from . import y`, `import hagen.x`.

    Чужие библиотеки (`gigaam._download_model`, `comtypes`-интерфейсы с `_iid_`)
    не считаются: их приватное — их дело, у нас на него двери нет.
    """
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] == "hagen":
                    out.add((a.asname or a.name).split(".")[-1])
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "").split(".")[0] == "hagen":
                for a in node.names:
                    out.add(a.asname or a.name)
    return out


def scan(path: str) -> list[str]:
    try:
        with io.open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src, path)
    except (OSError, SyntaxError) as err:
        return ["%s: не разобран: %s" % (path, err)]
    mods = module_names(tree) - SKIP
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr.startswith("_") \
                and not node.attr.startswith("__") \
                and isinstance(node.value, ast.Name) and node.value.id in mods:
            found.append("%s:%d  %s.%s" % (path, node.lineno, node.value.id, node.attr))
        elif isinstance(node, ast.ImportFrom) and node.level:
            # Модуля в узле может не быть вовсе: `from . import _имя` — самый
            # ходовой способ залезть к соседу, и по нему проверка молчала.
            where = "." * node.level + (node.module or "")
            for a in node.names:
                if a.name.startswith("_") and not a.name.startswith("__"):
                    found.append("%s:%d  from %s import %s" % (path, node.lineno, where, a.name))
    return found


def main(argv: list[str]) -> int:
    roots = argv or ["hagen", "run.py"]
    files: list[str] = []
    for root in roots:
        if os.path.isfile(root):
            files.append(root)
            continue
        for cur, _dirs, names in os.walk(root):
            if "__pycache__" in cur:
                continue
            files += [os.path.join(cur, n) for n in names if n.endswith(".py")]
    found: list[str] = []
    for f in sorted(files):
        found += scan(f)
    for line in found:
        print(line)
    print("чужих приватных имён: %d" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
