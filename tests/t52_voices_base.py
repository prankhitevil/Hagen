# -*- coding: utf-8 -*-
"""Проверка 52: база голосов версии 2 — имена, варианты, образцы, дубли.

Решения 13.09.2026:
  * порядок слов в имени не важен — это один человек автоматически;
  * инициалы, латиница, отчество, опечатки — только вопрос «это он?»;
  * у образца голоса записано происхождение, ошибку можно отменить;
  * «общее устройство» (переговорка) голос не учит;
  * имя по голосу встаёт само, только если лучший кандидат заметно впереди;
  * перед объединением и удалением людей — копия базы.

База на время проверки — временный файл: настоящая data/voices.json не трогается.
"""
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from harness import LINES, FAIL, say, check, finish  # noqa: E402


from hagen import config, store, voices  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t52_"))
REAL_PATH = voices.VOICES_PATH
voices.VOICES_PATH = TMP / "voices.json"
voices.reload()
OVR = {}
_real_get = config.get
config.get = lambda k, d=None: OVR[k] if k in OVR else _real_get(k, d)

rng = np.random.default_rng(52)


def voice(base=None, noise=0.0):
    v = rng.normal(size=256) if base is None else base + rng.normal(size=256) * noise
    return (v / np.linalg.norm(v) * 3.2).tolist()


