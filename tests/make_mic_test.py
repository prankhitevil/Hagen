# -*- coding: utf-8 -*-
"""Собирает файл для проверки правила пауз.

Структура специально такая:
  фраза 1 — три куска с внутренними паузами по 0,8 с (фразу рвать НЕЛЬЗЯ);
  пауза 3,0 с — здесь фраза 1 обязана закрепиться;
  фраза 2 — короткая;
  пауза 0,6 с (меньше порога) и сразу фраза 3 — на ней будет нажат «Стоп»,
  фраза 3 обязана дописаться, а не пропасть.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
TMP = PROJECT / "tests" / "mic_parts"
TMP.mkdir(parents=True, exist_ok=True)
NO_WINDOW = 0x08000000

CHUNKS = [
    ("speech", "Коллеги, давайте зафиксируем сроки"),
    ("gap", 0.8),
    ("speech", "по проекту Hagen"),
    ("gap", 0.8),
    ("speech", "до конца этой недели."),
    ("gap", 3.0),
    ("speech", "Второе. Подрядчик пока не подтвердил поставку оборудования."),
    ("gap", 0.6),
    ("speech", "И третье, самое важное: бюджет согласован полностью."),
]


def synth(text: str, out: Path) -> bool:
    txt = TMP / (out.stem + ".txt")
    with io.open(txt, "w", encoding="utf-8") as fh:
        fh.write(text)
    ps = (
        "Add-Type -AssemblyName System.Speech; "
        "$t = Get-Content -LiteralPath '%s' -Encoding UTF8 -Raw; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "try { $s.SelectVoice('Microsoft Irina Desktop') } catch {}; "
        "$s.Rate = 0; $s.SetOutputToWaveFile('%s'); $s.Speak($t); $s.Dispose()"
    ) % (str(txt), str(out))
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120, creationflags=NO_WINDOW)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 1000


def main() -> int:
    import numpy as np

    from hagen import audio_io

    rng = np.random.RandomState(7)
    pieces = []
    plan = []
    # лёгкий фоновый шум, чтобы проверить адаптивный порог, а не идеальную тишину
    def silence(sec: float):
        n = int(sec * 16000)
        return (rng.randn(n) * 0.0012).astype(np.float32)

    # небольшая тишина в начале
    pieces.append(silence(0.8))

    idx = 0
    for kind, value in CHUNKS:
        at = sum(len(p) for p in pieces) / 16000.0
        if kind == "gap":
            pieces.append(silence(float(value)))
            plan.append({"type": "пауза", "seconds": value, "at": round(at, 2)})
            print("  пауза %.1f с  на %.1f с" % (value, at))
            continue
        raw = TMP / ("c%02d.wav" % idx)
        norm = TMP / ("c%02d_16k.wav" % idx)
        idx += 1
        if not synth(value, raw):
            print("синтез не удался")
            return 1
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-i", str(raw), "-ac", "1", "-ar", "16000", str(norm)],
                       capture_output=True, timeout=120, creationflags=NO_WINDOW)
        pcm, _sr = audio_io.read_wav(norm)
        peak = float(np.max(np.abs(pcm))) or 1.0
        pcm = (pcm / peak * 0.6).astype(np.float32)
        pieces.append(pcm)
        plan.append({"type": "речь", "text": value, "at": round(at, 2),
                     "seconds": round(len(pcm) / 16000.0, 2)})
        print("  речь  %5.1f с  на %.1f с  %s" % (len(pcm) / 16000.0, at, value[:45]))

    # Хвост: «Стоп» будет нажат почти сразу после последней фразы
    pieces.append(silence(0.4))
    out = np.concatenate(pieces)
    dst = PROJECT / "tests" / "mic_test.wav"
    audio_io.write_wav(dst, out)
    with io.open(PROJECT / "tests" / "mic_test_plan.json", "w", encoding="utf-8") as fh:
        json.dump({"plan": plan, "duration_s": round(len(out) / 16000.0, 2)},
                  fh, ensure_ascii=False, indent=2)
    print("")
    print("готово: %s, %.1f с" % (dst.name, len(out) / 16000.0))
    print("ожидаем: фраза 1 (три куска, пауза внутри 0,8 с — НЕ делить),")
    print("         фраза 2, фраза 3 (дописывается после «Стоп»)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
