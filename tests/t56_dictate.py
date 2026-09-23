# -*- coding: utf-8 -*-
"""Проверка 56: диктовка текста горячей клавишей (ветка typer).

Решения 14.09: язык задаётся в настройках, свои замены слов, вставка
через буфер обмена, во время записи совещания диктовка спит.

Что проверяем — на настоящем Windows, без окна программы:
  1. Разбор сочетания клавиш: годные, негодные, как показывается человеку.
  2. Клавиша занимается у Windows по-настоящему; занятую второй раз не отдают;
     нажатие доходит до обработчика; после выключения клавиша освобождена.
  3. Обработка текста: свои замены, произнесённые знаки, пробелы, заглавные.
  4. Буфер обмена: текст кладётся, читается и прежнее содержимое возвращается.
     Ctrl+V НЕ нажимаем: он ушёл бы в чужое окно.
  5. Состояния диктовки на поддельном микрофоне: слушаю → распознаю → вставил;
     режимы «пока держу», «старт-стоп» и «сама»; отмена.
  6. Во время записи совещания диктовка не начинается.
  7. Слишком короткое касание не распознаётся; длинная речь режется по паузам.
  8. Точки службы: состояние, включение через настройки, проверка сочетания.

Настоящие settings.json и база голосов не трогаются (tests\\isolate.py).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t56_dictate.py
"""
import io
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()

import numpy as np  # noqa: E402
import win32api  # noqa: E402
import win32con  # noqa: E402
import win32gui  # noqa: E402

from hagen import config, dictate  # noqa: E402
from hagen.platform.windows import input as win_input  # noqa: E402

# Настоящая история диктовок — не наша: подменяем файл временным.
import tempfile  # noqa: E402

dictate.HISTORY_PATH = Path(tempfile.mkdtemp(prefix="dictate_hist_")) / "history.json"


say("=== 1. Разбор сочетания клавиш ===")
hk = win_input.parse_hotkey("ctrl+shift+space")
check("ctrl+shift+space разобрано",
      hk.mods == (win_input.MOD_CONTROL | win_input.MOD_SHIFT) and hk.vk == 0x20, hk)
check("показывается человеку понятно", hk.text == "Ctrl + Shift + Space", hk.text)
check("порядок клавиш не важен",
      win_input.parse_hotkey("shift + CTRL + Space") == hk)
check("F-клавишу можно занять одну", win_input.parse_hotkey("f9").vk == 0x78)

hk2 = win_input.parse_hotkey("ctrl+win")
check("сочетание из одних модификаторов разбирается",
      hk2.mods == (win_input.MOD_CONTROL | win_input.MOD_WIN) and hk2.vk == 0, hk2)
check("показывается без лишней клавиши", hk2.text == "Ctrl + Win", hk2.text)
check("для него нужен перехват", hk2.by_hook is True)
check("для обычного сочетания перехват не нужен",
      win_input.parse_hotkey("ctrl+space").by_hook is False)
check("двух клавиш достаточно — не только трёх",
      win_input.parse_hotkey("ctrl+space").text == "Ctrl + Space")

for bad, why in [("", "пусто"), ("ctrl", "нет обычной клавиши"),
                 ("ctrl+d+f", "две обычные клавиши"), ("ctrl+ф", "нет такой клавиши"),
                 ("d", "голая буква")]:
    try:
        win_input.parse_hotkey(bad)
        check("«%s» отвергается (%s)" % (bad, why), False, "принято")
    except ValueError as err:
        check("«%s» отвергается (%s)" % (bad, why), True, str(err)[:60])

say("")
say("=== 2. Клавиша занимается у Windows ===")
# Сочетание нарочно редкое: отбирать рабочее сочетание нельзя.
TEST_HK = win_input.parse_hotkey("ctrl+alt+shift+f24")
pressed = []
released = []
lis = win_input.HotkeyListener(TEST_HK, on_press=lambda: pressed.append(time.time()),
                             on_release=lambda held: released.append(held))
check("клавиша занята", lis.start() is True, lis.error)

second = win_input.HotkeyListener(TEST_HK, on_press=lambda: None)
check("занятую клавишу второй раз не отдают", second.start() is False, second.error)
second.stop()

