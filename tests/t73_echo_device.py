# -*- coding: utf-8 -*-
"""Проверка 73: предупреждение «звук идёт в динамики — будет эхо» (17.09).

17.09 звонок слушали через встроенные динамики вместо Jabra, и
половина чужих фраз попала в стенограмму как его. Ни программа, ни журнал об
этом заранее не говорили.

Судим по форме устройства, которую знает сама Windows
(PKEY_AudioEndpoint_FormFactor), а не по названию: «Динамики (Jabra SPEAK
510)» — это спикерфон со своим подавлением эха, а «Динамики (Realtek)» —
колонки ноутбука. Название остаётся запасным признаком.

Что проверяем:
  1. колонки, линейный выход и звук по HDMI — опасны;
  2. наушники, гарнитура, трубка — нет;
  3. спикерфоны узнаются по названию, даже когда форма «колонки»;
  4. без формы судим по названию, а непонятное не пугаем зря;
  5. форма читается на этой машине (если Windows её отдаёт);
  6. список устройств несёт признак, страница показывает предупреждение;
  7. в журнал при старте записи попадает предупреждение.

База голосов и настройки — временные (isolate).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t73_echo_device.py
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen.platform.windows import loopback  # noqa: E402

try:
    say("=== 1. Форма устройства решает ===")
    cases_risky = [
        ("Динамики (Realtek(R) Audio)", 1, "колонки ноутбука"),
        ("Speakers (High Definition Audio)", 1, "колонки"),
        ("Динамики (2- USB Audio)", 2, "линейный выход"),
        ("Dell S2417DG (HD Audio Driver for Display Audio)", 9, "звук по HDMI"),
        ("Цифровой выход (Realtek)", 8, "S/PDIF"),
    ]
    for name, form, why in cases_risky:
        r = loopback.echo_risk(name, form)
        check("опасно: %s" % why, r["risk"] is True and "динамики" in r["why"], (name, form, r))

    say("")
    say("=== 2. Наушники и гарнитуры ===")
    cases_safe = [
        ("Наушники (Realtek(R) Audio)", 3),
        ("Headset Earphone (Bluetooth)", 5),
        ("Handset (USB Phone)", 6),
    ]
    for name, form in cases_safe:
        r = loopback.echo_risk(name, form)
        check("безопасно: %s" % name, r["risk"] is False, (name, form, r))

    say("")
    say("=== 3. Спикерфоны узнаются по названию ===")
    for name in ("Динамики (Jabra SPEAK 510 USB)", "Echo Cancelling Speakerphone (Poly Sync 20)",
                 "Yealink CP900", "Konftel Ego"):
        r = loopback.echo_risk(name, 1)      # Windows считает их колонками
        check("спикерфон не пугает: %s" % name[:28], r["risk"] is False, (name, r))

    say("")
    say("=== 4. Формы нет — судим по названию ===")
    check("динамики по названию", loopback.echo_risk("Динамики (какие-то)")["risk"] is True)
    check("колонки по названию", loopback.echo_risk("Внешние колонки")["risk"] is True)
    check("наушники по названию", loopback.echo_risk("Наушники Sony")["risk"] is False)
    check("непонятное зря не пугает", loopback.echo_risk("HDMI Output 2")["risk"] is False,
          loopback.echo_risk("HDMI Output 2"))
    check("пустое имя не ломает", loopback.echo_risk("")["risk"] is False)

    say("")
    say("=== 5. Чтение формы на этой машине ===")
    forms = loopback._form_factors()
    say("   найдено устройств вывода: %d" % len(forms))
    check("формы читаются или честно пусты", isinstance(forms, dict))
    if forms:
        check("формы — числа", all(isinstance(v, int) for v in forms.values()), forms)
        for nm, form in forms.items():
            say("   %-58s форма %s → %s" % (nm[:58], form,
                                            "эхо" if loopback.echo_risk(nm, form)["risk"] else "ок"))

    say("")
    say("=== 6. Список устройств и страница ===")
    devs = loopback.list_devices()
    check("у каждого устройства есть признак эха",
          all("echo_risk" in d and "echo_why" in d for d in devs) if devs else True,
          [d.get("name") for d in devs][:4])
    js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
    html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
    check("предупреждение есть на странице", 'id="echo-warning"' in html)
    check("в нём сказано про двойники", "двойники" in html)
    check("сказано, что программа их чинит сама", "программно исправляется" in html)
    check("предлагается выход", "наушников" in html and "спикерфон" in html)
    check("оно перерисовывается при смене устройства", js.count("paintEchoWarning()") >= 3,
          js.count("paintEchoWarning()"))
    check("галочка «писать собеседников» учитывается", "chk-far').checked" in js)

    say("")
    say("=== 7. Журнал записи ===")
    live = io.open(PROJECT / "hagen" / "live.py", encoding="utf-8").read()
    check("при старте записи пишем предупреждение",
          "микрофон услышит собеседников, будут двойники" in live)
    check("неудача проверки не роняет запись",
          "проверка устройства на эхо не удалась" in live)

except Exception as err:                       # noqa: BLE001
    FAIL.append("проверка оборвалась")
    say("ОБОРВАЛОСЬ: %s: %s" % (type(err).__name__, err))

sys.exit(finish("t73"))
