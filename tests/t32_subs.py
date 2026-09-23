# -*- coding: utf-8 -*-
"""Проверка 32: разбор готовых субтитров (hagen/subs.py).

Почему проверок так много. Субтитры ломаются МОЛЧА: файл прочитался, реплик
ноль или текст поехал в кракозябры — и никто не заметит неделю. Здесь всё на
синтетических строках, без сети и без живого сервиса.
"""
import io
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from harness import LINES, FAIL, say  # noqa: E402

from hagen import store, subs

PASSED = 0
FAILED = 0
TMP = Path(tempfile.mkdtemp(prefix="t32_subs_"))


def check(name, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        say("   ок       %s" % name)
    else:
        FAILED += 1
        say("   ПРОВАЛ   %s  %s" % (name, detail))


def put(name, text, encoding="utf-8"):
    """Положить файл субтитров во временную папку."""
    p = TMP / name
    p.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    return p


class Handle:
    """Подделка jobs.JobHandle: запоминает прогресс, умеет притвориться отменённой."""

    def __init__(self, cancelled=False):
        self.calls = []
        self.logs = []
        self._cancelled = cancelled

    def progress(self, value, note=""):
        self.calls.append((float(value), note))

    def log(self, msg):
        self.logs.append(str(msg))

    @property
    def cancelled(self):
        return self._cancelled


def raises(fn, *args, **kwargs):
    """Вернуть текст исключения или None, если его не было."""
    try:
        fn(*args, **kwargs)
    except Exception as err:
        return str(err)
    return None


# ---------------------------------------------------------------- 1. имена файлов
say("=== имена файлов ===")
check("is_transcript_name('source.vtt')", subs.is_transcript_name("source.vtt") is True)
check("is_transcript_name('ЗАПИСЬ.SRT')", subs.is_transcript_name("ЗАПИСЬ.SRT") is True)
check("is_transcript_name('video.mp4')", subs.is_transcript_name("video.mp4") is False)
check("is_transcript_name(ссылка с tempauth)",
      subs.is_transcript_name("https://host/sites/a/source.vtt?tempauth=SECRET&x=1") is True)
check("is_transcript_name('') и None",
      subs.is_transcript_name("") is False and subs.is_transcript_name(None) is False)
check("TRANSCRIPT_EXTS — frozenset",
      isinstance(subs.TRANSCRIPT_EXTS, frozenset) and set(subs.TRANSCRIPT_EXTS) == {".vtt", ".srt"},
      repr(subs.TRANSCRIPT_EXTS))

# ---------------------------------------------------------------- 2. таймкоды
say("")
say("=== таймкоды ===")
check("ЧЧ:ММ:СС.мс", subs.tc_to_sec("00:01:02.500") == 62.5, subs.tc_to_sec("00:01:02.500"))
check("ММ:СС.мс (VTT без часов)", subs.tc_to_sec("01:02.500") == 62.5, subs.tc_to_sec("01:02.500"))
check("SRT с запятой", subs.tc_to_sec("00:00:01,250") == 1.25, subs.tc_to_sec("00:00:01,250"))
check("час с минутами", subs.tc_to_sec("01:00:00.000") == 3600.0)

MIXED = (
    "WEBVTT\n\n"
    "00:01:02.500 --> 00:01:05.000\nчас-минуты-секунды\n\n"
    "05:00.000 --> 05:02.000\nтолько минуты\n"
)
cues = subs.parse_cues(MIXED)
check("оба формата времени в одном файле", len(cues) == 2, len(cues))
check("старты посчитаны верно",
      cues and cues[0]["start"] == 62.5 and cues[1]["start"] == 300.0,
      [c["start"] for c in cues])

# ---------------------------------------------------------------- 3. обычный VTT
say("")
say("=== обычный VTT ===")
PLAIN = (
    "WEBVTT\n"
    "\n"
    "NOTE это примечание, в стенограмму идти не должно\n"
    "\n"
    "1\n"
    "00:00:01.000 --> 00:00:03.000\n"
    "<v Иванов Иван>первая реплика</v>\n"
    "\n"
    "00:00:10.000 --> 00:00:12.000\n"
    "<v Петров Пётр>вторая реплика</v>\n"
    "\n"
    "00:00:20.000 --> 00:00:22.000\n"
    "<v Иванов Иван>Смит &amp; сыновья</v>\n"
    "\n"
    "00:00:30.000 --> 00:00:32.000\n"
    "реплика без говорящего\n"
)
plain_path = put("plain.vtt", PLAIN)
plain_cues = subs.parse_cues(PLAIN)
check("реплик разобрано 4 (NOTE и номер отброшены)", len(plain_cues) == 4,
      [c["text"] for c in plain_cues])
check("имя из тега <v Фамилия Имя>",
      plain_cues and plain_cues[0]["speaker"] == "Иванов Иван", plain_cues[:1])
check("текст без разметки", plain_cues and plain_cues[0]["text"] == "первая реплика",
      plain_cues[:1])
check("html-мнемоника раскрыта (&amp; -> &)",
      plain_cues[2]["text"] == "Смит & сыновья", plain_cues[2]["text"])
check("<v.loud Имя> тоже понимается",
      subs.parse_cues("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n"
                      "<v.loud Пётр>кричит</v>\n")[0]["speaker"] == "Пётр")

# ---------------------------------------------------------------- 4. BOM
say("")
say("=== VTT с BOM (так отдаёт Teams) ===")
bom_path = put("bom.vtt", b"\xef\xbb\xbf" + (
    "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nреплика с BOM\n").encode("utf-8"))
bom_cues = subs.load_cues(bom_path)
check("файл с BOM разобран", len(bom_cues) == 1, len(bom_cues))
check("BOM не попал в текст",
      bom_cues and bom_cues[0]["text"] == "реплика с BOM", repr(bom_cues[:1]))
bom_inline = subs.parse_cues(
    chr(0xFEFF) + "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n" + chr(0xFEFF) + "текст\n")
check("BOM внутри строки (не из файла) тоже вырезан",
      len(bom_inline) == 1 and bom_inline[0]["text"] == "текст", repr(bom_inline))

# ---------------------------------------------------------------- 5. cp1251
say("")
say("=== SRT в cp1251 ===")
SRT_1251 = (
    "1\n"
    "00:00:01,000 --> 00:00:04,000\n"
    "Здравствуйте, коллеги, начнём совещание\n"
    "\n"
    "2\n"
    "00:00:10,000 --> 00:00:14,000\n"
    "Сегодня обсуждаем бюджет\n"
)
srt_path = put("old.srt", SRT_1251, encoding="cp1251")
srt_cues = subs.load_cues(srt_path)
check("cp1251 прочитан, реплик 2", len(srt_cues) == 2, len(srt_cues))
check("русский текст не поехал",
      srt_cues and srt_cues[0]["text"] == "Здравствуйте, коллеги, начнём совещание",
      repr(srt_cues[:1]))
check("SRT-таймкод с запятой",
      srt_cues and srt_cues[0]["start"] == 1.0 and srt_cues[0]["end"] == 4.0,
      srt_cues[:1])

u16_cues = subs.load_cues(put("utf16.srt", SRT_1251, encoding="utf-16"))
check("UTF-16 не превращается в кракозябры",
      len(u16_cues) == 2 and u16_cues[0]["text"] == "Здравствуйте, коллеги, начнём совещание",
      repr(u16_cues[:1]))

# ---------------------------------------------------------------- 6. имя из строки
say("")
say("=== имя говорящего из строки «Имя: текст» ===")
LINE_NAMES = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:03.000\nИванов Иван: начнём с бюджета\n\n"
    "00:00:10.000 --> 00:00:12.000\nАнна: у меня есть цифры\n\n"
    "00:00:20.000 --> 00:00:22.000\nАнна: пришлю после встречи\n\n"
    "00:00:30.000 --> 00:00:32.000\nИтак: поехали работать\n\n"
    "00:00:40.000 --> 00:00:42.000\nМузыка: играет\n\n"
    "00:00:50.000 --> 00:00:52.000\nМузыка: играет громче\n"
)
ln = subs.parse_cues(LINE_NAMES)
check("имя из двух слов принято сразу",
      ln[0]["speaker"] == "Иванов Иван" and ln[0]["text"] == "начнём с бюджета", ln[0])
