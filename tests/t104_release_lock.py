# -*- coding: utf-8 -*-
"""Проверка 104: выпуск и его опись — всё докачивается ровно тех версий, что проверены.

Архив выпуска несёт код, Python и модель разметки; библиотеки, ffmpeg и модели
распознавания установщик качает из открытых источников по описи lock.json. Что
проверяем:
  1. release.json: номер версии, сравнение версий, движок, откуда обновления;
  2. опись сходится с requirements.txt, с этим окружением .venv и с Python из
     Ustanovka.cmd; у каждого пакета — готовая сборка и сумма; сборки из
     vendor/ на месте и совпадают;
  3. скачивание: сумма сошлась — файл на месте, не сошлась — файла нет;
     повторно то же не качается;
  4. что ставить и что снимать: другая версия — ставим, та же — нет, свежий
     yt-dlp назад не откатываем, чужое (поставленное человеком) не снимаем;
  5. ffmpeg распаковывается из архива сборки; неполный архив не ломает рабочий;
  6. установщик: ставит по описи, стоящее по описи не трогает, ffmpeg берёт из
     описи — без сети, на подставных адресах file://.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t104_release_lock.py
"""
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from harness import LINES, FAIL, say, check, finish  # noqa: E402


from hagen import download, lockfile, release  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="hagen_t104_"))
try:
    say("=== 1. release.json ===")
    check("номер версии есть", bool(release.parse_version(release.version())), release.version())
    check("0.9.10 новее 0.9.9 (числами, а не строками)", release.newer("0.9.10", "0.9.9"))
    check("0.9 и 0.9.0 — одна версия", not release.newer("0.9.0", "0.9") and not release.newer("0.9", "0.9.0"))
    check("«v» в номере не мешает", release.newer("v1.0", "0.9.2") and release.parse_version("v0.9.2") == (0, 9, 2))
    check("мусор новее не бывает", not release.newer("завтра", "0.9.2"))
    check("обновления — из витрины", release.updates_repo() == "prankhitevil/Hagen", release.updates_repo())
    bad = tmp / "bad.json"
    bad.write_text('{"updates": "https://evil.example.com/x/y"}', encoding="utf-8")
    check("не «владелец/имя» — обновлений нет", release.updates_repo(bad) == "")
    check("нет файла — pyannote, как раньше", release.diarize_engine(tmp / "нет.json") == "pyannote")

    say("")
    say("=== 2. Опись сходится ===")
    lock = lockfile.read()
    base = lockfile.packages(lock, "onnx")
    full = lockfile.packages(lock, "pyannote")
    names = {lockfile.norm(p["name"]): p for p in base}
    check("опись есть и не пустая", len(base) > 30, len(base))
    reqs = []
    for raw in io.open(PROJECT / "requirements.txt", encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith(("#", "-")):
            continue
        m = re.match(r"([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*(==\s*([^\s;#]+))?", line)
        reqs.append((lockfile.norm(m.group(1)), m.group(4)))
    missing = [n for n, _v in reqs if n not in names]
    check("всё из requirements.txt — в описи", not missing, missing)
    wrong = [(n, v, names[n]["version"]) for n, v in reqs if v and n in names and names[n]["version"] != v]
    check("версии совпадают с requirements.txt", not wrong, wrong)
    check("pyannote — только в добавке своего релиза",
          "pyannote-audio" not in names and any(lockfile.norm(p["name"]) == "pyannote-audio" for p in full))
    no_whl = [p["name"] for p in full if not str(p.get("url") or p.get("file") or "").endswith(".whl")]
    check("у каждого пакета готовая сборка (.whl), собирать ничего не надо", not no_whl, no_whl)
    no_sha = [p["name"] for p in full if not re.fullmatch(r"[0-9a-f]{64}", str(p.get("sha256") or ""))]
    check("у каждого пакета сумма SHA-256", not no_sha, no_sha)
    # sympy в списке нет: его требует сам onnxruntime.
    heavy = {"torch", "torchaudio", "torchcodec", "gigaam", "silero-vad", "pandas",
             "scikit-learn", "matplotlib", "hydra-core", "omegaconf", "sentencepiece"}
    check("в основной описи нет ни torch, ни его спутников, ни gigaam, ни silero-vad",
          not (heavy & set(names)), sorted(heavy & set(names)))
    torch_src = [p["url"] for p in full if lockfile.norm(p["name"]) in ("torch", "torchaudio")]
    check("torch — только в добавке pyannote, сборкой без CUDA с сайта pytorch",
          len(torch_src) == 2 and all("pytorch.org" in u and "%2Bcpu" in u for u in torch_src), torch_src)
    odd = [p["name"] for p in full if p.get("url") and "pythonhosted.org" not in p["url"]
           and "pytorch.org" not in p["url"]]
    check("остальное — из PyPI", not odd, odd)
    vend = [p for p in full if p.get("file")]
    vend_bad = [p["file"] for p in vend if not (PROJECT / p["file"]).is_file()
                or download.sha256_file(PROJECT / p["file"]) != p["sha256"]]
    check("сборки из vendor/ на месте и совпадают с описью", not vend_bad, vend_bad)
    check("в описи нет пакетов из git", not any(str(p.get("url") or "").startswith("git+") for p in full))
    have = lockfile.installed(PROJECT)
    if have:
        drift = lockfile.to_install(base, have, lockfile.floor(lock))
        check("окружение .venv совпадает с описью (иначе — tools\\make_lock.py)",
              not drift, [(p["name"], have.get(lockfile.norm(p["name"])), p["version"]) for p in drift][:8])
    cmd = (PROJECT / "Ustanovka.cmd").read_text(encoding="ascii")
    nuget = re.search(r"python/(\d+\.\d+\.\d+)/", cmd)
    py_lock = (lock.get("python") or {}).get("version")
    check("Python в описи — тот, что качает Ustanovka.cmd",
          nuget and nuget.group(1) == py_lock, (nuget and nuget.group(1), py_lock))
    check("Ustanovka.cmd берёт именно 3.12, а не любой Python 3", "py -3.12" in cmd and "py -3 " not in cmd)
    own_py = PROJECT / "python" / "python.exe"
    if own_py.exists():
        got = subprocess.run([str(own_py), "-c", "import platform; print(platform.python_version())"],
                             capture_output=True, text=True).stdout.strip()
        check("Python в папке программы — той же версии", got == py_lock, (got, py_lock))
    ff = lock.get("ffmpeg") or {}
    check("ffmpeg: версия, адрес на GitHub-зеркале, сумма",
          ff.get("version") and "github.com/GyanD/codexffmpeg" in str(ff.get("url"))
          and re.fullmatch(r"[0-9a-f]{64}", str(ff.get("sha256") or "")), ff)
    from hagen import asr  # noqa: E402

    models = lock.get("models") or {}
    check("модели Hugging Face закреплены ревизией",
          all(re.fullmatch(r"[0-9a-f]{40}", str(models.get(r) or "")) for r in (asr.OX_REPO, asr.EN_REPO)),
          models)
    check("скачивание модели берёт ревизию из описи",
          lockfile.model_revision(asr.OX_REPO) == models.get(asr.OX_REPO)
          and "revision=lockfile.model_revision" in (PROJECT / "hagen" / "needs.py").read_text(encoding="utf-8"))

    say("")
    say("=== 3. Скачивание со сверкой суммы ===")
    src = tmp / "исходник.bin"
    src.write_bytes(b"hagen" * 1000)
    good = download.sha256_file(src)
    got = download.fetch(src.as_uri(), tmp / "dl" / "a.bin", sha256=good)
    check("сумма сошлась — файл на месте", got.read_bytes() == src.read_bytes())
    stamp = got.stat().st_mtime_ns
    download.fetch(src.as_uri(), tmp / "dl" / "a.bin", sha256=good)
    check("тот же файл второй раз не качается", got.stat().st_mtime_ns == stamp)
    real_sleep, real_open = download.time.sleep, download.urllib.request.urlopen
    download.time.sleep = lambda s: None
    try:
        try:
            download.fetch(src.as_uri(), tmp / "dl" / "b.bin", sha256="0" * 64)
            check("чужой файл не принимается", False)
        except download.DownloadError as err:
            check("чужой файл не принимается, и следа от него нет",
                  "сумма" in str(err) and not (tmp / "dl" / "b.bin").exists()
                  and not (tmp / "dl" / "b.bin.part").exists(), err)
        tries = []

        def flaky(req, timeout=60.0):
            tries.append(1)
            if len(tries) == 1:           # первый раз связь отдаёт мусор без ошибки
                return real_open((tmp / "dl" / "a.bin").as_uri().replace("a.bin", "мусор.bin"), timeout=timeout)
            return real_open(req, timeout=timeout)

        (tmp / "dl" / "мусор.bin").write_bytes(b"x" * 10)
        download.urllib.request.urlopen = flaky
        got = download.fetch(src.as_uri(), tmp / "dl" / "c.bin", sha256=good)
        check("пришло битым — со второго раза целое", len(tries) == 2 and got.read_bytes() == src.read_bytes(),
              len(tries))
    finally:
        download.time.sleep, download.urllib.request.urlopen = real_sleep, real_open

    say("")
    say("=== 4. Что ставить, что снимать ===")
    pk = [{"name": "Numpy", "version": "2.0"}, {"name": "yt-dlp", "version": "2026.8.19"},
          {"name": "новый", "version": "1"}]
    have = {"numpy": "1.9", "yt-dlp": "2026.9.1", "pyannote-audio": "4.0.7"}
    todo = [p["name"] for p in lockfile.to_install(pk, have, {"yt-dlp"})]
    check("другая версия и новый пакет — ставим", todo == ["Numpy", "новый"], todo)
    have["yt-dlp"] = "2026.1.1"
    check("старый yt-dlp — ставим из описи",
          "yt-dlp" in [p["name"] for p in lockfile.to_install(pk, have, {"yt-dlp"})])
    check("та же версия — не трогаем",
          not lockfile.to_install([{"name": "numpy", "version": "1.9"}], {"numpy": "1.9"}))
    gone = lockfile.to_remove([{"name": "старый"}, {"name": "numpy"}], [{"name": "numpy"}],
                              {"старый": "1", "numpy": "2", "pyannote-audio": "4"})
    check("снимаем только выпавшее из описи, чужое не трогаем", gone == ["старый"], gone)
    check("пакеты ложатся в .venv, даже если pip запущен в процессе программы",
          "--prefix" in lockfile.install_args(["x.whl"], tmp)
          and str(tmp / ".venv") in lockfile.install_args(["x.whl"], tmp))
    wheel = tmp / "vendor" / "demo-1.0-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"PK demo")
    entry = {"name": "demo", "version": "1.0", "file": "vendor/demo-1.0-py3-none-any.whl",
             "sha256": download.sha256_file(wheel)}
    files = lockfile.fetch_packages([entry], tmp / "wheels", tmp)
    check("пакет из vendor/ берётся из папки выпуска", files and files[0].read_bytes() == b"PK demo")
    try:
        lockfile.fetch_packages([dict(entry, sha256="1" * 64)], tmp / "wheels2", tmp)
        check("подменённый пакет из vendor/ не берётся", False)
    except lockfile.LockError as err:
        check("подменённый пакет из vendor/ не берётся", "не совпадает" in str(err), err)

    say("")
    say("=== 5. ffmpeg из архива сборки ===")
    fz = tmp / "ffmpeg-9.9-essentials_build.zip"
    with zipfile.ZipFile(fz, "w") as zf:
        zf.writestr("ffmpeg-9.9-essentials_build/bin/ffmpeg.exe", b"MZ ffmpeg")
        zf.writestr("ffmpeg-9.9-essentials_build/bin/ffprobe.exe", b"MZ ffprobe")
        zf.writestr("ffmpeg-9.9-essentials_build/doc/readme.txt", b"doc")
    target = tmp / "prog" / "ffmpeg"
    lockfile.unpack_ffmpeg(fz, target)
    check("ffmpeg.exe и ffprobe.exe — в ffmpeg\\bin, лишнего нет",
          sorted(p.name for p in (target / "bin").iterdir()) == ["ffmpeg.exe", "ffprobe.exe"])
    broken = tmp / "broken.zip"
    with zipfile.ZipFile(broken, "w") as zf:
        zf.writestr("x/bin/ffmpeg.exe", b"MZ")
    try:
        lockfile.unpack_ffmpeg(broken, target)
        check("неполный архив не ставится", False)
    except lockfile.LockError:
        check("неполный архив не ставится, рабочий ffmpeg цел",
              (target / "bin" / "ffprobe.exe").read_bytes() == b"MZ ffprobe")

    say("")
    say("=== 6. Установщик по описи ===")
    import install  # noqa: E402

    real = {"lock": install.lock, "FFMPEG_DIR": install.FFMPEG_DIR, "PROJECT": install.PROJECT,
            "run": install.run}
    calls = []
    try:
        check("Python этой версии установщику подходит", install.check_python())
        fake_lock = {"ffmpeg": {"version": "9.9", "url": fz.as_uri(), "sha256": download.sha256_file(fz),
                                "size": fz.stat().st_size},
                     "packages": [entry], "python": lock.get("python")}
        install.lock = lambda: fake_lock
        install.PROJECT = tmp
        install.FFMPEG_DIR = tmp / "ff2"
        check("ffmpeg ставится из описи", install._download_ffmpeg()
              and (tmp / "ff2" / "bin" / "ffmpeg.exe").read_bytes() == b"MZ ffmpeg")
        install.run = lambda cmd, title, timeout=3600: calls.append(cmd) or True
        check("библиотеки: чего нет — качается и ставится без сети и без поиска зависимостей",
              install.install_deps() and calls and "--no-deps" in calls[0] and "--no-index" in calls[0]
              and any(str(c).endswith("demo-1.0-py3-none-any.whl") for c in calls[0]), calls[:1])
        check("скачанное после установки убрано", not (tmp / "data" / "_install").exists())
        sp = lockfile.site_packages(tmp) / "demo-1.0.dist-info"
        sp.mkdir(parents=True)
        (sp / "METADATA").write_text("Metadata-Version: 2.1\nName: demo\nVersion: 1.0\n", encoding="utf-8")
        calls.clear()
        check("стоит по описи — ничего не качается и не ставится", install.install_deps() and not calls)
    finally:
        install.lock, install.FFMPEG_DIR, install.PROJECT, install.run = (
            real["lock"], real["FFMPEG_DIR"], real["PROJECT"], real["run"])
    src_install = (PROJECT / "install.py").read_text(encoding="utf-8")
    check("в установщике нет ни winget, ни «последней» сборки ffmpeg, ни ручных версий torch",
          "winget" not in src_install and "release-essentials" not in src_install
          and "TORCH_PINS" not in src_install)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(finish("t104"))
