# -*- coding: utf-8 -*-
r"""Собрать опись выпуска lock.json из окружения, на котором выпуск проверен.

Запуск из корня проекта, перед выпуском, когда проверки прошли:

    .venv\Scripts\python.exe tools\make_lock.py

Что записывается (подробнее — hagen/lockfile.py):
  - пакеты: requirements.txt разрешается pip-ом «как на чистой машине», но с
    версиями, прибитыми к тому, что стоит в .venv, — так в опись попадает
    всё окружение со всеми зависимостями зависимостей, и ровно тех версий, на
    которых всё проверялось. Для каждого пакета — адрес готовой сборки в
    открытом каталоге (PyPI; у пакетов pytorch — их сайт) и её сумма SHA-256.
    torch, gigaam и silero-vad в .venv мастерской стоят, но в requirements.txt
    их нет — в основную опись они не попадают;
  - pyannote: то же для requirements-pyannote.txt, добавкой к общим пакетам
    (там и torch, сборкой без CUDA);
  - пакеты, у которых в открытых каталогах только исходники (proxy_tools):
    собираются здесь один раз, сборка кладётся в vendor/ и едет в выпуске —
    при обновлении программа ставит пакеты без сети и собрать их не сможет.
    Так же — пакеты из git, если такие появятся;
  - ffmpeg: номер выпуска — из ffmpeg.exe в папке программы, архив — с
    GitHub-зеркала сборок gyan.dev (там старые выпуски не удаляют), сумму
    сообщает GitHub;
  - модели Hugging Face: ревизия, файлы которой совпадают с файлами на диске.
    Нет файлов на диске — последняя ревизия, и об этом предупреждение.

Нужен интернет: pip спрашивает каталоги, GitHub и Hugging Face — свои API.
"""
from __future__ import annotations

import argparse
import datetime
import io
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from hagen import download, lockfile  # noqa: E402

VENV_PY = PROJECT / ".venv" / "Scripts" / "python.exe"
VENDOR = PROJECT / "vendor"
PYPI = "https://pypi.org/simple"
TORCH_INDEX = "https://download.pytorch.org/whl/cpu"
#: С сайта pytorch берём только пакеты самого pytorch (сборки без CUDA). Копии
#: прочих пакетов там лежат без контрольных сумм — их берём с PyPI, тот же файл
#: по имени.
TORCH_OWN = {"torch", "torchaudio", "torchcodec", "torchvision"}
FFMPEG_REPO = "GyanD/codexffmpeg"
#: Пакеты, которые человек обновляет сам кнопкой: выпуск их назад не откатывает.
FLOOR = ["yt-dlp"]


def say(msg: str = "") -> None:
    print(msg, flush=True)


def reference_env() -> dict[str, dict]:
    """Что стоит в .venv: {имя: {"version", "commit"}}. commit — у пакетов из git."""
    import importlib.metadata as md

    out: dict[str, dict] = {}
    for dist in md.distributions(path=[str(lockfile.site_packages(PROJECT))]):
        name = dist.metadata.get("Name")
        if not name:
            continue
        info = {"name": name, "version": str(dist.version)}
        raw = dist.read_text("direct_url.json")
        if raw:
            try:
                data = json.loads(raw)
                info["url"] = data.get("url")
                info["commit"] = (data.get("vcs_info") or {}).get("commit_id")
            except ValueError:
                pass
        out[lockfile.norm(name)] = info
    return out


def vendor_build(name: str, version: str, src: str) -> Path:
    """Готовая сборка пакета в vendor/: собрать из src один раз и дальше брать её же.

    Нужна пакетам, у которых в открытых каталогах нет готовой сборки — только
    исходники (из git или архивом). Собирать их на машине человека нельзя: при
    обновлении программа ставит пакеты без сети. Сборка не пересобирается
    заново: иначе сумма менялась бы при каждом выпуске.
    """
    VENDOR.mkdir(exist_ok=True)
    stem = re.sub(r"[-.]+", "_", name).lower()
    found = [p for p in VENDOR.glob("*.whl")
             if p.name.lower().startswith("%s-%s-" % (stem, version.lower()))]
    if found:
        return found[0]
    say("  собираю %s %s из %s" % (name, version, src))
    subprocess.run([str(VENV_PY), "-m", "pip", "wheel", "--no-deps", "--quiet",
                    "--disable-pip-version-check", "-w", str(VENDOR), src], check=True)
    found = [p for p in VENDOR.glob("*.whl")
             if p.name.lower().startswith("%s-%s-" % (stem, version.lower()))]
    if not found:
        raise SystemExit("сборка %s не появилась в vendor/" % name)
    return found[0]


def vendor_wheel(pkg: dict) -> Path:
    """Сборка пакета из git того самого коммита, что стоит в .venv."""
    return vendor_build(pkg["name"], pkg["version"], "git+%s@%s" % (pkg["url"], pkg["commit"]))


