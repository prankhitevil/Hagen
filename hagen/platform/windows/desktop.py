# -*- coding: utf-8 -*-
"""Розетка «Окна» для Windows: начало звонка и текущая встреча из Outlook.

Что описывает розетка — в `hagen/platform/base.py`, класс `Desktop`.
Переехало сюда из `hagen/win_detect.py` без переделки.

Часть А — детект звонка по аудиосессиям Windows (pycaw/WASAPI, без прав
администратора). Мы ничего не захватываем, только читаем состояние сессий и
уровень сигнала, поэтому запрет Kaspersky на захват звука тут не работает.

Часть Б — чтение календаря классического Outlook через COM (pywin32).
Новый Outlook (olk.exe) COM не предоставляет, это честно отражено в
:func:`outlook_kind`.

Все тяжёлые зависимости (comtypes/pycaw, psutil, pywin32) импортируются лениво
внутри функций, чтобы служба стартовала быстро и падение одной подсистемы не
ломало импорт модуля.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from ... import config

log = logging.getLogger("hagen.desktop")

__all__ = [
    "list_sessions",
    "list_capture_sessions",
    "detect_call",
    "call_output_device",
    "watch_calls",
    "outlook_kind",
    "compose_mail",
    "current_meeting",
    "suggest_title",
    "attendee_names",
    "suggest_max_speakers",
]

# --- состояния аудиосессии (AudioSessionState из Windows SDK) ---
STATE_INACTIVE = 0
STATE_ACTIVE = 1
STATE_EXPIRED = 2

STATE_NAMES: dict[int, str] = {
    STATE_INACTIVE: "простаивает",
    STATE_ACTIVE: "активна",
    STATE_EXPIRED: "истекла",
}

# --- константы EDataFlow / DEVICE_STATE ---
E_RENDER = 0
E_CAPTURE = 1
DEVICE_STATE_ACTIVE = 1

#: ниже этого пика считаем, что сессия молчит (дрожание метра WASAPI)
PEAK_THRESHOLD = 0.0005

#: тишина дольше этого — звонок закончился
CALL_END_SILENCE_S = 15.0

#: короткие провалы уровня (пауза в речи) не сбрасывают дебаунс
ACTIVITY_GRACE_S = 5.0

#: опрос не чаще одного раза в столько секунд
MIN_POLL_S = 2.0

# Память о моменте начала активности: "процесс:pid" -> time.time().
# Нужна, чтобы detect_call() мог вернуть "since", оставаясь функцией без объекта.
_since_lock = threading.Lock()
_since: dict[str, float] = {}

# Удалось ли последнее перечисление аудиосессий: так отличаем «pycaw не работает»
# от «сессий просто нет» (иначе интерфейс врал бы, что детект недоступен).
_enum_ok = True


# ---------------------------------------------------------------- COM-помощники


def _co_init() -> bool:
    """Инициализирует COM в текущем потоке. True, если потом надо закрыть.

    Вызываем из фоновых потоков FastAPI, поэтому без этого любой COM-вызов
    упадёт. Если поток уже в MTA (RPC_E_CHANGED_MODE), COM всё равно пригоден,
    но закрывать его мы не должны — возвращаем False.
    """
    try:
        import pythoncom

        pythoncom.CoInitialize()
        return True
    except Exception:
        try:
            import comtypes

            comtypes.CoInitialize()
            return True
        except Exception as err:  # COM вообще недоступен
            log.debug("не удалось инициализировать COM: %s", err)
            return False


def _co_uninit(owned: bool) -> None:
    if not owned:
        return
    try:
        import pythoncom

        pythoncom.CoUninitialize()
        return
    except Exception:
        pass
    try:
        import comtypes

        comtypes.CoUninitialize()
    except Exception:
        pass


def _state_name(state: Any) -> str:
    try:
        return STATE_NAMES.get(int(state), "неизвестно")
    except Exception:
        return "неизвестно"


def _session_peak(ctl2: Any) -> float:
    """Пик сессии 0..1. Сессия может быть «активна», но молчать."""
    try:
        from pycaw.pycaw import IAudioMeterInformation

        meter = ctl2.QueryInterface(IAudioMeterInformation)
        return float(meter.GetPeakValue())
    except Exception:
        return 0.0


def _session_info(session: Any, ctl2: Any) -> dict[str, Any]:
    pid: int | None
    try:
        pid = int(session.ProcessId)
    except Exception:
        pid = None
    if pid == 0:
        pid = None  # системные звуки
    name: str | None = None
    try:
        proc = session.Process
        if proc is not None:
            name = proc.name()
    except Exception:
        name = None
    try:
        state = int(session.State)
    except Exception:
        state = STATE_INACTIVE
    return {
        "pid": pid,
        "process": name,
        "state": state,
        "state_name": _state_name(state),
        "peak": _session_peak(ctl2),
    }


def _endpoint_sessions(flow: int) -> list[dict[str, Any]]:
    """Сессии на ВСЕХ активных устройствах направления ``flow``.

    ``flow``: :data:`E_RENDER` (вывод) или :data:`E_CAPTURE` (захват).
    COM должен быть уже инициализирован вызывающим.

    Почему не ``AudioUtilities.GetAllSessions()``: он смотрит ТОЛЬКО устройство
    вывода по умолчанию. На этой машине активных устройств вывода два (гарнитура
    Jabra и встроенные колонки), и Teams/Zoom спокойно играют в НЕ-основное
    устройство — тогда сессию звонка мы бы не увидели вообще.
    """
    import comtypes
    from pycaw.pycaw import (
        AudioSession,
        AudioUtilities,
        IAudioSessionControl2,
        IAudioSessionManager2,
    )

    out: list[dict[str, Any]] = []
    enumerator = AudioUtilities.GetDeviceEnumerator()
    if enumerator is None:
        raise RuntimeError("перечислитель аудиоустройств недоступен")
    collection = enumerator.EnumAudioEndpoints(flow, DEVICE_STATE_ACTIVE)
    if collection is None:
        raise RuntimeError("не удалось получить список аудиоустройств")
    for i in range(collection.GetCount()):
        try:
            dev = collection.Item(i)
            if dev is None:
                continue
            try:
                friendly = AudioUtilities.CreateDevice(dev).FriendlyName or ""
            except Exception:
                friendly = ""
            iface = dev.Activate(IAudioSessionManager2._iid_, comtypes.CLSCTX_ALL, None)
            mgr = iface.QueryInterface(IAudioSessionManager2)
            enum_sessions = mgr.GetSessionEnumerator()
            for j in range(enum_sessions.GetCount()):
                try:
                    ctl = enum_sessions.GetSession(j)
                    if ctl is None:
                        continue
                    ctl2 = ctl.QueryInterface(IAudioSessionControl2)
                    info = _session_info(AudioSession(ctl2), ctl2)
                    info["device"] = friendly
                    out.append(info)
                except Exception:
                    continue
        except Exception:
            continue
    return out


def _default_render_sessions() -> list[dict[str, Any]]:
    """Резерв: сессии только устройства вывода по умолчанию (pycaw-путь)."""
    from pycaw.pycaw import AudioUtilities

    out: list[dict[str, Any]] = []
    for session in AudioUtilities.GetAllSessions():
        try:
            info = _session_info(session, session._ctl)
            info["device"] = ""
            out.append(info)
        except Exception:
            continue
    return out


def _collect_sessions(flow: int) -> list[dict[str, Any]]:
    """Перечисление сессий с мягкой деградацией. Наружу исключения не летят."""
    global _enum_ok
    owned = _co_init()
    try:
        out = _endpoint_sessions(flow)
        _enum_ok = True
        return out
    except Exception as err:
        log.debug("перечисление сессий (flow=%s) не удалось: %s", flow, err)
        if flow == E_RENDER:
            try:
                out = _default_render_sessions()
                _enum_ok = True
                return out
            except Exception as err2:
                log.debug("резервное перечисление сессий вывода не удалось: %s", err2)
        _enum_ok = False
        return []
    finally:
        _co_uninit(owned)


def list_sessions() -> list[dict[str, Any]]:
    """Все аудиосессии устройств ВЫВОДА (всех активных, не только основного).

    Возвращает список словарей
    ``{"pid", "process", "state", "state_name", "peak", "device"}``.
    Любая ошибка гасится — наружу уходит пустой список.
    """
    return _collect_sessions(E_RENDER)


def list_capture_sessions() -> list[dict[str, Any]]:
    """Аудиосессии устройств ЗАХВАТА (микрофоны): кто сейчас держит микрофон.

    Перебираем все активные endpoint'ы eCapture, на каждом активируем
    IAudioSessionManager2 и читаем его перечислитель сессий. Дополнительно к
    обычным полям кладём ``device`` — понятное имя устройства.
    """
    return _collect_sessions(E_CAPTURE)


# ------------------------------------------------------------------ детект звонка


def _watched_names() -> set[str]:
    names: Iterable[Any] = config.get("call_watch_processes") or []
    return {str(n).strip().lower() for n in names if str(n).strip()}


def _mark_since(key: str, now: float) -> float:
    """Запоминает момент, когда процесс впервые зашумел, и чистит остальные."""
    with _since_lock:
        started = _since.get(key)
        if started is None:
            started = now
        # держим только текущего кандидата: звонок один
        _since.clear()
        _since[key] = started
        return started


def _forget_since() -> None:
    with _since_lock:
        _since.clear()


#: Программы, которые держат микрофон ТОЛЬКО ради звонка. У остальных микрофон
#: бывает занят и без звонка — голосовое сообщение в Telegram, голосовой ввод
#: в браузере, — поэтому у них звонком считаем микрофон вместе с выводом звука.
#: Запись по звонку начинается сама, и ложный звонок стоил бы лишней записи.
CALL_ONLY_APPS = {"ms-teams.exe", "teams.exe", "zoom.exe", "cpthost.exe",
                  "webex.exe", "atmgr.exe"}


def detect_call() -> dict[str, Any]:
    """Идёт ли прямо сейчас звонок.

    При ``call_watch_require_mic`` (по умолчанию) звонок — это программа из
    ``call_watch_processes``, которая ДЕРЖИТ МИКРОФОН (активная сессия захвата)
    и открыла вывод звука (активная сессия вывода). Громкость не важна.

    Раньше требовали ещё и слышимый звук: пик выше :data:`PEAK_THRESHOLD`.
    Живой звонок 12.09 показал, чем это плохо: пока собеседник молчит, звонка
    «нет» — предложение записать приходило поздно, а когда у собеседника
    отвалился микрофон, звонок через 15 с «заканчивался» и через минуту
    «начинался» снова (шесть раз за четверть часа). Teams же держит микрофон
    и поток вывода всю встречу, даже в тишине, а вне звонка не держит ни одной
    сессии — проверено на этой машине.

    Без ``call_watch_require_mic`` остаётся старый признак: слышимый звук.

    Возвращает ``{"active", "process", "pid", "since", "peak", "mic_in_use",
    "candidates", "loud_processes", "available"}``.
    """
    now = time.time()
    result: dict[str, Any] = {
        "active": False,
        "process": None,
        "pid": None,
        "since": None,
        "peak": 0.0,
        "mic_in_use": False,
        "candidates": [],
        "loud_processes": [],
        "available": False,
    }

    sessions = list_sessions()
    result["available"] = bool(_enum_ok)
    watched = _watched_names()
    require_mic = bool(config.get("call_watch_require_mic"))

    if require_mic:
        return _detect_by_mic(sessions, watched, result, now)

    if not sessions:
        _forget_since()
        return result

    # кто сейчас реально шумит (для диагностики ложных срабатываний)
    loud: dict[str, float] = {}
    for s in sessions:
        name = (s.get("process") or "").strip()
        if not name:
            continue
        if int(s.get("state") or 0) != STATE_ACTIVE:
            continue
        peak = float(s.get("peak") or 0.0)
        if peak <= PEAK_THRESHOLD:
            continue
        key = name.lower()
        if peak > loud.get(key, 0.0):
            loud[key] = peak
    result["loud_processes"] = sorted(loud)

    # кандидаты: шумят И отслеживаются
    cands: list[dict[str, Any]] = []
    for s in sessions:
        name = (s.get("process") or "").strip()
        if not name or name.lower() not in watched:
            continue
        if int(s.get("state") or 0) != STATE_ACTIVE:
            continue
        peak = float(s.get("peak") or 0.0)
        if peak <= PEAK_THRESHOLD:
            continue
        cands.append({"process": name, "pid": s.get("pid"), "peak": peak})
    result["candidates"] = sorted({c["process"] for c in cands})
    result["mic_in_use"] = any(int(s.get("state") or 0) == STATE_ACTIVE
                               for s in list_capture_sessions())

    if not cands:
        _forget_since()
        return result
    return _choose(max(cands, key=lambda c: c["peak"]), result, now)


def _choose(chosen: dict[str, Any], result: dict[str, Any], now: float) -> dict[str, Any]:
    key = "%s:%s" % ((chosen.get("process") or "?").lower(), chosen.get("pid"))
    result["active"] = True
    result["process"] = chosen.get("process")
    result["pid"] = chosen.get("pid")
    result["peak"] = float(chosen.get("peak") or 0.0)
    result["since"] = _mark_since(key, now)
    return result


def _detect_by_mic(sessions: list[dict[str, Any]], watched: set[str],
                   result: dict[str, Any], now: float) -> dict[str, Any]:
    """Звонок по микрофону: см. :func:`detect_call`."""
    loud: dict[str, float] = {}
    playing: dict[str, dict[str, Any]] = {}
    for s in sessions:
        name = (s.get("process") or "").strip()
        if not name or int(s.get("state") or 0) != STATE_ACTIVE:
            continue
        key = name.lower()
        peak = float(s.get("peak") or 0.0)
        if peak > PEAK_THRESHOLD and peak > loud.get(key, 0.0):
            loud[key] = peak
        if key in watched:
            prev = playing.get(key)
            if prev is None or peak > prev["peak"]:
                playing[key] = {"process": name, "pid": s.get("pid"), "peak": peak}
    result["loud_processes"] = sorted(loud)

    holding: dict[str, dict[str, Any]] = {}
    for s in list_capture_sessions():
        if int(s.get("state") or 0) != STATE_ACTIVE:
            continue
        result["mic_in_use"] = True
        name = (s.get("process") or "").strip()
        key = name.lower()
        if name and key in watched and key not in holding:
            holding[key] = {"process": name, "pid": s.get("pid"), "peak": 0.0}
    result["candidates"] = sorted({v["process"] for v in holding.values()})

    # микрофон + вывод звука у одной программы — звонок в любой программе
    both = [playing[k] for k in holding if k in playing]
    if both:
        return _choose(max(both, key=lambda c: c["peak"]), result, now)
    # только микрофон — звонок лишь у программ, которым микрофон нужен для звонков
    apps = [v for k, v in holding.items() if k in CALL_ONLY_APPS]
    if apps:
        return _choose(apps[0], result, now)
    _forget_since()
    result["peak"] = max(loud.values()) if loud else 0.0
    return result


def call_output_device(sessions: list[dict[str, Any]] | None = None,
                       holders: set[str] | None = None) -> dict[str, Any] | None:
    """Устройство вывода, куда программа звонка СЕЙЧАС выводит звук.

    Зачем: запись собеседников идёт с петли вывода, и писать надо ровно то
    устройство, куда играет звонок. «Устройство по умолчанию» для этого не
    годится: 13.09 Windows держала по умолчанию выход монитора Dell, Teams играл
    в колонки, и дорожка собеседников весь звонок была ровной тишиной.

    Программа звонка — та, что ДЕРЖИТ МИКРОФОН (при ``call_watch_require_mic``,
    как и в :func:`detect_call`). Иначе любой звук в браузере или Telegram на
    другом устройстве уводил бы запись собеседников от Teams.
    ``holders`` — имена таких процессов; None при живом опросе —
    посмотреть самим, при готовом ``sessions`` — не ограничивать.

    Если у программы звонка активны сессии на нескольких устройствах, берём ту,
    где звук громче; при равенстве — устройство связи Windows.
    Возвращает {"device", "process", "peak"} или None.
    """
    if sessions is None:
        sessions = list_sessions()
        if holders is None and config.get("call_watch_require_mic"):
            holders = {(s.get("process") or "").strip().lower()
                       for s in list_capture_sessions()
                       if int(s.get("state") or 0) == STATE_ACTIVE}
    watched = _watched_names()
    cands = [s for s in sessions
             if (s.get("process") or "").strip().lower() in watched
             and (holders is None or (s.get("process") or "").strip().lower() in holders)
             and int(s.get("state") or 0) == STATE_ACTIVE and s.get("device")]
    if not cands:
        return None
    comm = ""
    if len(cands) > 1:
        try:
            # Сосед по этой же папке — звук; через розетку сюда не ходим.
            from . import loopback

            comm = loopback.default_render_devices().get("communications") or ""
        except Exception:
            comm = ""

    def rank(s: dict[str, Any]) -> tuple[int, float, int]:
        peak = float(s.get("peak") or 0.0)
        return (1 if peak > PEAK_THRESHOLD else 0, peak,
                1 if comm and str(s.get("device")) == comm else 0)

    best = max(cands, key=rank)
    return {"device": str(best.get("device")), "process": best.get("process"),
            "peak": float(best.get("peak") or 0.0)}


class CallWatcher:
    """Фоновый наблюдатель за началом и концом звонка.

    Опрашивает :func:`detect_call` раз в ``interval_s`` секунд (не чаще
    :data:`MIN_POLL_S`). Когда активность держится ``call_watch_debounce_s``
    секунд, ОДИН раз вызывает ``on_call_start(info)``. Пока звонок не кончился,
    повторно не дёргает. Тишина дольше :data:`CALL_END_SILENCE_S` секунд —
    ``on_call_end(info)`` и сброс состояния.
    """

    def __init__(
        self,
        on_call_start: Callable[[dict[str, Any]], None] | None = None,
        on_call_end: Callable[[dict[str, Any]], None] | None = None,
        interval_s: float = MIN_POLL_S,
    ) -> None:
        self.on_call_start = on_call_start
        self.on_call_end = on_call_end
        self.interval_s = max(MIN_POLL_S, float(interval_s or MIN_POLL_S))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active_since: float | None = None
        self._last_active: float | None = None
        self._notified = False
        self._snooze_until = 0.0
        self._last_info: dict[str, Any] = {}
        # последний снимок, когда звонок БЫЛ активен: по нему сообщаем о конце
        self._active_info: dict[str, Any] = {}
        self._last_check: float | None = None
        self._error: str | None = None

    # ---- управление

    def start(self) -> None:
        """Запускает поток опроса. Повторный вызов безопасен."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="call-watcher", daemon=True
            )
            self._thread.start()
        log.info("наблюдатель за звонками запущен, опрос раз в %.0f с", self.interval_s)

    def stop(self, timeout: float = 5.0) -> None:
        """Просит поток остановиться и ждёт его не дольше timeout."""
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        log.info("наблюдатель за звонками остановлен")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def snooze(self, seconds: float) -> float:
        """Не спрашивать про звонок ещё столько секунд (пользователь ответил «Нет»)."""
        until = time.time() + max(0.0, float(seconds or 0.0))
        with self._lock:
            self._snooze_until = max(self._snooze_until, until)
            return self._snooze_until

    def reset(self) -> None:
        """Полный сброс состояния: следующий звонок снова вызовет on_call_start."""
        with self._lock:
            self._active_since = None
            self._last_active = None
            self._notified = False
            self._last_info = {}
            self._active_info = {}

    @property
    def state(self) -> dict[str, Any]:
        with self._lock:
            info = dict(self._last_info)
            # во время паузы в речи info пустеет — процесс показываем по активному снимку
            owner = info if info.get("active") else (self._active_info or info)
            return {
                "running": self.running,
                "active": bool(info.get("active")),
                "notified": self._notified,
                "process": owner.get("process"),
                "pid": owner.get("pid"),
                "peak": float(info.get("peak") or 0.0),
                "mic_in_use": bool(info.get("mic_in_use")),
                "candidates": list(info.get("candidates") or []),
                "loud_processes": list(info.get("loud_processes") or []),
                "available": bool(info.get("available")),
                "since": self._active_since,
                "snoozed_until": self._snooze_until or None,
                "snoozed": self._snooze_until > time.time(),
                "last_check": self._last_check,
                "interval_s": self.interval_s,
                "error": self._error,
            }

    # ---- внутреннее

    def _debounce_s(self) -> float:
        try:
            return max(0.0, float(config.get("call_watch_debounce_s") or 0.0))
        except Exception:
            return 6.0

    def _fire(self, cb: Callable[[dict[str, Any]], None] | None, info: dict[str, Any]) -> None:
        if cb is None:
            return
        try:
            cb(dict(info))
        except Exception:
            log.exception("обработчик события звонка упал")

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as err:  # цикл не должен умирать никогда
                with self._lock:
                    self._error = str(err)
                log.debug("опрос звонка не удался: %s", err)
            self._stop.wait(self.interval_s)

    def _tick(self) -> None:
        if not config.get("call_watch_enabled", True):
            with self._lock:
                self._last_check = time.time()
            return

        info = detect_call()
        now = time.time()
        start_info: dict[str, Any] | None = None
        end_info: dict[str, Any] | None = None

        with self._lock:
            self._last_check = now
            self._last_info = info
            self._error = None

            if info.get("active"):
                gap_too_long = (
                    self._last_active is None or (now - self._last_active) > ACTIVITY_GRACE_S
                )
                if gap_too_long and not self._notified:
                    # перерыв был слишком долгим: отсчёт дебаунса с нуля
                    self._active_since = now
                elif self._active_since is None:
                    self._active_since = now
                self._last_active = now
                self._active_info = dict(info)

                ready = (now - float(self._active_since)) >= self._debounce_s()
                if ready and not self._notified and now >= self._snooze_until:
                    self._notified = True
                    start_info = dict(info)
                    start_info["since"] = self._active_since
            else:
                if (
                    self._last_active is not None
                    and (now - self._last_active) >= CALL_END_SILENCE_S
                ):
                    if self._notified:
                        # берём последний АКТИВНЫЙ снимок: в info уже тишина
                        end_info = dict(self._active_info or self._last_info)
                        end_info["since"] = self._active_since
                        end_info["ended_at"] = self._last_active
                        end_info["active"] = False
                    self._active_since = None
                    self._last_active = None
                    self._notified = False
                    self._active_info = {}

        if start_info is not None:
            log.info(
                "похоже на звонок: %s (pid %s), микрофон занят: %s",
                start_info.get("process"), start_info.get("pid"), start_info.get("mic_in_use"),
            )
            self._fire(self.on_call_start, start_info)
        if end_info is not None:
            log.info("звонок, похоже, закончился: %s", end_info.get("process"))
            self._fire(self.on_call_end, end_info)


