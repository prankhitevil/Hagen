# -*- coding: utf-8 -*-
"""Проверка 37: разбор ответа модели для саммари видео.

Повод — живой сбой 12.09: Claude CLI думал 131 секунду, выдал 9769 символов, а
разбор их не принял, и вся работа ушла в никуда. Причина была в самой просьбе:
общее правило «отвечай только разметкой Markdown» спорило с требованием строгого
JSON, и модель выбрала Markdown. Теперь просим две служебные строки и документ,
а разбор JSON остался запасным путём.

Проверяем все пять подходов (служебные строки → строгий JSON → починка переносов
строк → выборка полей по именам → приём обычного Markdown) и то, что сырой ответ
модели всегда остаётся на диске.
"""
import io
import json
import shutil
import sys
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается

isolate.voices()

from hagen import jobs, media, minutes, store  # noqa: E402

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


def check(name, got, want):
    ok = got == want
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + ": " + repr(got)
        + ("" if ok else "  (ждали " + repr(want) + ")"))


def wait_job(job_id, limit=120.0):
    t0 = time.time()
    while time.time() - t0 < limit:
        job = jobs.get(job_id) if hasattr(jobs, "get") else None
        if job is None:
            for j in jobs.list_all(50):
                if j.get("id") == job_id:
                    job = j
                    break
        if job and job.get("status") in ("done", "error", "cancelled"):
            return job
        time.sleep(0.3)
    return {"status": "timeout"}


DOC = ("## О чём запись\n\nОбсуждали бюджет на квартал.\n\n"
       "## Закупки [00:04:12]\n\nРешили перенести тендер.\n\n"
       "## Задачи и договорённости\n\n- Смета к четвергу\n")

NORMAL = "НАЗВАНИЕ: Планёрка\nПАПКА: бюджет квартала\n\n" + DOC


say("=== 1. Обычный ответ: две служебные строки и документ ===")
data = minutes._parse_summary_answer(NORMAL)
check("название взято", data.get("title"), "Планёрка")
check("имя папки взято", data.get("folder_name"), "бюджет квартала")
check("документ без служебных строк", data.get("summary_markdown"), DOC.strip())
check("спасать не пришлось", "_recovered" in data, False)

say("")
say("=== 2. Метки оформлены по-своему ===")
fancy = ("**НАЗВАНИЕ:** Планёрка\n**ПАПКА:** бюджет квартала\n\n" + DOC)
data = minutes._parse_summary_answer(fancy)
check("жирные метки поняты", data.get("title"), "Планёрка")
check("документ не задет", data.get("summary_markdown"), DOC.strip())

tail_labels = DOC + "\nНАЗВАНИЕ: Планёрка\nПАПКА: бюджет квартала\n"
data = minutes._parse_summary_answer(tail_labels)
check("метки в конце тоже находим", data.get("folder_name"), "бюджет квартала")
check("и вырезаем из документа",
      "НАЗВАНИЕ" in data.get("summary_markdown", ""), False)

data = minutes._parse_summary_answer('НАЗВАНИЕ: "Планёрка"\nПАПКА: бюджет\n\n' + DOC)
check("кавычки вокруг названия сняты", data.get("title"), "Планёрка")

only_one = "НАЗВАНИЕ: Планёрка\n\n" + DOC
data = minutes._parse_summary_answer(only_one)
check("одной метки достаточно", data.get("title"), "Планёрка")
check("папки нет — и не выдумана", data.get("folder_name"), None)

say("")
say("=== 3. Запасной путь: строгий JSON ===")
good = json.dumps({"title": "Планёрка", "folder_name": "бюджет квартала",
                   "summary_markdown": DOC}, ensure_ascii=False)
data = minutes._parse_summary_answer(good)
check("название взято", data.get("title"), "Планёрка")
check("документ целый", data.get("summary_markdown"), DOC)
check("отход от формата помечен",
      data.get("_recovered"), "ответ объектом JSON вместо служебных строк")

fenced = "```json\n" + good + "\n```"
check("тройные кавычки сняты",
      minutes._parse_summary_answer(fenced).get("title"), "Планёрка")

wrapped = "Вот саммари:\n\n" + good + "\n\nГотово! Если нужно — оформлю иначе."
data = minutes._parse_summary_answer(wrapped)
check("объект вырезан из болтовни", data.get("title"), "Планёрка")
check("документ не задет", data.get("summary_markdown"), DOC)

say("")
say("=== 4. Настоящие переносы строк внутри значения JSON ===")
broken = ('{\n  "title": "Планёрка",\n  "folder_name": "бюджет квартала",\n'
          '  "summary_markdown": "' + DOC + '"\n}')