def requirement_lines(path: Path, vendored: dict[str, Path]) -> list[str]:
    """Строки requirements-файла: вложенные -r раскрыты, пакеты из git — на сборку из vendor/."""
    out: list[str] = []
    for raw in io.open(path, encoding="utf-8").read().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = line.split(" #", 1)[0].strip()
        if line.startswith("-r"):
            out += requirement_lines(path.parent / line[2:].strip(), vendored)
            continue
        name = lockfile.norm(re.split(r"[ @=<>!~\[;]", line, 1)[0])
        if name in vendored:
            line = "%s @ %s" % (name, vendored[name].resolve().as_uri())
        out.append(line)
    return out


def from_pypi(name: str, version: str, url: str) -> tuple[str, str]:
    """Тот же файл (по имени), но с PyPI: адрес и сумма из каталога PyPI."""
    fname = urllib.parse.unquote(url.split("?", 1)[0].rsplit("/", 1)[-1])
    try:
        data = download.get_json("https://pypi.org/pypi/%s/%s/json" % (name, version))
    except urllib.error.HTTPError as err:
        raise SystemExit("на PyPI нет %s %s (%s) — а на сайте pytorch он без суммы"
                         % (name, version, err.code)) from None
    for item in data.get("urls") or []:
        if item.get("filename") == fname:
            return item["url"], (item.get("digests") or {}).get("sha256", "")
    raise SystemExit("на PyPI нет %s — а на сайте pytorch он без суммы" % fname)


def hash_by_download(url: str) -> str:
    """Сумма файла, для которого сайт pytorch её не сообщил: скачать и посчитать.

    Такие пакеты — только их собственные сборки без CUDA (torchcodec): на PyPI
    лежат другие файлы, взять оттуда нельзя.
    """
    fname = urllib.parse.unquote(url.split("?", 1)[0].rsplit("/", 1)[-1])
    say("  считаю сумму %s — сайт pytorch её не сообщил" % fname)
    with tempfile.TemporaryDirectory(prefix="hagen_lock_") as tmp:
        return download.sha256_file(download.fetch(url, Path(tmp) / fname))


def resolve(lines: list[str], env: dict[str, dict], vendored: dict[str, Path]) -> list[dict]:
    """Разрешить набор как на чистой машине, с версиями из .venv. Возвращает пакеты описи."""
    with tempfile.TemporaryDirectory(prefix="hagen_lock_") as tmp:
        req = Path(tmp) / "req.txt"
        con = Path(tmp) / "con.txt"
        report = Path(tmp) / "report.json"
        pip_pin = env.get("pip")
        io.open(req, "w", encoding="utf-8").write(
            "\n".join(lines + (["pip==%s" % pip_pin["version"]] if pip_pin else [])) + "\n")
        # «===» — версия строкой целиком: «==0.16.0» пропустило бы и сборку
        # 0.16.0+cpu с сайта pytorch, а в .venv стоит та, что с PyPI.
        io.open(con, "w", encoding="utf-8").write("\n".join(
            "%s===%s" % (p["name"], p["version"]) for n, p in sorted(env.items())
            if n not in vendored and not p.get("commit")) + "\n")
        subprocess.run([str(VENV_PY), "-m", "pip", "install", "--dry-run", "--ignore-installed",
                        "--quiet", "--disable-pip-version-check",
                        "--index-url", PYPI, "--extra-index-url", TORCH_INDEX,
                        "--report", str(report), "-r", str(req), "-c", str(con)], check=True)
        data = json.load(io.open(report, encoding="utf-8"))
    out = []
    for item in data.get("install") or []:
        meta = item.get("metadata") or {}
        info = item.get("download_info") or {}
        name, ver = meta.get("name"), meta.get("version")
        url = str(info.get("url") or "")
        hashes = (info.get("archive_info") or {}).get("hashes") or {}
        sha = hashes.get("sha256") or str((info.get("archive_info") or {}).get("hash") or "")
        sha = sha.split("=", 1)[1] if sha.startswith("sha256=") else sha
        entry: dict = {"name": name, "version": ver}
        if url.startswith("file:"):
            path = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path.lstrip("/")))
            entry["file"] = path.resolve().relative_to(PROJECT).as_posix()
            entry["sha256"] = download.sha256_file(path)
        else:
            if "pytorch.org" in url and lockfile.norm(name) not in TORCH_OWN:
                url, sha = from_pypi(name, ver, url)
            if not sha and "pytorch.org" in url:
                sha = hash_by_download(url)
            if not sha:
                raise SystemExit("у %s %s нет суммы в ответе pip" % (name, ver))
            if url.split("?", 1)[0].endswith(".whl"):
                entry["url"] = url
                entry["sha256"] = sha
            else:
                # Только исходники: собираем у себя и везём сборку в выпуске.
                wheel = vendor_build(name, ver, "%s#sha256=%s" % (url, sha))
                entry["file"] = wheel.relative_to(PROJECT).as_posix()
                entry["sha256"] = download.sha256_file(wheel)
                entry["source"] = url
        have = env.get(lockfile.norm(name))
        if have and have["version"] != ver:
            raise SystemExit("%s: в .venv %s, а разрешилось %s" % (name, have["version"], ver))
        out.append(entry)
    return sorted(out, key=lambda p: lockfile.norm(p["name"]))