if lis.running:
    # Настоящее нажатие не изображаем: keybd_event ушёл бы в чужое окно.
    # Проверяем, что сообщение от Windows доходит до обработчика.
    win32gui.PostMessage(lis.hwnd, win_input.WM_HOTKEY, 1, 0)
    for _ in range(50):
        if pressed:
            break
        time.sleep(0.02)
    check("нажатие доходит до обработчика", len(pressed) == 1, pressed)
    for _ in range(60):
        if released:
            break
        time.sleep(0.02)
    check("отпускание замечено (клавишу никто не держит)", len(released) == 1, released)
lis.stop()
check("после выключения клавиша свободна", not lis.running)
again = win_input.HotkeyListener(TEST_HK, on_press=lambda: None)
check("освобождённую клавишу можно занять снова", again.start() is True, again.error)
again.stop()

say("")
say("=== 2a. Ctrl+Win: перехват клавиатуры ===")
# Такое сочетание Windows обычным способом не отдаёт: RegisterHotKey принимает
# заявку без ошибки и молчит навсегда (проверено 14.09). Поэтому — перехват,
# и только под такие сочетания (решение 14.09).
hook_hk = win_input.parse_hotkey("ctrl+win")
check("для него выбирается перехват",
      isinstance(win_input.make_listener(hook_hk, lambda: None), win_input.HookListener))
check("для обычного — прежний безопасный способ",
      isinstance(win_input.make_listener(win_input.parse_hotkey("ctrl+alt+shift+f22"),
                                       lambda: None), win_input.HotkeyListener))

say("")
say("=== 2b. Win или Alt нажаты первыми: «Пуск» не открывается (15.09) ===")
# На ноутбуке Alt+Win открывал «Пуск»: Win нажимали первым, он уходил в Windows,
# Alt глотался, и отпускание Win Windows считала одиночным нажатием. Теперь в
# момент срабатывания посылается незанятая клавиша-маска. Проверяем порядок
# событий обработчика без настоящей клавиатуры: маска подменена записью.
KD, KU = 0x0100, 0x0101
LWIN, LALT, LCTRL = 0x5B, 0xA4, 0xA2
events = []
real_mask = win_input._send_mask_key
win_input._send_mask_key = lambda: events.append("МАСКА")
real_spawn = win_input._spawn
win_input._spawn = lambda fn: None          # обработчики нажатия здесь не нужны
try:
    def run(hk, seq):
        events.clear()
        lst = win_input.HookListener(win_input.parse_hotkey(hk), on_press=lambda: None,
                                   on_release=lambda held: None)
        for name, vk, wp in seq:
            swallowed = lst._handle(vk, wp)
            events.append("%s%s" % (name, " (проглочена)" if swallowed else ""))
        return list(events)

    got = run("alt+win", [("Win↓", LWIN, KD), ("Alt↓", LALT, KD), ("Alt↑", LALT, KU), ("Win↑", LWIN, KU)])
    check("Win первым, потом Alt: маска ровно одна и до отпускания Win",
          got.count("МАСКА") == 1 and got.index("МАСКА") < got.index("Win↑"), got)
    check("…Alt, замкнувший сочетание, проглочен", "Alt↓ (проглочена)" in got, got)

    got = run("alt+win", [("Alt↓", LALT, KD), ("Win↓", LWIN, KD), ("Win↑", LWIN, KU), ("Alt↑", LALT, KU)])
    check("Alt первым, потом Win: маска тоже есть (иначе Alt откроет меню окна)",
          got.count("МАСКА") == 1 and "Win↓ (проглочена)" in got, got)

    got = run("ctrl+win", [("Ctrl↓", LCTRL, KD), ("Win↓", LWIN, KD), ("Win↑", LWIN, KU), ("Ctrl↑", LCTRL, KU)])
    check("Ctrl первым, потом Win: маска не нужна — Win и так проглочен",
          "МАСКА" not in got and "Win↓ (проглочена)" in got, got)

    got = run("ctrl+win", [("Win↓", LWIN, KD), ("Ctrl↓", LCTRL, KD), ("Ctrl↑", LCTRL, KU), ("Win↑", LWIN, KU)])
    check("Win первым, потом Ctrl: маска есть, «Пуск» не откроется", got.count("МАСКА") == 1, got)

    got = run("alt+win", [("Win↓", LWIN, KD), ("Win↑", LWIN, KU)])
    check("просто нажали Win без сочетания — маски нет, «Пуск» работает как обычно", "МАСКА" not in got, got)

    got = run("alt+win", [("Win↓", LWIN, KD), ("Win↓", LWIN, KD), ("Alt↓", LALT, KD),
                          ("Alt↓", LALT, KD), ("Alt↑", LALT, KU), ("Win↑", LWIN, KU)])
    check("автоповтор клавиш не шлёт маску второй раз", got.count("МАСКА") == 1, got)
