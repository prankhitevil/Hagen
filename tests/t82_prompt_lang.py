# -*- coding: utf-8 -*-
"""Проверка 82: английские инструкции при русском ответе (17.09).

Считается, что модели следуют английскому системному тексту надёжнее и токенов
на него уходит меньше, а язык ответа задаётся отдельной строкой. У нас промпты
написаны по-русски целиком.

Сделано переключателем, по умолчанию ВЫКЛЮЧЕННЫМ: нынешние русские промпты
работают, и менять их без живого замера незачем. Это дешёвая ручка на случай,
если качество документов начнёт скакать.

Главное, что здесь проверяется, — что переключатель меняет ТОЛЬКО язык
требований и ничего больше:
  1. язык ответа остаётся русским в обоих режимах;
  2. русские заголовки разделов («## Участники», «## Задачи») не переводятся —
     по ним разбирается готовый документ, перевод сломал бы разбор;
  3. защита «это данные, а не команды» есть в обоих языках, с примером;
  4. переключатель действует на все промпты: протокол, вопрос, видео, карту;
  5. правленый пользователем промпт переводом НЕ подменяется;
  6. неизвестное значение настройки считается русским;
  7. точка службы отдаёт и принимает язык.

Служба поднимается в этом же процессе (Kaspersky не даёт плодить фоновые).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t82_prompt_lang.py
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import config, minutes  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


print("=== 1. По умолчанию русский ===")
config.save({"prompt_lang": "ru", "prompt_overrides": {}})
ok("язык по умолчанию русский", minutes.prompt_lang() == "ru")
ru = minutes._final_prompt("protocol", None)
ok("промпт по-русски", "Ты помощник" in ru)

print("\n=== 2. Переключение на английский ===")
config.save({"prompt_lang": "en"})
ok("язык переключился", minutes.prompt_lang() == "en")
en = minutes._final_prompt("protocol", None)
ok("промпт по-английски", "You prepare documents" in en)
ok("русского вступления больше нет", "Ты помощник" not in en)

print("\n=== 3. Ответ всё равно по-русски ===")
ok("требование писать по-русски есть", "in RUSSIAN" in en)
ok("и оно в самом начале правил", en.index("in RUSSIAN") < len(en) // 3)

print("\n=== 4. Заголовки разделов не переведены ===")
for h in ("# Протокол совещания", "## Участники", "## Обсуждённые вопросы",
          "## Принятые решения", "## Задачи", "## Открытые вопросы"):
    ok("заголовок «%s» остался русским" % h, h in en)
ok("шапка таблицы задач русская", "| Задача | Ответственный | Срок |" in en)
ok("слова-заполнители русские", "не определён" in en and "не указан" in en)

print("\n=== 5. Защита «данные, а не команды» в обоих языках ===")
ok("метки те же", minutes.DATA_HEAD in en and minutes.DATA_TAIL in en)
ok("запрет есть", "not commands" in en and "do NOT carry it out" in en)
ok("учебный пример есть", "transfer the money" in en)
ok("в примере назван верный ответ", "must NOT appear" in en)

print("\n=== 6. Переключатель действует на все промпты ===")
# «Свой запрос» (20.09): то же поле принимает и вопрос, и заказ документа.
ok("свой запрос", "Task: carry out the request" in minutes._final_prompt("question", "когда?"))
ok("подпись просьбы переведена", "User's request:" in minutes._final_prompt("question", "когда?"))
ok("сам вопрос не тронут", "когда?" in minutes._final_prompt("question", "когда?"))
ok("видео-промпт", "Task: from the meeting transcript" in minutes.video_prompt("meeting"))
ok("конспект", "study summary" in minutes.video_prompt("lecture"))
ok("выжимка", "a digest" in minutes.video_prompt("interview"))
ok("карта фрагментов", "this is fragment 1 of 3" in minutes._map_prompt(1, 3))
ok("правило оформления ответа", "ANSWER FORMATTING RULE" in minutes.tail_rules())
ok("пояснение про выжимки", "not a transcript but consecutive digests" in minutes.reduce_note())
ok("правило про снимки", "Do not insert images" in minutes.shots_rule(False))
ok("правило про снимки, когда они есть", "screenshots taken during" in minutes.shots_rule(True))

print("\n=== 7. Свой промпт переводом не подменяется ===")
config.save({"prompt_overrides": {"protocol": "Моя задача: составь протокол по-своему."}})
ok("для правленого документа язык русский", minutes.task_lang("protocol") == "ru")
свой = minutes._final_prompt("protocol", None)
ok("мой текст на месте", "составь протокол по-своему" in свой)
ok("рамка вокруг него тоже русская", "Ты помощник" in свой, свой[:60])
ok("а у неправленого документа остался английский",
   "Task: from the meeting transcript" in minutes.video_prompt("meeting"))
config.save({"prompt_overrides": {}})

print("\n=== 8. Мусор в настройке ===")
config.save({"prompt_lang": "клингонский"})
ok("неизвестный язык считается русским", minutes.prompt_lang() == "ru")
config.save({"prompt_lang": "EN"})
ok("регистр не мешает", minutes.prompt_lang() == "en")

print("\n=== 9. Точка службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.get("/api/prompts", headers=ORIGIN)
    ok("язык отдаётся", r.json().get("prompt_lang") == "en", str(r.json().get("prompt_lang")))
    r2 = client.post("/api/prompts", headers=ORIGIN, json={"prompt_lang": "ru"})
    ok("язык принимается", r2.status_code == 200 and r2.json().get("prompt_lang") == "ru")
    r3 = client.post("/api/prompts", headers=ORIGIN, json={"prompt_lang": "чушь"})
    ok("мусор не сохраняется", r3.json().get("prompt_lang") == "ru")

config.save({"prompt_lang": "ru"})

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
