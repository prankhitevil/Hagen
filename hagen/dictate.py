# -*- coding: utf-8 -*-
"""Диктовка текста в любое окно: нажал клавишу — сказал — текст вставился.

Считается всё на этом компьютере и теми же моделями, что уже стоят для
совещаний. Ни одного байта наружу не уходит.

Здесь — сама диктовка: когда слушать, что делать с распознанным текстом, когда
спать. Разговор с системой (горячая клавиша, вставка в чужое окно, щелчки,
капсула на экране) живёт в розетке ввода — , класс
.

Два решения, которые важно помнить.

1. Диктовка спит, пока идёт запись совещания (решение 14.09). Микрофон один, и
   делить его между двумя задачами незачем: иначе запись встречи получила бы
   дырку ровно там, где человек диктовал.

2. Язык задаётся в настройках и не угадывается по раскладке Windows. В словаре
   английской модели нет кириллицы: ошибись мы с языком — на выходе был бы не
   честный отказ, а правдоподобный английский бред. Та же причина, по которой
   нет автоопределения языка в asr.transcribe_precise.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Callable

import numpy as np

from . import config, platform

log = logging.getLogger("hagen.dictate")

SR = 16000
#: Предел на одну диктовку. Дальше слушать бессмысленно: скорее всего клавиша
#: залипла или про неё забыли, а память тратится всё это время.
MAX_SECONDS = 300.0
#: Короче этого — точно случайное касание клавиши, распознавать нечего.
MIN_SECONDS = 0.35
#: Граница между «нажал» и «держу» для режима «сама».
TAP_SECONDS = 0.4

# ---------------------------------------------------------------- текст

#: Произнесённые знаки препинания. Включаются отдельной галочкой: сама GigaAM
#: знаки уже расставляет, и человеку, который диктует обычную речь, замена
#: слова «точка» на «.» только мешала бы.
SPOKEN_MARKS: list[tuple[str, str]] = [
    ("новый абзац", "\n\n"),
    ("с новой строки", "\n"),
    ("новая строка", "\n"),
    ("абзац", "\n\n"),
    ("вопросительный знак", "?"),
    ("восклицательный знак", "!"),
    ("многоточие", "…"),
    ("точка с запятой", ";"),
    ("двоеточие", ":"),
    ("запятая", ","),
    ("точка", "."),
    ("тире", "—"),
    ("дефис", "-"),
    ("открыть скобку", "("),
    ("закрыть скобку", ")"),
    ("кавычки", "\""),
]

#: Знаки, которые пишутся вплотную к предыдущему слову.
_TIGHT = ".,;:!?…)»\""
_NO_SPACE_AFTER = "(«"


def _word_re(phrase: str) -> re.Pattern:
    """Шаблон на отдельное слово или оборот, с любым регистром.

    \\b с кириллицей в Python работает верно (он смотрит на \\w, а тот по
    умолчанию юникодный), но перед словом может стоять уже поставленный моделью
    знак — поэтому границу задаём через «не буква».
    """
    body = r"\s+".join(re.escape(w) for w in phrase.split())
    return re.compile(r"(?<!\w)%s(?!\w)" % body, re.IGNORECASE)


def tidy_spaces(text: str) -> str:
    """Прибрать пробелы вокруг знаков и заглавные буквы после точки."""
    out = str(text or "")
    out = re.sub(r"[ \t]+([%s])" % re.escape(_TIGHT), r"\1", out)
    out = re.sub(r"([%s])[ \t]+" % re.escape(_NO_SPACE_AFTER), r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"[ \t]*\n[ \t]*", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    # После точки, «?» и «!» следующее слово с заглавной — модель это делает
    # сама, но после нашей замены «точка» на «.» заглавной там не окажется.
    out = re.sub(r"([.!?…]\s+|\n)([а-яёa-z])",
                 lambda m: m.group(1) + m.group(2).upper(), out)
    return out.strip()


def apply_marks(text: str) -> str:
    """Заменить произнесённые знаки препинания на сами знаки."""
    out = str(text or "")
    for phrase, mark in SPOKEN_MARKS:
        out = _word_re(phrase).sub(mark, out)
    return out


def apply_replacements(text: str, rules: Any) -> str:
    """Свои замены из настроек: [{"from": "джира", "to": "Jira"}, ...].

    Замена идёт по целому слову и без оглядки на регистр: модель то и дело
    ставит заглавную в начале фразы, и правило «джира» не должно из-за этого
    промахиваться. Что писать вместо — берём как написано, буква в букву.
    """
    out = str(text or "")
    for rule in (rules or []):
        if not isinstance(rule, dict):
            continue
        src = str(rule.get("from") or "").strip()
        dst = str(rule.get("to") or "")
        if not src:
            continue
        try:
            out = _word_re(src).sub(lambda m, d=dst: d, out)
        except re.error:
            log.debug("правило замены «%s» не применилось", src)
    return out


#: Тянущиеся звуки: «э-э-э», «ээээ», «м-м», «ы-ы-ы». Через дефисы или подряд.
#: Одиночные «а» и «у» — обычные слова, поэтому в одиночку не убираются; они
#: попадают под правило только в растянутом виде («а-а-а»).
_STRETCH = re.compile(r"(?<!\w)([эаыумн])(?:\s*-\s*\1|\1)+(?!\w)", re.IGNORECASE)
#: Одиночные звуки, которые словами не бывают: «э», «м», «ы».
_SINGLE = re.compile(r"(?<!\w)[эмы](?!\w)", re.IGNORECASE)


def strip_fillers(text: str, words: Any = None) -> str:
    """Убрать слова-паразиты и тянущиеся звуки из надиктованного (16.09).

    Работает списком, без всяких моделей и без интернета: слово из списка
    вырезается целиком, вместе с повисшим пробелом и запятой. Правило тянущихся
    звуков работает всегда: «э-э-э» и «ммм» словами не бывают, перечислять их
    в списке незачем.

    Стенограмм звонков это не касается: там речь остаётся как сказана, а
    паразитов убирает модель при «Сделать документ» (решение 16.09).
    """
    out = str(text or "")
    if not out.strip():
        return out
    # Сначала звуки вместе с их запятыми: «Сейчас, а-а-а, найду» -> «Сейчас найду».
    for pat in (_STRETCH, _SINGLE):
        out = re.sub(r"\s*,\s*(?:%s)\s*,\s*" % pat.pattern, " ", out, flags=re.IGNORECASE)
        out = pat.sub(" ", out)
    for word in (words if words is not None else config.get("dictate_fillers")) or []:
        phrase = " ".join(str(word or "").split())
        if not phrase:
            continue
        try:
            # Вводное слово забирает с собой обе свои запятые: иначе от
            # «Это, короче, важно» осталось бы «Это, важно» с лишней запятой.
            # Изредка запятая была нужна не ему («Я думаю, ну, надо») — тогда
            # её придётся вернуть руками; лишняя запятая мозолит глаз сильнее.
            body = r"\s+".join(re.escape(w) for w in phrase.split())
            out = re.sub(r"\s*,\s*%s\s*,\s*" % body, " ", out, flags=re.IGNORECASE)
            out = _word_re(phrase).sub(" ", out)
        except re.error:
            log.debug("слово-паразит «%s» не применилось", phrase)
    # Предложение, состоявшее из одних паразитов, оставило бы висеть свою точку:
    # «…посмотреть. .». Склеиваем её с предыдущей — обязательно ДО того, как
    # уберём пробел перед знаком, иначе получится «..». Многоточие из трёх точек
    # подряд (его диктуют знаками) не трогаем: там пробелов нет.
    out = re.sub(r"([.!?…])(?:[ \t]*[,;:])*[ \t]+[.!?…]", r"\1", out)
    # Прибрать следы: пробел перед знаком, сдвоенные запятые, запятая в начале
    # предложения и перед точкой — иначе от «ну, вот, готово» осталось бы «, , готово».
    out = re.sub(r"[ \t]+([,.;:!?…])", r"\1", out)
    out = re.sub(r"([,;:])(\s*[,;:])+", r"\1", out)
    out = re.sub(r"\s*[,;:]\s*(?=[.!?…])", "", out)
    out = re.sub(r"(\A|[.!?…][ \t]*|\n)[ \t]*[,;:][ \t]*", r"\1", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    # Первое предложение состояло из одних паразитов — его знак остался в начале.
    out = re.sub(r"\A[ \t]*[.!?…,;:]+[ \t]*", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = out.strip()
    # Фраза состояла из одних паразитов — вставлять голый знак препинания незачем.
    if not re.search(r"\w", out):
        return ""
    # Убрали первое слово предложения — заглавную вернуть новому первому.
    if out and text.strip()[:1].isupper() and out[:1].islower():
        out = out[:1].upper() + out[1:]
    return out


def polish(text: str, marks: bool | None = None, rules: Any = None,
           fillers: Any = None) -> str:
    """Весь путь от распознанного к вставляемому: замены, паразиты, знаки, пробелы.

    ``fillers``: None — как сказано в настройках; False — не чистить вовсе;
    список (хоть пустой) — чистить им. Пустой список не отменяет уборку
    тянущихся звуков, поэтому решает галочка, а не длина списка.
    """
    if marks is None:
        marks = bool(config.get("dictate_marks"))
    if rules is None:
        # Словарь замен один на программу (решение 18.09): берём из
        # него то, что помечено как действующее в диктовке.
        from . import fixes

        rules = fixes.rules("dictation")
    out = apply_replacements(text, rules)
    if fillers is None:
        if config.get("dictate_strip_fillers"):
            out = strip_fillers(out, config.get("dictate_fillers"))
    elif fillers is not False:
        out = strip_fillers(out, fillers)
    if marks:
        out = apply_marks(out)
    return tidy_spaces(out)




# ---------------------------------------------------------------- распознавание


class DictateNeedsModel(RuntimeError):
    """Точная модель ещё не скачана — сказать об этом, а не качать молча."""


def recognise(pcm: np.ndarray, lang: str = "ru") -> str:
    """Распознать надиктованное. Длинную речь режем по паузам.

    У GigaAM жёсткий предел 25 с на кусок, и transcribe_precise просто обрезает
    всё лишнее. Молча потерять хвост длинной диктовки нельзя, поэтому длинное
    сначала разбираем на участки речи, как это делается для файлов.

    words=True здесь не ради отметок времени (они диктовке не нужны), а ради
    выбора пути: при words=False точная модель идёт через ONNX, а ONNX для неё
    в папке не лежит — и первая же диктовка молча ушла бы на несколько минут в
    разовый экспорт модели. С words=True работает torch — тот самый путь, что
    уже прогрет «Перечитать точнее». Появится готовый ONNX точной модели —
    можно будет вернуть False и получить ускорение.
    """
    from . import asr, needs, vad

    arr = np.asarray(pcm, dtype=np.float32).reshape(-1)
    if arr.size < int(MIN_SECONDS * SR):
        return ""
    # Точной модели может не быть в папке (в сборку её не кладут, решение
    # 16.09). gigaam скачал бы её молча, посреди диктовки, — а человек
    # ждал бы вставки текста и не понимал, почему её нет.
    if not needs.precise_ready():
        raise DictateNeedsModel(
            "Для набора текста голосом нужна точная модель распознавания (около 430 МБ). "
            "Скачать: «Настройки → Модели → Части, которые качаются отдельно»."
        )
    limit = asr.max_chunk_seconds(lang)
    if arr.size <= int(limit * SR):
        return asr.transcribe_precise(arr, words=True, lang=lang).text
    spans = vad.split_for_asr(arr, max_s=limit)
    if not spans:
        return ""
    parts = asr.transcribe_spans(arr, spans, precise=True, words=True, lang=lang)
    return " ".join(p["text"] for p in parts if p.get("text"))



# ---------------------------------------------------------------- история

#: Сколько последних диктовок помним. Лежат рядом с записями, на этом же
#: компьютере, и никуда не отправляются — как и всё остальное в программе.
HISTORY_MAX = 100
HISTORY_PATH = config.DATA_DIR / "dictate_history.json"
_history_lock = threading.Lock()


def history() -> list[dict[str, Any]]:
    """Последние диктовки, новые сверху."""
    import json

    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []


def remember(text: str, seconds: float) -> None:
    """Дописать диктовку в историю. Пустые не храним."""
    text = str(text or "").strip()
    if not text:
        return
    with _history_lock:
        items = history()
        items.insert(0, {"at": time.time(), "seconds": round(float(seconds), 1),
                         "text": text})
        try:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            config.atomic_json(HISTORY_PATH, items[:HISTORY_MAX])
        except OSError as err:
            log.warning("история диктовок не сохранилась: %s", err)


def forget_all() -> None:
    with _history_lock:
        try:
            HISTORY_PATH.unlink(missing_ok=True)
        except OSError as err:
            log.warning("история диктовок не очистилась: %s", err)


# ---------------------------------------------------------------- сама диктовка


class Dictation:
    """Состояние диктовки: спит → ждёт → слушает → распознаёт → вставляет.

    busy() — идёт ли сейчас запись совещания. Пока идёт, диктовка не включается:
    микрофон один (решение 14.09).
    on_state(payload) — чтобы окно программы могло показать «капсулу».
    """

    #: Что написано в капсуле на каждом шаге.
    CAPTION = {
        "listening": "Слушаю…",
        "thinking": "Распознаю…",
        "sleeping": "Идёт запись — диктовка спит",
    }

    def __init__(self, busy: Callable[[], bool] | None = None,
                 on_state: Callable[[dict[str, Any]], Any] | None = None,
                 paste: Callable[[str], bool] | None = None,
                 capsule: Any = None) -> None:
        self.busy = busy or (lambda: False)
        self.on_state = on_state
        # Вставку можно подменить — этим пользуются проверки; по умолчанию она
        # берётся у розетки ввода, как и всё остальное общение с системой.
        self.paste = paste or (lambda text: platform.input().paste_text(text))
        self.capsule = capsule
        self.stage = "off"          # off | idle | listening | thinking | sleeping
        self.last_text = ""
        self.last_error = ""
        self.hotkey: Any = None          # разобранное сочетание из розетки ввода
        self.listener: Any = None
        self._rec: Any = None
        self._chunks: list[np.ndarray] = []
        self._started_at = 0.0
        self._lock = threading.RLock()
        self._latched = False       # «нажал — начал»: ждём второго нажатия

    # -------- включение и выключение
    def enable(self) -> dict[str, Any]:
        """Занять сочетание клавиш. Возвращает то же, что и status()."""
        with self._lock:
            self.disable()
            raw = config.get("dictate_hotkey") or "ctrl+shift+space"
            try:
                hk = platform.input().parse_hotkey(raw)
            except ValueError as err:
                self.last_error = str(err)
                log.warning("диктовка не включилась: %s", err)
                return self.status()
            mode = self.mode()
            listener = platform.input().listen(
                hk, on_press=self._on_press,
                on_release=(self._on_release if mode != "toggle" else None),
            )
            if not listener.start():
                self.last_error = (
                    "Сочетание %s занять не удалось: %s. Выберите другое."
                    % (hk.text, listener.error or "причина неизвестна"))
                log.warning("диктовка не включилась: %s", self.last_error)
                return self.status()
            self.hotkey = hk
            self.listener = listener
            self.last_error = ""
            self.stage = "idle"
            self._emit()
            return self.status()

    def disable(self) -> None:
        with self._lock:
            self._stop_capture(drop=True)
            if self.listener is not None:
                try:
                    self.listener.stop()
                except Exception:
                    log.debug("слушатель клавиши не остановился", exc_info=True)
            self.listener = None
            self.hotkey = None
            self._latched = False
            if self.stage != "off":
                self.stage = "off"
                self._emit()

    def sync(self) -> dict[str, Any]:
        """Привести состояние к настройкам: включить, выключить или перезанять."""
        want = bool(config.get("dictate_enabled"))
        if not want:
            self.disable()
            return self.status()
        raw = config.get("dictate_hotkey") or ""
        same = False
        try:
            same = (self.hotkey is not None and self.listener is not None
                    and self.listener.running
                    and platform.input().parse_hotkey(raw) == self.hotkey)
        except ValueError:
            same = False
        if same:
            return self.status()
        return self.enable()

    @staticmethod
    def mode() -> str:
        m = str(config.get("dictate_mode") or "smart").strip().lower()
        return m if m in ("smart", "hold", "toggle") else "smart"

    # -------- нажатия
    def _on_press(self) -> None:
        with self._lock:
            if self.stage == "listening":
                # Второе нажатие в режиме «старт-стоп» (или после короткого касания)
                self._finish()
                return
            if self.stage == "thinking":
                return
            self._begin()

    def _on_release(self, held: float) -> None:
        with self._lock:
            if self.stage != "listening":
                return
            if self.mode() == "smart" and held < TAP_SECONDS:
                # Короткое касание: остаёмся слушать до следующего нажатия.
                self._latched = True
                self._emit()
                return
            self._finish()

    def toggle(self) -> dict[str, Any]:
        """Из окна программы или из меню значка — без горячей клавиши."""
        self._on_press()
        return self.status()

    def cancel(self) -> dict[str, Any]:
        """Бросить начатую диктовку, ничего не вставляя."""
        with self._lock:
            if self.stage == "listening":
                self._stop_capture(drop=True)
                self.stage = "idle"
                self._latched = False
                platform.input().click("stop")
                self._emit()
            return self.status()

    # -------- работа
    def _begin(self) -> None:
        if self.busy():
            self.stage = "sleeping"
            self.last_error = "Идёт запись совещания — диктовка пока спит."
            log.info("диктовка не начата: идёт запись")
            self._emit()
            # Возвращаемся в «жду» сами: иначе капсула застыла бы на предупреждении.
            threading.Timer(2.5, self._wake).start()
            return
        self._chunks = []
        # Время начала ставим ДО открытия микрофона: первый кусок звука может
        # прийти прямо из rec.start(), и сторож «диктовка идёт слишком долго»
        # сравнил бы его с нулём и оборвал бы диктовку на первой же сотне
        # миллисекунд.
        self._started_at = time.time()
        try:
            # Свой микрофон для диктовки; не задан — тот же, что и для записи.
            device = config.get("dictate_device_index")
            if device in (None, ""):
                device = config.get("mic_device_index")
            rec = platform.audio().open_mic(
                device if device not in (None, "") else None, self._take)
            rec.start()
        except Exception as err:
            self.last_error = "Микрофон не открылся: %s" % str(err)[:200]
            log.warning("диктовка: %s", self.last_error)
            self.stage = "idle"
            self._emit()
            return
        self._rec = rec
        self.stage = "listening"
        self.last_error = ""
        self.last_text = ""
        # Точная модель на старте программы не греется и выгружается после
        # простоя. Начинаем грузить её сейчас, пока человек говорит, — к «стоп»
        # она обычно уже в памяти. Английский путь идёт другой моделью.
        if str(config.get("dictate_lang") or "ru").lower() != "en":
            from . import asr

            asr.preload_precise()
        platform.input().click("start")
        self._emit()

    def _take(self, pcm: np.ndarray) -> None:
        chunks = self._chunks
        if chunks is None:
            return
        chunks.append(np.asarray(pcm, dtype=np.float32).reshape(-1).copy())
        if time.time() - self._started_at > MAX_SECONDS:
            log.info("диктовка идёт дольше %d с — заканчиваю сама", int(MAX_SECONDS))
            threading.Thread(target=self._finish_outside, name="dictate-cap",
                             daemon=True).start()

    def _finish_outside(self) -> None:
        with self._lock:
            if self.stage == "listening":
                self._finish()

    def _stop_capture(self, drop: bool = False) -> np.ndarray:
        rec, self._rec = self._rec, None
        if rec is not None:
            try:
                rec.stop()
            except Exception:
                log.debug("микрофон диктовки не закрылся", exc_info=True)
        chunks, self._chunks = self._chunks, []
        if drop or not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks)

    def _finish(self) -> None:
        pcm = self._stop_capture()
        self._latched = False
        platform.input().click("stop")
        seconds = pcm.size / float(SR)
        if seconds < MIN_SECONDS:
            log.info("диктовка слишком короткая (%.2f с) — пропускаю", seconds)
            self.stage = "idle"
            self._emit()
            return
        self.stage = "thinking"
        self._emit()
        threading.Thread(target=self._work, args=(pcm,), name="dictate-asr",
                         daemon=True).start()

    def _work(self, pcm: np.ndarray) -> None:
        lang = str(config.get("dictate_lang") or "ru").lower()
        t0 = time.time()
        try:
            raw = recognise(pcm, lang=lang)
        except Exception as err:
            log.warning("диктовка не распозналась", exc_info=True)
            with self._lock:
                self.last_error = "Распознать не вышло: %s" % str(err)[:200]
                self.stage = "idle"
                self._emit()
            return
        text = polish(raw)
        seconds = pcm.size / float(SR)
        log.info("диктовка: %.1f с речи, %d знаков, распознано за %.1f с",
                 seconds, len(text), time.time() - t0)
        ok = False
        if text:
            remember(text, seconds)
            try:
                ok = bool(self.paste(text))
            except Exception as err:
                log.warning("вставка не удалась: %s", err)
        with self._lock:
            self.last_text = text
            self.last_error = "" if (ok or not text) else (
                "Текст распознан, но вставить его не вышло — он остался в буфере обмена.")
            self.stage = "idle"
            self._emit()

    def _wake(self) -> None:
        with self._lock:
            if self.stage == "sleeping":
                self.stage = "idle"
                self.last_error = ""
                self._emit()

    # -------- наружу
    def status(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.listener is not None and self.listener.running),
            "stage": self.stage,
            "hotkey": self.hotkey.text if self.hotkey else "",
            "mode": self.mode(),
            # Показываем в настройках честно: поднят ли общий перехват клавиатуры.
            "by_hook": bool(self.hotkey.by_hook) if self.hotkey else False,
            "lang": str(config.get("dictate_lang") or "ru"),
            "latched": bool(self._latched),
            "text": self.last_text,
            "error": self.last_error,
        }

    def _emit(self) -> None:
        self._paint_capsule()
        if self.on_state is None:
            return
        try:
            self.on_state(self.status())
        except Exception:
            log.debug("состояние диктовки не ушло в окно", exc_info=True)

    def _paint_capsule(self) -> None:
        """Капсула поверх всех окон. Её может не быть — это не беда."""
        cap = self.capsule
        if cap is None:
            return
        try:
            caption = self.CAPTION.get(self.stage)
            if caption is None or not config.get("dictate_pill", True):
                cap.hide()
                return
            if self.stage == "listening" and self._latched:
                caption = "Слушаю — нажмите ещё раз, чтобы закончить"
            cap.show(caption, self.stage)
        except Exception:
            log.debug("капсула не показалась", exc_info=True)