finally:
    win_input._send_mask_key = real_mask
    win_input._spawn = real_spawn

hp, hr = [], []
hook = win_input.make_listener(hook_hk, on_press=lambda: hp.append(1),
                             on_release=lambda held: hr.append(held))
check("перехват поднялся", hook.start() is True, hook.error or "")
if hook.running:
    UP = win32con.KEYEVENTF_KEYUP
    VK_LCTRL, VK_LWIN = 0xA2, 0x5B
    fg = win32gui.GetForegroundWindow()
    win32api.keybd_event(VK_LCTRL, 0, 0, 0)
    time.sleep(0.05)
    win32api.keybd_event(VK_LWIN, 0, 0, 0)
    time.sleep(0.45)
    win32api.keybd_event(VK_LWIN, 0, UP, 0)
    time.sleep(0.05)
    win32api.keybd_event(VK_LCTRL, 0, UP, 0)
    time.sleep(0.6)
    check("Ctrl+Win сработало", hp == [1], hp)
    check("отпускание поймано с длительностью",
          len(hr) == 1 and 0.3 < hr[0] < 1.5, hr)
    check("«Пуск» не открылся: клавиша Win до Windows не дошла",
          win32gui.GetForegroundWindow() == fg)
    hp.clear()
    for vk in (0x41, 0xA2):          # чужая буква и одиночный Ctrl
        win32api.keybd_event(vk, 0, 0, 0)
        time.sleep(0.05)
        win32api.keybd_event(vk, 0, UP, 0)
        time.sleep(0.25)
    check("чужие клавиши и одиночный Ctrl ничего не запускают", hp == [], hp)
hook.stop()
check("перехват снялся", not hook.running)

say("")
say("=== 3. Обработка текста ===")
check("своя замена целым словом",
      dictate.apply_replacements("Задача в джире висит",
                                 [{"from": "джире", "to": "Jira"}]) == "Задача в Jira висит")
check("замена не срабатывает внутри слова",
      dictate.apply_replacements("джирафы", [{"from": "джира", "to": "Jira"}]) == "джирафы")
check("регистр сказанного не важен",
      dictate.apply_replacements("Джира и джира",
                                 [{"from": "джира", "to": "Jira"}]) == "Jira и Jira")
check("пустое правило пропускается",
      dictate.apply_replacements("текст", [{"from": "", "to": "!"}, None, "мусор"]) == "текст")

check("произнесённые знаки",
      dictate.apply_marks("привет точка как дела вопросительный знак")
      == "привет . как дела ?")
check("«новая строка» становится переводом строки",
      dictate.apply_marks("первая новая строка вторая") == "первая \n вторая")
check("без галочки знаки остаются словами",
      dictate.polish("привет точка", marks=False, rules=[]) == "привет точка")

check("пробел перед знаком убирается",
      dictate.tidy_spaces("привет , как дела ?") == "привет, как дела?")
check("заглавная после точки",
      dictate.tidy_spaces("привет. как дела") == "привет. Как дела")
check("двойные пробелы схлопываются",
      dictate.tidy_spaces("раз   два") == "раз два")
# Первую букву не трогаем намеренно: диктовать можно и в середину чужой фразы,
# а заглавную в начале модель и так ставит сама.
check("первая буква остаётся как сказано", dictate.tidy_spaces("привет") == "привет")
got = dictate.polish("задача в джире точка новая строка сделать вопросительный знак",
                     marks=True, rules=[{"from": "джире", "to": "Jira"}])
check("весь путь сразу", got == "задача в Jira.\nСделать?", repr(got))