check("одиночное имя принято, потому что повторилось",
      ln[1]["speaker"] == "Анна" and ln[2]["speaker"] == "Анна", ln[1:3])
check("приставка «Имя:» из текста убрана",
      ln[1]["text"] == "у меня есть цифры", ln[1]["text"])
check("«Итак:» говорящим не стало",
      ln[3]["speaker"] == "" and ln[3]["text"] == "Итак: поехали работать", ln[3])
check("«Музыка:» отсеяна стоп-словом даже при повторе",
      ln[4]["speaker"] == "" and ln[5]["speaker"] == "", ln[4:6])

# ---------------------------------------------------------------- 7. ключи говорящих
say("")
say("=== сегменты и ключи говорящих ===")
segs = subs.load_segments(plain_path)
check("сегментов 4", len(segs) == 4, len(segs))
check("дорожка «file» у всех",
      all(s["track"] == store.TRACK_FILE for s in segs), [s["track"] for s in segs])
check("набор полей совпадает с store.make_segment",
      set(segs[0].keys()) == set(store.make_segment(store.TRACK_FILE, 0, 1, "x").keys()),
      sorted(segs[0].keys()))
check("один человек — один ключ на весь файл",
      segs[0]["speaker_key"] == segs[2]["speaker_key"] == "sub1",
      [s["speaker_key"] for s in segs])
