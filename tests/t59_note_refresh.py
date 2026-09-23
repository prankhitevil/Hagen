# -*- coding: utf-8 -*-
"""Проверка 59: заметка в Obsidian — выгрузка из программы и обновляется сама (15.09).

Найдено на звонке 15.09 9:30: заметку записал «Стоп» до разметки голосов, и в
ней остались «Участник» — ни разметка, ни подпись имён, ни «Сделать документ»
её не обновили. Решения 15.09:
  1. уже существующая заметка переписывается сама после разметки голосов,
     подписи имён, «Перечитать точнее» и разделения голосов;
  2. «Сделать документ» переписывает заметку: все документы записи, под ними
     стенограмма (если она там была), участники свежие;
  3. заметка — выгрузка из программы, в Obsidian её не правят: правки там не
     сохраняются;
  4. неудачное сохранение пишется в журнал и видно на экране.

Хранилище — временная папка, база голосов — временная (isolate). Записи
создаются и удаляются проверкой.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t59_note_refresh.py
"""
import io
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()

MADE = []


from hagen import config, obsidian, recordings, store  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t59_"))
VAULT = TMP / "vault"
VAULT.mkdir()
OVERRIDES = {"vault_path": str(VAULT), "vault_subfolder": "Meetings", "categories": ["Встречи"],
             "default_category": "Встречи", "hidden_categories": [], "owner_name": "Иван П.",
             "diarize_auto": False}
_real_get, _real_save = config.get, config.save
config.get = lambda k, d=None: OVERRIDES[k] if k in OVERRIDES else _real_get(k, d)
config.save = lambda patch: (OVERRIDES.update(patch), dict(OVERRIDES))[1]

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 59"):
        store.delete(old["id"])


def record(title):
    rid = store.create(title=title, mode="online", source="live", category="Встречи")["id"]
    MADE.append(rid)
    segs = [store.make_segment("far", 0, 4, "Доброе утро.", speaker="Участник", speaker_key="SPEAKER_00"),
            store.make_segment("mic", 5, 8, "Привет.", speaker="Я", speaker_key="me"),
            store.make_segment("far", 9, 12, "Начинаем.", speaker="Участник", speaker_key="SPEAKER_01")]
    store.replace_segments(rid, segs)
    store.update(rid, {"diarized": True})
    store.refresh_participants(rid)
    return rid


def rename(rid, key, name):
    store.rename_speaker(rid, key, name)
    store.refresh_participants(rid)


def note_text(rid):
    p = Path((store.get(rid) or {}).get("vault_path") or "")
    return io.open(p, encoding="utf-8").read() if p.is_file() else ""


def props(text):
    """Свойства заметки (frontmatter) без остального текста."""
    return text.split("\n---", 1)[0]


def write_doc(rid, name, text):
    io.open(store.rec_dir(rid) / name, "w", encoding="utf-8").write(text + "\n")


class Keep(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage())


from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
NOTICES = []
_real_publish = server.hub.publish


def spy_publish(ev):
    if isinstance(ev, dict) and ev.get("type") == "notice":
        NOTICES.append(ev)
    return _real_publish(ev)


server.hub.publish = spy_publish
keep = Keep()
logging.getLogger("hagen.server").addHandler(keep)
logging.getLogger("hagen.recordings").addHandler(keep)   # автообновление заметки живёт в ядре