def watch_calls(on_call_start: Callable[[dict[str, Any]], Any],
                on_call_end: Callable[[dict[str, Any]], Any]) -> CallWatcher:
    """Завести сторожа звонков. Наружу отдаётся только `start()` и `stop()`."""
    return CallWatcher(on_call_start=on_call_start, on_call_end=on_call_end)


# ----------------------------------------------------------------------- Outlook


def outlook_kind() -> dict[str, Any]:
    """Какой Outlook запущен: классический (COM есть) или новый (COM нет)."""
    classic = False
    new = False
    try:
        import psutil

        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except Exception:
                continue
            if name == "outlook.exe":
                classic = True
            elif name == "olk.exe":
                new = True
            if classic and new:
                break
    except Exception as err:
        log.debug("не удалось перечислить процессы Outlook: %s", err)

    pywin32_ok = True
    try:
        import win32com.client  # noqa: F401
    except Exception:
        pywin32_ok = False

    com_available = bool(classic and pywin32_ok)
    if com_available:
        note = "Классический Outlook запущен, календарь читается через COM."
    elif classic and not pywin32_ok:
        note = "Outlook запущен, но pywin32 недоступен — календарь прочитать нельзя."
    elif new and not classic:
        note = (
            "Запущен новый Outlook (olk.exe): он не даёт COM-интерфейс, "
            "встречу подтянуть нельзя. Нужен классический OUTLOOK.EXE."
        )
    else:
        note = "Outlook не запущен — встреча не определяется."
    return {"classic": classic, "new": new, "com_available": com_available, "note": note}


