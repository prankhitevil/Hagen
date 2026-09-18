# -*- coding: utf-8 -*-
"""Проверки не трогают настоящую базу голосов data/voices.json.

Зачем. С 13.09 имена из загруженной расшифровки сразу попадают в базу голосов,
а подпись говорящего меняет её. Проверки видеоконвейера и SharePoint грузят
тестовые субтитры с именами — без этой изоляции «Иван Петров» оседал бы в
настоящей базе. Кроме того, первое чтение новой версией переводит базу в новый
формат, а запущенная старая версия программы его не понимает.

Использование — сразу после sys.path.insert:
    import isolate; isolate.voices()

То же самое для настроек — isolate.settings(). Проверка, которая что-то
сохраняет (диктовка, звонки, вид), обязана звать её: иначе она перепишет
настоящий settings.json, включая выбранные модели и ключи.
"""
import atexit
import shutil
import tempfile
from pathlib import Path


def voices() -> Path:
    """Подменить файл базы голосов временным на всё время проверки."""
    from hagen import voices as v

    tmp = Path(tempfile.mkdtemp(prefix="voices_test_"))
    real = v.VOICES_PATH
    v.VOICES_PATH = tmp / "voices.json"
    v._cache, v._cache_stamp = None, None

    def restore() -> None:
        # настоящую базу не перечитываем: только сброс кеша
        v.VOICES_PATH = real
        v._cache, v._cache_stamp = None, None
        shutil.rmtree(tmp, ignore_errors=True)

    atexit.register(restore)
    return tmp


def settings(**start: object) -> Path:
    """Подменить settings.json временным. Начальные значения — только DEFAULTS.

    Настоящий файл не читается вовсе: в нём лежат ключи и токен, и проверке
    они не нужны. start — то, что надо положить в подменённый файл сразу.
    """
    from hagen import config as c

    tmp = Path(tempfile.mkdtemp(prefix="settings_test_"))
    real = c.SETTINGS_PATH
    c.SETTINGS_PATH = tmp / "settings.json"
    c._cache = None
    if start:
        c.save(dict(start))

    def restore() -> None:
        c.SETTINGS_PATH = real
        c._cache = None
        shutil.rmtree(tmp, ignore_errors=True)

    atexit.register(restore)
    return tmp