check("строгий разбор такое не берёт", minutes._try_json(broken), None)
data = minutes._parse_summary_answer(broken)
check("но мы разобрали", data.get("title"), "Планёрка")
check("документ восстановлен целиком", data.get("summary_markdown"), DOC)
check("спасение помечено", data.get("_recovered"), "переносы строк внутри JSON")

escaped = minutes._escape_raw_controls(good)
check("правильный JSON не изменился", escaped, good)
check("текст вне строк не трогаем",
      minutes._escape_raw_controls('{\n"a": "б"\n}'), '{\n"a": "б"\n}')

say("")
say("=== 5. Неэкранированные кавычки внутри документа ===")
# Починка переносов тут не спасает: лишняя кавычка обрывает строку, и объект
# уже не собрать. Остаётся выбрать поля по именам.
quoted = DOC + '\nЦитата: "сделаем к четвергу".\n'
rough = ('{"title": "Планёрка", "folder_name": "бюджет квартала", '
         '"summary_markdown": "' + quoted + '"}')
data = minutes._parse_summary_answer(rough)
check("название вытащили", data.get("title"), "Планёрка")
check("имя папки вытащили", data.get("folder_name"), "бюджет квартала")
check("документ на месте",
      data.get("summary_markdown", "").startswith("## О чём запись"), True)
check("кавычки внутри уцелели",
      '"сделаем к четвергу"' in data.get("summary_markdown", ""), True)
check("спасение помечено", data.get("_recovered"), "выборка полей по именам")

say("")
say("=== 6. Раскрытие экранированных знаков ===")
check("перенос строки", minutes._unescape("а\\nб"), "а\nб")
check("кавычка", minutes._unescape('он сказал \\"да\\"'), 'он сказал "да"')
check("обратная косая", minutes._unescape("C:\\\\Users"), "C:\\Users")
check("код символа", minutes._unescape("\\u0410"), "А")
check("одинокая косая не ломает", minutes._unescape("100\\"), "100\\")

say("")
say("=== 7. Модель прислала просто Markdown, без служебных строк ===")
# Ровно так ответил живой Claude CLI 12.09.
plain = DOC + ("\n\nПодробности по каждому пункту приводятся ниже.\n\n"
               "## Детали\n\n" + "Обсуждение шло около часа. " * 30)
data = minutes._parse_summary_answer(plain)
check("документ принят",
      data.get("summary_markdown", "").startswith("## О чём запись"), True)
check("названия нет — и не выдумано", data.get("title"), None)
check("спасение помечено",
      data.get("_recovered"), "ответ без служебных строк, принят как документ")

say("")
say("=== 8. Отговорка документом не считается ===")
for bad in ("Не могу выполнить эту задачу.", "", "   ", "Готово!",
            "Извините, стенограмма слишком длинная для одного ответа."):
    try:
        minutes._parse_summary_answer(bad)
        FAIL.append("отговорка принята: %r" % bad[:40])
        say("   ПЛОХО отговорка принята: %r" % bad[:40])
    except RuntimeError as err:
        say("   ok    отказ с понятным текстом: %r" % str(err)[:60])
check("короткий текст не документ", minutes._looks_like_document("Не могу."), False)
check("длинный Markdown — документ", minutes._looks_like_document(plain), True)
check("одни метки без документа не считаются",
      minutes._parse_labelled("НАЗВАНИЕ: Планёрка\nПАПКА: бюджет"), {})

say("")
say("=== 9. Запуск CLI: батник разворачивается в настоящий файл ===")
# Из-за батника до модели доходила только ПЕРВАЯ строка инструкции: cmd.exe
# обрезает многострочный аргумент. Здесь проверяем сам разворот, без запуска.
fake_npm = PROJECT / "data" / "_uploads" / "t37_npm"
bin_dir = fake_npm / "node_modules" / "@anthropic-ai" / "claude-code" / "bin"
bin_dir.mkdir(parents=True, exist_ok=True)
real_exe = bin_dir / "claude.exe"
io.open(real_exe, "w", encoding="utf-8").write("")
wrapper = fake_npm / "claude.cmd"
io.open(wrapper, "w", encoding="utf-8").write(
    '@ECHO off\r\n"%dp0%\\node_modules\\@anthropic-ai\\claude-code\\bin\\claude.exe"   %*\r\n')

check("батник заменён бинарником",
      minutes._unwrap_launcher(str(wrapper)), str(real_exe))
check("файл без расширения тоже",
      minutes._unwrap_launcher(str(fake_npm / "claude")), str(real_exe))
check("настоящий .exe не трогаем",
      minutes._unwrap_launcher(str(real_exe)), str(real_exe))

lonely = fake_npm / "one" / "claude.cmd"
lonely.parent.mkdir(parents=True, exist_ok=True)
io.open(lonely, "w", encoding="utf-8").write("@ECHO off\r\necho нет ссылки на бинарник\r\n")
check("не нашли бинарник — оставили как было",
      minutes._unwrap_launcher(str(lonely)), str(lonely))