#: Письмо создаётся тем же COM, которым читается календарь (olMailItem = 0).
#: Новый Outlook (olk.exe) COM не даёт — там ядро уходит на путь `mailto:`.
OL_MAIL_ITEM = 0


def compose_mail(subject: str, body: str,
                 attachments: list[str] | None = None,
                 to: list[str] | None = None) -> bool:
    """Открыть новое письмо в классическом Outlook. Не отправляет — показывает.

    `Display()` вместо `Send()` намеренно: отправка письма необратима, и
    последнее слово остаётся за человеком — как и с задачами в Todoist.
    Возвращает False, если классического Outlook нет: собрать письмо с
    вложением нечем, и звавший уходит на короткий `mailto:`.
    """
    owned = _co_init()
    try:
        import win32com.client

        try:
            app = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            # Outlook не в памяти — поднимаем его сам: человек нажал «Отправить»,
            # то есть письмо ему нужно прямо сейчас.
            try:
                app = win32com.client.Dispatch("Outlook.Application")
            except Exception as err:
                log.info("письмо через Outlook не собрать: %s", err)
                return False
        mail = app.CreateItem(OL_MAIL_ITEM)
        mail.Subject = str(subject or "")
        mail.Body = str(body or "")
        for addr in (to or []):
            addr = str(addr or "").strip()
            if addr:
                mail.Recipients.Add(addr)
        for path in (attachments or []):
            try:
                mail.Attachments.Add(str(path))
            except Exception as err:
                # Одно непривязавшееся вложение не должно отменять письмо:
                # человек увидит письмо и поймёт, чего в нём не хватает.
                log.warning("вложение %s не добавилось: %s", path, err)
        mail.Display()
        return True
    except Exception as err:
        log.warning("письмо в Outlook не открылось: %s", err)
        return False
    finally:
        _co_uninit(owned)


