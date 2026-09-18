# -*- coding: utf-8 -*-
"""Единый запуск проверок: один вызов вместо восьмидесяти.

Раньше каждая проверка запускалась руками, и «прогнать всё» означало восемьдесят
команд подряд. Отсюда же брались проверки, которые молча падали: никто их не
запускал.

Как устроено.
  * Каждая проверка идёт ОТДЕЛЬНЫМ процессом: они лезут в COM, грузят модели и
    поднимают службу в своём процессе, и общий процесс от этого разваливался бы.
  * Что кому нужно — в таблице NEEDS ниже, одним местом. Новая проверка, не
    попавшая в таблицу, считается лёгкой, и об этом честно пишется в конце.
  * Итог — сводка и ненулевой код выхода, если хоть одна проверка отказала.

Запуск из корня проекта:
    .venv\\Scripts\\python.exe tests\\run_all.py --fast     — без тяжёлых
    .venv\\Scripts\\python.exe tests\\run_all.py            — всё, кроме черновых
    .venv\\Scripts\\python.exe tests\\run_all.py --only t62,t71
    .venv\\Scripts\\python.exe tests\\run_all.py --list
"""
from __future__ import annotations

import argparse
import io
import os
import re
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parent
PROJECT = TESTS.parent
PYTHON = PROJECT / ".venv" / "Scripts" / "python.exe"

#: Что проверке нужно сверх обычного Python. Пусто — ничего, идёт в «быстрых».
#: Названия причин короткие: они печатаются в строке «пропущено».
NEEDS: dict[str, str] = {
    # модели распознавания и разметки
    "t2_pyannote": "модель разметки говорящих",
    "t4_onnx": "модель распознавания",
    "t8_e2e": "точная модель и разметка говорящих",
    "t33_asr_cloud": "облачное распознавание и ключ",
    "t34_english": "английская модель",
    "t41_window_step": "модель разметки говорящих",
    "t53_speakers": "модель разметки говорящих",
    "t56_dictate": "модель распознавания и клавиатура",
    "t57_precise_idle": "точная модель",
    "t58_diarize_tuning": "модель разметки говорящих",
    "t68_for_tester": "сборка папки целиком",
    "t9_voices": "модели распознавания и разметки говорящих",
    "t44_claude_limit": "Claude CLI",
    # звук, устройства Windows, окна
    "t6_loopback_capture": "звуковые устройства",
    "t23_loopback_real": "звуковые устройства",
    "t42_capture_watchdog": "звуковые устройства",
    "t45_tray": "значок у часов",
    "t46_window_tray": "окно и значок у часов",
    "t50_call_device": "звуковые устройства",
    "t55_portable": "сборка папки целиком",
    # t73 стояла здесь как «звуковые устройства», но звук ей не нужен вовсе:
    # она судит об эхе по форме устройства и по названию. Список устройств
    # Windows отдаёт и в тишине, а раздел про форму написан терпимо.
    "t75_mic_pill": "окна Windows",
    "t86_splash": "окна Windows",
    # браузер и сеть
    "t15_display_audio": "звук в колонках — играет вслух",
    "t26_live_service": "звук в колонках — играет вслух, живое распознавание",
    "t29_final_bisect": "живая служба",
    "t31_ui_states": "окно WebView2",
    "t35_video_pipeline": "ffmpeg и видео",
    "t43_call_automation": "браузер",
    "t48_sharepoint": "сеть и вход в SharePoint",
    "t49_ui_smoke": "браузер",
    "t14_outlook": "Outlook на этой машине",
}

#: Черновые сценарии: они ничего не утверждают и не возвращают код выхода —
#: это следы разбирательств, а не проверки, и в общий прогон не идут.
#: Сейчас таких нет: последние двое удалены 18.09 вместе с функцией, которую
#: звали только они. Новый черновик — сюда, пока он не стал проверкой.
DRAFTS: set[str] = set()


def say(msg: str = "") -> None:
    """Печать, переживающая консоль cp1251: там нет ни стрелок, ни тире."""
    text = str(msg)
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", "replace").decode("ascii"), flush=True)


def number(name: str) -> tuple[int, str]:
    """«t7_contracts» → (7, '_contracts'): сначала по числу, потом по остатку."""
    m = re.match(r"t(\d+)(.*)", name)
    return (int(m.group(1)), m.group(2)) if m else (9999, name)


def all_tests() -> list[str]:
    return sorted((p.stem for p in TESTS.glob("t*.py") if p.stem != "run_all"), key=number)


#: Код, с которым Windows валит процесс при обращении по чужому адресу. У нас
#: это случается НА ВЫХОДЕ из Python и к итогу проверки отношения не имеет.
CRASH_ON_EXIT = 3221225477


#: Шум, который библиотеки печатают при закрытии и на который смотреть незачем.
#: Без отсева он занимает весь хвост, и вердикт проверки в него не помещается —
#: на этом я один раз потерял причину отказа (17.09).
NOISE = ("nanobind:", " - leaked ", "StarletteDeprecationWarning",
         "from starlette.testclient import", "filtered by duration:",
         "See https://nanobind")


