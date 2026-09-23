# -*- coding: utf-8 -*-
"""Проверка 105: обновление программы по кнопке — подготовка, замена, откат.

Всё идёт во временной «папке программы» с настоящим окружением .venv (пустым)
и настоящим pip: пакет из архива выпуска правда ставится и снимается. GitHub
подменён: вместо сети — список выпусков и архив file://. Что проверяем:
  1. кого кнопкой не обновить: рабочую копию git, папку без описи;
  2. проверка: самый свежий выпуск с архивом, черновики мимо, «новее нет»;
  3. подготовка: что заменить, что убрать, какие пакеты скачать — и ничего
     в папке программы до перезапуска не меняется;
  4. отказы подготовки: не новее, чужой Python, не архив выпуска, выход за
     пределы папки программы;
  5. замена на старте: файлы, пакет, опись; прежнее отложено;
  6. откат: новая версия дважды не поднялась — старый код вернулся; служба
     поднялась — откатывать нечего;
  7. библиотеки не встали — код возвращается сразу, программа прежняя;
  8. маршруты «О программе» и шаг обновления в run.py;
  9. страница: вкладка «О программе», проверка только по кнопке.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t105_updates.py
"""
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen import download, github, lockfile, updates  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="hagen_t105_"))
PY = "%d.%d.%d" % sys.version_info[:3]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def wheel_bytes(name, ver, broken=False):
    """Настоящая сборка крошечного пакета: pip её правда ставит."""
    files = {"%s/__init__.py" % name: "V = %r\n" % ver,
             "%s-%s.dist-info/METADATA" % (name, ver): "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, ver),
             "%s-%s.dist-info/WHEEL" % (name, ver): "Wheel-Version: 1.0\nGenerator: t105\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    buf = io.BytesIO()
    rec = []
    with zipfile.ZipFile(buf, "w") as zf:
        for n, text in files.items():
            data = text.encode()
            zf.writestr(n, data)
            dig = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            rec.append("%s,sha256=%s,%d" % (n, dig, len(data)))
        if not broken:
            rec.append("%s-%s.dist-info/RECORD,," % (name, ver))
            zf.writestr("%s-%s.dist-info/RECORD" % (name, ver), "\n".join(rec) + "\n")
    return buf.getvalue()


def lock_for(pkgs, ffmpeg_ver="9.0.1", py=PY):
    return {"python": {"version": py}, "packages": pkgs, "extras": {}, "floor": ["yt-dlp"],
            "ffmpeg": {"version": ffmpeg_ver, "url": "", "sha256": ""},
            "models": {"istupakov/gigaam-v3-onnx": "a" * 40}}


def make_program(root, version="0.9.2"):
    """Установленная программа: код, опись, список файлов, пустое окружение."""
    if root.exists():
        shutil.rmtree(root)
    code = {"run.py": "print('старый')\n", "hagen/a.py": "A = 1\n", "hagen/b.py": "B = 1\n",
            "hagen/gone.py": "G = 1\n", "tests/t1.py": "# старая проверка\n"}
    for rel, text in code.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode("utf-8"))   # без замены \n на \r\n
    (root / "release.json").write_text(json.dumps(
        {"version": version, "diarize": "onnx", "updates": "prankhitevil/Hagen"}), encoding="utf-8")
    (root / "lock.json").write_text(json.dumps(lock_for([])), encoding="utf-8")
    listing = {rel: sha((root / rel).read_bytes()) for rel in list(code) + ["release.json", "lock.json"]}
    (root / "files.json").write_text(json.dumps({"version": version, "files": listing}), encoding="utf-8")
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")], check=True,
                   capture_output=True)
    (root / "data").mkdir(exist_ok=True)
    (root / "data" / "запись.txt").write_text("мои данные", encoding="utf-8")


