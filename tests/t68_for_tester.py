# -*- coding: utf-8 -*-
"""Проверка 68: сборка для постороннего человека, --for-tester (16.09).

Обычная сборка кладёт в архив настройки с ключами, папку .git со всей историей
разработки и личные поля — имя, путь к сейфу Obsidian, свои
замены с названиями компаний. Отдавать такой архив постороннему нельзя.

Решение 16.09: ключ --for-tester. Он включает --no-keys и вдобавок
убирает .git, чистит личное, выключает автозапуск и кладёт «Условия передачи»
вместо лицензии MIT.

Что проверяем:
  1. --for-tester сам включает --no-keys;
  2. из настроек уходят секреты и устройства этого компьютера;
  3. уходит личное: имя, путь к сейфу, замены, инструкции, папки снимков;
  4. автозапуск выключен, а нужные для работы настройки (модели, пороги)
     остаются;
  5. обычная сборка личное НЕ трогает — режим не включается сам;
  6. .git и .gitignore не копируются только у --for-tester;
  7. «Условия передачи»: кому, версия, дата, что можно и чего нельзя, про
     токен HuggingFace и про то, что исходники видны;
  8. номер сборки читается из .git без запуска git;
  9. в «КАК УСТАНОВИТЬ» сказано про токен HuggingFace.

Настоящий settings.json не читается: подставляется временный (правило проверок).
Архив не собирается — это часы работы и гигабайты.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t68_for_tester.py
"""
import io
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

LINES = []
FAIL = []


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


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail)[:300] if detail != "" else ""))


from tools import make_portable as mp  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="t68_"))
# Настройки как настоящие — но свои, настоящий файл проверка не открывает.
MINE = {
    "hf_token": "hf_ТОКЕН_ВЛАДЕЛЬЦА", "api_keys": {"anthropic": "sk-ключ"},
    "yt_proxy": "http://логин:пароль@proxy:3128",
    "mic_device_index": 3, "far_device_index": 5, "mic_device_name": "Jabra",
    "vault_confirmed": True,
    "owner_name": "Иван Петров",
    "vault_path": "C:\\Users\\tester\\Documents\\Obsidian",
    "dictate_replacements": [{"from": "вайзэдвайс", "to": "WiseAdvice"}],
    "prompt_overrides": {"protocol": "Наши внутренние правила протокола"},
    "screenshot_folders": ["C:\\Users\\tester\\Снимки"],
    "assets_dir": "D:\\Видео",
    "autostart_windows": True,
    "hidden_categories": ["Личное"],
    "default_category": "Звонки",
    # то, что должно остаться: без этого программа работает хуже
    "diarize_window_step_s": 2.0, "voice_match_threshold": 0.7, "ui_theme": "light",
}
SRC = TMP / "settings.json"
io.open(SRC, "w", encoding="utf-8").write(json.dumps(MINE, ensure_ascii=False))


def built(tester, keys=False):
    dst = TMP / ("tester" if tester else "plain")
    dst.mkdir(exist_ok=True)
    mp.settings_for(dst, keys=keys, tester=tester, src=SRC)
    return json.load(io.open(dst / "settings.json", encoding="utf-8"))