def useful_tail(out: str, lines: int = 14) -> list[str]:
    """Последние строки вывода без библиотечного шума."""
    rows = [r for r in (out or "").strip().splitlines()
            if not any(mark in r for mark in NOISE)]
    return rows[-lines:]


def run_one(stem: str, timeout: float) -> dict:
    started = time.time()
    try:
        # Проверки печатают по-русски; чтобы отчёт не превратился в кракозябры,
        # просим их выводить UTF-8 и так же его читаем — консоль тут ни при чём.
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        proc = subprocess.run([str(PYTHON), str(TESTS / (stem + ".py"))],
                              cwd=str(PROJECT), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env=env)
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as err:
        code = 124
        out = (err.stdout or "") + "\nне уложилась в %.0f с" % timeout
    return {"name": stem, "code": code, "seconds": round(time.time() - started, 1),
            "tail": "\n".join(useful_tail(out))}


def main() -> int:
    ap = argparse.ArgumentParser(description="Единый запуск проверок Hagen")
    ap.add_argument("--fast", action="store_true",
                    help="только те, которым не нужны модели, звук, браузер и сеть")
    ap.add_argument("--only", default="", help="через запятую: t62,t71 или t62_transcript_move")
    ap.add_argument("--list", action="store_true", help="показать список и что кому нужно")
    ap.add_argument("--drafts", action="store_true", help="гонять и черновые сценарии")
    ap.add_argument("--timeout", type=float, default=900.0, help="предел на одну проверку, с")
    args = ap.parse_args()

    names = all_tests()
    if args.only:
        want = [w.strip() for w in args.only.split(",") if w.strip()]
        names = [n for n in names if any(n == w or n.startswith(w + "_") for w in want)]
        missing = [w for w in want if not any(n == w or n.startswith(w + "_") for n in names)]
        if missing:
            say("не нашёл: " + ", ".join(missing))
            return 2

    if args.list:
        for n in names:
            why = NEEDS.get(n, "")
            kind = "черновой" if n in DRAFTS else ("нужно: " + why if why else "быстрая")
            say("%-28s %s" % (n, kind))
        say("\nвсего: %d" % len(names))
        return 0

    if not PYTHON.exists():
        say("не найден %s — запускайте из корня проекта" % PYTHON)
        return 2

    done: list[dict] = []
    skipped: list[tuple[str, str]] = []
    t0 = time.time()
    for n in names:
        if n in DRAFTS and not args.drafts:
            skipped.append((n, "черновой сценарий без итога"))
            continue
        if args.fast and n in NEEDS:
            skipped.append((n, NEEDS[n]))
            continue
        say("-> %s" % n)
        row = run_one(n, args.timeout)
        # 0xC0000005 на ВЫХОДЕ из Python — беда родной библиотеки (soxr через
        # nanobind), а не отказ проверки: все её строки к этому моменту уже
        # напечатаны как «ok». Сама проверка при этом честно прошла, поэтому
        # даём второй заход и говорим о падении вслух, а не прячем его.
        if row["code"] == CRASH_ON_EXIT:
            say("   упала на выходе (0xC0000005) — повторяю")
            again = run_one(n, args.timeout)
            again["seconds"] = round(row["seconds"] + again["seconds"], 1)
            again["flaky"] = True
            row = again
        done.append(row)
        say("   %s  %.1f с" % ("ok" if row["code"] == 0 else "ОТКАЗ", row["seconds"]))

    bad = [r for r in done if r["code"] != 0]
    say("\n" + "=" * 62)
    for name, why in skipped:
        say("пропущено: %-26s %s" % (name, why))
    if skipped:
        say("-" * 62)
    for r in sorted(done, key=lambda x: -x["seconds"])[:5]:
        say("дольше всех: %-26s %.1f с" % (r["name"], r["seconds"]))
    say("-" * 62)
    say("прошло: %d   отказ: %d   пропущено: %d   время: %.0f с"
        % (len(done) - len(bad), len(bad), len(skipped), time.time() - t0))
    flaky = [r["name"] for r in done if r.get("flaky")]
    if flaky:
        say("падали на выходе и прошли со второго раза: " + ", ".join(flaky))
    unknown = [n for n in names if n not in NEEDS and n not in DRAFTS
               and n not in {r["name"] for r in done}]
    if unknown:
        say("не запускались и не описаны: " + ", ".join(unknown))
    for r in bad:
        say("\n--- %s (код %d) ---\n%s" % (r["name"], r["code"], r["tail"]))

    lines = ["прошло: %d, отказ: %d, пропущено: %d, время: %.0f с"
             % (len(done) - len(bad), len(bad), len(skipped), time.time() - t0)]
    lines += ["%-28s %-6s %.1f с" % (r["name"], "ok" if r["code"] == 0 else "ОТКАЗ", r["seconds"])
              for r in done]
    lines += ["%-28s пропущено: %s" % (n, why) for n, why in skipped]
    io.open(TESTS / "_run_all_result.txt", "w", encoding="utf-8").write("\n".join(lines))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