def _naive(value: Any) -> datetime | None:
    """Снять с pywintypes.datetime показания часов, не трогая пояс."""
    if value is None:
        return None
    try:
        return datetime(
            int(value.year), int(value.month), int(value.day),
            int(value.hour), int(value.minute), int(getattr(value, "second", 0) or 0),
        )
    except Exception:
        return None


def _to_naive_dt(value: Any, utc_value: Any = None) -> datetime | None:
    """pywintypes.datetime -> обычный naive datetime в местном времени.

    Поясу, который приписывает pywin32, верить нельзя. Найдено 16.09 на рабочем
    ноутбуке: встреча в 9:30 по Москве приходит как ``9:30 tzinfo=GMT (UTC+0)``,
    и перевод «в местное время» делал из неё 12:30 — звонок в 12:21 получил имя
    встречи, которая была тремя часами раньше. Но и обратное однажды наблюдалось
    (13.09): время приходило в UTC, и без перевода встреча уезжала на три часа
    назад.

    Поэтому пояс определяем по самой встрече: рядом с ``Start`` Outlook отдаёт
    ``StartUTC``. Разница между ними и есть настоящее смещение показаний часов:
      * разница ноль — часы показывают UTC, переводим в местное;
      * разница есть — часы уже местные, берём как есть.
    Нет ``StartUTC`` — верим показаниям как местным: так их показывает Outlook.
    """
    shown = _naive(value)
    if shown is None:
        return None
    in_utc = _naive(utc_value)
    if in_utc is None:
        return shown
    try:
        if abs((shown - in_utc).total_seconds()) < 60:
            offset = datetime.now().astimezone().utcoffset() or timedelta(0)
            return shown + offset
    except Exception:
        return shown
    return shown