def ffmpeg_entry(old: dict) -> dict:
    exe = PROJECT / "ffmpeg" / "bin" / "ffmpeg.exe"
    first = subprocess.run([str(exe), "-hide_banner", "-version"], capture_output=True,
                           text=True, encoding="utf-8", errors="replace").stdout.split("\n", 1)[0]
    m = re.search(r"version (\d+(?:\.\d+)+)-essentials_build", first)
    if not m:
        raise SystemExit("не понял версию ffmpeg: %s" % first)
    ver = m.group(1)
    if old.get("version") == ver and old.get("sha256") and old.get("url"):
        return old
    rel = download.get_json("https://api.github.com/repos/%s/releases/tags/%s" % (FFMPEG_REPO, ver))
    name = "ffmpeg-%s-essentials_build.zip" % ver
    asset = next((a for a in rel.get("assets") or [] if a.get("name") == name), None)
    if not asset or not str(asset.get("digest") or "").startswith("sha256:"):
        raise SystemExit("на %s нет %s с суммой" % (FFMPEG_REPO, name))
    return {"version": ver, "url": asset["browser_download_url"], "size": asset["size"],
            "sha256": asset["digest"].split(":", 1)[1]}


def model_revision(repo: str, local_dir: Path, files: list[str]) -> str:
    """Ревизия, у которой файлы совпадают с лежащими на диске."""
    api = "https://huggingface.co/api/models/%s" % repo
    local = {f: local_dir / f for f in files if (local_dir / f).is_file()}
    commits = [c["id"] for c in download.get_json(api + "/commits/main")][:30]
    if not local:
        say("  ! %s: на диске нет файлов, в опись идёт последняя ревизия" % repo)
        return commits[0]
    sums = {f: download.sha256_file(p) for f, p in local.items()}
    for rev in commits:
        tree = {t["path"]: t for t in download.get_json("%s/tree/%s" % (api, rev))}
        ok = all(((tree.get(f) or {}).get("lfs") or {}).get("oid") == s
                 or (not (tree.get(f) or {}).get("lfs") and f in tree)
                 for f, s in sums.items())
        if ok:
            return rev
    raise SystemExit("%s: ни одна из последних ревизий не совпала с файлами на диске" % repo)


def main() -> int:
    ap = argparse.ArgumentParser(description="Собрать опись выпуска lock.json")
    ap.add_argument("--out", default=str(lockfile.PATH))
    args = ap.parse_args()

    old = lockfile.read(args.out)
    env = reference_env()
    say("окружение .venv: пакетов %d" % len(env))
    vendored = {n: vendor_wheel(p) for n, p in env.items() if p.get("commit")}

    say("разрешаю requirements.txt…")
    base = resolve(requirement_lines(PROJECT / "requirements.txt", vendored), env, vendored)
    say("разрешаю requirements-pyannote.txt…")
    full = resolve(requirement_lines(PROJECT / "requirements-pyannote.txt", vendored), env, vendored)
    base_keys = {(lockfile.norm(p["name"]), p["version"]) for p in base}
    base_names = {k[0] for k in base_keys}
    extra = [p for p in full if (lockfile.norm(p["name"]), p["version"]) not in base_keys]
    clash = [p["name"] for p in extra if lockfile.norm(p["name"]) in base_names]
    if clash:
        raise SystemExit("с pyannote другие версии общих пакетов: %s" % ", ".join(clash))

    say("ffmpeg…")
    ffmpeg = ffmpeg_entry(old.get("ffmpeg") or {})
    say("модели…")
    from hagen import asr

    models = {
        asr.OX_REPO: model_revision(asr.OX_REPO, asr.OX_DIR,
                                    sorted({f for e in asr.OX_MODELS for f in asr.ox_files(e)})),
        asr.EN_REPO: model_revision(asr.EN_REPO, asr.EN_DIR,
                                    ["encoder-model.int8.onnx", "decoder_joint-model.int8.onnx",
                                     "nemo128.onnx", "vocab.txt", "config.json"]),
    }
    import platform as _platform

    lock = {
        "about": "Опись выпуска: точные версии всего, что установщик докачивает. "
                 "Собирается tools/make_lock.py, руками не править.",
        "made": datetime.date.today().isoformat(),
        "python": {"version": _platform.python_version()},
        "packages": base,
        "extras": {"pyannote": extra},
        "floor": FLOOR,
        "ffmpeg": ffmpeg,
        "models": models,
    }
    text = json.dumps(lock, ensure_ascii=False, indent=1) + "\n"
    io.open(args.out, "w", encoding="utf-8", newline="\n").write(text)
    used = {p.get("file") for p in base + extra if p.get("file")}
    for stale in VENDOR.glob("*.whl"):
        if stale.relative_to(PROJECT).as_posix() not in used:
            say("  убираю старую сборку %s" % stale.name)
            stale.unlink()
    say("готово: %s — пакетов %d (+%d для pyannote), ffmpeg %s, моделей %d"
        % (args.out, len(base), len(extra), ffmpeg["version"], len(models)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