say("")
say("=== 4. Буфер обмена ===")
BEFORE = "текст до проверки"
win_input._set_clipboard(BEFORE)
check("текст кладётся и читается", win_input._clipboard_text() == BEFORE)
saved = win_input._clipboard_text()
win_input._set_clipboard("надиктованное")
check("новое содержимое встало", win_input._clipboard_text() == "надиктованное")
win_input._set_clipboard(saved)
check("прежнее содержимое возвращается", win_input._clipboard_text() == BEFORE)

say("")
say("=== 5. Состояния диктовки на поддельном микрофоне ===")


class FakeMic:
    """Подделка loopback.MicRecorder: сыплет тишину заданной длины."""

    made = []

    def __init__(self, device_index=None, on_audio=None, block_ms=100):
        self.on_audio = on_audio
        self.stopped = False
        FakeMic.made.append(self)

    def start(self):
        # секунда «речи» одним куском — этого хватает всем проверкам ниже
        if self.on_audio is not None:
            self.on_audio(np.full(dictate.SR, 0.1, dtype=np.float32))

    def stop(self):
        self.stopped = True


from hagen.platform.windows import loopback  # noqa: E402

loopback.MicRecorder = FakeMic
REAL_RECOGNISE = dictate.recognise
dictate.recognise = lambda pcm, lang="ru": "привет это диктовка"
config.save({"dictate_sound": False, "dictate_replacements": [], "dictate_marks": False})

pasted = []
d = dictate.Dictation(busy=lambda: False, paste=lambda t: (pasted.append(t), True)[1])
d.stage = "idle"


def wait_idle(obj, limit=8.0):
    end = time.time() + limit
    while time.time() < end:
        if obj.stage == "idle":
            return True
        time.sleep(0.02)
    return False


d._on_press()
check("после нажатия слушаем", d.stage == "listening", d.stage)
check("микрофон открыт", len(FakeMic.made) == 1 and not FakeMic.made[0].stopped)
d._on_release(1.0)                       # держали дольше порога — это «пока держу»
check("после долгого удержания заканчиваем", wait_idle(d), d.stage)
check("микрофон закрыт", FakeMic.made[0].stopped is True)
check("текст вставлен", pasted == ["привет это диктовка"], pasted)

pasted.clear()
config.save({"dictate_mode": "smart"})
d._on_press()
d._on_release(0.1)                       # короткое касание — ждём второго нажатия
check("короткое касание оставляет слушать", d.stage == "listening", d.stage)
check("видно, что ждём второго нажатия", d.status().get("latched") is True)
d._on_press()
check("второе нажатие заканчивает", wait_idle(d), d.stage)
check("текст вставлен и во втором режиме", pasted == ["привет это диктовка"], pasted)

pasted.clear()
d._on_press()
check("отмена возвращает в ожидание", d.cancel().get("stage") == "idle")
time.sleep(0.3)
check("после отмены ничего не вставлено", pasted == [], pasted)

say("")
say("=== 5b. Капсула поверх чужих окон ===")


class FakeCapsule:
    """Капсула без настоящего окна: проверяем, что и когда ей велят показать."""

    def __init__(self):
        self.calls = []

    def show(self, text, kind="listening"):
        self.calls.append((kind, text))

    def hide(self):
        self.calls.append(("hide", ""))


cap = FakeCapsule()
loopback.MicRecorder = FakeMic
dictate.recognise = lambda pcm, lang="ru": "привет это диктовка"
d4 = dictate.Dictation(busy=lambda: False, paste=lambda t: True, capsule=cap)
d4.stage = "idle"
d4._on_press()
check("на «слушаю» капсула зовётся", cap.calls[-1] == ("listening", "Слушаю…"), cap.calls)
d4._on_release(0.1)
check("после короткого касания подсказано, что делать",
      "ещё раз" in cap.calls[-1][1], cap.calls[-1])
d4._on_press()
check("на распознавании капсула меняет надпись",
      ("thinking", "Распознаю…") in cap.calls, cap.calls)
check("в конце капсула прячется", wait_idle(d4) and cap.calls[-1][0] == "hide", cap.calls[-1])
cap.calls.clear()
config.save({"dictate_pill": False})
d4._on_press()
check("выключенная капсула не показывается", cap.calls == [("hide", "")], cap.calls)
d4.cancel()
config.save({"dictate_pill": True})