def _item_dt(item: Any, field: str) -> datetime | None:
    """Время встречи в местном поясе: показания часов сверяем с UTC-двойником."""
    return _to_naive_dt(getattr(item, field, None), getattr(item, field + "UTC", None))


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    try:
        return dt.astimezone().isoformat(timespec="seconds")
    except Exception:
        return dt.isoformat(timespec="seconds")


def _split_attendees(raw: Any) -> list[str]:
    """Строка «Иванов; Петров» -> список имён без лишних пробелов."""
    if not raw:
        return []
    out: list[str] = []
    for part in str(raw).replace("\r", " ").replace("\n", " ").split(";"):
        name = " ".join(part.split()).strip()
        if name:
            out.append(name)
    return out


def _dedup(names: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in names:
        key = n.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(n)
    return out


def _appointment_to_dict(item: Any) -> dict[str, Any] | None:
    start = _item_dt(item, "Start")
    end = _item_dt(item, "End")
    if start is None:
        return None

    names: list[str] = []
    names += _split_attendees(getattr(item, "RequiredAttendees", ""))
    names += _split_attendees(getattr(item, "OptionalAttendees", ""))
    try:
        recipients = item.Recipients
        for i in range(1, int(recipients.Count) + 1):
            try:
                rname = " ".join(str(recipients.Item(i).Name or "").split()).strip()
                if rname:
                    names.append(rname)
            except Exception:
                continue
    except Exception:
        pass
    names = _dedup(names)

    organizer = ""
    try:
        organizer = " ".join(str(getattr(item, "Organizer", "") or "").split()).strip()
    except Exception:
        organizer = ""

    body = ""
    try:
        body = str(getattr(item, "Body", "") or "")
    except Exception:
        body = ""
    body_preview = " ".join(body.split())[:300]

    subject = ""
    try:
        subject = " ".join(str(getattr(item, "Subject", "") or "").split()).strip()
    except Exception:
        subject = ""

    location = ""
    try:
        location = " ".join(str(getattr(item, "Location", "") or "").split()).strip()
    except Exception:
        location = ""

    return {
        "subject": subject or "Без темы",
        "start": _iso(start),
        "end": _iso(end),
        "organizer": organizer,
        "attendees": names,
        "location": location,
        "body_preview": body_preview,
        "n_attendees": len(names),
        # служебное, наружу не мешает
        "_start_dt": start,
        "_end_dt": end,
    }


#: Форматы даты для Items.Restrict. ВАЖНО: Outlook разбирает дату по локали своего
#: интерфейса, а не по американскому образцу. На русском Outlook строка
#: "09/11/2026" читается как 9 НОЯБРЯ, и фильтр молча отдаёт другой диапазон
#: (проверено на этой машине). Поэтому форматы перебираем и калибруемся по факту:
#: подходящим считаем тот, который реально вернул встречи внутри окна.
_RESTRICT_FORMATS = (
    "%d/%m/%Y %H:%M",   # локаль с днём впереди (русский Outlook)
    "%m/%d/%Y %H:%M",   # американский порядок
    "%Y-%m-%d %H:%M",   # ISO, понимают не все сборки
    "%d.%m.%Y %H:%M",   # русский короткий формат
)

_fmt_lock = threading.Lock()
_good_fmt: str | None = None  # формат, который сработал в прошлый раз


def _ordered_formats() -> list[str]:
    with _fmt_lock:
        good = _good_fmt
    order = list(_RESTRICT_FORMATS)
    if good and good in order:
        order.remove(good)
        order.insert(0, good)
    return order


def _remember_format(fmt: str) -> None:
    global _good_fmt
    with _fmt_lock:
        _good_fmt = fmt


def _scan_window(
    cal: Any, fmt: str, lower: datetime, upper: datetime, now: datetime, window_s: float
) -> list[dict[str, Any]]:
    """Один проход по календарю с данным форматом даты в фильтре.

    Возвращает только встречи, у которых НАСТОЯЩЕЕ время попадает в окно: даже
    если Outlook понял фильтр криво, наружу неверная встреча не уйдёт.
    """
    items = cal.Items
    items.Sort("[Start]")          # СОРТИРОВКА строго ДО IncludeRecurrences
    items.IncludeRecurrences = True
    flt = (
        "[Start] >= '" + lower.strftime(fmt) + "'"
        " AND [Start] <= '" + upper.strftime(fmt) + "'"
    )
    try:
        found = items.Restrict(flt)
    except Exception as err:
        log.debug("фильтр календаря с форматом %r отвергнут: %s", fmt, err)
        return []

    out: list[dict[str, Any]] = []
    seen = 0
    past_upper = 0
    for item in found:  # с IncludeRecurrences перебор только итератором
        seen += 1
        if seen > 2000:
            break
        try:
            start = _item_dt(item, "Start")
            if start is None:
                continue
            if start > upper:
                # коллекция отсортирована по [Start]: дальше только позже
                past_upper += 1
                if past_upper >= 20:
                    break
                continue
            past_upper = 0
            if start < lower:
                continue
            if bool(getattr(item, "AllDayEvent", False)):
                continue
            # 5 = olMeetingCanceled, 7 = olMeetingReceivedAndCanceled
            if int(getattr(item, "MeetingStatus", 0) or 0) in (5, 7):
                continue
            data = _appointment_to_dict(item)
        except Exception:
            continue
        if data is None:
            continue
        s_dt: datetime = data["_start_dt"]
        e_dt: datetime | None = data["_end_dt"]
        ongoing = e_dt is not None and s_dt <= now <= e_dt
        # Только вперёд: встреча, которая уже кончилась, названия не даёт, даже
        # если началась недавно. Найдено 16.09 — звонок в 12:21 получил имя
        # встречи, стоявшей на 12:30 (решение: брать идущую, будущую —
        # только если она вот-вот начнётся).
        soon = 0 <= (s_dt - now).total_seconds() <= window_s
        if not (ongoing or soon):
            continue
        data["ongoing"] = bool(ongoing)
        out.append(data)
    log.debug("формат %r: просмотрено %d, подошло %d", fmt, seen, len(out))
    return out


#: Сколько других встреч того же времени отдавать вместе с выбранной.
MAX_ALTERNATIVES = 2


def order_meetings(candidates: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Встречи-кандидаты по порядку: первой — та, что начинается ближе к звонку.

    Решение 22.09. Раньше идущая всегда была важнее будущей, и звонок в 14:59
    получил имя встречи, стоявшей в календаре 14:30–15:30, хотя та кончилась в
    14:51, а в 15:00 начиналась другая: встречи в календаре часто стоят
    внахлёст, а кончаются раньше. Начало за 11 секунд ближе, чем начало
    полчаса назад. Ошибиться так можно, опоздав на идущую встречу, когда
    следующая вот-вот начнётся, — на этот случай вторая встреча отдаётся
    вместе с первой, и человек переключит её одной кнопкой.
    """
    return sorted(candidates, key=lambda d: abs((d["_start_dt"] - now).total_seconds()))


def current_meeting(
    window_minutes: int | None = None, start_outlook: bool = False,
    with_alternatives: bool = False,
) -> dict[str, Any] | None:
    """Встреча, которая идёт сейчас или начнётся в ближайшие window_minutes.

    Окно смотрит только вперёд: закончившаяся встреча имени записи не даёт.
    Пусто — берём «за сколько минут до встречи считать звонок её началом» из
    настроек звонков (решение 16.09; было жёстко ±15 минут, и звонок
    за девять минут до планёрки получал её название).

    ``with_alternatives`` — положить в ответ ``alternatives``: другие встречи,
    которые тоже подходят по времени (порядок — `order_meetings`).

    По умолчанию Outlook НЕ запускаем: если его нет в памяти, возвращаем None.
    ``start_outlook=True`` разрешает Dispatch, который поднимет Outlook сам.
    Любая ошибка — None и запись в лог, наружу исключения не летят.
    """
    if not config.get("outlook_enabled", True):
        log.debug("чтение Outlook отключено настройкой outlook_enabled")
        return None

    if window_minutes is None:
        window_minutes = config.get("meeting_lookahead_min")
    window = max(1, int(window_minutes or 5))
    owned = _co_init()
    try:
        import win32com.client

        app = None
        try:
            app = win32com.client.GetActiveObject("Outlook.Application")
        except Exception:
            if not start_outlook:
                log.debug("Outlook не запущен, стартовать его не просили")
                return None
            try:
                app = win32com.client.Dispatch("Outlook.Application")
            except Exception as err:
                log.warning("не удалось подключиться к Outlook: %s", err)
                return None
        if app is None:
            return None

        ns = app.GetNamespace("MAPI")
        cal = ns.GetDefaultFolder(9)  # olFolderCalendar

        now = datetime.now()
        # нижнюю границу берём с запасом, иначе длинная идущая встреча не попадёт
        lower = now - timedelta(hours=12)
        upper = now + timedelta(minutes=window)
        window_s = window * 60.0

        candidates: list[dict[str, Any]] = []
        for fmt in _ordered_formats():
            candidates = _scan_window(cal, fmt, lower, upper, now, window_s)
            if candidates:
                _remember_format(fmt)
                break

        if not candidates:
            log.debug("подходящей встречи в календаре нет")
            return None

        ordered = order_meetings(candidates, now)
        for d in ordered:
            d.pop("_start_dt", None)
            d.pop("_end_dt", None)
        best, others = ordered[0], ordered[1:1 + MAX_ALTERNATIVES]
        log.info(
            "встреча из Outlook: %r, участников %d", best.get("subject"), best.get("n_attendees")
        )
        if others:
            log.info("в это же время в календаре ещё: %s",
                     ", ".join(repr(d.get("subject")) for d in others))
        if with_alternatives:
            best["alternatives"] = others
        return best
    except Exception as err:
        log.warning("не удалось прочитать встречу из Outlook: %s", err)
        return None
    finally:
        _co_uninit(owned)


def suggest_title(meeting: dict[str, Any] | None) -> str:
    """Название записи: тема встречи, иначе дата и время."""
    if meeting:
        subject = " ".join(str(meeting.get("subject") or "").split()).strip()
        if subject and subject != "Без темы":
            return subject[:200]
    return "Запись " + datetime.now().strftime("%d.%m.%Y %H:%M")


def attendee_names(meeting: dict[str, Any] | None) -> list[str]:
    """Имена приглашённых из встречи (пустой список, если встречи нет)."""
    if not meeting:
        return []
    raw = meeting.get("attendees") or []
    if isinstance(raw, str):
        raw = _split_attendees(raw)
    return _dedup(" ".join(str(n).split()).strip() for n in raw if str(n).strip())


def suggest_max_speakers(meeting: dict[str, Any] | None) -> int | None:
    """Подсказка max_speakers для диаризации: от 2 до 10 по числу приглашённых."""
    if not meeting:
        return None
    try:
        n = int(meeting.get("n_attendees") or 0)
    except Exception:
        n = 0
    if n <= 0:
        n = len(attendee_names(meeting))
    if n <= 0:
        return None
    return max(2, min(n, 10))
