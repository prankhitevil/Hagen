# -*- coding: utf-8 -*-
"""Проверка 112: записи с телефона — папки входящих и страница в локальной сети (23.09).

Запись с телефона попадает в программу сама, двумя дорогами:
  * папки входящих: программа следит за папками из настроек и берёт новый
    звуковой или видеофайл — распознать и разметить голоса, без документа;
  * страница в локальной сети: по QR-коду из настроек телефон открывает
    страницу в браузере и отправляет файл в те же входящие.

Что проверяем:
  1. папки из настроек: раскрытие переменных, повторы, есть ли папка;
  2. сторож: что лежало при добавлении папки — не берётся; новый файл берётся,
     когда дописан (размер не меняется положенный срок) и не во время записи;
     не звуковые файлы, повторы и файлы с ошибкой — не берутся дважды; память
     переживает перезапуск сторожа; убранная папка забывается; выключенная
     возможность — сторож спит;
  3. приём: копия в data\\_uploads, оригинал на месте, задание «стенограмма и
     голоса, без документа», карточка помнит, откуда файл;
  4. страница в сети: без ключа — 403, с ключом — страница и проба связи,
     файл принимается и уходит во входящие, чужой формат — 400, «Новый ключ»
     закрывает старую ссылку, ключ наружу не отдаётся;
  5. адрес для ссылки: домашний Wi-Fi раньше туннеля, выбранный — если он есть;
  6. служба на порту: поднимается по настройкам, слушает, отвечает по сети,
     гаснет при выключении;
  7. маршруты службы и страница настроек: вкладка «С телефона», список папок с
     «×», QR-код.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t112_phone.py
"""
import json
import os
import shutil
import socket
import sys
import tempfile
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()

from hagen import config, inbox, lan, media  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="hagen_t112_"))
inbox.SEEN_PATH = tmp / "inbox.json"
inbox.UPLOAD_DIR = tmp / "_uploads"

#: Что «обработка» получила бы: подменяем media.submit_file, чтобы не гонять модели.
SUBMITTED = []
real_submit = media.submit_file


def fake_submit(path, name, opts, extra=None):
    SUBMITTED.append({"path": Path(path), "name": name, "opts": dict(opts), "extra": dict(extra or {})})
    return {"job_id": "job-%d" % len(SUBMITTED), "rec_id": "rec-%d" % len(SUBMITTED), "meta": {}}


media.submit_file = fake_submit


def write(path: Path, size: int = 100) -> None:
    path.write_bytes(b"x" * size)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