# Настоящее окно: главное — что оно не забирает фокус. Иначе Ctrl+V ушёл бы в него.
import win32gui  # noqa: E402

real = win_input.Capsule()
check("окно капсулы создалось", bool(real.hwnd), real.error or "")
if real.hwnd:
    before = win32gui.GetForegroundWindow()
    real.show("Слушаю…", "listening")
    time.sleep(0.6)
    check("фокус остался у чужого окна", win32gui.GetForegroundWindow() == before)
    check("капсула видна", bool(win32gui.IsWindowVisible(real.hwnd)))
    ex = win32gui.GetWindowLong(real.hwnd, -20)      # GWL_EXSTYLE
    check("поверх всех окон", bool(ex & 0x00000008))
    check("никогда не активируется", bool(ex & 0x08000000))
    check("нет в Alt+Tab и на панели задач", bool(ex & 0x00000080))
    narrow = win32gui.GetWindowRect(real.hwnd)
    real.show("Слушаю — нажмите ещё раз, чтобы закончить", "listening")
    time.sleep(0.5)
    wide = win32gui.GetWindowRect(real.hwnd)
    check("ширина подстраивается под надпись",
          (wide[2] - wide[0]) > (narrow[2] - narrow[0]),
          (narrow[2] - narrow[0], wide[2] - wide[0]))
    check("стоит по центру внизу", abs(((wide[0] + wide[2]) // 2)
                                       - ((narrow[0] + narrow[2]) // 2)) <= 1
          and wide[1] == narrow[1], (narrow, wide))
    real.hide()
    time.sleep(0.4)
    check("прячется", not win32gui.IsWindowVisible(real.hwnd))
real.stop()
check("окно закрылось", real.hwnd is None)

say("")
say("=== 5a. История диктовок ===")
items = dictate.history()
check("надиктованное попало в историю", len(items) >= 2
      and items[0]["text"] == "привет это диктовка", items[:1])
check("новые сверху", items[0]["at"] >= items[1]["at"], [i["at"] for i in items])
check("запомнена длина речи", items[0]["seconds"] == 1.0, items[0].get("seconds"))
was = len(dictate.history())
dictate.remember("   ", 1.0)
check("пустую диктовку не храним", len(dictate.history()) == was, len(dictate.history()))
for n in range(dictate.HISTORY_MAX + 5):
    dictate.remember("строка %d" % n, 0.5)
check("история не растёт без конца", len(dictate.history()) == dictate.HISTORY_MAX,
      len(dictate.history()))
dictate.forget_all()
check("очистка убирает всё", dictate.history() == [])
check("очистка пустой истории не падает", dictate.forget_all() is None)

say("")
say("=== 6. Во время записи совещания диктовка спит ===")
busy = {"on": True}
mics_before = len(FakeMic.made)
d2 = dictate.Dictation(busy=lambda: busy["on"], paste=lambda t: pasted.append(t) or True)
d2.stage = "idle"
d2._on_press()
check("диктовка не началась", d2.stage == "sleeping", d2.stage)
check("человеку сказано почему", "запись" in (d2.status().get("error") or ""),
      d2.status().get("error"))
check("микрофон не открывался", len(FakeMic.made) == mics_before, len(FakeMic.made))
busy["on"] = False
check("после записи диктовка просыпается сама", wait_idle(d2, limit=5.0), d2.stage)

say("")
say("=== 7. Длина надиктованного ===")
seen = {}
dictate.recognise = lambda pcm, lang="ru": seen.setdefault("n", pcm.size) and "" or ""


class ShortMic(FakeMic):
    def start(self):
        if self.on_audio is not None:
            self.on_audio(np.zeros(int(0.1 * dictate.SR), dtype=np.float32))


loopback.MicRecorder = ShortMic
d3 = dictate.Dictation(busy=lambda: False, paste=lambda t: pasted.append(t) or True)
d3.stage = "idle"
d3._on_press()
d3._on_release(1.0)
time.sleep(0.3)
check("случайное касание не распознаётся", "n" not in seen and d3.stage == "idle",
      (seen, d3.stage))

# Длинную речь режет не сама диктовка, а разбор по паузам — проверяем выбор пути.
dictate.recognise = REAL_RECOGNISE
calls = []
import hagen.asr as asr_mod  # noqa: E402
import hagen.vad as vad_mod  # noqa: E402

real_precise, real_spans, real_split = (asr_mod.transcribe_precise,
                                        asr_mod.transcribe_spans, vad_mod.split_for_asr)
asr_mod.transcribe_precise = lambda pcm, words=True, lang="ru", role="files": (
    calls.append(("целиком", words, role)), asr_mod.Result("коротко"))[1]
asr_mod.transcribe_spans = lambda pcm, spans, precise=True, words=True, **kw: (
    calls.append(("по кускам", words)), [{"text": "часть", "start": 0, "end": 1}])[1]
vad_mod.split_for_asr = lambda pcm, max_s=24.0: [{"start": 0, "end": pcm.size}]
try:
    dictate.recognise(np.zeros(10 * dictate.SR, dtype=np.float32))
    check("короткая диктовка идёт целиком, моделью голосового ввода",
          calls == [("целиком", True, "voice")], calls)
    # Отметки времени диктовке не нужны, но именно они уводят на torch. При
    # words=False точная модель пошла бы через ONNX, которого в папке нет, и
    # первая диктовка молча ушла бы в многоминутный экспорт модели.
    check("просим время слов — значит идём проверенным путём, а не в экспорт",
          calls[0][1] is True, calls)
    calls.clear()
    dictate.recognise(np.zeros(40 * dictate.SR, dtype=np.float32))
    check("длинная диктовка режется по паузам", calls == [("по кускам", True)], calls)
finally:
    asr_mod.transcribe_precise, asr_mod.transcribe_spans = real_precise, real_spans
    vad_mod.split_for_asr = real_split

say("")
say("=== 8. Точки службы ===")
from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    r = cli.get("/api/dictate", headers=ORIGIN)
    check("состояние диктовки отдаётся", r.status_code == 200 and "stage" in r.json(), r.text)

    r = cli.post("/api/dictate/hotkey", headers=ORIGIN, json={"hotkey": "ctrl+alt+j"})
    check("годное сочетание принимается",
          r.json().get("ok") is True and r.json().get("text") == "Ctrl + Alt + J", r.text)
    r = cli.post("/api/dictate/hotkey", headers=ORIGIN, json={"hotkey": "j"})
    check("негодное сочетание объясняется",
          r.json().get("ok") is False and "Ctrl" in (r.json().get("error") or ""), r.text)

    # Включение через настройки должно занять клавишу прямо сейчас.
    r = cli.post("/api/settings", headers=ORIGIN,
                 json={"dictate_enabled": True, "dictate_hotkey": "ctrl+alt+shift+f23"})
    # В подробности кладём только код ответа: r.text — это все настройки целиком,
    # и им незачем оседать в файле итогов.
    check("настройки сохранились", r.status_code == 200, r.status_code)
    st = cli.get("/api/dictate", headers=ORIGIN).json()
    check("диктовка включилась и заняла клавишу", st.get("enabled") is True, st)
    check("показано, какое сочетание занято",
          st.get("hotkey") == "Ctrl + Alt + Shift + F23", st)

    cli.post("/api/settings", headers=ORIGIN, json={"dictate_enabled": False})
    st = cli.get("/api/dictate", headers=ORIGIN).json()
    check("выключение освобождает клавишу", st.get("enabled") is False, st)

    dictate.remember("из истории", 2.0)
    r = cli.get("/api/dictate/history", headers=ORIGIN)
    check("история отдаётся службой",
          [x["text"] for x in r.json().get("items") or []] == ["из истории"], r.text)
    r = cli.request("DELETE", "/api/dictate/history", headers=ORIGIN)
    check("история чистится службой", r.json().get("items") == []
          and dictate.history() == [], r.text)
    r = cli.post("/api/dictate/paste", headers=ORIGIN, json={"text": "  "})
    check("пустую вставку служба не делает", r.status_code == 400, r.status_code)

say("")
say("=== Щелчки ===")
paths = win_input.sound_paths()
check("оба щелчка записаны",
      all(p.exists() and p.stat().st_size > 1000 for p in paths.values()),
      {k: (p.exists(), p.stat().st_size if p.exists() else 0) for k, p in paths.items()})

sys.exit(finish("t56"))
