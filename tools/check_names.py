# -*- coding: utf-8 -*-
"""Поиск имён, которых в модуле нет: беда переноса кода между файлами.

Запуск из корня проекта:
    .venv\Scripts\python.exe tools\check_names.py hagen hagen\api

Нашлось 17.09 при разрезании server.py на роутеры: в перенесённых файлах не
хватало пяти имён (UploadFile, UPLOAD_DIR, audio_io, json, uuid, threading,
sessions). Проверки этого не видели — те маршруты требуют браузера и сети и в
быстрый набор не входят. Строка ждала бы первого перетаскивания видео.

Ложные срабатывания бывают на параметрах лямбд — их разбор не отслеживает.

Проверки вызовом ловят такое, только если дойдут до нужной строки. А строка
вроде `threading.Thread(...)` внутри редкого маршрута может годами не
выполняться. Здесь — простой разбор: берём имена, которые модуль ИСПОЛЬЗУЕТ на
верхнем уровне и внутри функций, и вычитаем всё, что он определил или
импортировал, плюс локальные переменные каждой функции.
"""
import ast
import builtins
import io
import os
import sys


def defined_locally(fn: ast.AST) -> set[str]:
    """Имена, которые появляются внутри функции: присваивания, циклы, with, except."""
    out: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in (list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs)):
                    out.add(a.arg)
                for extra in (args.vararg, args.kwarg):
                    if extra is not None:
                        out.add(extra.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, ast.Global):
            out.update(node.names)
        elif isinstance(node, (ast.comprehension,)):
            pass
    return out


def check(path: str) -> list[str]:
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    top: set[str] = set(dir(builtins))
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                top.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            top.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for t in ast.walk(node):
                if isinstance(t, ast.Name) and isinstance(t.ctx, ast.Store):
                    top.add(t.id)
    bad: list[str] = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        known = top | defined_locally(node)
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in known:
                bad.append("%s:%d  %s (в %s)" % (os.path.basename(path), n.lineno, n.id, node.name))
    return bad


rows = []
for folder in sys.argv[1:]:
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            rows += check(os.path.join(folder, name))
print("\n".join(rows) if rows else "неизвестных имён нет")
