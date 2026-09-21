# -*- coding: utf-8 -*-
"""Собирает тестовое «совещание» из трёх акустически разных голосов.

Голос 1 — синтез Windows (Irina) без изменений.
Голос 2 — тот же синтез со сдвинутым вниз тембром (другие форманты).
Голос 3 — готовый синтетический образец example.wav.
Реплики подобраны так, чтобы в протоколе были задачи, ответственные и сроки.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
TMP = PROJECT / "tests" / "meeting_parts"
TMP.mkdir(parents=True, exist_ok=True)

NO_WINDOW = 0x08000000

# (кто, текст, сдвиг тембра: 1.0 = без изменений, меньше 1 = ниже голос)
LINES = [
    ("A", "Коллеги, давайте зафиксируем сроки по проекту Hagen.", 1.0),
    ("B", "Я подготовлю отчёт по закупкам до пятницы.", 0.80),
    ("A", "Мне нужно проверить цифры в смете. Сделаю к среде.", 1.0),
    ("B", "Тогда я соберу общий файл сразу после этого.", 0.80),
    ("A", "Открытый вопрос: подрядчик ещё не подтвердил поставку оборудования.", 1.0),
    ("B", "Я напишу подрядчику сегодня и пришлю ответ в общий чат.", 0.80),
    ("A", "Принято. Следующая встреча во вторник в одиннадцать.", 1.0),
]


def synth(text: str, out: Path) -> bool:
    """Синтез речи через System.Speech. Текст передаём файлом, иначе ломается кириллица."""
    txt_file = TMP / (out.stem + ".txt")
    with io.open(txt_file, "w", encoding="utf-8") as fh:
        fh.write(text)
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$t = Get-Content -LiteralPath '%s' -Encoding UTF8 -Raw; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { $s.SelectVoice('Microsoft Irina Desktop') } catch {}; "
        "$s.Rate = 0; "
        "$s.SetOutputToWaveFile('%s'); "
        "$s.Speak($t); $s.Dispose()"
    ) % (str(txt_file), str(out))
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=120, creationflags=NO_WINDOW,
    )
    if r.returncode != 0:
        print("синтез не удался:", (r.stderr or "")[:300])
        return False
    return out.exists() and out.stat().st_size > 1000


def shift(src: Path, dst: Path, factor: float) -> bool:
    """Сдвиг тембра без изменения темпа: меняем частоту дискретизации и возвращаем темп."""
    af = "asetrate=16000*%.3f,aresample=16000,atempo=%.4f" % (factor, 1.0 / factor)
    r = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-i", str(src), "-af", af, "-ac", "1", "-ar", "16000", str(dst)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=180, creationflags=NO_WINDOW,
    )
    if r.returncode != 0:
        print("сдвиг тембра не удался:", (r.stderr or "")[:300])
        return False
    return dst.exists()


def main() -> int:
    import numpy as np

    from hagen import audio_io

    pieces = []
    plan = []
    gap = np.zeros(int(0.55 * 16000), dtype=np.float32)

    for i, (who, text, factor) in enumerate(LINES):
        raw = TMP / ("line%02d_raw.wav" % i)
        if not synth(text, raw):
            print("прерываю: синтез недоступен")
            return 1
        # приводим к 16 кГц моно
        norm = TMP / ("line%02d_16k.wav" % i)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
             "-i", str(raw), "-ac", "1", "-ar", "16000", str(norm)],
            capture_output=True, timeout=120, creationflags=NO_WINDOW,
        )
        use = norm
        if abs(factor - 1.0) > 0.01:
            shifted = TMP / ("line%02d_shift.wav" % i)
            if shift(norm, shifted, factor):
                use = shifted
        pcm, sr = audio_io.read_wav(use)
        # нормируем громкость, чтобы все голоса были слышны одинаково
        peak = float(np.max(np.abs(pcm))) or 1.0
        pcm = (pcm / peak * 0.55).astype(np.float32)
        start = sum(len(p) for p in pieces) / 16000.0
        pieces.append(pcm)
        pieces.append(gap)
        plan.append({"who": who, "text": text, "start": round(start, 2),
                     "end": round(start + len(pcm) / 16000.0, 2)})
        print("  %s  %5.1f c  %s" % (who, len(pcm) / 16000.0, text[:52]))

    # третий голос — готовый синтетический образец
    human, _sr = audio_io.read_wav(PROJECT / "tests" / "example.wav")
    peak = float(np.max(np.abs(human))) or 1.0
    human = (human / peak * 0.5).astype(np.float32)
    start = sum(len(p) for p in pieces) / 16000.0
    pieces.append(human)
    plan.append({"who": "C (образец)", "text": "(чтение Пушкина)",
                 "start": round(start, 2), "end": round(start + len(human) / 16000.0, 2)})
    print("  C  %5.1f c  готовый образец" % (len(human) / 16000.0))

    out = np.concatenate(pieces)
    dst = PROJECT / "tests" / "meeting.wav"
    audio_io.write_wav(dst, out)
    with io.open(PROJECT / "tests" / "meeting_plan.json", "w", encoding="utf-8") as fh:
        json.dump({"lines": plan, "duration_s": round(len(out) / 16000.0, 2)},
                  fh, ensure_ascii=False, indent=2)

    # версия в mp4, чтобы проверить перетаскивание видеофайла
    mp4 = PROJECT / "tests" / "meeting_video.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", "color=c=navy:s=320x240:r=2",
         "-i", str(dst), "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
         "-c:a", "aac", str(mp4)],
        capture_output=True, timeout=300, creationflags=NO_WINDOW,
    )

    print("")
    print("готово: %s, длительность %.1f c" % (dst.name, len(out) / 16000.0))
    print("видеоверсия: %s (%s)" % (mp4.name, "есть" if mp4.exists() else "не собралась"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
