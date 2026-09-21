# -*- coding: utf-8 -*-
"""Точка входа: поднимает службу на 127.0.0.1 и открывает окно программы.

`--app` — собственное окно (pywebview), `--tray` — то же, но сразу в трее. Если
окно не открылось, страница открывается во вкладке браузера: это запасной путь
только для интерфейса, звук и в этом случае пишет служба.

Наружу ничего не выставляется ни при каких условиях: слушаем только 127.0.0.1.
"""
from __future__ import annotations

import argparse
import io
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT))

HOST = "127.0.0.1"



def setup_logging(level: str = "INFO") -> None:
    from hagen import config

    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
                            datefmt="%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.handlers.clear()

    fh = RotatingFileHandler(config.LOGS_DIR / "hagen.log", maxBytes=2_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)

    sh = logging.StreamHandler(
        io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        if hasattr(sys.stdout, "buffer") else sys.stdout
    )
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # httpx и httpcore на уровне INFO пишут полный адрес каждого запроса. У
    # ссылок скачивания SharePoint в адресе лежит tempauth — пропуск к файлу:
    # 13.09 он оседал в журнале. Поэтому их — только предупреждения и ошибки.
    for noisy in ("uvicorn.access", "watchfiles", "matplotlib", "numba",
                  "speechbrain", "pytorch_lightning", "huggingface_hub",
                  "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((HOST, port)) == 0


def pick_port(preferred: int) -> int:
    if not port_busy(preferred):
        return preferred
    for p in range(preferred + 1, preferred + 12):
        if not port_busy(p):
            return p
    raise RuntimeError("не нашёл свободный порт рядом с %d" % preferred)


def open_browser_when_ready(port: int, delay: float = 0.8) -> None:
    def run() -> None:
        url = "http://%s:%d/" % (HOST, port)
        deadline = time.time() + 25
        while time.time() < deadline:
            if port_busy(port):
                break
            time.sleep(0.25)
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            print("Откройте в браузере: %s" % url)

    threading.Thread(target=run, daemon=True).start()





WINDOW_TITLE = "Hagen, Your Consigliere"


def _service_alive(port: int, timeout: float = 2.0) -> bool:
    """Отвечает ли на этом порту именно наш «Hagen»."""
    import json
    import urllib.error
    import urllib.request

    url = "http://%s:%d/api/health" % (HOST, port)
    req = urllib.request.Request(url, headers={"Origin": "http://%s:%d" % (HOST, port)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return bool(data.get("ok"))
    except Exception:
        return False


def _window_icon() -> str | None:
    """Значок окна и панели задач. Нет файла — pywebview поставит свой (17.09)."""
    from hagen import platform

    try:
        path = platform.shell().icon_paths().get("idle")
    except Exception:
        return None
    return str(path) if path and path.exists() else None


def _webview_storage() -> str:
    """Своя служебная папка окна на каждый запуск.

    WebView2 держит свою папку под замком: если запустить второй экземпляр с той
    же папкой, окно не откроется вовсе. Поэтому папка своя на процесс, а чужие
    остатки от давно закрытых экземпляров подчищаем.
    """
    from hagen import config as cfg

    base = cfg.DATA_DIR / "_webview"
    base.mkdir(parents=True, exist_ok=True)
    for old in base.iterdir():
        if not old.is_dir() or not old.name.isdigit():
            continue
        if int(old.name) == os.getpid():
            continue
        try:
            import psutil

            if psutil.pid_exists(int(old.name)):
                continue
        except Exception:
            pass
        try:
            import shutil as _sh

            _sh.rmtree(old, ignore_errors=True)
        except Exception:
            pass
    mine = base / str(os.getpid())
    mine.mkdir(parents=True, exist_ok=True)
    return str(mine)


def _message_box(title: str, text: str) -> None:
    """Показать ошибку окном, когда консоль спрятана.

    Обёртка, а не прямой вызов розетки: окно ошибки должно показаться и тогда,
    когда сама программа не поднялась, — а если не поднялся даже слой
    платформы, остаётся напечатать.
    """
    try:
        from hagen import platform

        platform.shell().show_error(title, text)
    except Exception:
        print("%s: %s" % (title, text))


def _request_show(port: int, timeout: float = 3.0) -> bool:
    """Попросить уже запущенный «Hagen» показать своё окно.

    Окно может быть спрятано в трей: тогда поиск видимого окна его не найдёт,
    и второй запуск открыл бы лишнее окно. Служба сама покажет своё.
    """
    import json
    import urllib.request

    url = "http://%s:%d/api/app/show" % (HOST, port)
    req = urllib.request.Request(url, data=b"{}", method="POST", headers={
        "Origin": "http://%s:%d" % (HOST, port), "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("shown"))
    except Exception:
        return False


def run_as_app(port: int, log, own_service: bool = True, start_hidden: bool = False) -> int:
    """Открыть приложение в собственном окне, без браузера.

    Служба поднимается в фоновом потоке, а окно занимает главный поток — этого
    требует оболочка окна на Windows. Окно показывает ту же страницу
    127.0.0.1, поэтому проверка Host и Origin работает как обычно.

    start_hidden — запуск вместе с Windows: окно сразу спрятано, работает значок
    у часов.
    """
    try:
        import webview
    except Exception as err:
        log.error("оболочка окна недоступна: %s", err)
        return -1

    from hagen import platform


    import uvicorn

    from hagen import config as cfg
    from hagen import server as srv
    from hagen.server import app, sessions

    server = None
    thread = None
    if own_service:
        server_cfg = uvicorn.Config(app, host=HOST, port=port, log_level="warning",
                                    ws_max_size=8 * 1024 * 1024,
                                    timeout_graceful_shutdown=20, access_log=False)
        server = uvicorn.Server(server_cfg)
        thread = threading.Thread(target=server.run, name="hagen-http", daemon=True)
        thread.start()

        deadline = time.time() + 60
        while time.time() < deadline:
            if port_busy(port):
                break
            if not thread.is_alive():
                _message_box("Hagen",
                             "Служба не запустилась. Подробности в файле logs/hagen.log")
                return 1
            time.sleep(0.2)

    url = "http://%s:%d/" % (HOST, port)
    log.info("открываю окно приложения: %s", url)

    # Значок у часов и уведомления с кнопками. Если не поднялись — программа
    # работает как раньше: крестик закрывает окно, а запуск «в трей» всё равно
    # показывает окно — иначе программа оказалась бы невидимой.
    shell = None
    tray_ok = False
    if own_service and cfg.get("tray_enabled", True):
        try:
            gui = platform.shell()
            shell = gui.app_shell(None, srv)
            shell.notifier = gui.make_notifier(srv, on_open=shell.open_window)
            tray_ok = shell.start()
            if not tray_ok:
                log.warning("значок в трее не поднялся, крестик будет закрывать программу")
            srv.set_app_hooks(show=shell.open_window)
            gui.sync_autostart()
        except Exception as err:
            log.warning("трей недоступен: %s", err)
            shell = None

    # Окно вот-вот появится: заставке пора уйти, чтобы она не оказалась поверх
    # него. Закрывает тот, кто её поднимал, — заставка одна на программу.
    try:
        platform.shell().splash_close()
    except Exception:
        pass

    window = webview.create_window(
        WINDOW_TITLE,
        url,
        width=1280, height=820, min_size=(900, 600),
        confirm_close=False, text_select=True,
        hidden=bool(start_hidden and tray_ok),
    )
    if shell is not None:
        shell.window = window

    def on_closing() -> bool:
        """Крестик прячет окно в трей; закрыть во время записи — только спросив."""
        if shell is not None and not shell.on_closing():
            return False
        if shell is not None and shell.quitting:
            return True          # запись уже остановлена из меню значка
        try:
            active = sessions.active_session()
        except Exception:
            active = None
        if active is not None:
            ok = window.create_confirmation_dialog(
                "Идёт запись",
                "Сейчас идёт запись. Закрыть приложение и остановить её?")
            if not ok:
                return False
            try:
                sessions.stop_all()
            except Exception:
                log.error("не удалось остановить запись при закрытии", exc_info=True)
        return True

    def on_closed() -> None:
        if shell is not None:
            shell.stop()
        if server is not None:
            log.info("окно закрыто, останавливаю службу")
            server.should_exit = True
        else:
            log.info("окно закрыто, служба продолжает работать в другом экземпляре")

    window.events.closing += on_closing
    window.events.closed += on_closed

    platform.system().hide_console()
    try:
        webview.start(gui="edgechromium", private_mode=False,
                      storage_path=_webview_storage(), icon=_window_icon())
    except Exception as err:
        log.error("окно не открылось: %s", err)
        return -1

    if server is not None:
        server.should_exit = True
    if thread is not None:
        thread.join(timeout=25)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Hagen — локальная служба")
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--app", action="store_true",
                    help="открыть в собственном окне вместо браузера")
    ap.add_argument("--browser", action="store_true",
                    help="принудительно открыть во вкладке браузера")
    ap.add_argument("--tray", action="store_true",
                    help="запуск вместе с Windows: окно спрятано, работает значок у часов")
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--prepare", action="store_true",
                    help="только подготовить модели (экспорт в ONNX) и выйти")
    args = ap.parse_args()

    os.environ.setdefault("OMP_NUM_THREADS", "7")
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    from hagen import platform

    platform.system().set_console_title("Hagen — служба (не закрывайте во время записи)")
    setup_logging(args.log_level)
    log = logging.getLogger("hagen.run")

    # Заставку поднимаем как можно раньше — до импорта моделей и оболочки окна:
    # именно они съедают те самые две секунды. Запуск сразу в трей и подготовка
    # моделей обходятся без неё: мигать экраном незачем.
    splash = None
    if args.app and not args.tray and not args.prepare:
        try:
            splash = platform.shell().splash_show()
        except Exception as err:
            log.info("заставка не показана: %s", err)

    from hagen import asr, config

    config.ensure_dirs()
    # Папку программы могли перенести (другой компьютер, другая папка): пути
    # к Python и ярлыки исправляются сами. Ярлыки — не при подготовке моделей.
    try:
        platform.system().ensure_portable(shortcuts=not args.prepare)
    except Exception as err:
        log.warning("проверка переносимости не удалась: %s", err)

    if args.prepare:
        # Готовится то, что нужно выбору в «Настройки → Модели» (решение 21.09).
        for title, ok in asr.prepare_all():
            log.info("%s: %s", title, "готово" if ok else "НЕ УДАЛОСЬ")
        return 0

    preferred = args.port or int(config.get("port") or 8787)
    want_app_early = args.app or (config.get("ui_mode") == "app" and not args.browser)

    # Уже запущен? Тогда не поднимаем вторую службу: в режиме окна просто
    # открываем ещё одно окно к работающей, иначе сообщаем и выходим.
    if _service_alive(preferred):
        log.info("Hagen уже запущен на порту %d", preferred)
        if args.tray:
            log.info("запуск с Windows: служба уже работает, второй экземпляр не нужен")
            return 0
        if _request_show(preferred):
            log.info("попросил работающий Hagen показать окно")
            return 0
        if platform.shell().focus_existing(WINDOW_TITLE):
            log.info("поднял уже открытое окно, второй экземпляр не нужен")
            return 0
        if want_app_early and not args.no_browser:
            from hagen import server as _srv0

            _srv0.set_actual_port(preferred)
            code = run_as_app(preferred, log, own_service=False)
            if code >= 0:
                return code
        _message_box("Hagen",
                     "Hagen уже запущен. Окно должно быть на панели задач, "
                     "либо откройте http://127.0.0.1:%d/" % preferred)
        return 0

    port = pick_port(preferred)
    if port != preferred:
        # В настройки запасной порт НЕ пишем: иначе он будет уползать вверх при
        # каждом неудачном запуске. Настройка остаётся прежней, а этот запуск
        # работает на свободном порту.
        log.warning("порт %d занят, работаю на %d", preferred, port)

    from hagen import server as _srv

    _srv.set_actual_port(port)

    want_app = want_app_early and not args.no_browser

    if not want_app and not args.no_browser and config.get("open_browser"):
        open_browser_when_ready(port)

    log.info("=" * 62)
    log.info("Hagen: http://%s:%d/", HOST, port)
    log.info("Интерпретатор: %s", sys.executable)
    log.info("Процесс: %d", os.getpid())
    log.info("Записи: %s", config.DATA_DIR)
    log.info("Стенограммы: %s", config.vault_root())
    log.info("Чтобы остановить службу — закройте это окно или нажмите Ctrl+C")
    log.info("=" * 62)

    if want_app:
        code = run_as_app(port, log, start_hidden=args.tray)
        if code >= 0:
            return code
        # окно не открылось — не бросаем пользователя, уходим в браузер
        log.warning("не удалось открыть окно приложения, открываю браузер")
        open_browser_when_ready(port)

    import uvicorn

    from hagen.server import app

    uvicorn.run(app, host=HOST, port=port, log_level=args.log_level.lower(),
                ws_max_size=8 * 1024 * 1024, timeout_graceful_shutdown=20,
                access_log=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nСлужба остановлена.")
        sys.exit(0)