check("второй говорящий получил sub2", segs[1]["speaker_key"] == "sub2",
      segs[1]["speaker_key"])
check("реплика без имени ушла в «Участник»/far",
      segs[3]["speaker"] == store.SPEAKER_FAR and segs[3]["speaker_key"] == "far",
      (segs[3]["speaker"], segs[3]["speaker_key"]))
check("ИМЯ НЕ ВКЛЕЕНО В ТЕКСТ (иначе minutes подставит его второй раз)",
      all(not s["text"].startswith(str(s["speaker"]) + ":") for s in segs),
      [s["text"] for s in segs])

spk = subs.speakers_from_segments(segs)
check("speakers_from_segments вернул два ключа",
      set(spk.keys()) == {"sub1", "sub2"}, sorted(spk.keys()))
check("имена на месте",
      spk.get("sub1", {}).get("name") == "Иванов Иван"
      and spk.get("sub2", {}).get("name") == "Петров Пётр", spk)
check("заглушка «Участник» в список говорящих не попала", "far" not in spk, sorted(spk))

# ---------------------------------------------------------------- 8. ползущие субтитры
say("")
say("=== ползущие автосубтитры YouTube ===")
ROLLING = (
    "WEBVTT\n\n"
    "00:00:00.000 --> 00:00:02.000\nпривет всем\n\n"
    "00:00:02.000 --> 00:00:04.000\nпривет <c.colorE5E5E5>всем</c> кто пришёл\n\n"
    "00:00:04.000 --> 00:00:06.000\nкто пришёл <00:00:05.000><c>сегодня</c>\n\n"
    "00:00:06.000 --> 00:00:08.000\nсегодня на встречу\n\n"
    "00:00:08.000 --> 00:00:10.000\nна встречу\n"
)
NOT_ROLLING = ROLLING.replace("<c.colorE5E5E5>", "").replace("</c>", "")
NOT_ROLLING = NOT_ROLLING.replace("<00:00:05.000><c>", "")
check("looks_rolling видит караоке-теги", subs.looks_rolling(ROLLING) is True)
check("looks_rolling не срабатывает на обычном VTT",
      subs.looks_rolling(PLAIN) is False and subs.looks_rolling(NOT_ROLLING) is False)

roll_path = put("rolling.vtt", ROLLING)
roll_segs = subs.load_segments(roll_path)
check("5 наезжающих реплик схлопнулись в одну", len(roll_segs) == 1, len(roll_segs))
check("текст собран без повторов",
      roll_segs and roll_segs[0]["text"] == "привет всем кто пришёл сегодня на встречу",
      roll_segs[0]["text"] if roll_segs else None)
check("конец последней реплики сохранён",
      roll_segs and roll_segs[0]["end"] == 10.0, roll_segs[0]["end"] if roll_segs else None)

flat_path = put("not_rolling.vtt", NOT_ROLLING)
flat_segs = subs.load_segments(flat_path)
check("без караоке-тегов схлопывания нет (проверка, что оно вообще работает)",
      len(flat_segs[0]["text"]) > len(roll_segs[0]["text"]),
      (len(flat_segs[0]["text"]), len(roll_segs[0]["text"])))

# ---------------------------------------------------------------- 9. склейка
say("")
say("=== склейка коротких реплик ===")
GLUE = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:02.000\n<v Анна>раз</v>\n\n"
    "00:00:02.500 --> 00:00:03.500\n<v Анна>два</v>\n\n"
    "00:00:04.000 --> 00:00:05.000\n<v Анна>три</v>\n\n"
    "00:00:05.500 --> 00:00:06.500\n<v Борис>четыре</v>\n\n"
    "00:00:30.000 --> 00:00:31.000\n<v Борис>пять</v>\n"
)
glue = subs.load_segments(put("glue.vtt", GLUE))
check("три реплики одного говорящего склеились",
      glue[0]["text"] == "раз два три" and glue[0]["speaker"] == "Анна", glue[0])
