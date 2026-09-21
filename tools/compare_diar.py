# -*- coding: utf-8 -*-
"""Сравнить два движка разметки говорящих на настоящих записях.

    .venv\\Scripts\\python.exe tools\\compare_diar.py <запись> [<запись> …]
    .venv\\Scripts\\python.exe tools\\compare_diar.py <запись> --step 1 --max-speakers 3
    .venv\\Scripts\\python.exe tools\\compare_diar.py <запись> --deep

Запись — путь к wav или к папке записи (берётся far.wav, если его нет —
source.wav). Оба движка получают одну и ту же склейку речи — ту, что готовит
diarize.py, — и одни и те же настройки. Сравнивается:
  * согласие по кадрам — у обычной разметки и у разметки без наложений;
  * число голосов;
  * косинус отпечатков голоса у пар, сопоставленных по согласию;
  * время.
С ключом --deep дополнительно сверяются промежуточные шаги: сегментация,
отпечатки мест, группировка. Это нужно, чтобы найти, где разошлись, если
разошлись.

Инструмент разработчика: нужен pyannote.audio и снимок community-1 в
models\\hf. Токен не нужен — модель читается с диска.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
os.environ.setdefault("HF_HUB_OFFLINE", "1")

from hagen import audio_io, config, diar_onnx, diar_pyannote, diarize, vad  # noqa: E402
from diar_measure import agreement, label_mapping, speaker_grid  # noqa: E402

SR = 16000
HUB = PROJECT / "models" / "hf" / "hub" / "models--pyannote--speaker-diarization-community-1"


def wav_of(arg: str) -> Path:
    p = Path(arg)
    if p.is_dir():
        for name in ("far.wav", "source.wav"):
            if (p / name).exists():
                return p / name
        sys.exit("в папке %s нет far.wav и source.wav" % p)
    return p


def glued_speech(path: Path) -> tuple[np.ndarray, float]:
    """Склейка речи, как в diarize.diarize_pcm. Возвращает (склейка, длина записи)."""
    pcm, sr = audio_io.read_wav(path)
    arr = np.ascontiguousarray(audio_io.resample(pcm, sr, SR).astype(np.float32))
    spans = vad.speech_timestamps(arr)
    pieces, n = diarize._build_pieces(spans, arr.shape[0], SR)
    return diarize._glue(arr, pieces, n), arr.shape[0] / float(SR)


def pyannote_pipeline(threads: int):
    """community-1 через pyannote, настроенный так же, как в программе."""
    import torch
    from pyannote.audio import Pipeline

    snap = sorted((HUB / "snapshots").glob("*"))[-1]
    torch.set_num_threads(threads)
    pipe = Pipeline.from_pretrained(str(snap))
    pipe.to(torch.device("cpu"))
    diar_pyannote._remember_base(pipe)
    if config.get("fast_embeddings", True):
        diar_pyannote._speed_up_embeddings(pipe)
    diar_pyannote.use_pipeline(pipe, "pyannote/speaker-diarization-community-1")
    diar_pyannote._threads_used = threads
    return pipe


def run_engine(eng, glued: np.ndarray, step: float, limits: dict) -> tuple[float, dict]:
    """Один прогон движка — так же, как его зовёт diarize.diarize_pcm."""
    tuning = dict(diarize.tuning(), step_s=step)
    t0 = time.time()
    res = eng.run(glued, tuning=tuning, **limits)
    spent = time.time() - t0
    if res.get("centroids") is None:
        res["centroids"] = np.zeros((0, 256))
    return spent, res


def centroid_cosines(a: dict, b: dict, mapping: dict) -> list[float]:
    out = []
    for la, lb in mapping.items():
        if la not in a["labels"] or lb not in b["labels"]:
            continue
        va = np.asarray(a["centroids"][a["labels"].index(la)], dtype=np.float64)
        vb = np.asarray(b["centroids"][b["labels"].index(lb)], dtype=np.float64)
        na, nb = np.linalg.norm(va), np.linalg.norm(vb)
        if na > 0 and nb > 0:
            out.append(float(va @ vb / (na * nb)))
    return out


def deep(pipe, model, glued: np.ndarray, step: float) -> None:
    """Сверить промежуточные шаги двух движков на одной склейке."""
    import torch

    diar_pyannote.apply_tuning(pipe, dict(diarize.tuning(), step_s=step))
    file = {"waveform": torch.from_numpy(glued).unsqueeze(0), "sample_rate": SR, "uri": "x"}
    seg_p = pipe.get_segmentations(file).data
    seg_o = model.segment(glued, step, None)
    same = float(np.mean(seg_p == seg_o)) if seg_p.shape == seg_o.shape else float("nan")
    print("    сегментация: форма %s / %s, совпало %.5f %%" % (seg_p.shape, seg_o.shape, 100 * same))

    from pyannote.core import SlidingWindowFeature

    swf = SlidingWindowFeature(seg_p, pipe.get_segmentations(file).sliding_window)
    count_p = pipe.speaker_count(swf, pipe._segmentation.model.receptive_field,
                                 warm_up=(0.0, 0.0)).data[:, 0]
    count_o = model.speaker_count(seg_o, step)
    print("    подсчёт голосов: кадров %d / %d, совпало %.5f %%"
          % (count_p.shape[0], count_o.shape[0],
             100 * float(np.mean(count_p[:count_o.shape[0]] == count_o[:count_p.shape[0]]))))

    emb_p = pipe.get_embeddings(file, swf, exclude_overlap=pipe.embedding_exclude_overlap)
    emb_o = model.embeddings(glued, seg_o, step, None)
    a = emb_p.reshape(-1, emb_p.shape[-1])
    b = emb_o.reshape(-1, emb_o.shape[-1])
    ok = np.isfinite(a).all(1) & np.isfinite(b).all(1)
    cos = (a[ok] * b[ok]).sum(1) / (np.linalg.norm(a[ok], axis=1) * np.linalg.norm(b[ok], axis=1))
    print("    отпечатки мест: %d пар, наименьший косинус %.7f, средний %.7f"
          % (int(ok.sum()), float(cos.min()), float(cos.mean())))

    params = pipe.parameters(instantiated=True)["clustering"]
    hard_p, _, cent_p = pipe.clustering(embeddings=emb_p, segmentations=swf,
                                        num_clusters=None, min_clusters=1, max_clusters=np.inf)
    ratio = diar_onnx._normalize_tuning({"step_s": step}, model.chunk_s)["min_voice_s"] / model.chunk_s
    hard_o, cent_o = model.cluster(emb_o, seg_o, params["threshold"], params["Fa"], params["Fb"],
                                   ratio, None, 1, np.inf)
    print("    группировка: голосов %d / %d, мест с тем же номером %.3f %%"
          % (cent_p.shape[0], cent_o.shape[0], 100 * float(np.mean(hard_p == hard_o))))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("records", nargs="+")
    ap.add_argument("--step", type=float, default=None,
                    help="шаг окна, с (по умолчанию — как в настройках программы)")
    ap.add_argument("--min-speakers", type=int, default=None)
    ap.add_argument("--max-speakers", type=int, default=None)
    ap.add_argument("--deep", action="store_true", help="сверить промежуточные шаги")
    args = ap.parse_args()

    step = args.step or float(config.get("diarize_window_step_s") or 1.0)
    limits = {k: v for k, v in (("min_speakers", args.min_speakers),
                                ("max_speakers", args.max_speakers)) if v is not None}
    threads = diarize._threads()
    print("шаг окна %.1f с, потоков %d, ограничения %s" % (step, threads, limits or "нет"))

    # Сначала ONNX по всем записям, потом pyannote: потоки torch после работы
    # ещё какое-то время крутятся вхолостую и отнимали бы у onnxruntime
    # процессор — замер времени вышел бы нечестным.
    model = diar_onnx.load(threads)
    items = []
    for arg in args.records:
        wav = wav_of(arg)
        glued, total = glued_speech(wav)
        to, ro = run_engine(diar_onnx, glued, step, limits)
        items.append((wav.parent.name, glued, total, to, ro))
        print("ONNX: %s — %.1f с" % (wav.parent.name, to))
    pipe = pyannote_pipeline(threads)

    rows = []
    for name, glued, total, to, ro in items:
        speech = glued.shape[0] / float(SR)
        print("\n=== %s: запись %.0f с, речи %.0f с" % (name, total, speech))
        tp, rp = run_engine(diar_pyannote, glued, step, limits)
        ga, gb = speaker_grid(rp["turns"], speech), speaker_grid(ro["turns"], speech)
        ea, eb = speaker_grid(rp["exclusive_turns"], speech), speaker_grid(ro["exclusive_turns"], speech)
        agree, agree_x = agreement(ga, gb), agreement(ea, eb)
        cos = centroid_cosines(rp, ro, label_mapping(ea, eb))
        row = {"name": name, "speech": speech, "agree": agree, "agree_x": agree_x,
               "voices": (len(rp["labels"]), len(ro["labels"])),
               "cos": min(cos) if cos else float("nan"), "time": (tp, to)}
        rows.append(row)
        print("    голосов: pyannote %d, ONNX %d" % row["voices"])
        print("    согласие по кадрам: %.1f %% (без наложений %.1f %%)" % (agree, agree_x))
        print("    косинус центроидов: наименьший %.6f" % row["cos"])
        print("    время: pyannote %.1f с, ONNX %.1f с (RTF по речи %.3f / %.3f)"
              % (tp, to, tp / max(speech, 1e-6), to / max(speech, 1e-6)))
        if args.deep:
            deep(pipe, model, glued, step)

    print("\n=== Итого")
    print("%-24s %7s %9s %9s %8s %10s %9s %9s"
          % ("запись", "речь,с", "согл.,%", "без нал.", "голоса", "косинус", "pyannote", "ONNX"))
    for r in rows:
        print("%-24s %7.0f %9.1f %9.1f %8s %10.6f %8.1fс %8.1fс"
              % (r["name"], r["speech"], r["agree"], r["agree_x"], "%d/%d" % r["voices"],
                 r["cos"], r["time"][0], r["time"][1]))
    bad = [r["name"] for r in rows
           if r["agree"] < 99.0 or r["voices"][0] != r["voices"][1]
           or not (r["cos"] >= 0.999)]
    print("\nцель: согласие ≥ 99 %, то же число голосов, косинус ≥ 0,999 — "
          + ("выполнена" if not bad else "не выполнена: " + ", ".join(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
