# -*- coding: utf-8 -*-
"""Проверка 69: тяжёлые части качаются по требованию (16.09).

Решение 16.09: точная модель распознавания, английская модель и
браузер для SharePoint в сборку не кладутся — они нужны не всем и не сразу, а
весят больше гигабайта вместе. Программа спрашивает «нужно скачать столько-то,
качать?» тогда, когда человек впервые нажал кнопку, которой они нужны.

Что проверяем:
  1. список частей: вес, для чего нужна, скачана ли;
  2. уже скачанную часть второй раз не качаем;
  3. неудача объясняется по-русски и говорит про корпоративную сеть;
  4. точки службы: список, постановка задачи, 404 на неизвестную часть,
     отказ ставить вторую такую же задачу;
  5. диктовка не качает модель молча, а говорит, где её взять;
  6. torch остаётся в сборке: без него не работает поиск речи при записи;
  7. на странице: окно с «Скачать»/«Отмена», список в «Моделях», проверка
     перед «Перечитать точнее», обновление после задачи.

Ничего не скачивается: установка подменена.
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t69_needs.py
"""
import io
import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
# Две модели, точная на torch: здесь проверяются отказы без части «precise».
isolate.settings(asr_count=2, asr_calls="fast", asr_voice="precise", asr_engine="torch")


from hagen import config, needs  # noqa: E402

say("=== 1. Список частей ===")
# Браузер для входа нужен только SharePoint и сайтам по паролю: пока обе
# возможности выключены, его в списке нет (решение 22.09: выключенная
# возможность убирает и свои загружаемые части).
config.set_feature("video_sharepoint", False)
config.set_feature("video_password", False)
check("возможности выключены — браузера в списке нет",
      "browser" not in {p["key"] for p in needs.state()})
config.set_feature("video_sharepoint", True)
parts = {p["key"]: p for p in needs.state()}
# Быстрая модель и два варианта весов onnx-asr — части моделей распознавания,
# их выбирают в «Настройки → Модели» (решение 21.09).
check("в списке шесть частей", set(parts) == {"fast", "precise", "english", "browser",
                                             "precise_ox_int8", "precise_ox_fp32"}, list(parts))
check("у каждой есть вес", all(p["size_mb"] > 0 for p in parts.values()),
      {k: p["size_mb"] for k, p in parts.items()})
check("у каждой сказано, для чего она", all(len(p["why"]) > 20 for p in parts.values()))
check("быстрая модель весит около 440 МБ", 300 < parts["fast"]["size_mb"] < 600,
      parts["fast"]["size_mb"])
check("точная модель весит около 430 МБ", 300 < parts["precise"]["size_mb"] < 600,
      parts["precise"]["size_mb"])
check("английская — около 630 МБ", 500 < parts["english"]["size_mb"] < 800,
      parts["english"]["size_mb"])
check("сказано, что английская нужна только для английского",
      "английск" in parts["english"]["why"].lower(), parts["english"]["why"])
check("сказано, что без браузера вход по коду работает",
      "коду устройства" in parts["browser"]["why"], parts["browser"]["why"])

say("")
say("=== 2. Скачивание ===")
calls = []
real = {k: needs.PARTS[k]["install"] for k in needs.PARTS}
ready_flags = {"fast": True, "precise": True, "english": False, "browser": False,
               "precise_ox_int8": True, "precise_ox_fp32": True}
real_ready = {k: needs.PARTS[k]["ready"] for k in needs.PARTS}


def fake_install(key):
    def run(note=None):
        calls.append(key)
        if note:
            note("качаю %s…" % key)
        ready_flags[key] = True
        return {"ready": True}
    return run