try:
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        say("=== 1. «Стоп» записал заметку — там «Участник» ===")
        r1 = record("Проверка 59 — обновляется сама")
        obsidian.save_note(r1)
        check("в заметке пока «Участник»", "] Участник:**" in note_text(r1), note_text(r1)[:300])

        say("")
        say("=== 2. Имена поменялись — заметка обновилась сама ===")
        rename(r1, "SPEAKER_00", "Сергей Фещенко")
        recordings.refresh_note(r1, "проверка")
        text = note_text(r1)
        check("свойства: участник по имени", "Сергей Фещенко" in props(text), text[:300])
        check("названия записи в тексте нет (формат 15.09)", "# Проверка 59" not in text)
        check("только стенограмма — «О записи» и «Стенограмма»",
              [ln for ln in text.splitlines() if ln.startswith("# ")] == ["# О записи", "# Стенограмма"],
              [ln for ln in text.splitlines() if ln.startswith("# ")])
        about = text.split("# О записи", 1)[1].split("\n# ", 1)[0]
        check("в «О записи» нет участников — они в свойствах", "Участники" not in about
              and "Сергей Фещенко" not in about, about)
        check("стенограмма: реплика подписана именем", "] Сергей Фещенко:**" in text)

        say("")
        say("=== 3. Правки в Obsidian не сохраняются — заметка из программы ===")
        path = Path(store.get(r1)["vault_path"])
        edited = text.replace("Начинаем.", "Начинаем, коллеги (поправлено в Obsidian).")
        edited = edited.replace("tags: [", "project: Hagen\ntags: [", 1)
        io.open(path, "w", encoding="utf-8", newline="\n").write(edited)
        rename(r1, "SPEAKER_01", "София Перелыгина")
        recordings.refresh_note(r1, "проверка")
        t3 = note_text(r1)
        check("заметка переписана из программы: новое имя на месте", "] София Перелыгина:**" in t3)
        check("правка стенограммы из Obsidian не сохранилась", "поправлено в Obsidian" not in t3)
        check("своё свойство из Obsidian не сохранилось", "project: Hagen" not in t3)

        say("")
        say("=== 4. Документы: все в одной заметке, над стенограммой; правки не сохраняются ===")
        r4 = record("Проверка 59 — документы")
        obsidian.save_note(r4)
        write_doc(r4, "minutes.md", "ТЕКСТ ПРОТОКОЛА")
        obsidian.append_minutes(r4, "ТЕКСТ ПРОТОКОЛА")
        path4 = Path(store.get(r4)["vault_path"])
        io.open(path4, "w", encoding="utf-8", newline="\n").write(
            note_text(r4).replace("ТЕКСТ ПРОТОКОЛА", "ПРОТОКОЛ, ПОПРАВЛЕННЫЙ РУКАМИ"))
        write_doc(r4, "summary.md", "ТЕКСТ САММАРИ")
        rename(r4, "SPEAKER_00", "Саша Пушкин")
        obsidian.append_minutes(r4, "ТЕКСТ САММАРИ", heading=obsidian.H_SUMMARY)
        t4 = note_text(r4)
        check("оба документа в заметке", "ТЕКСТ ПРОТОКОЛА" in t4 and "ТЕКСТ САММАРИ" in t4, t4[:500])
        check("правка протокола из Obsidian не сохранилась", "ПОПРАВЛЕННЫЙ РУКАМИ" not in t4)
        check("документы над стенограммой",
              max(t4.index("\n# Протокол\n"), t4.index("\n# Краткое содержание\n")) < t4.index("\n# Стенограмма\n"))
        check("главные разделы: «О записи», протокол, саммари, стенограмма",
              [ln for ln in t4.splitlines() if ln.startswith("# ")]
              == ["# О записи", "# Протокол", "# Краткое содержание", "# Стенограмма"],
              [ln for ln in t4.splitlines() if ln.startswith("# ")])
        check("стенограмма переписана вместе с документом: новое имя", "] Саша Пушкин:**" in t4)
        check("свойства: участники свежие", "Саша Пушкин" in props(t4))
        write_doc(r4, "minutes.md", "НОВЫЙ ПРОТОКОЛ")
        obsidian.append_minutes(r4, "НОВЫЙ ПРОТОКОЛ")
        t4b = note_text(r4)
        check("пересобрали протокол — заменён только он, саммари осталось",
              "НОВЫЙ ПРОТОКОЛ" in t4b and "ТЕКСТ ПРОТОКОЛА" not in t4b and "ТЕКСТ САММАРИ" in t4b
              and t4b.count("\n# Протокол\n") == 1)

        say("")
        say("=== 5. Заметки нет / заметка только с документами ===")
        r6 = record("Проверка 59 — без заметки")
        rename(r6, "SPEAKER_00", "Иван Петров")
        recordings.refresh_note(r6, "проверка")
        check("заметки нет — сама в хранилище ничего не кладёт",
              not (store.get(r6) or {}).get("vault_path")
              and obsidian.refresh_note(r6).get("skipped") == "no_note")
        write_doc(r6, "minutes.md", "ПРОТОКОЛ БЕЗ СТЕНОГРАММЫ")
        obsidian.append_minutes(r6, "ПРОТОКОЛ БЕЗ СТЕНОГРАММЫ")
        t6 = note_text(r6)
        check("«Сделать документ» без заметки — заметка только с документами",
              "ПРОТОКОЛ БЕЗ СТЕНОГРАММЫ" in t6 and "# Стенограмма" not in t6, t6[:300])
        rename(r6, "SPEAKER_01", "Анна Нилова")
        recordings.refresh_note(r6, "проверка")
        t6b = note_text(r6)
        check("автообновление стенограмму туда не добавляет, участники свежие",
              "# Стенограмма" not in t6b and "Анна Нилова" in props(t6b), t6b[:300])
        r = cli.post("/api/recordings/%s/save" % r6, headers=ORIGIN)
        t6c = note_text(r6)
        check("«Сохранить заметку» добавляет стенограмму под документ",
              r.status_code == 200 and "\n# Стенограмма\n" in t6c
              and t6c.index("\n# Протокол\n") < t6c.index("\n# Стенограмма\n"), r.text[:200])

        say("")
        say("=== 6. Заметка, записанная до 15.09 (без признака стенограммы) ===")
        r7 = record("Проверка 59 — старая заметка")
        obsidian.save_note(r7)
        meta_path = store.paths(r7)["meta"]
        m7 = json.loads(io.open(meta_path, encoding="utf-8").read())
        m7.pop("vault_transcript", None)
        io.open(meta_path, "w", encoding="utf-8").write(json.dumps(m7, ensure_ascii=False))
        rename(r7, "SPEAKER_00", "Олег Кузнецов")
        recordings.refresh_note(r7, "проверка")
        check("старая заметка со стенограммой обновилась, стенограмма на месте",
              "] Олег Кузнецов:**" in note_text(r7))

        say("")
        say("=== 7. Через службу: подпись имени обновляет заметку ===")
        r8 = record("Проверка 59 — подпись через службу")
        obsidian.save_note(r8)
        r = cli.post("/api/recordings/%s/speaker" % r8, headers=ORIGIN,
                     json={"speaker_key": "SPEAKER_00", "name": "Мария Проверкина",
                           "decision": {"new_person": True}, "remember": False})
        check("подпись принята", r.status_code == 200, r.text[:200])
        check("заметка обновилась сразу после подписи", "] Мария Проверкина:**" in note_text(r8))

        say("")
        say("=== 8. Неудачное сохранение — в журнале и на экране ===")
        real_save_note = obsidian.save_note

        def broken(*a, **kw):
            raise OSError("Не удалось записать заметку: файл занят (синхронизация Яндекс.Диска)")

        obsidian.save_note = broken
        try:
            keep.lines.clear()
            r = cli.post("/api/recordings/%s/save" % r8, headers=ORIGIN)
            check("кнопка: ошибка с понятной причиной", r.status_code == 500 and "файл занят" in r.text,
                  r.text[:200])
            check("кнопка: ошибка записана в журнал",
                  any("не сохранилась" in x and "файл занят" in x for x in keep.lines), keep.lines[-3:])
            rename(r8, "SPEAKER_01", "Пётр Ошибкин")
            NOTICES.clear()
            keep.lines.clear()
            recordings.refresh_note(r8, "проверка")
            check("автообновление: ошибка на экране",
                  any(n.get("level") == "err" and "не обновилась" in n.get("text", "") for n in NOTICES), NOTICES)
            check("автообновление: ошибка в журнале", any("не обновилась" in x for x in keep.lines),
                  keep.lines[-3:])
        finally:
            obsidian.save_note = real_save_note

        say("")
        say("=== 9. Подсказки у кнопок и индексная заметка ===")
        html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
        save_tip = html.split('id="btn-save"', 1)[1].split("</button>", 1)[0]
        doc_tip = html.split('id="btn-add-doc"', 1)[1].split("</button>", 1)[0]
        check("«Сохранить заметку»: документы, под ними стенограмма, участники",
              "под ними стенограмма с именами" in save_tip and "список участников" in save_tip, save_tip)
        check("«Сделать документ»: заметка перепишется, все документы и участники",
              "все документы записи и список участников" in doc_tip, doc_tip)
        idx = obsidian._render_index()
        check("индексная заметка: правки здесь не сохраняются",
              "не сохраняются" in idx and "править прямо здесь можно" not in idx)
finally:
    server.hub.publish = _real_publish
    logging.getLogger("hagen.server").removeHandler(keep)
    logging.getLogger("hagen.recordings").removeHandler(keep)
    config.get, config.save = _real_get, _real_save
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t59"))