try:
    say("=== 1. Ключи командной строки ===")
    ap_src = io.open(PROJECT / "tools" / "make_portable.py", encoding="utf-8").read()
    check("ключ --for-tester есть", '"--for-tester"' in ap_src)
    check("--for-tester включает --no-keys",
          "if args.for_tester:" in ap_src and "args.no_keys = True" in ap_src)
    check("можно указать, кому передаём", '"--to"' in ap_src)

    say("")
    say("=== 2. Секреты и устройства ===")
    data = built(tester=True)
    for k in mp.SECRET_KEYS:
        check("секрет «%s» не уехал" % k, k not in data)
    for k in mp.MACHINE_KEYS:
        check("устройство «%s» не уехало" % k, k not in data or k == "vault_confirmed")
    check("vault_confirmed сброшен", data.get("vault_confirmed") is False, data.get("vault_confirmed"))

    say("")
    say("=== 3. Личное ===")
    check("имя владельца заменено", data["owner_name"] == "Я", data["owner_name"])
    check("путь к сейфу пуст — установщик спросит свой", data["vault_path"] == "", data["vault_path"])
    check("свои замены убраны (в них названия компаний)", data["dictate_replacements"] == [],
          data["dictate_replacements"])
    check("инструкции документов убраны", data["prompt_overrides"] == {}, data["prompt_overrides"])
    check("папки снимков убраны", data["screenshot_folders"] == [], data["screenshot_folders"])
    check("папка видео убрана", data["assets_dir"] == "", data["assets_dir"])
    check("скрытые категории убраны", data["hidden_categories"] == [], data["hidden_categories"])
    check("категория по умолчанию — обычная", data["default_category"] == "Встречи",
          data["default_category"])
    check("автозапуск не прописывается чужому компьютеру",
          data["autostart_windows"] is False, data["autostart_windows"])

    say("")
    say("=== 4. Нужное остаётся ===")
    check("настройки разметки на месте", data["diarize_window_step_s"] == 2.0)
    check("пороги голосов на месте", data["voice_match_threshold"] == 0.7)
    check("тема оформления на месте", data["ui_theme"] == "light")

    say("")
    say("=== 5. Обычная сборка личное не трогает ===")
    plain = built(tester=False, keys=True)
    check("имя владельца остаётся", plain["owner_name"] == "Иван Петров", plain["owner_name"])
    check("путь к сейфу остаётся", plain["vault_path"] == MINE["vault_path"])
    check("ключи остаются, раз просили с ключами", plain.get("hf_token") == MINE["hf_token"])
    check("устройства этого компьютера всё равно убраны",
          "mic_device_index" not in plain and "mic_device_name" not in plain)
    plain_nokeys = json.loads(json.dumps(plain))
    check("а с --no-keys — уходят и ключи", "hf_token" not in built(tester=False, keys=False),
          list(built(tester=False, keys=False))[:3])

    say("")
    say("=== 6. Что не копируется ===")
    check(".git и .gitignore есть в обычном списке",
          ".git" in mp.TOP_DIRS and ".gitignore" in mp.TOP_FILES)
    check("у --for-tester .git не копируется",
          'd == ".git"' in ap_src and "args.for_tester" in ap_src)
    check("у --for-tester лицензия заменяется",
          '"LICENSE", ".gitignore"' in ap_src)

    say("")
    say("=== 6б. Тяжёлое в сборку не кладём ===")
    from pathlib import Path as _P

    lean = mp.ignore_for(_P("."), lean=True)
    fat = mp.ignore_for(_P("."), lean=False)
    gig = str(_P("models") / "gigaam")
    names = ["v3_e2e_rnnt.ckpt", "v3_e2e_ctc.ckpt", "v3_e2e_rnnt_tokenizer.model", "прочее.txt"]
    check("точная и запасная модели пропускаются",
          lean(gig, names) == {"v3_e2e_rnnt.ckpt", "v3_e2e_ctc.ckpt", "v3_e2e_rnnt_tokenizer.model"},
          sorted(lean(gig, names)))
    check("посторонний файл не трогаем", "прочее.txt" not in lean(gig, names))
    check("английская модель пропускается",
          "onnx-asr" in lean(str(_P("models")), ["onnx", "onnx-asr", "hf"]),
          sorted(lean(str(_P("models")), ["onnx", "onnx-asr", "hf"])))
    check("быстрая модель и pyannote остаются",
          not ({"onnx", "hf"} & lean(str(_P("models")), ["onnx", "onnx-asr", "hf"])))
    check("ненужный ONNX точной модели пропускается",
          "v3_e2e_rnnt_encoder.onnx" in lean(str(_P("models") / "onnx"),
                                             ["v3_e2e_ctc.onnx", "v3_e2e_rnnt_encoder.onnx"]))
    check("быстрая модель в ONNX остаётся",
          "v3_e2e_ctc.onnx" not in lean(str(_P("models") / "onnx"),
                                        ["v3_e2e_ctc.onnx", "v3_e2e_rnnt_encoder.onnx"]))
    site = str(_P(".venv") / "Lib" / "site-packages")
    check("playwright пропускается", "playwright" in lean(site, ["playwright", "numpy", "torch"]),
          sorted(lean(site, ["playwright", "numpy", "torch"])))
    check("torch остаётся: без него не работает поиск речи при записи",
          "torch" not in lean(site, ["playwright", "numpy", "torch"]))
    check("с --all-models тяжёлое кладётся", fat(gig, names) == set(), sorted(fat(gig, names)))
    check("ключ --all-models есть", '"--all-models"' in ap_src)
    check("в памятке сказано про докачку",
          "качается по кнопке" in mp.README and "430 МБ" in mp.README)

    say("")
    say("=== 7. Условия передачи ===")
    terms = mp.TERMS.format(to="Иван Иванов", version="abc1234", date="16.09.2026",
                            to_contact="Иван Петров")
    check("кому передано", "Иван Иванов" in terms)
    check("версия и дата", "abc1234" in terms and "16.09.2026" in terms)
    check("есть «что можно»", "Что можно" in terms)
    check("есть «чего нельзя»", "Чего нельзя" in terms and "третьим лицам" in terms)
    check("сказано, что исходники видны и защита письменная",
          "открытым" in terms and "техническими средствами" in terms)
    check("сказано про токен HuggingFace и pyannote",
          "HuggingFace" in terms and "pyannote" in terms)
    check("сказано, что записи остаются у получателя",
          "остаются на компьютере получателя" in terms)
    check("файл называется по-русски и кладётся в папку",
          '"Условия передачи.txt"' in ap_src)
    check("имя архива отличается", '"-для-проверки"' in ap_src)

    say("")
    say("=== 8. Номер сборки ===")
    ver = mp.build_version()
    check("номер сборки прочитан из .git", len(ver) == 7 and ver != "без номера", ver)
    check("git как программа не запускается",
          "subprocess" not in ap_src and "Popen" not in ap_src)

    say("")
    say("=== 9. Памятка установки ===")
    check("в памятке сказано про токен HuggingFace",
          "токен HuggingFace" in mp.README and "pyannote" in mp.README)
    check("сказано, что без токена запись всё равно работает",
          "Без него запись и стенограмма" in mp.README)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t68_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
