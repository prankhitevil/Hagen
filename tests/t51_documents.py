# -*- coding: utf-8 -*-
"""Проверка 51: документы записи — одна кнопка, свои инструкции, имя владельца.

Решения 13.09:
  * одна кнопка «Сделать документ»: протокол, саммари, конспект, выжимка, вопрос;
    «Краткое резюме» убрано;
  * у каждого документа свой файл и свой раздел заметки — документы больше не
    затирают друг друга;
  * «Настройки → Обработка»: задачу и структуру документа можно править, общие
    правила закреплены; имя владельца меняется («Я» или «Иван П.»);
  * снимки экрана: модель получает только время и имя файла и ставит ссылку
    ![[файл|700]] в нужное место документа;
  * категорию можно убрать из списка — папка в сейфе остаётся;
  * саммари после обработки видео по умолчанию не запускается;
  * снимки по умолчанию только из папки «Снимки экрана».

Модель подменена: проверяется путь промпта, файлов и заметки, а не Claude.
Сейф и настройки на время проверки — временные: настоящий settings.json и сейф
на Яндекс.Диске проверка не трогает.
"""
import io
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()

LINES = []
FAIL = []
MADE = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def _signs(text):
    """Сколько подписей под документами в тексте: любой из двух форм."""
    return text.count("_Подготовил Hagen") + text.count("_Сформировано")


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


from hagen import config, jobs, media, minutes, obsidian, store  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t51_"))
VAULT = TMP / "vault"
VAULT.mkdir()
OVERRIDES = {"vault_path": str(VAULT), "vault_subfolder": "Meetings", "categories": ["Встречи"],
             "default_category": "Встречи", "hidden_categories": [], "prompt_overrides": {},
             "owner_name": "Я", "smart_folder_name": False, "minutes_engine": "claude_cli"}
_real_get, _real_save = config.get, config.save
config.get = lambda k, d=None: OVERRIDES[k] if k in OVERRIDES else _real_get(k, d)
config.save = lambda patch: (OVERRIDES.update(patch), dict(OVERRIDES))[1]

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 51"):
        store.delete(old["id"])


def new_record(title, **meta):
    rid = store.create(title=title, mode="online", source=meta.pop("source", "live"),
                       category="Встречи")["id"]
    MADE.append(rid)
    if meta:
        store.update(rid, meta)
    return rid


def wait_job(job_id, limit=60.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        job = jobs.get(job_id)
        if job and job.get("status") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.1)
    return jobs.get(job_id) or {"status": "timeout"}


PROMPTS = []
COUNT = {"n": 0}


def fake_engine(engine, prompt, text, timeout=None, role="strong", handle=None):
    PROMPTS.append((prompt, text))
    COUNT["n"] += 1
    n = COUNT["n"]
    # «Свой запрос» (20.09): одно поле и на вопрос, и на заказ документа.
    if "выполни просьбу пользователя" in prompt:
        return "ОТВЕТ-%d по стенограмме." % n
    if "протокол совещания" in prompt.lower() and "НАЗВАНИЕ:" not in prompt:
        # как у настоящего Claude: участники — своим разделом, суть — в следующих
        return "# Протокол совещания\n\n## Участники\n\n- Я\n\n## Принятые решения\n\nПРОТОКОЛ-%d\n" % n
    body = "САММАРИ" if "краткое, но содержательное" in prompt else (
        "КОНСПЕКТ" if "конспект" in prompt else "ВЫЖИМКА")
    return "НАЗВАНИЕ: Проверка\nПАПКА: проверка\n\n## О чём запись\n\n%s-%d\n" % (body, n)


real_engine = minutes._run_engine
minutes._run_engine = fake_engine

