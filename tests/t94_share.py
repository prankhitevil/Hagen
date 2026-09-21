# -*- coding: utf-8 -*-
"""Проверка 94: «Отправить» — письмо или Telegram (20.09).

Продолжение копирования документа: там он ложится в буфер в нужной разметке,
здесь — уходит адресату. Решения 20.09:

  * при нажатии открывается окно, в котором ГАЛОЧКАМИ отмечают, что отправить;
  * в письмо документы кладутся ТОЛЬКО ВЛОЖЕНИЕМ;
  * получателей программа не подставляет — адресатов вписывает человек.

Главная трудность — длина: и `mailto:`, и ссылки Telegram передают текст внутри
самой ссылки, а её Windows обрезает на тысячах знаков. Поэтому вложение
доезжает лишь через классический Outlook (COM), а без него программа честно
открывает короткое письмо-ссылку и показывает папку с файлами.

**Письмо не отправляется, а показывается**: отправка наружу необратима, и
последнее слово за человеком — тот же порядок, что с задачами в Todoist.

Windows здесь не трогается: розетки платформы подменены заглушками, и проверка
смотрит, что именно ушло в `compose_mail` и `open_link`.

Что проверяем:
  1. что можно отправить: документы и стенограмма, без выдумок;
  2. вложения: по файлу на отмеченное, имена с датой и названием записи,
     вычеркнутые пункты в них не попадают;
  3. письмо: тема, вложения, получатели пустые, письмо показано, не отправлено;
  4. без классического Outlook — путь `mailto:` и показ папки с файлами;
  5. Telegram: открывается клиент, файлов не готовим;
  6. отказы: неизвестное «куда», пустой выбор, чужая запись;
  7. розетка ссылок пускает только свои схемы.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t94_share.py
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import config, minutes, platform, share, store  # noqa: E402

platform.use("fake")
from hagen.platform.fake import desktop as fake_desktop  # noqa: E402
from hagen.platform.fake import system as fake_system  # noqa: E402

FAIL = []


def ok(label, cond, detail=""):
    print("   %s   %s%s" % ("ok  " if cond else "ПРОВАЛ", label,
                            ("" if not detail else ": %s" % detail)))
    if not cond:
        FAIL.append(label)


fake_desktop.reset()
fake_system.LINKS.clear()
fake_system.OPENED.clear()

print("=== 1. Что можно отправить ===")
rec_id = store.create(title="Переговоры о поставке", source="live")["id"]
store.replace_segments(rec_id, [
    store.make_segment("mic", 0.0, 4.0, "Нужны сроки."),
    store.make_segment("far", 4.0, 8.0, "Отгрузим в пятницу.", speaker="Иван Петров"),
])
# Документ кладём файлом записи — ровно так его хранит программа.
doc_path = store.rec_dir(rec_id) / minutes.DOC_KINDS["protocol"]["file"]
doc_path.write_text("# Протокол совещания\n\n## Решения\n"
                    "- Отгрузка в пятницу.\n- Лишний пункт.\n", encoding="utf-8")

res = share.available(rec_id)
keys = [it["key"] for it in res["items"]]
ok("документ записи виден", "protocol" in keys, str(keys))
ok("стенограмма видна", "transcript" in keys, str(keys))
ok("название записи отдано", res["title"] == "Переговоры о поставке", res["title"])
ok("сказано, доезжает ли вложение", "mail_attachments" in res, str(res)[:120])
try:
    share.available("нет-такой-записи")
    ok("чужая запись отвергается", False, "исключения не было")
except ValueError:
    ok("чужая запись отвергается", True)

print("\n=== 2. Файлы вложений ===")
store.update(rec_id, {"dropped": {"protocol": ["- Лишний пункт."]}})
files = share.prepare_files(rec_id, ["protocol", "transcript"])
ok("по файлу на отмеченное", len(files) == 2, str(len(files)))
names = [Path(f["path"]).name for f in files]
ok("в имени файла дата и название записи",
   all(n.startswith("2026-") and "Переговоры о поставке" in n for n in names), str(names))
ok("файлы — Markdown", all(n.endswith(".md") for n in names), str(names))
text = Path(files[0]["path"]).read_text(encoding="utf-8")
ok("документ попал в файл", "Отгрузка в пятницу" in text, text[:80])
ok("вычеркнутое в файл не попало", "Лишний пункт" not in text, text)
tr = Path(files[1]["path"]).read_text(encoding="utf-8")
ok("стенограмма собралась", "Отгрузим в пятницу." in tr, tr[:120])
try:
    share.prepare_files(rec_id, [])
    ok("без отметок файлов не готовим", False, "исключения не было")
except ValueError:
    ok("без отметок файлов не готовим", True)

print("\n=== 3. Письмо с вложением ===")
fake_desktop.reset()
fake_system.LINKS.clear()
out = share.send(rec_id, "mail", ["protocol"])
ok("письмо открыто", out.get("attached") is True and out.get("opened") is True, str(out))
ok("письмо ровно одно", len(fake_desktop.MAILS) == 1, str(len(fake_desktop.MAILS)))
mail = fake_desktop.MAILS[0] if fake_desktop.MAILS else {}
ok("в теме название записи и документ",
   "Переговоры о поставке" in mail.get("subject", "")
   and "Протокол" in mail.get("subject", ""), mail.get("subject"))
ok("документ ушёл вложением", len(mail.get("attachments") or []) == 1,
   str(mail.get("attachments")))
ok("вложение существует на диске",
   Path((mail.get("attachments") or [""])[0]).exists())
ok("получателей не подставили", mail.get("to") == [], str(mail.get("to")))
ok("в теле письма только сопроводительное",
   "во вложении" in mail.get("body", "").lower()
   and "Отгрузка в пятницу" not in mail.get("body", ""), mail.get("body", "")[:160])
ok("ссылку mailto при этом не открывали", fake_system.LINKS == [], str(fake_system.LINKS))

print("\n=== 4. Без классического Outlook ===")
fake_desktop.reset()
fake_desktop.MAIL_OK = False      # COM недоступен: письмо собрать нечем
fake_system.LINKS.clear()
fake_system.OPENED.clear()
out2 = share.send(rec_id, "mail", ["protocol"])
ok("ушли на путь mailto", out2.get("attached") is False, str(out2))
ok("ссылка почтовая", fake_system.LINKS and fake_system.LINKS[0].startswith("mailto:"),
   str(fake_system.LINKS)[:80])
ok("в ссылке есть тема", "subject=" in (fake_system.LINKS[0] if fake_system.LINKS else ""))
ok("длина ссылки в разумных пределах",
   len(fake_system.LINKS[0]) < 4000 if fake_system.LINKS else False,
   str(len(fake_system.LINKS[0]) if fake_system.LINKS else 0))
ok("папку с файлами показали", len(fake_system.OPENED) == 1, str(fake_system.OPENED))
ok("человеку сказано, что приложить надо самому",
   "приложите" in out2.get("note", "").lower(), out2.get("note"))

print("\n=== 5. Telegram ===")
# Просто «tg://msg» открывает клиент и на этом останавливается — выбора чата
# не будет. Нужен «tg://msg_url» с текстом: по нему Telegram показывает, кому
# отправить, и подставляет готовое сообщение.
fake_desktop.reset()
fake_system.LINKS.clear()
out3 = share.send(rec_id, "telegram", ["protocol"], text="**Протокол**\n• Отгрузка в пятницу")
link = fake_system.LINKS[0] if fake_system.LINKS else ""
ok("открыт выбор чата, а не просто клиент", link.startswith("tg://msg_url?url="), link[:60])
ok("текст уехал в ссылку", "%D0%9F%D1%80%D0%BE%D1%82%D0%BE%D0%BA%D0%BE%D0%BB" in link, link[:90])
ok("письма при этом не открывали", fake_desktop.MAILS == [], str(fake_desktop.MAILS))
ok("файлов не готовили", out3.get("files") == [], str(out3.get("files")))
ok("текст влез целиком", out3.get("cut") is False, str(out3))
ok("сказано, что надо выбрать чат", "выберите чат" in out3.get("note", "").lower(), out3.get("note"))

fake_system.LINKS.clear()
long_text = "Очень длинный протокол. " * 200
out4 = share.send(rec_id, "telegram", ["protocol"], text=long_text)
ok("длинный текст обрезан", out4.get("cut") is True, str(out4)[:160])
ok("ссылка осталась в разумных пределах",
   fake_system.LINKS and len(fake_system.LINKS[0]) < 20000, str(len(fake_system.LINKS[0])))
ok("сказано, что целиком документ в буфере",
   "буфер" in out4.get("note", "").lower(), out4.get("note"))

fake_system.LINKS.clear()
out5 = share.send(rec_id, "telegram", ["protocol"], text="")
ok("без текста открываем просто клиент",
   fake_system.LINKS == ["tg://msg"], str(fake_system.LINKS))
ok("и говорим про буфер обмена", "буфер" in out5.get("note", "").lower(), out5.get("note"))

print("\n=== 6. Отказы ===")
try:
    share.send(rec_id, "факс", ["protocol"])
    ok("неизвестное «куда» отвергается", False, "исключения не было")
except ValueError:
    ok("неизвестное «куда» отвергается", True)
try:
    share.send("нет-такой-записи", "mail", ["protocol"])
    ok("чужая запись отвергается", False, "исключения не было")
except ValueError:
    ok("чужая запись отвергается", True)
try:
    share.send(rec_id, "mail", [])
    ok("пустой выбор отвергается", False, "исключения не было")
except ValueError:
    ok("пустой выбор отвергается", True)

print("\n=== 7. Розетка ссылок ===")
fake_system.LINKS.clear()
for good in ("mailto:?subject=X", "tg://msg", "https://example.com"):
    try:
        platform.system().open_link(good)
        ok("своя схема открывается: %s" % good.split(":")[0], True)
    except ValueError as err:
        ok("своя схема открывается: %s" % good.split(":")[0], False, str(err))
for bad in ("file:///C:/Windows/system32/calc.exe", "C:\\Windows\\notepad.exe", "javascript:1"):
    try:
        platform.system().open_link(bad)
        ok("чужая схема не открывается: %s" % bad[:12], False, "открылась")
    except ValueError:
        ok("чужая схема не открывается: %s" % bad[:12], True)

print("\n=== 8. Точки службы ===")
from fastapi.testclient import TestClient  # noqa: E402
from hagen.server import app  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
fake_desktop.reset()
with TestClient(app, base_url="http://127.0.0.1:8787") as client:
    r = client.get("/api/recordings/%s/share" % rec_id, headers=ORIGIN)
    ok("список отправляемого отдаётся", r.status_code == 200, r.text[:160])
    r2 = client.post("/api/recordings/%s/share" % rec_id,
                     json={"target": "mail", "keys": ["protocol"]}, headers=ORIGIN)
    ok("отправка принята", r2.status_code == 200, r2.text[:160])
    ok("письмо открылось через службу", len(fake_desktop.MAILS) == 1,
       str(len(fake_desktop.MAILS)))
    r3 = client.post("/api/recordings/%s/share" % rec_id,
                     json={"target": "факс", "keys": ["protocol"]}, headers=ORIGIN)
    ok("неизвестное «куда» — отказ службы", r3.status_code == 400, str(r3.status_code))
    r4 = client.get("/api/recordings/нет-такой/share", headers=ORIGIN)
    ok("чужая запись — 404", r4.status_code == 404, str(r4.status_code))

platform.use(None)
try:
    store.delete(rec_id)
except Exception:
    pass

print("\nВсего провалов: %d" % len(FAIL))
if FAIL:
    for f in FAIL:
        print("  - %s" % f)
sys.exit(1 if FAIL else 0)
