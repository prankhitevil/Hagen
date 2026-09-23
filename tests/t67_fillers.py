# -*- coding: utf-8 -*-
"""Проверка 67: слова-паразиты при диктовке (16.09).

Решение 16.09: надиктованный текст чистится от «э-э-э», «ну», «типа»
на этом же компьютере — списком, без облачных моделей. Стенограммы звонков это
не трогает: там речь остаётся как сказана, а паразитов убирает модель при
«Сделать документ».

Что проверяем:
  1. тянущиеся звуки убираются всегда, даже с пустым списком;
  2. слова из списка убираются целиком, в любом регистре, обороты — тоже;
  3. вводное слово уносит свои запятые, опустевшее предложение не оставляет
     висящую точку, заглавная в начале возвращается;
  4. нужные слова не страдают: «а» и «у» поодиночке, «Проверка...», двоеточие;
  5. чистка идёт после своих замен и до знаков препинания;
  6. выключенная настройка ничего не трогает;
  7. стенограмма звонка не чистится — путь другой;
  8. поле и галочка есть на вкладке «Набор текста» и подключены.

Настоящие настройки не читаются (isolate).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t67_fillers.py
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen import config, dictate  # noqa: E402

LIST = ["ну", "вот", "типа", "как бы", "короче", "блин"]


def clean(text, words=LIST):
    return dictate.strip_fillers(text, words)


say("=== 1. Тянущиеся звуки ===")
check("«э-э-э» в начале фразы", clean("Э-э-э, я посмотрю.") == "Я посмотрю.", clean("Э-э-э, я посмотрю."))
check("«ммм» подряд", clean("Ммм, понятно.") == "Понятно.", clean("Ммм, понятно."))
check("«а-а-а» в середине", clean("Сейчас, а-а-а, найду.") == "Сейчас найду.", clean("Сейчас, а-а-а, найду."))
check("звуки убираются и с пустым списком",
      clean("Э-э-э, готово.", []) == "Готово.", clean("Э-э-э, готово.", []))
check("одиночные «э», «м», «ы» — тоже звуки",
      clean("Э, ну, м, поехали.") == "Поехали.", clean("Э, ну, м, поехали."))

say("")
say("=== 2. Слова из списка ===")
check("слово в середине", clean("Я вчера, типа, попросил.") == "Я вчера попросил.",
      clean("Я вчера, типа, попросил."))
check("оборот из двух слов", clean("Это, как бы, черновик.") == "Это черновик.",
      clean("Это, как бы, черновик."))
check("регистр не важен", clean("Блин, готово.") == "Готово.", clean("Блин, готово."))
check("слово без запятых", clean("Ну ладно, поехали.") == "Ладно, поехали.",
      clean("Ну ладно, поехали."))
check("своё слово в списке работает",
      clean("Это, значит, так.", ["значит"]) == "Это так.", clean("Это, значит, так.", ["значит"]))

say("")
say("=== 3. Следы уборки ===")
check("предложение из одних паразитов не оставляет точку",
      clean("Я посмотрю. Ну, вот, короче.") == "Я посмотрю.", clean("Я посмотрю. Ну, вот, короче."))
check("первое предложение из паразитов не оставляет точку в начале",
      clean("Вот, блин. Идём дальше.") == "Идём дальше.", clean("Вот, блин. Идём дальше."))
check("текст из одних паразитов становится пустым",
      clean("Ну, вот, блин, как бы.") == "", repr(clean("Ну, вот, блин, как бы.")))
check("заглавная возвращается новому первому слову",
      clean("Ну что ж, идём.") == "Что ж, идём.", clean("Ну что ж, идём."))
check("вводное слово уносит обе свои запятые",
      clean("Это, короче, важно!") == "Это важно!", clean("Это, короче, важно!"))
check("пустая строка остаётся пустой", clean("") == "", repr(clean("")))

say("")
say("=== 4. Нужное не страдает ===")
keep = [
    ("А теперь по существу.", "одиночное «а» — союз, не звук"),
    ("У нас всё готово.", "одиночное «у» — предлог"),
    ("Проверка... и точки на месте.", "многоточие не склеивается"),
    ("Сроки такие: конец октября.", "двоеточие на месте"),
    ("Ничего лишнего здесь нет.", "текст без паразитов не меняется"),
    ("Ему нужен вотчер, а не вот.", "слово внутри другого слова не трогаем"),
]
for text, why in keep:
    got = clean(text)
    expect = text if "вот." not in text else "Ему нужен вотчер, а не."
    check(why, got == text or (text.endswith("не вот.") and "вотчер" in got), got)

say("")
say("=== 5. Порядок в общем пути ===")
rules = [{"from": "джира", "to": "Jira"}]
got = dictate.polish("Ну, джира, короче, упала точка", marks=True, rules=rules, fillers=LIST)
check("замены сработали до чистки, знаки — после", got == "Jira упала.", got)
got = dictate.polish("Ну, э-э, всё готово", marks=False, rules=[], fillers=False)
check("явный отказ от чистки ничего не трогает", got == "Ну, э-э, всё готово", got)
got = dictate.polish("Ну, э-э, всё готово", marks=False, rules=[], fillers=[])
check("пустой список: звук ушёл со своими запятыми, слово осталось",
      got == "Ну всё готово", got)

say("")
say("=== 6. Настройка ===")
check("заводская галочка включена", config.DEFAULTS["dictate_strip_fillers"] is True,
      config.DEFAULTS["dictate_strip_fillers"])
check("заводской список не пустой и в нём «ну»",
      "ну" in config.DEFAULTS["dictate_fillers"], config.DEFAULTS["dictate_fillers"])
config.save({"dictate_strip_fillers": False, "dictate_fillers": LIST})
check("выключенная настройка ничего не трогает",
      dictate.polish("Ну, всё готово", marks=False, rules=[]) == "Ну, всё готово",
      dictate.polish("Ну, всё готово", marks=False, rules=[]))
config.save({"dictate_strip_fillers": True})
check("включённая настройка чистит",
      dictate.polish("Ну, всё готово", marks=False, rules=[]) == "Всё готово",
      dictate.polish("Ну, всё готово", marks=False, rules=[]))

say("")
say("=== 7. Стенограмма звонка не чистится ===")
src = io.open(PROJECT / "hagen" / "live.py", encoding="utf-8").read()
check("живая запись не зовёт чистку паразитов",
      "strip_fillers" not in src and "polish(" not in src)
check("чистка живёт в диктовке", callable(getattr(dictate, "strip_fillers", None)))

say("")
say("=== 8. Страница ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
pane = html[html.find('id="t-dictate"'):html.find('id="t-keys"')]
check("галочка «убирать слова-паразиты»", 'id="set-dictate-fillers"' in pane)
check("поле списка", 'id="set-dictate-fillers-list"' in pane)
check("сказано, что тянущиеся звуки убираются всегда", "перечислять не нужно" in pane)
check("сказано, что стенограммы это не трогает", "стенограммы звонков не трогает" in pane)
check("предупреждение про нужные слова в подсказке «?»", "вот документ" in pane)
check("поля читаются и сохраняются",
      "$('set-dictate-fillers').checked = s.dictate_strip_fillers !== false;" in js
      and "dictate_strip_fillers: $('set-dictate-fillers').checked," in js
      and "dictate_fillers:" in js)
check("список разбирается по запятым и пустые не попадают",
      ".split(',').map((w) => w.trim()).filter(Boolean)" in js)

sys.exit(finish("t67"))