found = minutes.resolve_claude_cli()
say("   найденный CLI: %s" % found)
check("на этой машине найден не батник",
      bool(found) and Path(found).suffix.lower() not in (".cmd", ".bat", ".ps1"), True)

env = minutes._cli_env()
check("метка вложенной сессии снята", "CLAUDECODE" in env, False)
check("остальное окружение на месте", "PATH" in env or "Path" in env, True)
shutil.rmtree(fake_npm, ignore_errors=True)

say("")
say("=== 10. Сквозной прогон: ответ доходит до заметки ===")
# Остатки прошлых прогонов, если проверка когда-то упала посередине
for _old in store.list_all():
    if str(_old.get("title") or "").startswith("t37_"):
        store.delete(_old["id"])
tmp_dir = PROJECT / "data" / "_uploads"
tmp_dir.mkdir(parents=True, exist_ok=True)
vtt = tmp_dir / "t37_meeting.vtt"
io.open(vtt, "w", encoding="utf-8", newline="\n").write(
    "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\n"
    "<v Иванов Иван>Добрый день, начнём с бюджета.\n\n"
    "00:00:04.500 --> 00:00:08.000\n"
    "<v Петрова Мария>Смету подготовлю к четвергу.\n")

res = media.submit_file(vtt, "t37_meeting.vtt",
                        {"make_summary": False, "diarize_auto": False,
                         "prefer_transcript": True, "keep_video": False})
rec_id = res["rec_id"]
MADE.append(rec_id)
job = wait_job(res["job_id"])
check("запись готова", job.get("status"), "done")

calls = []
real_engine = minutes._run_engine
answer = NORMAL


def fake_engine(engine, prompt, text, timeout=None, role="strong", handle=None):
    """Движок-подменыш: отвечает заранее заданным текстом."""
    calls.append(role)
    return answer


minutes._run_engine = fake_engine
try:
    out = minutes.generate_video_summary(rec_id)
finally:
    minutes._run_engine = real_engine

check("движок вызван один раз", calls, ["strong"])
check("саммари собрано", out.get("markdown"), DOC.strip())
check("название записи взято", out.get("title"), "Планёрка")
check("имя папки предложено", out.get("folder_name"), "бюджет квартала")
check("файл протокола записан", Path(str(out.get("saved_to") or "")).exists(), True)
check("запись помечена готовой", (store.get(rec_id) or {}).get("has_minutes"), True)
check("название сохранено в запись",
      (store.get(rec_id) or {}).get("summary_title"), "Планёрка")

raw_file = store.rec_dir(rec_id) / "summary_raw.txt"
check("при обычном ответе лишних файлов нет", raw_file.exists(), False)

say("")
say("=== 11. Кривой ответ: разобран, но сырой оставлен на диске ===")
answer = broken
calls.clear()
minutes._run_engine = fake_engine
try:
    out = minutes.generate_video_summary(rec_id)
finally:
    minutes._run_engine = real_engine
check("саммари всё равно собрано", out.get("markdown"), DOC.strip())
check("сырой ответ сохранён", raw_file.exists(), True)
if raw_file.exists():
    check("сохранён именно ответ модели",
          io.open(raw_file, "r", encoding="utf-8").read(), broken)

say("")
say("=== 12. Совсем негодный ответ: ошибка и сохранённый ответ ===")
nonsense = "Не могу."
answer = nonsense
store.update(rec_id, {"has_minutes": False})
try:
    raw_file.unlink(missing_ok=True)
except OSError:
    pass

minutes._run_engine = fake_engine
err_text = ""
try:
    minutes.generate_video_summary(rec_id)
    FAIL.append("негодный ответ прошёл")
    say("   ПЛОХО негодный ответ прошёл")
except RuntimeError as err:
    err_text = str(err)
    say("   ok    отказ: %r" % err_text[:80])
finally:
    minutes._run_engine = real_engine

check("в ошибке сказано про сохранённый ответ", "сохранён" in err_text, True)
check("ответ и правда лежит", raw_file.exists(), True)
if raw_file.exists():
    check("в файле — ответ модели",
          io.open(raw_file, "r", encoding="utf-8").read(), nonsense)

say("")
say("=== 13. Уборка ===")
for rid in MADE:
    m = store.get(rid) or {}
    folder = str(m.get("assets_folder") or "")
    if folder:
        shutil.rmtree(store.assets_dir(rid, folder), ignore_errors=True)
    store.delete(rid)
check("тестовые записи убраны", [r for r in MADE if store.get(r) is not None], [])
try:
    vtt.unlink(missing_ok=True)
except OSError:
    pass

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t37_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