try:
    for key in needs.PARTS:
        needs.PARTS[key]["install"] = fake_install(key)
        needs.PARTS[key]["ready"] = (lambda k=key: ready_flags[k])

    res = needs.install("precise")
    check("скачанная часть второй раз не качается",
          res.get("skipped") is True and not calls, (res, calls))
    notes = []
    res = needs.install("english", note=notes.append)
    check("нескачанная часть качается", calls == ["english"], calls)
    check("ход работы рассказывается словами", any("качаю" in n for n in notes), notes)
    check("после скачивания часть считается готовой", needs.ready("english"))
    try:
        needs.install("нет-такой")
        check("неизвестная часть отвергается", False, "установка прошла")
    except needs.NeedError as err:
        check("неизвестная часть отвергается", "Неизвестная" in str(err), str(err))

    say("")
    say("=== 3. Неудача объясняется ===")

    def broken(note=None):
        raise needs.NeedError("Английскую модель скачать не вышло: сеть закрыта.\n"
                              "Нужен интернет и доступ к huggingface.co.")

    needs.PARTS["english"]["install"] = broken
    ready_flags["english"] = False
    try:
        needs.install("english")
        check("ошибка поймана", False, "установка прошла")
    except needs.NeedError as err:
        check("сказано, что нужен интернет", "интернет" in str(err), str(err)[:120])
    src = io.open(PROJECT / "hagen" / "needs.py", encoding="utf-8").read()
    check("про корпоративную сеть сказано", "корпоративной сети" in src)
    check("pip зовётся библиотекой, без отдельного процесса",
          "fetch.pip_install" in src and "subprocess" not in src)

    say("")
    say("=== 4. Точки службы ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import jobs, server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    needs.PARTS["english"]["install"] = fake_install("english")
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.get("/api/needs")
        check("служба отдаёт список частей",
              r.status_code == 200 and len(r.json().get("parts", [])) == 6, r.text[:200])
        r = cli.post("/api/needs/нет-такой", headers=ORIGIN)
        check("неизвестная часть — 404", r.status_code == 404, r.status_code)
        r = cli.post("/api/needs/english", headers=ORIGIN)
        job_id = r.json().get("job_id")
        check("скачивание поставлено задачей", r.status_code == 200 and bool(job_id), r.text[:200])
        job = {"status": "timeout"}
        t0 = time.time()
        while time.time() - t0 < 60:
            job = jobs.get(job_id) or job
            if job.get("status") in ("done", "error", "cancelled"):
                break
            time.sleep(0.1)
        check("задача прошла", job.get("status") == "done", job.get("error"))
        check("задача названа по-человечески", "Скачиваю" in (job.get("title") or ""),
              job.get("title"))
        check("часть скачана из задачи", calls.count("english") >= 1, calls)

    say("")
    say("=== 4б. Задача не начинается без нужной части ===")
    from hagen import store  # noqa: E402

    ready_flags["precise"] = False
    rid = store.create(title="Проверка 69", mode="online", source="live",
                       category="Встречи")["id"]
    store.replace_segments(rid, [store.make_segment("far", 0, 3, "Раз два три")])
    store.update(rid, {"duration_s": 3.0, "status": "done"})
    try:
        with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
            r = cli.post("/api/recordings/%s/retranscribe" % rid, json={}, headers=ORIGIN)
            check("«Перечитать точнее» без модели — понятный отказ",
                  r.status_code == 409 and "Настройки" in r.text and "МБ" in r.text, r.text[:200])
            r = cli.post("/api/media/source",
                         json={"kind": "webauth", "url": "https://сайт/видео",
                               "login": "u", "password": "p", "asr_lang": "ru"},
                         headers=ORIGIN)
            check("вход по паролю без браузера — понятный отказ",
                  r.status_code == 409 and "браузер" in r.text.lower(), r.text[:200])
            ready_flags["precise"] = True
            r = cli.post("/api/recordings/%s/retranscribe" % rid, json={}, headers=ORIGIN)
            check("со скачанной моделью заслон пропускает",
                  r.status_code != 409, r.status_code)
    finally:
        store.delete(rid)
        ready_flags["precise"] = True

    say("")
    say("=== 5. Диктовка не качает молча ===")
    from hagen import dictate  # noqa: E402

    ready_flags["precise"] = False
    real_precise = needs.precise_ready
    needs.precise_ready = lambda: False
    try:
        dictate.recognise(np.zeros(16000 * 2, dtype=np.float32))
        check("диктовка отказалась и объяснила", False, "распознавание пошло")
    except dictate.DictateNeedsModel as err:
        check("диктовка отказалась и объяснила",
              "430" in str(err) and "Настройки" in str(err), str(err)[:160])
    except Exception as err:
        check("диктовка отказалась и объяснила", False, "другая ошибка: %r" % err)
    finally:
        needs.precise_ready = real_precise
        ready_flags["precise"] = True

    say("")
    say("=== 6. torch остаётся в сборке ===")
    vad_src = io.open(PROJECT / "hagen" / "vad.py", encoding="utf-8").read()
    check("поиск речи работает на torch, значит torch нужен всегда",
          "import torch" in vad_src)
    check("в списке частей torch нет", "torch" not in [p["key"] for p in needs.state()])
    check("почему — написано в модуле", "silero-vad" in src and "остаётся в сборке" in src)
finally:
    for key in needs.PARTS:
        needs.PARTS[key]["install"] = real[key]
        needs.PARTS[key]["ready"] = real_ready[key]

say("")
say("=== 7. Страница ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
css = io.open(PROJECT / "hagen" / "static" / "app.css", encoding="utf-8").read()
video = io.open(PROJECT / "hagen" / "static" / "video.js", encoding="utf-8").read()
check("окно «Нужно скачать» есть", 'id="dlg-need"' in html and "Нужно скачать" in html)
check("в окне кнопки «Скачать» и «Отмена»",
      'id="btn-need-ok"' in html and ">Скачать<" in html and ">Отмена<" in html)
check("сказано, что скачивается один раз", "Скачивается один раз" in html)
check("список частей в «Моделях»", 'id="needs-list"' in html and ".needs{" in css)
check("страница спрашивает список у службы", "'/api/needs'" in js)
check("кнопка в списке ставит скачивание", "/api/needs/${key}" in js)
check("перед «Перечитать точнее» проверяется точная модель",
      "if (!await ensurePart((S.asr && S.asr.precise_part) || 'precise')) return;" in js)
check("английский предлагается скачать в разделе «Видео»",
      "ensurePart('english')" in video)
check("браузер предлагается скачать до ввода пароля",
      "ensurePart('browser')" in video)
srv = io.open(PROJECT / "hagen" / "server.py", encoding="utf-8").read()
# Сторожа «нужна скачанная часть» и маршруты видео переехали в пакет api
# — смотрим туда, где код живёт теперь.
deps = io.open(PROJECT / "hagen" / "api" / "deps.py", encoding="utf-8").read()
media = io.open(PROJECT / "hagen" / "api" / "media.py", encoding="utf-8").read()
# Какая часть нужна точной модели, зависит от варианта распознавания (стенд
# 18.09): служба спрашивает её у asr.precise_part().
check("служба не начинает работу без нужной части",
      "def require_part" in deps and "require_part(asr.precise_part())" in (srv + deps)
      and 'require_part("browser")' in media)
check("для английского видео спрашивается своя модель",
      "def require_media_parts" in deps and 'require_part("english")' in deps)
check("облаку свои модели не нужны", 'where == "cloud"' in deps)
check("отказ в окне ничего не качает", "finish(false)" in js and "askDownload" in js)
check("после задачи список обновляется",
      "j.kind === 'needs'" in js and "loadNeeds()" in js)
# Обработчик события — в таблице EVENT_HANDLERS, а не в switch.
check("событие о скачанном обновляет список", "needs(ev) {" in js)

sys.exit(finish("t69"))