try:
    say("=== 1. Папки из настроек ===")
    folder_a = tmp / "Входящие"
    folder_b = tmp / "Другая"
    folder_a.mkdir()
    os.environ["HAGEN_T112"] = str(tmp)
    config.save({"inbox_folders": ["%HAGEN_T112%\\Входящие", str(folder_a) + "\\", str(folder_b), " "]})
    fl = inbox.folders()
    check("переменная раскрыта, повтор с косой в конце снят, пустая строка отброшена",
          [f["path"] for f in fl] == [str(folder_a), str(folder_b)], fl)
    check("есть ли папка — сказано", [f["exists"] for f in fl] == [True, False], fl)

    say("")
    say("=== 2. Сторож папок ===")
    config.save({"features": {"phone": True}, "inbox_folders": [str(folder_a)]})
    now = [1000.0]
    taken_paths = []

    def fake_take(p):
        taken_paths.append(p)
        if p.name.startswith("плохой"):
            raise RuntimeError("не читается")
        return {"rec_id": "rec-%d" % len(taken_paths)}

    busy = {"on": False}
    w = inbox.Watcher(poll_s=0.05, stable_s=5.0, take_fn=fake_take, busy=lambda: busy["on"],
                      clock=lambda: now[0])
    write(folder_a / "старая.m4a")
    got = w.scan_once()
    check("первая встреча с папкой: что лежало — не берётся", got == [] and not taken_paths)
    now[0] += 10
    check("и на следующем обходе тоже", w.scan_once() == [] and not taken_paths)

    write(folder_a / "новая.m4a", 50)
    check("новый файл увиден, но сразу не берётся (может дописываться)", w.scan_once() == [])
    now[0] += 1
    write(folder_a / "новая.m4a", 90)              # растёт
    check("файл растёт — ждём", w.scan_once() == [])
    now[0] += 3
    check("размер поменялся — срок считается заново", w.scan_once() == [])
    now[0] += 3
    busy["on"] = True
    check("идёт запись — файл ждёт", w.scan_once() == [])
    busy["on"] = False
    got = w.scan_once()
    check("дописан и запись не идёт — взят", [p.name for p in got] == ["новая.m4a"], got)
    check("второй раз не берётся", w.scan_once() == [] and len(taken_paths) == 1)

    write(folder_a / "заметка.txt")
    write(folder_a / "плохой.mp3")
    now[0] += 5
    w.scan_once()                                  # увиден, ждёт срок
    now[0] += 6
    got = w.scan_once()
    check("текстовый файл не берётся, звуковой с ошибкой — попытка одна",
          [p.name for p in got] == [] and [p.name for p in taken_paths] == ["новая.m4a", "плохой.mp3"],
          taken_paths)
    now[0] += 6
    w.scan_once()
    check("файл с ошибкой второй раз не берётся", len(taken_paths) == 2)
    seen = json.loads(inbox.SEEN_PATH.read_text(encoding="utf-8"))
    check("память на диске: папка, взятый файл с номером записи, ошибка с текстом",
          str(folder_a).lower() in seen["folders"]
          and (seen["files"].get(str(folder_a / "новая.m4a").lower()) or {}).get("rec_id") == "rec-1"
          and "не читается" in (seen["files"].get(str(folder_a / "плохой.mp3").lower()) or {}).get("error", ""),
          seen)

    w2 = inbox.Watcher(poll_s=0.05, stable_s=5.0, take_fn=fake_take, busy=lambda: False,
                       clock=lambda: now[0])
    now[0] += 5
    check("новый сторож помнит взятое — не берёт заново", w2.scan_once() == [] and len(taken_paths) == 2)
    write(folder_a / "новая.m4a", 400)               # тот же путь, другой файл
    now[0] += 5
    w2.scan_once()
    now[0] += 5
    got = w2.scan_once()
    check("файл с тем же именем, но другого размера — новый, берётся",
          [p.name for p in got] == ["новая.m4a"] and len(taken_paths) == 3)

    config.save({"inbox_folders": []})
    w2.scan_once()
    seen = json.loads(inbox.SEEN_PATH.read_text(encoding="utf-8"))
    check("папку убрали — отправная точка забыта", seen["folders"] == {}, seen["folders"])
    write(folder_a / "пока-нас-не-было.m4a")
    config.save({"inbox_folders": [str(folder_a)]})
    now[0] += 5
    w2.scan_once()
    now[0] += 5
    check("вернули папку — снова отправная точка, старое не берётся",
          w2.scan_once() == [] and len(taken_paths) == 3)

    config.save({"features": {"phone": False}})
    write(folder_a / "при-выключенной.m4a")
    now[0] += 5
    w2.scan_once()
    now[0] += 5
    check("возможность выключена — сторож ничего не берёт", w2.scan_once() == [] and len(taken_paths) == 3)
    config.save({"features": {"phone": True}})

    w.start()
    time.sleep(0.2)
    w.stop()
    check("поток сторожа запускается и останавливается", w._thread is None)

    say("")
    say("=== 3. Приём файла ===")
    SUBMITTED.clear()
    src = folder_a / "Запись 12.m4a"
    write(src, 777)
    res = inbox.take(src)
    s = SUBMITTED[-1]
    check("копия в _uploads, имя сохранено", s["path"].parent == inbox.UPLOAD_DIR
          and s["path"].name.endswith("_Запись 12.m4a") and s["name"] == "Запись 12.m4a", s)
    check("оригинал на месте", src.exists() and src.stat().st_size == 777)
    check("копия — тот же файл", s["path"].stat().st_size == 777)
    o = s["opts"]
    check("задание: без документа, с разметкой, встреча, звук хранить",
          o.get("make_summary") is False and o.get("diarize_auto") is True
          and o.get("video_kind") == "meeting" and o.get("store_media") == "audio", o)
    check("категория — как у встреч", o.get("category") == media.category_for_kind("meeting"), o)
    check("карточка помнит, откуда файл", s["extra"] == {"origin": "inbox", "inbox_from": str(src)}, s["extra"])
    check("ответ — как у загрузки со страницы", res.get("rec_id") == "rec-1", res)
    check("разметка по типу и заданию нужна", media._wants_diarize(o) is True)

    say("")
    say("=== 4. Страница в сети ===")
    from fastapi.testclient import TestClient  # noqa: E402

    config.save({"lan_enabled": True, "lan_port": 8788})
    k = lan.key()
    check("ключ заведён сам, длинный", len(k) >= 10, k)
    check("ключ не уходит в настройки страницы", "lan_key" not in config.public())
    with TestClient(lan.lan_app) as cli:
        r = cli.get("/")
        check("без ключа — 403 и подсказка про QR-код", r.status_code == 403 and "QR" in r.text, r.status_code)
        r = cli.get("/?k=неправильный")
        check("с чужим ключом — 403", r.status_code == 403)
        r = cli.get("/?k=" + k)
        check("с ключом — страница для телефона", r.status_code == 200 and "Выбрать запись" in r.text
              and "viewport" in r.text, r.status_code)
        check("страница не кэшируется", r.headers.get("cache-control") == "no-store")
        r = cli.get("/ping?k=" + k)
        check("проба связи", r.status_code == 200 and r.json().get("ok") is True)
        check("проба без ключа — 403", cli.get("/ping").status_code == 403)
        SUBMITTED.clear()
        r = cli.post("/upload", data={"k": k}, files={"file": ("голос.ogg", b"OggS" * 50, "audio/ogg")})
        check("файл принят", r.status_code == 200 and r.json().get("ok") is True and r.json().get("rec_id") == "rec-1",
              (r.status_code, r.text[:200]))
        s = SUBMITTED[-1]
        check("ушёл во входящие с пометкой «с телефона»", s["name"] == "голос.ogg"
              and s["extra"] == {"origin": "phone"} and s["opts"].get("make_summary") is False, s)
        check("лежит в _uploads", s["path"].exists() and s["path"].stat().st_size == 200)
        r = cli.post("/upload", data={"k": k}, files={"file": ("заметка.txt", b"abc", "text/plain")})
        check("чужой формат — 400", r.status_code == 400 and ".txt" in r.text, (r.status_code, r.text[:120]))
        r = cli.post("/upload", data={"k": "чужой"}, files={"file": ("a.mp3", b"abc", "audio/mpeg")})
        check("файл без ключа — 403", r.status_code == 403)
        r = cli.post("/upload", data={"k": k}, files={"file": ("пусто.mp3", b"", "audio/mpeg")})
        check("пустой файл — 400", r.status_code == 400)
        check("другие адреса — 404", cli.get("/api/state?k=" + k).status_code == 404)
        k2 = lan.new_key()
        check("новый ключ другой", k2 != k and lan.key() == k2)
        check("старая ссылка закрыта", cli.get("/?k=" + k).status_code == 403
              and cli.get("/?k=" + k2).status_code == 200)

    say("")
    say("=== 5. Адрес для ссылки ===")
    order = sorted(["10.8.0.2", "192.168.1.20", "172.19.0.1", "169.254.3.3", "100.64.1.1"],
                   key=lambda ip: (lan._rank(ip), ip))
    check("домашний Wi-Fi первым, потом 10.x, 172.x, прочие, 169.254 последним",
          order == ["192.168.1.20", "10.8.0.2", "172.19.0.1", "100.64.1.1", "169.254.3.3"], order)
    ips = ["192.168.1.20", "172.19.0.1"]
    config.save({"lan_address": ""})
    check("не выбран — первый", lan.address(ips) == "192.168.1.20")
    config.save({"lan_address": "172.19.0.1"})
    check("выбран — он", lan.address(ips) == "172.19.0.1")
    config.save({"lan_address": "10.0.0.5"})
    check("выбранного больше нет — первый", lan.address(ips) == "192.168.1.20")
    check("адресов нет — пусто и ссылки нет", lan.address([]) == "" and lan.url("") == "")
    real = lan.addresses()
    check("настоящие адреса читаются, петли нет", all(not ip.startswith("127.") for ip in real), real)
    config.save({"lan_address": "", "lan_port": 8788})
    link = lan.url("192.168.1.20")
    check("ссылка с адресом, портом и ключом", link == "http://192.168.1.20:8788/?k=" + lan.key(), link)
    svg = lan.qr_svg(link)
    check("QR-код — картинка SVG", svg.startswith("<svg") and "path" in svg, svg[:60])
    st = lan.state()
    check("состояние: ссылка, код, ключ маской", st["url"].endswith(lan.key()) and st["qr_svg"]
          and st["key_hint"] and lan.key() not in st["key_hint"], {k: v for k, v in st.items() if k != "qr_svg"})
    config.save({"lan_port": 80})
    check("порт вне разрешённых — умолчание", lan.port() == 8788)
    config.save({"lan_port": "мусор"})
    check("порт мусор — умолчание", lan.port() == 8788)

    say("")
    say("=== 6. Служба на порту ===")
    import httpx  # noqa: E402

    from hagen import vpn  # noqa: E402

    p = free_port()
    config.save({"lan_enabled": False, "lan_port": p})
    check("выключена — не нужна", lan.wanted() is False and lan.apply() is False and not lan.running())
    config.save({"lan_enabled": True})
    ok = lan.apply()
    check("включили — поднялась", ok and lan.running(), lan.state().get("error"))
    check("порт слушает", vpn.listening(p))
    r = httpx.get("http://127.0.0.1:%d/?k=%s" % (p, lan.key()), timeout=5)
    check("отвечает по сети страницей", r.status_code == 200 and "Hagen" in r.text, r.status_code)
    r = httpx.get("http://127.0.0.1:%d/" % p, timeout=5)
    check("по сети без ключа — 403", r.status_code == 403)
    check("повторный apply ничего не меняет", lan.apply() is True and lan.running())
    p2 = free_port()
    config.save({"lan_port": p2})
    lan.apply()
    check("сменили порт — переехала", lan.running() and vpn.listening(p2) and not vpn.listening(p), lan.state().get("error"))
    with socket.socket() as blocker:
        blocker.bind(("0.0.0.0", free_port()))
        busy_port = blocker.getsockname()[1]
        blocker.listen(1)
        config.save({"lan_port": busy_port})
        got = lan.apply()
        st = lan.state()
        check("порт занят — не поднялась, причина видна", got is False and not lan.running()
              and "не поднялась" in st.get("error", ""), st.get("error"))
    config.save({"lan_port": p2})
    lan.apply()
    config.save({"lan_enabled": False})
    lan.apply()
    check("выключили — погасла", not lan.running() and not vpn.listening(p2))
    config.save({"lan_enabled": True, "features": {"phone": False}})
    check("возможность выключена — не поднимается", lan.apply() is False and not lan.running())
    config.save({"features": {"phone": True}})

    say("")
    say("=== 7. Маршруты службы и страница настроек ===")
    from hagen import server  # noqa: E402

    ORIGIN = {"Origin": "http://127.0.0.1:8787"}
    p3 = free_port()
    config.save({"lan_enabled": False, "lan_port": p3, "inbox_folders": [str(folder_a), str(folder_b)]})
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        r = cli.get("/api/phone/inbox")
        d = r.json()
        check("папки входящих отдаются с признаком «есть»",
              r.status_code == 200 and [f["exists"] for f in d["folders"]] == [True, False]
              and d.get("enabled") is True and isinstance(d.get("taken"), int), d)
        r = cli.get("/api/phone/lan")
        d = r.json()
        check("страница выключена — ссылки нет", r.status_code == 200 and d["enabled"] is False
              and d["url"] == "" and d["qr_svg"] == "", {k: v for k, v in d.items() if k != "qr_svg"})
        r = cli.post("/api/settings", json={"lan_enabled": True}, headers=ORIGIN)
        check("настройки сохранены, ключа в ответе нет", r.status_code == 200 and "lan_key" not in r.json())
        time.sleep(0.3)
        d = cli.get("/api/phone/lan").json()
        check("включили в настройках — служба поднялась, ссылка с портом",
              d["running"] is True and d["url"].startswith("http://") and (":%d/?k=" % p3) in d["url"]
              and d["qr_svg"].startswith("<svg"), {k: v for k, v in d.items() if k != "qr_svg"})
        before = d["url"]
        d = cli.post("/api/phone/lan/key", headers=ORIGIN).json()
        check("«Новый ключ» — другая ссылка", d["url"] != before and d["running"] is True)
        cli.post("/api/settings", json={"lan_enabled": False}, headers=ORIGIN)
        time.sleep(0.3)
        check("выключили в настройках — погасла", cli.get("/api/phone/lan").json()["running"] is False)
        r = cli.post("/api/phone/inbox/scan", headers=ORIGIN)
        check("обход по кнопке", r.status_code == 200 and isinstance(r.json().get("taken"), list))
        html = cli.get("/").text
        js = cli.get("/static/app.js").text
        check("страница телефона отдаётся и с основного адреса не нужна",
              cli.get("/static/phone.html").status_code == 200)
    check("вкладка «С телефона» за возможностью", 'data-tab="t-phone" data-feature="phone"' in html)
    check("папки входящих — той же кнопкой выбора папки, строкой в список",
          'data-for="set-inbox-folders" data-add="line"' in html)
    check("список папок с «×»", "js-inbox-del" in js and "×" in js)
    check("галочка страницы, адрес, порт, код, «Новый ключ»",
          all(x in html for x in ('id="set-lan"', 'id="set-lan-address"', 'id="set-lan-port"',
                                  'id="lan-qr"', 'id="btn-lan-key"')))
    check("возможность в списке", "key: 'phone'" in js)
    check("страница зовёт свои маршруты", "/api/phone/inbox" in js and "/api/phone/lan" in js
          and "/api/phone/lan/key" in js)
    check("поля уходят в настройки", all(x in js for x in ("inbox_folders:", "lan_enabled:", "lan_port:", "lan_address:")))
finally:
    media.submit_file = real_submit
    lan.stop()
    shutil.rmtree(tmp, ignore_errors=True)

sys.exit(finish("t112"))
