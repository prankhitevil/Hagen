# -*- coding: utf-8 -*-
"""Проверка 2: pyannote 4.x — разметка говорящих, эмбеддинги, прогресс, скорость.

Звук подаём готовым массивом (waveform), а не путём к файлу: pyannote 4.x
декодирует файлы через torchcodec, которому нужны DLL FFmpeg 4-7, а в системе
стоит FFmpeg 8 и только в виде exe. Массив обходит эту зависимость полностью.
"""
import inspect
import io
import json
import sys
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

from hagen import audio_io, config  # noqa: E402

LINES = []


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


def dump():
    io.open(PROJECT / "tests" / "t2_result.txt", "w", encoding="utf-8").write("\n".join(LINES))


def main():
    token = config.get("hf_token")
    say("токен задан: %s" % bool(token))
    if not token:
        return 1

    import numpy as np
    import torch

    torch.set_num_threads(7)
    from pyannote.audio import Pipeline
    import pyannote.audio

    say("pyannote.audio: %s | torch: %s" % (pyannote.audio.__version__, torch.__version__))

    # «разговор»: тот же файл + его сдвинутая и изменённая по тембру копия,
    # чтобы на входе было заведомо больше одного голоса
    pcm, sr = audio_io.read_wav(PROJECT / "tests" / "example.wav")
    shifted = audio_io.resample(pcm, sr, int(sr * 1.18))[: len(pcm)]
    gap = np.zeros(int(0.7 * sr), dtype=np.float32)
    multi = np.concatenate([pcm, gap, shifted, gap, pcm[: len(pcm) // 2], gap, shifted[: len(shifted) // 2]])
    audio_io.write_wav(PROJECT / "tests" / "multi.wav", multi, sr)
    dur = len(multi) / float(sr)
    say("тестовый разговор: %.2f c (склеен из двух разных тембров)" % dur)

    waveform = torch.from_numpy(np.ascontiguousarray(multi)).unsqueeze(0)
    payload = {"waveform": waveform, "sample_rate": sr}

    report = {}
    for model_id in ("pyannote/speaker-diarization-community-1", "pyannote/speaker-diarization-3.1"):
        say("")
        say("=== %s ===" % model_id)
        t0 = time.time()
        try:
            pipe = Pipeline.from_pretrained(model_id, token=token)
        except Exception as err:
            say("НЕ ЗАГРУЗИЛАСЬ: %s: %s" % (type(err).__name__, str(err)[:400]))
            report[model_id] = {"ok": False, "error": str(err)[:400]}
            continue
        if pipe is None:
            say("from_pretrained вернул None — нет доступа к модели или не принята лицензия")
            report[model_id] = {"ok": False, "error": "from_pretrained вернул None"}
            continue
        say("загружена за %.1f c" % (time.time() - t0))
        pipe.to(torch.device("cpu"))

        steps = []

        def hook(step_name, step_artefact, file=None, completed=None, total=None):
            if completed is None:
                steps.append(step_name)

        t0 = time.time()
        out = pipe.apply(payload, min_speakers=1, max_speakers=4, hook=hook)
        dt = time.time() - t0
        say("разметка: %.2f c на %.2f c аудио -> RTF=%.3f | ЧАС ЗАПИСИ = %.1f МИНУТ"
            % (dt, dur, dt / dur, dt / dur * 60.0))
        say("тип результата: %s" % type(out).__name__)
        say("шаги пайплайна (через hook): %s" % steps)

        ann = out.speaker_diarization
        excl = out.exclusive_speaker_diarization
        emb = out.speaker_embeddings

        labels = list(ann.labels())
        say("спикеров: %d %s" % (len(labels), labels))
        segs = []
        for turn, _tr, label in ann.itertracks(yield_label=True):
            segs.append({"start": round(turn.start, 2), "end": round(turn.end, 2), "speaker": label})
        say("сегментов (с наложениями): %d | без наложений: %d"
            % (len(segs), len(list(excl.itertracks()))))
        for s in segs[:14]:
            say("   %6.2f - %6.2f  %s" % (s["start"], s["end"], s["speaker"]))

        if emb is not None:
            say("ЭМБЕДДИНГИ: shape=%s dtype=%s, порядок строк = %s"
                % (tuple(emb.shape), emb.dtype, labels))
            norms = np.linalg.norm(emb, axis=1)
            say("нормы векторов: %s" % np.round(norms, 3).tolist())
            if emb.shape[0] >= 2:
                a = emb[0] / (norms[0] or 1.0)
                b = emb[1] / (norms[1] or 1.0)
                say("косинусная близость спикер1 к спикер2: %.3f" % float(np.dot(a, b)))
        else:
            say("ЭМБЕДДИНГИ: None — нужен отдельный вызов")

        report[model_id] = {
            "ok": True,
            "rtf": round(dt / dur, 3),
            "hour_minutes": round(dt / dur * 60.0, 1),
            "n_speakers": len(labels),
            "labels": labels,
            "n_segments": len(segs),
            "segments": segs,
            "embeddings_shape": list(emb.shape) if emb is not None else None,
            "steps": steps,
        }
        del pipe
        break

    io.open(PROJECT / "tests" / "t2_result.json", "w", encoding="utf-8").write(
        json.dumps(report, ensure_ascii=False, indent=2)
    )
    say("")
    say("ГОТОВО")
    return 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:
        say("СБОЙ:")
        say(traceback.format_exc())
    finally:
        dump()
    sys.exit(code)
