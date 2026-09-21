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
  6в. сборка с разметкой ONNX не берёт ничего, что нужно только pyannote, и
      пишет свой release.json;
  6г. в сборку не едут закрытые от git «.local.» файлы, следы прогонов,
      журналы загрузок и пути этой машины; открытый релиз (--public) — с
      лицензией GPL и без настроек владельца; сборка для других перед
      упаковкой проверяется списком личного;
  7. «Условия передачи»: кому, версия, дата, что можно и чего нельзя, про
     то, что исходники видны; про токен HuggingFace — только у pyannote;
  8. номер сборки читается из .git без запуска git;
  9. в «КАК УСТАНОВИТЬ» про токен HuggingFace — только у pyannote, у ONNX —
     что разметка работает сразу.

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
    "asr_keep_parts": ["english"], "asr_pending_delete": ["fast"],
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
    from hagen import asr as _asr  # noqa: E402

    check("модели — рекомендованные: другой в архиве нет",
          all(data.get(k) == v for k, v in _asr.RECOMMENDED.items()),
          {k: data.get(k) for k in _asr.RECOMMENDED})

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

    from hagen import asr  # noqa: E402

    # Решение 21.09: из моделей распознавания в архиве одна — рекомендованная
    # точная на onnx-asr с полными весами; остальное качается по кнопке.
    lean = mp.ignore_for(_P("."), lean=True)
    fat = mp.ignore_for(_P("."), lean=False)
    gig = str(_P("models") / "gigaam")
    names = ["v3_e2e_rnnt.ckpt", "v3_e2e_ctc.ckpt", "v3_e2e_rnnt_tokenizer.model",
             "v3_e2e_ctc_tokenizer.model", "прочее.txt"]
    check("torch-веса обеих моделей пропускаются",
          lean(gig, names) == set(names) - {"прочее.txt"}, sorted(lean(gig, names)))
    check("посторонний файл не трогаем", "прочее.txt" not in lean(gig, names))
    models = str(_P("models"))
    check("папки моделей целиком не пропускаются: решают файлы",
          not ({"onnx", "onnx-asr", "hf"} & lean(models, ["onnx", "onnx-asr", "hf"])),
          sorted(lean(models, ["onnx", "onnx-asr", "hf"])))
    onnx_names = ["v3_e2e_ctc.onnx", "v3_e2e_ctc.yaml", "v3_e2e_rnnt_encoder.onnx"]
    check("быстрая модель и неиспользуемый ONNX точной пропускаются",
          lean(str(_P("models") / "onnx"), onnx_names) == set(onnx_names),
          sorted(lean(str(_P("models") / "onnx"), onnx_names)))
    en_names = asr.english_files() + ["gigaam-v3"]
    check("английская модель пропускается, папка точной — нет",
          lean(str(_P("models") / "onnx-asr"), en_names) == set(asr.english_files()),
          sorted(lean(str(_P("models") / "onnx-asr"), en_names)))
    ox = str(_P("models") / "onnx-asr" / "gigaam-v3")
    full, packed = asr.ox_files(None), asr.ox_files("int8")
    check("рекомендованная точная модель кладётся целиком",
          asr.recommended_part() == "precise_ox_fp32" and not (set(full) & lean(ox, full)),
          (asr.recommended_part(), sorted(lean(ox, full))))
    check("сжатые веса пропускаются, общие файлы остаются",
          lean(ox, packed) == set(packed) - set(full), sorted(lean(ox, packed)))
    site = str(_P(".venv") / "Lib" / "site-packages")
    check("playwright пропускается", "playwright" in lean(site, ["playwright", "numpy", "torch"]),
          sorted(lean(site, ["playwright", "numpy", "torch"])))
    check("torch остаётся: без него не работает поиск речи при записи",
          "torch" not in lean(site, ["playwright", "numpy", "torch"]))
    check("с --all-models тяжёлое кладётся", fat(gig, names) == set(), sorted(fat(gig, names)))
    check("ключ --all-models есть", '"--all-models"' in ap_src)
    check("в памятке сказано про докачку",
          "качается по кнопке" in mp.README and "одна модель распознавания" in mp.README)

    say("")
    say("=== 6в. Разметка ONNX: ничего от pyannote ===")
    by_onnx = mp.ignore_for(_P("."), lean=True, engine="onnx")
    by_pya = mp.ignore_for(_P("."), lean=True, engine="pyannote")
    libs = ["pyannote", "lightning", "pandas", "numpy", "torch", "onnxruntime",
            "huggingface_hub", "PIL", "scipy"]
    dropped = by_onnx(site, libs)
    from hagen import diar_pyannote  # noqa: E402

    if diar_pyannote.installed():
        check("pyannote и его зависимости не кладутся",
              {"pyannote", "lightning", "pandas"} <= dropped, sorted(dropped))
    else:
        say("   (pyannote не установлен — выкидывать нечего)")
    check("нужное программе остаётся",
          not ({"numpy", "torch", "onnxruntime", "huggingface_hub", "PIL", "scipy"} & dropped),
          sorted(dropped))
    check("у сборки с pyannote он остаётся", "pyannote" not in by_pya(site, libs))
    hub = str(_P("models") / "hf" / "hub")
    hub_names = ["models--pyannote--speaker-diarization-community-1",
                 "models--istupakov--parakeet-tdt-0.6b-v2-onnx"]
    if (mp.PROJECT / "models" / "hf" / "hub" / hub_names[0]).exists():
        check("модели pyannote из кэша не кладутся, английская остаётся",
              by_onnx(hub, hub_names) == {hub_names[0]}, sorted(by_onnx(hub, hub_names)))
    check("модель разметки ONNX кладётся",
          not by_onnx(str(_P("models")), ["diar"]) and not by_onnx(str(_P("models") / "diar"),
                                                                  ["segmentation.onnx"]))
    check("ключ --diarize есть и release.json пишется в сборку",
          '"--diarize"' in ap_src and '"release.json"' in ap_src)
    check("набор пакетов для pyannote едет в сборке",
          "requirements-pyannote.txt" in mp.TOP_FILES)

    say("")
    say("=== 6г. Ни следов этой машины, ни локального ===")
    tools_dir = str(_P("tools"))
    check("закрытое от git «.local.» не кладётся",
          lean(tools_dir, ["make_mirror.py", "mirror_check.local.txt"]) == {"mirror_check.local.txt"},
          sorted(lean(tools_dir, ["make_mirror.py", "mirror_check.local.txt"])))
    hf_dir = str(_P("models") / "hf")
    check("журналы и кэш загрузок моделей не кладутся",
          {"xet", "token"} <= lean(hf_dir, ["xet", "hub", "token"]) and "hub" not in lean(hf_dir, ["hub"]),
          sorted(lean(hf_dir, ["xet", "hub", "token"])))
    venv_dir, scripts_dir = str(_P(".venv")), str(_P(".venv") / "Scripts")
    check("копия pyvenv.cfg и скрипты activate не кладутся, python.exe — да",
          lean(venv_dir, ["pyvenv.cfg", "pyvenv.cfg.bak"]) == {"pyvenv.cfg.bak"}
          and {"activate", "Activate.ps1"} <= lean(scripts_dir, ["activate", "Activate.ps1", "python.exe"])
          and "python.exe" not in lean(scripts_dir, ["python.exe"]))
    import fnmatch  # noqa: E402

    pats = mp.test_patterns()
    tested = {n for n in ["t1_x.py", "meeting.wav", "meeting_plan.json", "t8_rec.json", "t1_result.txt"]
              if any(fnmatch.fnmatch(n, p) for p in pats)}
    check("проверки — ровно то, что под git: следы прогонов не едут",
          tested == {"t1_x.py", "meeting.wav", "meeting_plan.json"}, sorted(tested))
    cfg_dir = TMP / "venvcfg"
    (cfg_dir / ".venv").mkdir(parents=True)
    (cfg_dir / ".venv" / "pyvenv.cfg").write_text(
        "home = C:\\Users\\tester\\Apps\\Hagen\\python\ninclude-system-site-packages = false\n"
        "version = 3.12.10\nexecutable = C:\\Users\\tester\\Apps\\Hagen\\python\\python.exe\n"
        "command = C:\\Users\\tester\\Apps\\Hagen\\python\\python.exe -m venv "
        "C:\\Users\\tester\\Apps\\Hagen\\.venv\n", encoding="utf-8")
    (cfg_dir / ".venv" / "Scripts").mkdir()
    script = cfg_dir / ".venv" / "Scripts" / "tool.py"
    script.write_text("#!%s\\.venv\\Scripts\\python.exe\nprint(1)\n" % mp.machine_paths()[0],
                      encoding="utf-8")
    mp.neutral_venv(cfg_dir)
    cfg_text = (cfg_dir / ".venv" / "pyvenv.cfg").read_text(encoding="utf-8")
    check("в pyvenv.cfg сборки нет путей этой машины",
          "Users" not in cfg_text and "home = %s\\python" % mp.NEUTRAL_ROOT in cfg_text
          and "version = 3.12.10" in cfg_text, cfg_text)
    script_text = script.read_text(encoding="utf-8")
    check("в скриптах .venv\\Scripts путь этой машины заменён",
          script_text.startswith("#!%s\\.venv" % mp.NEUTRAL_ROOT)
          and not any(p.lower() in script_text.lower() for p in mp.machine_paths()),
          script_text.splitlines()[0])
    check("открытый релиз: ключ --public, лицензия GPL остаётся, настроек владельца нет",
          '"--public"' in ap_src and '"--version"' in ap_src
          and 'args.public and f == ".gitignore"' in ap_src and "if not args.public:" in ap_src)
    check("сборка для других проверяется списком личного перед упаковкой",
          "bad = check_stage(stage)" in ap_src and "if outside:" in ap_src)

    say("")
    say("=== 7. Условия передачи ===")
    terms = mp.terms_for("pyannote", to="Иван Иванов", version="abc1234", date="16.09.2026",
                         to_contact="Иван Петров")
    terms_onnx = mp.terms_for("onnx", to="Иван Иванов", version="abc1234", date="16.09.2026",
                              to_contact="Иван Петров")
    check("кому передано", "Иван Иванов" in terms)
    check("версия и дата", "abc1234" in terms and "16.09.2026" in terms)
    check("есть «что можно»", "Что можно" in terms)
    check("есть «чего нельзя»", "Чего нельзя" in terms and "третьим лицам" in terms)
    check("сказано, что исходники видны и защита письменная",
          "открытым" in terms and "техническими средствами" in terms)
    check("у pyannote сказано про токен HuggingFace",
          "HuggingFace" in terms and "pyannote" in terms)
    check("у ONNX про токен ни слова",
          "HuggingFace" not in terms_onnx and "токен" not in terms_onnx)
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
    readme_py, readme_onnx = mp.readme_for("pyannote"), mp.readme_for("onnx")
    check("у pyannote в памятке сказано про токен HuggingFace",
          "токен HuggingFace" in readme_py and "pyannote" in readme_py)
    check("у pyannote сказано, что без токена запись всё равно работает",
          "Без него запись и стенограмма" in readme_py)
    check("у ONNX токен HuggingFace не требуется, разметка работает сразу",
          "HuggingFace" not in readme_onnx and "pyannote" not in readme_onnx
          and "Разметка говорящих работает сразу" in readme_onnx)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t68_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