try:
    say("=== 1. Имя владельца ===")
    check("по умолчанию — «Я»", store.owner_name() == "Я", store.owner_name())
    check("правило «пиши Я»", "Так и пиши «Я»" in minutes.common_rules())
    OVERRIDES["owner_name"] = "  Иван   П.  "
    check("лишние пробелы убраны", store.owner_name() == "Иван П.", store.owner_name())
    check("реплики «Я» подписываются именем", store.display_speaker("Я") == "Иван П.")
    check("чужие имена не трогаются", store.display_speaker("Иван") == "Иван")
    rules = minutes.common_rules()
    check("в общих правилах — имя владельца", "«Иван П.» — владелец записи" in rules
          and "Так и пиши «Я»" not in rules, rules[-300:])
    rid = new_record("Проверка 51 — имя")
    store.replace_segments(rid, [store.make_segment("mic", 0.0, 3.0, "Начинаем."),
                                 store.make_segment("far", 10.0, 12.0, "Добрый день.")])
    text = minutes.build_transcript_text(rid)
    check("в стенограмме для модели — имя вместо «Я»", "Иван П.: Начинаем." in text
          and "] Я: " not in text, text[-200:])
    check("в контрольном списке протокола — имя",
          "«Иван П.» остаётся «Иван П.»" in minutes._with_reminder("текст", "protocol"))
    note = Path(obsidian.save_note(rid)["path"]).read_text(encoding="utf-8")
    check("в заметке реплики подписаны именем", "Иван П." in note and "Начинаем." in note)
    OVERRIDES["owner_name"] = ""
    check("пустое имя — снова «Я»", store.owner_name() == "Я")
    OVERRIDES["owner_name"] = "Я"

    say("")
    say("=== 2. Свои инструкции («Настройки → Обработка») ===")
    from fastapi.testclient import TestClient

    from hagen import server

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        got = cli.get("/api/prompts").json()
        keys = [p["key"] for p in got["prompts"]]
        check("пять документов в редакторе", keys == ["protocol", "meeting", "lecture", "interview", "question"],
              keys)
        check("все исходные", not any(p["overridden"] for p in got["prompts"]))
        custom = "Задача: составь протокол СВОЕЙ структуры.\n# Итоги\n## Решения"
        r = cli.post("/api/prompts", headers=ORIGIN, json={
            "owner_name": "Иван П.",
            "overrides": {"protocol": custom, "lecture": minutes.DEFAULT_TASKS["lecture"] + "\n\n",
                          "чужой": "что-то"}})
        got = r.json()
        prot = next(p for p in got["prompts"] if p["key"] == "protocol")
        check("своя инструкция протокола сохранена", prot["overridden"] and prot["current"] == custom.strip(),
              prot["current"][:80])
        check("имя сохранено", got["owner_name"] == "Иван П." and OVERRIDES["owner_name"] == "Иван П.")
        check("текст, равный исходному, не хранится",
              "lecture" not in OVERRIDES["prompt_overrides"], OVERRIDES["prompt_overrides"].keys())
        check("чужой ключ отброшен", "чужой" not in OVERRIDES["prompt_overrides"])
        final = minutes._final_prompt("protocol", None)
        check("в промпт ушла своя структура", "СВОЕЙ структуры" in final
              and "Первая строка ответа — ровно «# Протокол совещания»" not in final)
        check("общие правила остались на месте", final.startswith("Ты помощник")
              and "только по-русски" in final and "«Иван П.» — владелец записи" in final)
        check("правило оформления ответа — последним", final.rstrip().endswith("внешними службами не пользуйся."))
        check("свой протокол — без контрольного списка исходной структуры",
              "КОНТРОЛЬНЫЙ СПИСОК" not in minutes._with_reminder("текст", "protocol"))
        cli.post("/api/prompts", headers=ORIGIN, json={"overrides": {"lecture": "Задача: СВОЙ конспект."}})
        check("своя инструкция конспекта — в промпте видео", "СВОЙ конспект" in minutes.video_prompt("lecture")
              and "НАЗВАНИЕ:" in minutes.video_prompt("lecture"))
        r = cli.post("/api/prompts", headers=ORIGIN, json={
            "owner_name": "", "overrides": {"protocol": minutes.DEFAULT_TASKS["protocol"], "lecture": ""}})
        got = r.json()
        check("«Вернуть исходный» и пустое поле сбрасывают правку",
              not any(p["overridden"] for p in got["prompts"]) and OVERRIDES["prompt_overrides"] == {},
              OVERRIDES["prompt_overrides"])
        check("пустое имя сохраняется как «Я»", got["owner_name"] == "Я")
        check("исходный протокол снова с контрольным списком",
              "КОНТРОЛЬНЫЙ СПИСОК" in minutes._with_reminder("текст", "protocol"))

        say("")
        say("=== 3. Снимки экрана: время и имя файла, ссылка в нужном месте ===")
        shot_rec = new_record("Проверка 51 — снимки")
        store.replace_segments(shot_rec, [store.make_segment("mic", 0.0, 3.0, "Открываю отчёт."),
                                          store.make_segment("far", 20.0, 22.0, "Видим рост.")])
        store.update(shot_rec, {"screenshots": [
            {"file": "Снимок 1.png", "at_s": 12.0, "path": str(TMP / "Снимок 1.png"), "source": "folder"}]})
        text = minutes.build_transcript_text(shot_rec)
        i1, i2, i3 = text.find("Открываю отчёт."), text.find("[00:00:12] 🖼 Снимок экрана: Снимок 1.png"), \
            text.find("Видим рост.")
        check("строка снимка стоит между репликами по времени", 0 <= i1 < i2 < i3, (i1, i2, i3))
        check("правило ссылок, когда снимки есть",
              "![[имя файла|700]]" in minutes._final_prompt("protocol", None, has_shots=True))
        check("без снимков — запрет вставлять картинки",
              "НЕ вставляй" in minutes._final_prompt("protocol", None, has_shots=False))
        check("правило ссылок и в саммари", "![[имя файла|700]]" in minutes.video_prompt("meeting", True))
        kinds = cli.get("/api/documents/kinds?rec_id=%s" % shot_rec).json()
        check("окно документа знает число снимков", kinds.get("shots") == 1, kinds.get("shots"))
        check("пять видов документа", [d["key"] for d in kinds["docs"]]
              == ["protocol", "meeting", "lecture", "interview", "question"])
        check("с микрофона предлагается протокол", kinds.get("default_doc") == "protocol")
        PROMPTS.clear()
        r = cli.post("/api/recordings/%s/document" % shot_rec, headers=ORIGIN, json={"doc": "protocol"})
        job = wait_job(r.json().get("job_id"))
        check("протокол со снимками собран", job.get("status") == "done", job.get("error"))
        sent_prompt, sent_text = PROMPTS[-1] if PROMPTS else ("", "")
        check("модели ушло правило ссылок", "![[имя файла|700]]" in sent_prompt)
        check("модели ушло имя снимка, но не картинка",
              "Снимок экрана: Снимок 1.png" in sent_text and "![[" not in sent_text)

        say("")
        say("=== 4. Одна кнопка — пять документов, у каждого свой файл и раздел ===")
        rec = new_record("Проверка 51 — документы")
        store.replace_segments(rec, [store.make_segment("mic", 0.0, 3.0, "Начнём планёрку."),
                                     store.make_segment("far", 5.0, 8.0, "Смета готова к четвергу.")])
        r = cli.post("/api/recordings/%s/save" % rec, headers=ORIGIN, json={})
        note_path = Path(r.json()["path"])

        def make(doc, **extra):
            resp = cli.post("/api/recordings/%s/document" % rec, headers=ORIGIN, json=dict(extra, doc=doc))
            if resp.status_code != 200:
                return {"status": "http %d" % resp.status_code, "error": resp.text}
            return wait_job(resp.json()["job_id"])

        def note_text():
            return note_path.read_text(encoding="utf-8") if note_path.exists() else ""

        check("протокол", make("protocol").get("status") == "done")
        check("саммари встречи", make("meeting").get("status") == "done")
        j = make("question", question="Когда смета?")
        check("вопрос", j.get("status") == "done", j.get("error"))
        j = make("question", question="Кто ведёт?")
        check("второй вопрос", j.get("status") == "done", j.get("error"))
        j = make("lecture", video_kind="lecture")
        check("конспект с выбором типа «лекция»", j.get("status") == "done", j.get("error"))
        d = store.rec_dir(rec)
        check("у каждого документа свой файл",
              all((d / f).exists() for f in ("minutes.md", "summary.md", "qa.md", "conspect.md")),
              sorted(p.name for p in d.glob("*.md")))
        check("тип записи из окна запомнился", (store.get(rec) or {}).get("video_kind") == "lecture")
        qa = (d / "qa.md").read_text(encoding="utf-8")
        check("ответы на вопросы копятся", qa.count("ОТВЕТ-") == 2, qa)
        docs = cli.get("/api/recordings/%s/documents" % rec).json()
        check("список документов по порядку, последний — конспект",
              [x["key"] for x in docs["documents"]] == ["protocol", "meeting", "lecture", "question"]
              and docs["last"] == "lecture", ([x["key"] for x in docs["documents"]], docs["last"]))
        body = note_text()
        # «Свои запросы» — с 20.09: в разделе и ответы на вопросы, и документы,
        # заказанные своими словами.
        heads = [h for h in ("# Протокол", "# Саммари", "# Конспект", "# Свои запросы", "# Стенограмма")
                 if ("\n" + h + "\n") in body]
        check("в заметке все разделы и стенограмма", len(heads) == 5, heads)
        mains = [ln for ln in body.splitlines() if ln.startswith("# ")]
        check("главные разделы: «О записи», 4 документа, стенограмма (формат 15.09)",
              mains == ["# О записи", "# Протокол", "# Саммари", "# Конспект", "# Свои запросы",
                        "# Стенограмма"], mains)
        about = body.split("# О записи", 1)[1].split("\n# ", 1)[0]
        check("«О записи» — только то, чего нет в свойствах",
              "Текст получен" in about and not any(x in about for x in ("Дата", "Длительность", "Категория",
                                                                         "Режим", "Участники")), about)
        check("названия записи в тексте нет", "# Проверка 51" not in body)
        check("в заметке тексты всех документов",
              all(m in body for m in ("ПРОТОКОЛ-", "САММАРИ-", "КОНСПЕКТ-")) and body.count("ОТВЕТ-") == 2)
        check("документы стоят перед стенограммой",
              max(body.find("ПРОТОКОЛ-"), body.find("САММАРИ-"), body.find("КОНСПЕКТ-")) < body.find("\n# Стенограмма"))

        check("раздел заметки совпадает с файлом документа (с подписью под ним)",
              _signs(body) == 5, _signs(body))
        before = body
        check("протокол заново", make("protocol").get("status") == "done")
        body = note_text()
        prot_before = [ln for ln in before.splitlines() if ln.startswith("ПРОТОКОЛ-")]
        prot_now = [ln for ln in body.splitlines() if ln.startswith("ПРОТОКОЛ-")]
        check("новый протокол заменил старый, а не добавился", len(prot_now) == 1 and prot_now != prot_before,
              (prot_before, prot_now))
        check("саммари, конспект и ответы при этом на месте",
              all(m in body for m in ("САММАРИ-", "КОНСПЕКТ-")) and body.count("ОТВЕТ-") == 2)
        check("подпись под документом не задвоилась",
              _signs(body) == _signs(before),
              (_signs(before), _signs(body)))

        r = cli.post("/api/recordings/%s/save" % rec, headers=ORIGIN, json={})
        body = note_text()
        check("пересохранение заметки держит все документы",
              all(m in body for m in ("ПРОТОКОЛ-", "САММАРИ-", "КОНСПЕКТ-")) and body.count("ОТВЕТ-") == 2
              and "Смета готова к четвергу." in body)
        check("в пересохранённой заметке подпись не задвоилась",
              _signs(body) == _signs(before), _signs(body))

        # Запись без заметки: первый документ создаёт заметку без стенограммы
        rec_b = new_record("Проверка 51 — без заметки")
        store.replace_segments(rec_b, [store.make_segment("mic", 0.0, 3.0, "Коротко.")])
        rb = cli.post("/api/recordings/%s/document" % rec_b, headers=ORIGIN, json={"doc": "meeting"})
        wait_job(rb.json()["job_id"])
        rb = cli.post("/api/recordings/%s/document" % rec_b, headers=ORIGIN, json={"doc": "protocol"})
        wait_job(rb.json()["job_id"])
        rb = cli.post("/api/recordings/%s/document" % rec_b, headers=ORIGIN, json={"doc": "meeting"})
        wait_job(rb.json()["job_id"])
        nb = Path((store.get(rec_b) or {}).get("vault_path") or "")
        tb = nb.read_text(encoding="utf-8") if nb.exists() else ""
        check("без заметки: оба документа в новой заметке, саммари заменено",
              "ПРОТОКОЛ-" in tb and tb.count("САММАРИ-") == 1 and "# Стенограмма" not in tb, tb[-400:])
        check("без заметки: главные разделы — «О записи», протокол и саммари",
              [ln for ln in tb.splitlines() if ln.startswith("# ")] == ["# О записи", "# Протокол", "# Саммари"],
              [ln for ln in tb.splitlines() if ln.startswith("# ")])
        check("без заметки: подпись последнего раздела не задвоилась",
              _signs(tb) == 2, _signs(tb))

        say("")
        say("=== 5. Ошибки и повтор ===")
        r = cli.post("/api/recordings/%s/document" % rec, headers=ORIGIN, json={"doc": "summary_short"})
        check("неизвестный документ — отказ", r.status_code == 400, r.status_code)
        r = cli.post("/api/recordings/%s/document" % rec, headers=ORIGIN, json={"doc": "question"})
        check("вопрос без текста — отказ", r.status_code == 400, r.status_code)
        r = cli.post("/api/recordings/нет-такой/document", headers=ORIGIN, json={"doc": "protocol"})
        check("нет записи — 404", r.status_code == 404, r.status_code)

        def failing(*a, **k):
            raise RuntimeError("подменный сбой")

        minutes._run_engine = failing
        jm = wait_job(cli.post("/api/recordings/%s/document" % rec, headers=ORIGIN,
                               json={"doc": "meeting"}).json()["job_id"])
        jp = wait_job(cli.post("/api/recordings/%s/document" % rec, headers=ORIGIN,
                               json={"doc": "protocol"}).json()["job_id"])
        minutes._run_engine = fake_engine
        check("повтор саммари — тем же документом",
              (jm.get("retry") or {}).get("body") == {"document": "meeting"}
              and "/api/media/" in (jm.get("retry") or {}).get("url", ""), jm.get("retry"))
        check("повтор протокола — протоколом", (jp.get("retry") or {}).get("body") == {"template": "protocol"},
              jp.get("retry"))
        body = note_text()
        check("сбой не испортил документы в заметке",
              all(m in body for m in ("ПРОТОКОЛ-", "САММАРИ-", "КОНСПЕКТ-")))

        say("")
        say("=== 6. Старые записи: документ из summary.md разносится по видам ===")
        old_l = new_record("Проверка 51 — старая лекция", source="file", video_kind="lecture")
        io.open(store.rec_dir(old_l) / "summary.md", "w", encoding="utf-8").write("СТАРЫЙ КОНСПЕКТ")
        meta = store.get(old_l) or {}
        meta.pop("documents", None)
        docs = minutes.stored_documents(old_l)
        check("конспект старой лекции переехал в свой файл",
              [x["key"] for x in docs] == ["lecture"] and (store.rec_dir(old_l) / "conspect.md").exists()
              and not (store.rec_dir(old_l) / "summary.md").exists(), [x["key"] for x in docs])
        check("и показывается", "СТАРЫЙ КОНСПЕКТ" in minutes.load_minutes(old_l))
        old_m = new_record("Проверка 51 — старое совещание", source="file", video_kind="meeting")
        io.open(store.rec_dir(old_m) / "summary.md", "w", encoding="utf-8").write("СТАРОЕ САММАРИ")
        io.open(store.rec_dir(old_m) / "minutes.md", "w", encoding="utf-8").write("СТАРЫЙ ПРОТОКОЛ")
        check("у старой встречи оба документа",
              [x["key"] for x in minutes.stored_documents(old_m)] == ["protocol", "meeting"])

        say("")
        say("=== 6a. Заметка — выгрузка из программы: правки в Obsidian не сохраняются (15.09) ===")
        # Решение 15.09 отменяет прежнее решение: заметку в Obsidian
        # не правят, она всегда переписывается из данных записи.
        rid21 = new_record("Проверка 51 — правки руками")
        store.replace_segments(rid21, [store.make_segment("mic", 0.0, 3.0, "Первая часть.")])
        io.open(store.rec_dir(rid21) / "minutes.md", "w", encoding="utf-8").write(
            "## Решения\n\nМАШИННЫЙ ПРОТОКОЛ\n")
        note21 = Path(obsidian.save_note(rid21)["path"])
        check("протокол попал в заметку", "МАШИННЫЙ ПРОТОКОЛ" in note21.read_text(encoding="utf-8"))
        # кто-то правит протокол и стенограмму прямо в Obsidian
        note21.write_text(
            note21.read_text(encoding="utf-8").replace("МАШИННЫЙ ПРОТОКОЛ", "ПРАВКА РУКАМИ")
            .replace("Первая часть.", "Первая часть (поправлено)."),
            encoding="utf-8")
        # «Стоп» пересохраняет заметку
        store.replace_segments(rid21, [store.make_segment("mic", 0.0, 3.0, "Первая часть."),
                                       store.make_segment("mic", 5.0, 8.0, "Вторая часть.")])
        after = Path(obsidian.save_note(rid21)["path"]).read_text(encoding="utf-8")
        check("протокол — снова из программы, правка из Obsidian не сохранилась",
              "МАШИННЫЙ ПРОТОКОЛ" in after and "ПРАВКА РУКАМИ" not in after, after[:400])
        check("стенограмма — из программы, с новой репликой",
              "Вторая часть." in after and "поправлено" not in after)
        check("протокол над стенограммой", after.index("\n# Протокол\n") < after.index("\n# Стенограмма\n"))
        check("разделов протокола по-прежнему один", after.count("\n# Протокол\n") == 1,
              after.count("\n# Протокол\n"))
        check("подраздел документа «Решения» — внутри протокола, вторым уровнем",
              "\n## Решения\n" in after and after.index("\n# Протокол\n") < after.index("\n## Решения\n")
              < after.index("\n# Стенограмма\n"))
        check("подпись заметки не размножилась",
              after.count("Файл создан приложением") == 1, after.count("Файл создан приложением"))
        # как в программе: документ сначала ложится в файл записи, потом в заметку
        io.open(store.rec_dir(rid21) / "minutes.md", "w", encoding="utf-8").write(
            "## Решения\n\nНОВЫЙ ПРОТОКОЛ\n")
        obsidian.append_minutes(rid21, "## Решения\n\nНОВЫЙ ПРОТОКОЛ")
        after2 = note21.read_text(encoding="utf-8")
        check("«Сделать документ» переписывает заметку: новый протокол, стенограмма на месте",
              "НОВЫЙ ПРОТОКОЛ" in after2 and "МАШИННЫЙ ПРОТОКОЛ" not in after2
              and "Вторая часть." in after2 and after2.index("\n# Протокол\n") < after2.index("\n# Стенограмма\n"),
              after2[:300])
        io.open(store.rec_dir(rid21) / "summary.md", "w", encoding="utf-8").write("САММАРИ ВСТРЕЧИ\n")
        obsidian.append_minutes(rid21, "САММАРИ ВСТРЕЧИ", heading=obsidian.H_SUMMARY)
        after3 = note21.read_text(encoding="utf-8")
        check("другой вид документа — в той же заметке все документы, стенограмма под ними",
              "НОВЫЙ ПРОТОКОЛ" in after3 and "САММАРИ ВСТРЕЧИ" in after3
              and max(after3.index("\n# Протокол\n"), after3.index("\n# Саммари\n"))
              < after3.index("\n# Стенограмма\n"), after3[:500])
        mains3 = [ln for ln in after3.splitlines() if ln.startswith("# ")]
        check("главные разделы: «О записи», протокол, саммари, стенограмма",
              mains3 == ["# О записи", "# Протокол", "# Саммари", "# Стенограмма"], mains3)
        # протокол как у настоящего Claude: название, строка даты, участники — всё это уже в свойствах
        io.open(store.rec_dir(rid21) / "minutes.md", "w", encoding="utf-8").write(
            "# Протокол совещания\nДата: 15.09.2026, начало в 10:03, длительность 18 минут.\n\n"
            "## Участники\n- Иван Петров\n- Сергей Куренков\n\n## Обсуждённые вопросы\n- Смета.\n\n"
            "## Задачи\n### Срочные\n- Подписать.\n")
        obsidian.save_note(rid21)
        prot = note21.read_text(encoding="utf-8").split("\n# Протокол\n", 1)[1].split("\n# ", 1)[0]
        check("в «# Протокол» нет названия документа, строки даты и «Участников»",
              "Протокол совещания" not in prot and "Дата:" not in prot and "Участники" not in prot
              and "Сергей Куренков" not in prot, prot)
        check("подразделы протокола — ##, глубже — ###",
              "\n## Обсуждённые вопросы\n" in prot and "\n## Задачи\n" in prot and "\n### Срочные\n" in prot, prot)

        say("")
        say("=== 7. Категории: убрать из списка, папка остаётся ===")
        r = cli.post("/api/vault/category", headers=ORIGIN, json={"name": "Проекты"})
        cats = r.json().get("categories") or []
        folder = VAULT / "Meetings" / "Проекты"
        check("категория создана папкой", "Проекты" in cats and folder.is_dir(), cats)
        (folder / "моя заметка.md").write_text("не трогать", encoding="utf-8")
        r = cli.post("/api/vault/category", headers=ORIGIN, json={"name": "Проекты"})
        check("папка с таким именем уже есть — ничего не перезаписано",
              r.status_code == 200 and (folder / "моя заметка.md").read_text(encoding="utf-8") == "не трогать")
        OVERRIDES["default_category"] = "Проекты"
        r = cli.post("/api/vault/category/remove", headers=ORIGIN, json={"name": "Проекты"})
        got = r.json()
        check("убрана из списка", r.status_code == 200 and "Проекты" not in got["categories"], got)
        check("папка и заметка в ней остались", (folder / "моя заметка.md").exists())
        check("категория по умолчанию сменилась на оставшуюся", got.get("default") != "Проекты", got.get("default"))
        check("в списке программы её больше нет, хотя папка есть", "Проекты" not in obsidian.list_categories())
        r = cli.post("/api/vault/category/remove", headers=ORIGIN, json={"name": "Проекты"})
        check("повторно убрать нельзя — 404", r.status_code == 404, r.status_code)
        r = cli.post("/api/vault/category", headers=ORIGIN, json={"name": "Проекты"})
        check("добавили снова — вернулась, заметки на месте",
              "Проекты" in r.json()["categories"] and (folder / "моя заметка.md").exists())
        for name in list(obsidian.list_categories())[:-1]:
            cli.post("/api/vault/category/remove", headers=ORIGIN, json={"name": name})
        last = obsidian.list_categories()
        r = cli.post("/api/vault/category/remove", headers=ORIGIN, json={"name": last[0]})
        check("последнюю категорию убрать нельзя", len(last) == 1 and r.status_code == 400, (last, r.status_code))

    say("")
    say("=== 8. Значения по умолчанию ===")
    check("саммари после обработки видео — только по кнопке", config.DEFAULTS.get("make_summary") is False)
    check("галочка задания без явного значения — выключена",
          media._flag({}, "make_summary") is (bool(_real_get("make_summary"))), _real_get("make_summary"))
    check("снимки — только из папки", config.DEFAULTS.get("screenshots_from_clipboard") is False)
    check("имя владельца по умолчанию — «Я»", config.DEFAULTS.get("owner_name") == "Я")
    from hagen.sources import sharepoint  # noqa: F401

    check("httpx не пишет ссылки скачивания в журнал",
          logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING)
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    # После этапа 6 создание документа — вкладка «+ Документ» в ряду вкладок.
    check("в окне одна кнопка документа", 'id="btn-add-doc"' in html
          and 'id="btn-minutes"' not in html and 'id="btn-summary"' not in html)
    check("вкладка «Обработка» в настройках", 'id="prompt-list"' in html and 'id="set-owner"' in html)
    # Подпись «авторазметка» с экрана записи убрана (п. 6.1): настройка одна,
    # и живёт она в настройках.
    check("авторазметка — только в основных настройках", 'id="set-auto-diarize"' in html
          and 'id="auto-diarize-note"' not in html)
    check("у кнопок есть подсказки",
          all(('id="%s"' % b) in html for b in ("btn-copy-transcript", "btn-hide", "btn-del-cat"))
          and html.count('title="') >= 40, html.count('title="'))
finally:
    minutes._run_engine = real_engine
    config.get, config.save = _real_get, _real_save
    for rid in MADE:
        try:
            store.delete(rid)
        except Exception:
            pass
    shutil.rmtree(TMP, ignore_errors=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t51_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