def make_archive(path, version, *, py=PY, pkg=True, listing=True, extra=None, broken_wheel=False,
                 top="Hagen/"):
    """Архив выпуска: новый код, пакет в vendor/, опись и список файлов."""
    whl = wheel_bytes("hagenprobe", "2.0", broken=broken_wheel)
    wname = "vendor/hagenprobe-2.0-py3-none-any.whl"
    pkgs = [{"name": "hagenprobe", "version": "2.0", "file": wname, "sha256": sha(whl)}] if pkg else []
    files = {"run.py": "print('новый')\n".encode(), "hagen/a.py": b"A = 2\n",
             "hagen/b.py": b"B = 1\n", "hagen/new.py": b"N = 1\n",
             "tests/t1.py": "# старая проверка\n".encode(),
             "release.json": json.dumps({"version": version, "diarize": "onnx",
                                         "updates": "prankhitevil/Hagen"}).encode(),
             "lock.json": json.dumps(lock_for(pkgs, py=py)).encode(), wname: whl,
             "python/python.exe": b"MZ python"}
    files.update(extra or {})
    with zipfile.ZipFile(path, "w") as zf:
        for rel, data in files.items():
            zf.writestr(top + rel, data)
        if listing:
            lst = {rel: sha(data) for rel, data in files.items() if not rel.startswith("python/")}
            zf.writestr(top + "files.json", json.dumps({"version": version, "files": lst}))
    return path