try:
    say("=== 1. Ключ имени ===")
    check("порядок слов не важен", voices.name_key("Петров Иван") == voices.name_key("иван  ПЕТРОВ"))
    check("ё = е, приписка в скобках отброшена",
          voices.name_key("Семён Фёдоров (ООО Ромашка)") == voices.name_key("Федоров Семен"))
    check("разные люди — разные ключи", voices.name_key("Иван Петров") != voices.name_key("Иван Сидоров"))

    say("")
    say("=== 2. Похожие имена — только вопрос ===")
    cases = [
        ("Иван Петров", "Petrov Ivan", "то же имя другими буквами"),
        ("Юлия Петрова", "Petrova Yulia", "то же имя другими буквами"),
        ("Иван Петров", "Петров И.", "инициалы"),
        ("Иван Петров", "Петров Иван Сергеевич", "лишнее слово (например, отчество)"),
        ("Александр Константинопольский", "Александр Констатинопольский", "похоже написано"),
    ]
    for a, b, reason in cases:
        got = voices._similar_reason(a, b)
        check("«%s» ~ «%s» — %s" % (a, b, reason), got == reason, got)
    check("порядок слов — не «похожее», а то же имя", voices._similar_reason("Иван Петров", "Петров Иван") is None)
    check("разные люди не похожи", voices._similar_reason("Иван Петров", "Мария Сидорова") is None)

    say("")
    say("=== 3. Люди и варианты имени ===")
    ivan = voices.create_person("Иван Петров")
    check("заведён", ivan["name"] == "Иван Петров" and not ivan["has_voice"])
    try:
        voices.create_person("Петров Иван")
        check("второй с тем же именем (другой порядок) не заводится", False)
    except voices.NameTaken as err:
        check("второй с тем же именем (другой порядок) не заводится", err.other["id"] == ivan["id"])
    check("resolve по переставленному имени", (voices.resolve("петров иван") or {}).get("id") == ivan["id"])
    voices.add_alias(ivan["id"], "Petrov Ivan")
    check("resolve по варианту имени", (voices.resolve("Ivan Petrov") or {}).get("id") == ivan["id"])
    twin = voices.create_person("Иван Петров", namesake=True)
    check("тёзка получает номер", twin["name"] == "Иван Петров 2" and twin["id"] != ivan["id"], twin["name"])
    room = voices.create_person("Переговорная 3")
    check("переговорка заводится общим устройством", room["kind"] == voices.KIND_SHARED)
    maria = voices.create_person("Мария Сидорова")

    say("")
    say("=== 4. Поиск для окна имени ===")
    names = [p["name"] for p in voices.search("пет ив")]
    check("«пет ив» находит Ивана Петрова", "Иван Петров" in names, names)
    names = [p["name"] for p in voices.search("сидор")]
    check("по части фамилии", names == ["Мария Сидорова"], names)
    found = voices.search("petr")
    check("латиницей находит кириллицу", any(p["id"] == ivan["id"] for p in found), [p["name"] for p in found])
    found = voices.search("ivan")
    hit = next((p for p in found if p["id"] == ivan["id"]), {})
    check("совпадение по варианту имени показано", hit.get("matched") in ("Petrov Ivan", None), hit)

    say("")
    say("=== 5. Образцы голоса и происхождение ===")
    v_ivan = voice()
    v_maria = voice()
    r = voices.add_sample(None, v_ivan, person_id=ivan["id"], rec_id="rec1", speaker_key="SPEAKER_00")
    check("образец добавлен", r["added"] and r["count"] == 1)
    r = voices.add_sample(None, voice(np.array(v_ivan), 0.02), person_id=ivan["id"],
                          rec_id="rec1", speaker_key="SPEAKER_00")
    check("тот же говорящий той же записи не дублируется", r["count"] == 1, r["count"])
    voices.add_sample(None, v_maria, person_id=maria["id"], rec_id="rec1", speaker_key="SPEAKER_01")
    r = voices.add_sample(None, voice(), person_id=room["id"], rec_id="rec1", speaker_key="SPEAKER_02")
    check("голос общего устройства не сохраняется", r["added"] is False and r["count"] == 0)
    check("видно, у кого образец говорящего", [p["name"] for p in voices.samples_from("rec1", "SPEAKER_01")]
          == ["Мария Сидорова"])
    moved = voices.remove_samples_from("rec1", "SPEAKER_01", keep_person=ivan["id"])
    check("образец убирается ровно у того, кому попал", moved == ["Мария Сидорова"]
          and not voices.get_person(maria["id"])["has_voice"], moved)
    voices.add_sample(None, v_maria, person_id=maria["id"], rec_id="rec1", speaker_key="SPEAKER_01")

    say("")
    say("=== 6. Сравнение голосов и отрыв от второго ===")
    OVR.update({"voice_match_threshold": 0.7, "voice_suggest_threshold": 0.45, "voice_match_margin": 0.1})
    near = voice(np.array(v_ivan), 0.05)
    m = voices.match(near)
    check("свой голос узнан уверенно", m and m["person_id"] == ivan["id"] and m["confident"], m)
    # второй человек с почти тем же голосом — уверенности быть не должно
    voices.add_sample(None, voice(np.array(v_ivan), 0.08), person_id=twin["id"], rec_id="rec2",
                      speaker_key="SPEAKER_00")
    m = voices.match(near)
    check("два похожих голоса — только подсказка", m and not m["confident"], m)
    scores = voices.voice_scores(near)
    check("общие устройства в сравнении не участвуют", all(s["person_id"] != room["id"] for s in scores))
    found = voices.search("", embedding=near)
    check("без текста сверху похожие по голосу", found and found[0]["id"] in (ivan["id"], twin["id"])
          and found[0]["voice"] and found[0]["voice"] > 0.9, [(p["name"], p["voice"]) for p in found[:3]])

    say("")
    say("=== 7. Переименование и объединение ===")
    try:
        voices.rename_person(maria["id"], "Петров Иван")
        check("переименование в чужое имя — отказ, а не молчаливое слияние", False)
    except voices.NameTaken as err:
        check("переименование в чужое имя — отказ, а не молчаливое слияние", err.other["id"] == ivan["id"])
    r = voices.rename_person(maria["id"], "Мария Сидорова-Кац")
    check("старое имя стало вариантом", "Мария Сидорова" in r["aliases"], r["aliases"])
    dupes = voices.possible_duplicates()
    check("«Иван Петров» и «Иван Петров 2» — возможные дубли",
          any({d["a"]["id"], d["b"]["id"]} == {ivan["id"], twin["id"]} for d in dupes),
          [(d["a"]["name"], d["b"]["name"], d["reason"]) for d in dupes])
    voices.mark_not_same(ivan["id"], twin["id"])
    check("«разные люди» из дублей пропадают",
          not any({d["a"]["id"], d["b"]["id"]} == {ivan["id"], twin["id"]} for d in voices.possible_duplicates()))
    petrov_i = voices.create_person("Петров И.")
    voices.add_sample(None, voice(np.array(v_ivan), 0.05), person_id=petrov_i["id"], rec_id="rec3",
                      speaker_key="sub1", origin="teams")
    before = len(list(voices.backups_dir().glob("*.json")))
    merged = voices.merge_people(petrov_i["id"], ivan["id"])
    check("объединение: образцы и имя-вариант перешли", merged["count"] == 2 and "Петров И." in merged["aliases"],
          merged)
    check("перед объединением сделана копия базы", len(list(voices.backups_dir().glob("*.json"))) == before + 1)
    det = voices.person_details(ivan["id"])
    check("в карточке видно происхождение образцов",
          [s["origin"] for s in det["samples"]] == ["manual", "teams"], det["samples"])

    say("")
    say("=== 8. Общее устройство и подозрительные образцы ===")
    for i in range(3):
        voices.add_sample(None, voice(np.array(v_maria), 0.05), person_id=maria["id"], rec_id="r%d" % i,
                          speaker_key="S")
    voices.add_sample(None, voice(), person_id=maria["id"], rec_id="odd", speaker_key="S")
    det = voices.person_details(maria["id"])
    odd = [s["rec_id"] for s in det["samples"] if s["outlier"]]
    check("чужой голос среди образцов помечен", odd == ["odd"], odd)
    r = voices.set_kind(maria["id"], voices.KIND_SHARED)
    check("отметка «общее устройство» стирает смешанный голос", r["kind"] == "shared" and not r["has_voice"])

    say("")
    say("=== 9. Владелец записи ===")
    # Имя нарочно не из тех, что заведены выше: иначе владелец столкнулся бы с
    # уже существующим человеком и проверка ловила бы не то, что проверяет.
    OVR["owner_name"] = "Орлов С."
    owner = voices.owner_person()
    check("владелец заведён под своим именем", owner["owner"] and owner["name"] == "Орлов С.", owner)
    check("владелец не попадает в обычный поиск", all(not p["owner"] for p in voices.search("")))
    check("и не подставляется по голосу другим записям",
          all(s["person_id"] != owner["id"] for s in voices.voice_scores(voice())))
    OVR["owner_name"] = "Сергей Орлов"
    check("смена имени владельца подхватывается", voices.owner_person()["name"] == "Сергей Орлов")

    say("")
    say("=== 9б. Очистить базу и вернуть из копии ===")
    before = len(voices.list_people())
    res = voices.clear_all()
    check("база очищена", voices.list_people() == [] and res["removed"] == before, res)
    backups = voices.list_backups()
    check("копия перед очисткой видна в списке", backups and backups[0]["people"] == before, backups[:2])
    res = voices.restore_backup(backups[0]["name"])
    check("база вернулась из копии", len(voices.list_people()) == before and res["people"] == before, res)
    check("текущая база перед возвратом тоже скопирована",
          any("before-restore" in b["name"] for b in voices.list_backups()))
    try:
        voices.restore_backup("..\\voices.json")
        check("чужой путь вместо копии — отказ", False)
    except ValueError:
        check("чужой путь вместо копии — отказ", True)

    say("")
    say("=== 10. Переход со старой базы ===")
    old = {"version": 1, "people": {"ivan-petrov-abc123": {
        "name": "Иван Петров", "samples": [voice()], "centroid": [], "count": 1,
        "created_at": "2026-09-12T10:00:00+03:00", "updated_at": "2026-09-12T10:00:00+03:00"}}}
    voices.VOICES_PATH = TMP / "old" / "voices.json"
    voices.VOICES_PATH.parent.mkdir()
    voices.VOICES_PATH.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    voices.reload()
    people = voices.list_people()
    check("люди из старой базы на месте", [p["name"] for p in people] == ["Иван Петров"] and people[0]["has_voice"])
    saved = json.loads(voices.VOICES_PATH.read_text(encoding="utf-8"))
    check("файл переписан в версию 2", saved.get("version") == 2
          and saved["people"]["ivan-petrov-abc123"]["samples"][0]["origin"] == "legacy")
    check("старый файл сохранён копией", len(list(voices.backups_dir().glob("*v1.json"))) == 1)
    voices.reload()
    voices.reload()
    check("копия не плодится при повторном чтении", len(list(voices.backups_dir().glob("*.json"))) == 1)
finally:
    config.get = _real_get
    # Настоящую базу НЕ перечитываем: чтение переводит её в новый формат, а
    # запущенная старая версия программы его не поймёт. Переход — при перезапуске.
    voices.VOICES_PATH = REAL_PATH
    voices._cache, voices._cache_stamp = None, None
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t52"))