check("склеенный кусок растянут по времени",
      glue[0]["start"] == 1.0 and glue[0]["end"] == 5.0, (glue[0]["start"], glue[0]["end"]))
check("разные говорящие не склеиваются", len(glue) == 3, [s["text"] for s in glue])
check("пауза больше 2 секунд не склеивается",
      glue[1]["text"] == "четыре" and glue[2]["text"] == "пять", [s["text"] for s in glue])

# ---------------------------------------------------------------- 10. кривые файлы
say("")
say("=== кривые и пустые файлы ===")
BROKEN_TC = (
    "WEBVTT\n\n"
    "00:00:1x.000 --> 00:00:05.000\nреплика с битым таймингом\n\n"
    "00:00:06.000 --> 00:00:08.000\nа эта нормальная\n"
)
bt = subs.parse_cues(BROKEN_TC)
check("битый тайминг пропущен, файл не потерян",
      len(bt) == 1 and bt[0]["text"] == "а эта нормальная", [c["text"] for c in bt])

BACKWARDS = "WEBVTT\n\n00:00:10.000 --> 00:00:05.000\nзадом наперёд\n"
bw = subs.parse_cues(BACKWARDS)
check("конец раньше начала подтянут к началу",
      bw[0]["start"] == 10.0 and bw[0]["end"] == 10.0, bw[0])

empty_path = put("empty.vtt", "")
err = raises(subs.load_segments, empty_path)
check("пустой файл — понятная ошибка", err is not None and "empty.vtt" in err, err)
check("probe на пустом файле не падает",
      subs.probe(empty_path) == {"cues": 0, "speakers": [], "duration_s": 0.0},
      subs.probe(empty_path))

no_cues_path = put("notes.vtt", "WEBVTT\n\nПросто текст без единого таймкода.\nИ ещё строка.\n")
err2 = raises(subs.load_segments, no_cues_path)
check("файл без реплик — понятная ошибка",
      err2 is not None and "notes.vtt" in err2 and "таймкод" in err2, err2)

err3 = raises(subs.load_segments, TMP / "нет-такого-файла.vtt")
check("пропавший файл — понятная ошибка",
      err3 is not None and "не найден" in err3, err3)

ARROW = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:03.000\nперенесли с 10:00 --> 11:30, все в курсе\n\n"
    "00:00:20.000 --> 00:00:22.000\nвторая реплика\n"
)
ar = subs.parse_cues(ARROW)
check("«-->» внутри текста не рвёт реплику",
      len(ar) == 2 and ar[0]["text"] == "перенесли с 10:00 --> 11:30, все в курсе",
      [c["text"] for c in ar])
check("реплика после текста со стрелкой прочитана",
      len(ar) == 2 and ar[1]["text"] == "вторая реплика", [c["text"] for c in ar])

# ---------------------------------------------------------------- 11. probe
say("")
say("=== probe ===")
pr = subs.probe(plain_path)
check("probe: ключи на месте",
      set(pr.keys()) == {"cues", "speakers", "duration_s"}, sorted(pr.keys()))
check("probe: реплик 4", pr["cues"] == 4, pr["cues"])
check("probe: говорящие по порядку появления",
      pr["speakers"] == ["Иванов Иван", "Петров Пётр"], pr["speakers"])
check("probe: длительность по последней реплике", pr["duration_s"] == 32.0, pr["duration_s"])

# ---------------------------------------------------------------- 12. прогресс и отмена
say("")
say("=== прогресс и отмена ===")
h = Handle()
subs.load_segments(plain_path, h)
check("прогресс сообщается", len(h.calls) >= 2, h.calls)
check("доли прогресса в пределах 0..1",
      all(0.0 <= v <= 1.0 for v, _ in h.calls), h.calls)

err4 = raises(subs.load_segments, plain_path, Handle(cancelled=True))
check("отмена прерывает разбор", err4 is not None and "отменено" in err4, err4)

# ---------------------------------------------------------------- итог
say("")
say("Проверок пройдено: %d, провалено: %d" % (PASSED, FAILED))
shutil.rmtree(TMP, ignore_errors=True)
io.open(PROJECT / "tests" / "t32_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(0 if FAILED == 0 else 1)