ROOT = TMP / "Hagen"
try:
    say("=== 1. Кого кнопкой не обновить ===")
    ok, why = updates.can_update(PROJECT)
    if (PROJECT / ".git").exists():
        check("рабочую копию git — нет, объяснено почему", not ok and "git" in why, why)
    else:
        check("установленную программу (не git) — можно", ok, why)
    make_program(ROOT)
    check("установленную программу с описью — можно", updates.can_update(ROOT) == (True, ""))
    (ROOT / "lock.json").rename(ROOT / "lock.bak")
    check("без описи — нельзя", not updates.can_update(ROOT)[0])
    (ROOT / "lock.bak").rename(ROOT / "lock.json")
    check("до обновлений делать на старте нечего", not updates.pending(ROOT))

    say("")
    say("=== 2. Проверка ===")
    arch = make_archive(TMP / "Hagen-v0.9.4-windows.zip", "0.9.4")
    feed = [
        {"tag": "v0.9.3", "title": "0.9.3", "notes": "старее", "draft": False,
         "assets": [{"name": "Hagen-v0.9.3-windows.zip", "url": "x", "size": 1, "sha256": ""}]},
        {"tag": "v0.9.4", "title": "0.9.4", "notes": "что нового", "draft": False, "page": "p",
         "assets": [{"name": "Hagen-v0.9.4-windows.zip", "url": arch.as_uri(),
                     "size": arch.stat().st_size, "sha256": download.sha256_file(arch)},
                    {"name": "Hagen-v0.9.4-windows-full.zip", "url": "y", "size": 9, "sha256": ""}]},
        {"tag": "v1.0", "title": "черновик", "draft": True,
         "assets": [{"name": "Hagen-v1.0-windows.zip", "url": "z", "size": 1, "sha256": ""}]},
    ]
    info = updates.check(ROOT, releases=lambda repo: feed)
    check("свежий выпуск найден, черновик и полный архив мимо",
          info["newer"] and info["latest"] == "0.9.4"
          and info["asset"]["name"] == "Hagen-v0.9.4-windows.zip" and info["notes"] == "что нового", info)
    same = updates.check(ROOT, releases=lambda repo: feed[:1][:0])
    check("выпусков нет — «новее нет»", same["newer"] is False and same["latest"] is None)
    old = updates.check(ROOT, releases=lambda repo: [dict(feed[0], assets=[
        {"name": "Hagen-v0.9.1-windows.zip", "url": "x", "size": 1, "sha256": ""}])])
    check("выпуск старее стоящего — не новее", old["newer"] is False)
    real_get = download.get_json
    try:
        def fake_404(url, headers=None, timeout=20.0):
            import urllib.error

            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        download.get_json = fake_404
        try:
            github.releases("prankhitevil/Hagen")
            check("закрытый репозиторий — понятное сообщение", False)
        except github.NotAvailable as err:
            check("закрытый репозиторий — понятное сообщение и что делать",
                  "закрытый" in str(err) and "из файла" in str(err), err)
        download.get_json = lambda url, headers=None, timeout=20.0: [{
            "tag_name": "v0.9.4", "name": "Hagen 0.9.4", "body": "текст", "draft": False,
            "prerelease": True, "html_url": "https://github.com/x", "assets": [
                {"name": "Hagen-v0.9.4-windows.zip", "browser_download_url": "https://github.com/a.zip",
                 "size": 5, "digest": "sha256:" + "b" * 64}]}]
        rel = github.releases("prankhitevil/Hagen")
        check("ответ GitHub приведён к своему виду, сумма — из digest",
              rel[0]["assets"][0]["sha256"] == "b" * 64 and rel[0]["notes"] == "текст", rel)
    finally:
        download.get_json = real_get
    got = updates.download_latest(root=ROOT, releases=lambda repo: feed)
    check("архив скачан и сверен с суммой", got.exists() and download.sha256_file(got) == download.sha256_file(arch))

    say("")
    say("=== 3. Подготовка ничего не меняет ===")
    def program_state():
        return {p: p.read_bytes() for p in ROOT.rglob("*")
                if p.is_file() and not {".venv", "data"} & set(p.relative_to(ROOT).parts)}

    before = program_state()
    plan = updates.prepare(got, root=ROOT)
    after = program_state()
    check("в папке программы до перезапуска ничего не поменялось", before == after)
    check("заменить — изменённое и новое, одинаковое не трогать",
          set(plan["write"]) >= {"run.py", "hagen/a.py", "hagen/new.py", "release.json", "lock.json"}
          and "hagen/b.py" not in plan["write"] and "tests/t1.py" not in plan["write"], plan["write"])
    check("убрать — то, чего в выпуске больше нет", plan["delete"] == ["hagen/gone.py"], plan["delete"])
    check("Python из архива не берётся", not any(w.startswith("python/") for w in plan["write"]))
    check("пакет по описи скачан заранее", plan["wheels"] == ["hagenprobe-2.0-py3-none-any.whl"],
          plan["wheels"])
    check("на старте есть что делать", updates.pending(ROOT))
    st = updates.status(ROOT)
    check("«О программе» знает, что готово", st["staged"] and st["staged"]["to"] == "0.9.4", st["staged"])

    say("")
    say("=== 4. Отказы подготовки ===")

    def refused(path, piece):
        try:
            updates.prepare(path, root=ROOT)
        except updates.UpdateError as err:
            return piece in str(err), str(err)
        return False, "не отказал"

    r = refused(make_archive(TMP / "same.zip", "0.9.2"), "не новее")
    check("та же версия — отказ", r[0], r[1])
    r = refused(make_archive(TMP / "py.zip", "0.9.5", py="3.99.0"), "Ustanovka.cmd")
    check("выпуск под другой Python — отказ и совет поставить заново", r[0], r[1])
    junk = TMP / "junk.zip"
    with zipfile.ZipFile(junk, "w") as zf:
        zf.writestr("readme.txt", "не выпуск")
    r = refused(junk, "не архив выпуска")
    check("не архив выпуска — отказ", r[0], r[1])
    r = refused(TMP / "нет.zip", "не открылся")
    check("файла нет — отказ", r[0], r[1])
    check("после отказа подготовленного нет", not updates.pending(ROOT))
    evil = make_archive(TMP / "evil.zip", "0.9.6", extra={"../../вне.py": b"x", "data/x.txt": b"x"})
    plan = updates.prepare(evil, root=ROOT)
    check("пути за пределы папки и в data\\ из архива не берутся",
          not (TMP / "вне.py").exists() and not any(w.startswith(("..", "data/")) for w in plan["write"]),
          plan["write"])
    src_zip = make_archive(TMP / "src.zip", "0.9.4", listing=False, top="Hagen-0.9.4/")
    plan = updates.prepare(src_zip, root=ROOT)
    check("«Source code» без списка файлов тоже годится", "hagen/a.py" in plan["write"]
          and not any(w.startswith("python/") for w in plan["write"]), plan["write"])
    updates.prepare(got, root=ROOT)

    say("")
    say("=== 5. Замена на старте ===")
    res = updates.apply_pending(ROOT)
    check("поставлено", res and res["phase"] == "applied" and res["to"] == "0.9.4", res)
    check("код новый", (ROOT / "hagen" / "a.py").read_text(encoding="utf-8") == "A = 2\n"
          and (ROOT / "hagen" / "new.py").exists() and (ROOT / "run.py").read_text(encoding="utf-8") == "print('новый')\n")
    check("удалённое из выпуска убрано", not (ROOT / "hagen" / "gone.py").exists())
    check("пакет стоит в .venv программы", lockfile.installed(ROOT).get("hagenprobe") == "2.0",
          lockfile.installed(ROOT))
    check("опись и список файлов — новые",
          json.loads((ROOT / "release.json").read_text(encoding="utf-8"))["version"] == "0.9.4"
          and json.loads((ROOT / "files.json").read_text(encoding="utf-8"))["version"] == "0.9.4")
    check("записи не тронуты", (ROOT / "data" / "запись.txt").read_text(encoding="utf-8") == "мои данные")
    check("прежнее отложено", (updates.work_dir(ROOT) / "backup" / "files" / "hagen" / "a.py").exists())
    check("подготовленное убрано", not (updates.work_dir(ROOT) / "staged").exists())

    say("")
    say("=== 6. Откат и подтверждение ===")
    check("первый неудачный запуск — ещё не откат", updates.apply_pending(ROOT) is None)
    res = updates.apply_pending(ROOT)
    check("второй неудачный — откат", res and res["phase"] == "rolled_back", res)
    check("старый код вернулся, новое убрано, удалённое на месте",
          (ROOT / "hagen" / "a.py").read_text(encoding="utf-8") == "A = 1\n"
          and not (ROOT / "hagen" / "new.py").exists() and (ROOT / "hagen" / "gone.py").exists()
          and json.loads((ROOT / "release.json").read_text(encoding="utf-8"))["version"] == "0.9.2")
    check("после отката на старте делать нечего", not updates.pending(ROOT))
    check("«О программе» скажет про откат", updates.status(ROOT)["last"]["phase"] == "rolled_back")
    make_program(ROOT)
    updates.prepare(make_archive(TMP / "ok.zip", "0.9.4"), root=ROOT)
    updates.apply_pending(ROOT)
    updates.confirm_started(ROOT)
    check("служба поднялась — подтверждено, откатывать нечего",
          not updates.pending(ROOT) and updates.status(ROOT)["last"]["confirmed"])
    updates.mark_seen(ROOT)
    check("итог показан один раз", updates.status(ROOT)["last"]["seen"])

    say("")
    say("=== 7. Библиотеки не встали ===")
    make_program(ROOT)
    updates.prepare(make_archive(TMP / "bad.zip", "0.9.4", broken_wheel=True), root=ROOT)
    res = updates.apply_pending(ROOT)
    check("отказ pip — обновление не встало, причина записана",
          res and res["phase"] == "failed" and res.get("error"), res)
    check("код прежний, новое убрано",
          (ROOT / "hagen" / "a.py").read_text(encoding="utf-8") == "A = 1\n"
          and not (ROOT / "hagen" / "new.py").exists()
          and json.loads((ROOT / "release.json").read_text(encoding="utf-8"))["version"] == "0.9.2")
    check("повторять на следующем запуске нечего", not updates.pending(ROOT))

    say("")
    say("=== 8. Маршруты и запуск ===")
    from fastapi.testclient import TestClient  # noqa: E402

    import run  # noqa: E402
    from hagen import jobs, server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        st = cli.get("/api/updates").json()
        git_copy = (PROJECT / ".git").exists()
        check("версия видна; кнопкой обновляется всё, кроме рабочей копии git",
              st["version"] and st["can_update"] is (not git_copy)
              and (not git_copy or "git" in st["reason"]), st)
        real_rel = github.releases

        def closed(repo, timeout=20.0):
            raise github.NotAvailable("Репозиторий %s не отвечает: он закрытый" % repo)

        github.releases = closed          # в сеть проверки не ходят
        try:
            r = cli.post("/api/updates/check", headers=ORIGIN).json()
        finally:
            github.releases = real_rel
        check("GitHub не отвечает или обновлять нельзя — объяснение, а не падение",
              ("git" if git_copy else "закрытый") in r.get("error", ""), r)
        real_dir = updates.PROJECT_DIR
        make_program(ROOT)
        updates.PROJECT_DIR = ROOT
        try:
            with open(arch, "rb") as fh:
                r = cli.post("/api/updates/file", headers=ORIGIN,
                             files={"file": ("Hagen-v0.9.4-windows.zip", fh, "application/zip")})
            jid = r.json().get("job_id")
            deadline = time.time() + 60
            job = {}
            while time.time() < deadline:
                job = next((j for j in jobs.list_all() if j.get("id") == jid), {})
                if job.get("status") in ("done", "error", "cancelled"):
                    break
                time.sleep(0.2)
            check("«Установить из файла…»: задача подготовила обновление",
                  job.get("status") == "done" and updates.pending(ROOT), job.get("status"))
            st = cli.get("/api/updates").json()
            check("и «О программе» это видит", st["staged"] and st["staged"]["to"] == "0.9.4", st["staged"])
            r = cli.post("/api/updates/file", headers=ORIGIN,
                         files={"file": ("отчёт.docx", b"x", "application/octet-stream")})
            check("не архив .zip — отказ сразу", r.status_code == 400)
            cli.post("/api/updates/discard", headers=ORIGIN)
            check("«Отменить обновление» убирает подготовленное", not updates.pending(ROOT))
            updates.prepare(arch, root=ROOT)
            args = type("A", (), {"prepare": False, "port": 65000})()
            os.environ.pop("HAGEN_UPDATED", None)
            real_alive = run._service_alive
            run._service_alive = lambda port, timeout=2.0: True
            check("работает другой экземпляр — файлы не трогаем",
                  run.apply_update(args) is None and updates.pending(ROOT))
            run._service_alive = lambda port, timeout=2.0: False
            res = run.apply_update(args)
            check("run.py ставит подготовленное первым делом", res and res["phase"] == "applied", res)
            os.environ["HAGEN_UPDATED"] = "1"
            check("после перезапуска в том же процессе второй раз не ставит",
                  run.apply_update(args) is None)
            os.environ.pop("HAGEN_UPDATED", None)
            run._service_alive = real_alive
        finally:
            updates.PROJECT_DIR = real_dir
    run_src = (PROJECT / "run.py").read_text(encoding="utf-8")
    main_src = run_src[run_src.index("def main()"):]
    check("в run.py обновление — раньше загрузки остальной программы",
          main_src.index("apply_update(args)") < main_src.index("from hagen import platform"))

    say("")
    say("=== 9. Страница ===")
    html = (PROJECT / "hagen" / "static" / "index.html").read_text(encoding="utf-8")
    js = (PROJECT / "hagen" / "static" / "app.js").read_text(encoding="utf-8")
    check("вкладка «О программе» с версией и кнопками",
          'data-tab="t-about"' in html and 'id="about-version"' in html
          and 'id="btn-upd-check"' in html and 'id="btn-upd-file"' in html)
    check("проверка — только по кнопке: при запуске страница GitHub не спрашивает",
          "checkUpdates" in js and js.count("/api/updates/check") == 1
          and "checkUpdates()" not in js.split("function bindAbout")[0].split("async function checkUpdates")[0])
    check("после подготовки — «закройте программу и откройте снова»",
          "«Выход»" in js and "откройте снова" in js)
finally:
    shutil.rmtree(TMP, ignore_errors=True)

sys.exit(finish("t105"))
