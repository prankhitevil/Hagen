# -*- coding: utf-8 -*-
"""Выгрузить сети разметки говорящих community-1 в ONNX.

    .venv\\Scripts\\python.exe tools\\export_diar_onnx.py
    .venv\\Scripts\\python.exe tools\\export_diar_onnx.py --wav путь\\к\\far.wav

Инструмент разработчика: нужен pyannote.audio и снимок модели
`pyannote/speaker-diarization-community-1` в `models\\hf`. Запускается один раз;
результат, папка `models\\diar\\`, лежит в репозитории, и движку разметки без
pyannote ни pyannote, ни токен, ни Hugging Face больше не нужны.

Что выгружается. У community-1 две сети и группировка:
  * сеть сегментации (SincNet + LSTM): 10 секунд звука → по каждому кадру
    вероятности семи сочетаний «кто из трёх голосов говорит» (powerset);
  * «ствол» сети отпечатков голоса (ResNet34): признаки fbank → признаки по
    кадрам. Взвешенное среднее и разброс по кадрам и последний линейный слой
    считаются в numpy — так маски на любое число голосов обходятся без
    подгонки размеров; веса этого слоя кладутся рядом, в `embedding_head.npz`;
  * PLDA для группировки VBx — два `.npz`, копируются как есть.
Всё, что движку надо знать о сетях и группировке (длина окна, сетка кадров,
таблица сочетаний, заводские параметры), пишется в `diar.json`.

После выгрузки сети сверяются с pyannote на настоящих окнах звука: ONNX и torch
получают одинаковый вход, расхождение печатается. Если оно выше порога,
инструмент завершается с ошибкой.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import date
from pathlib import Path

import numpy as np

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

# Модель читается только с диска: в сеть ходить незачем.
os.environ.setdefault("HF_HUB_OFFLINE", "1")

SOURCE = "pyannote/speaker-diarization-community-1"
HUB = PROJECT / "models" / "hf" / "hub" / "models--pyannote--speaker-diarization-community-1"
OUT = PROJECT / "models" / "diar"
DATA = PROJECT / "data"

#: Пороги сверки. Сегментация — наибольшая разница вероятностей (не их
#: логарифмов: на хвостах порядка 1e-9 логарифм раздувает ничтожную разницу);
#: отпечатки — наименьший косинус между признаками ствола по кадрам.
SEG_MAX_DIFF = 1e-3
TRUNK_MIN_COS = 0.99999


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_dir() -> Path:
    snaps = sorted((HUB / "snapshots").glob("*"))
    if not snaps:
        sys.exit("Нет снимка %s в %s — нечего выгружать." % (SOURCE, HUB))
    return snaps[-1]


def find_wav(given: str | None) -> Path:
    """Звук для сверки: заданный или самый длинный far.wav среди записей."""
    if given:
        return Path(given)
    found = sorted(DATA.glob("*/far.wav"), key=lambda p: p.stat().st_size, reverse=True)
    found += sorted(DATA.glob("*/source.wav"), key=lambda p: p.stat().st_size, reverse=True)
    if not found:
        sys.exit("Нет звука для сверки: укажите --wav.")
    return found[0]


def real_chunks(wav: Path, count: int, samples: int) -> np.ndarray:
    """Окна настоящего звука, равномерно по записи, 16 кГц моно."""
    from hagen import audio_io

    pcm, sr = audio_io.read_wav(wav)
    pcm = audio_io.resample(pcm, sr, 16000)
    if pcm.shape[0] < samples:
        pcm = np.pad(pcm, (0, samples - pcm.shape[0]))
    starts = np.linspace(0, pcm.shape[0] - samples, count).astype(int)
    return np.stack([pcm[s:s + samples] for s in starts]).astype(np.float32)


def trunk_module(resnet):
    """Обёртка для выгрузки: fbank → признаки по кадрам (без усреднения)."""
    import torch

    class Trunk(torch.nn.Module):
        def __init__(self, net):
            super().__init__()
            self.net = net

        def forward(self, fbank):
            return self.net.forward_frames(fbank)

    return Trunk(resnet).eval()


def export(onnx_model, args: tuple, path: Path, names_in: list[str], names_out: list[str],
           dynamic: dict) -> None:
    import torch

    with torch.inference_mode():
        torch.onnx.export(
            onnx_model, args, str(path),
            input_names=names_in, output_names=names_out,
            dynamic_axes=dynamic, opset_version=17, dynamo=False,
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wav", help="звук для сверки (по умолчанию — самая длинная запись)")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    import onnxruntime as ort
    import torch
    from pyannote.audio import Pipeline, __version__ as pa_version
    from pyannote.audio.utils.powerset import Powerset

    torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
    snap = snapshot_dir()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print("снимок модели:", snap.name)
    pipe = Pipeline.from_pretrained(str(snap))
    if pipe is None:
        sys.exit("pyannote не загрузил пайплайн из %s" % snap)
    pipe.to(torch.device("cpu"))

    seg_model = pipe._segmentation.model.eval()
    spec = seg_model.specifications
    sr = int(seg_model.audio.sample_rate)
    seg_samples = int(round(spec.duration * sr))
    seg_frames = int(seg_model.num_frames(seg_samples))
    rf = seg_model.receptive_field
    powerset = Powerset(len(spec.classes), spec.powerset_max_classes)
    mapping = powerset.mapping.cpu().numpy().astype(int)

    emb_model = pipe._embedding.model_.eval()
    resnet = emb_model.resnet
    hp = emb_model.hparams

    # ---- выгрузка сетей
    seg_path = out / "segmentation.onnx"
    export(seg_model, (torch.zeros(1, 1, seg_samples),), seg_path,
           ["waveform"], ["powerset"],
           {"waveform": {0: "batch"}, "powerset": {0: "batch"}})
    print("выгружено:", seg_path.name, "%.1f МБ" % (seg_path.stat().st_size / 1e6))

    # Длина fbank на примере — как у окна 10 с; ось времени всё равно переменная.
    trunk = trunk_module(resnet)
    trunk_path = out / "embedding.onnx"
    export(trunk, (torch.zeros(1, 998, int(hp.num_mel_bins)),), trunk_path,
           ["fbank"], ["frames"],
           {"fbank": {0: "batch", 1: "time"}, "frames": {0: "batch", 3: "time"}})
    print("выгружено:", trunk_path.name, "%.1f МБ" % (trunk_path.stat().st_size / 1e6))

    head = resnet.seg_1
    np.savez(out / "embedding_head.npz",
             weight=head.weight.detach().cpu().numpy().astype(np.float32),
             bias=head.bias.detach().cpu().numpy().astype(np.float32))

    for name in ("plda.npz", "xvec_transform.npz"):
        shutil.copyfile(snap / "plda" / name, out / name)

    # ---- сверка с torch на настоящем звуке
    wav = find_wav(args.wav)
    print("сверка на:", wav.parent.name + "\\" + wav.name)
    chunks = real_chunks(wav, 12, seg_samples)
    so = ort.SessionOptions()
    so.intra_op_num_threads = max(1, (os.cpu_count() or 2) - 1)
    seg_sess = ort.InferenceSession(str(seg_path), so, providers=["CPUExecutionProvider"])
    trunk_sess = ort.InferenceSession(str(trunk_path), so, providers=["CPUExecutionProvider"])

    wav_t = torch.from_numpy(chunks).unsqueeze(1)
    with torch.inference_mode():
        seg_torch = seg_model(wav_t).cpu().numpy()
    seg_onnx = seg_sess.run(None, {"waveform": chunks[:, None, :]})[0]
    seg_diff = float(np.abs(np.exp(seg_torch) - np.exp(seg_onnx)).max())
    seg_same = float(np.mean(seg_torch.argmax(-1) == seg_onnx.argmax(-1)))
    print("  сегментация: наибольшая разница вероятностей %.2e, совпадение класса "
          "по кадрам %.4f %%" % (seg_diff, 100.0 * seg_same))
    # LSTM выгружен с пачкой 1: проверяем, что пачка любого размера даёт то же
    one = np.concatenate([seg_sess.run(None, {"waveform": chunks[i:i + 1, None, :]})[0]
                          for i in range(chunks.shape[0])])
    batch_diff = float(np.abs(np.exp(one) - np.exp(seg_onnx)).max())
    seg_diff = max(seg_diff, batch_diff)
    print("  сегментация: по одному окну против пачки — разница %.2e" % batch_diff)

    with torch.inference_mode():
        fbank = emb_model.compute_fbank(wav_t)
        frames_torch = resnet.forward_frames(fbank.clone()).cpu().numpy()
    frames_onnx = trunk_sess.run(None, {"fbank": fbank.cpu().numpy()})[0]
    a = frames_torch.reshape(frames_torch.shape[0], -1)
    b = frames_onnx.reshape(frames_onnx.shape[0], -1)
    cos = (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1) + 1e-12)
    trunk_diff = float(np.abs(frames_torch - frames_onnx).max())
    print("  ствол отпечатков: наименьший косинус %.7f, наибольшая разница %.2e"
          % (float(cos.min()), trunk_diff))

    # короткий кусок — ось времени у ствола переменная
    with torch.inference_mode():
        short = emb_model.compute_fbank(wav_t[:1, :, : sr * 3])
        short_torch = resnet.forward_frames(short.clone()).cpu().numpy()
    short_onnx = trunk_sess.run(None, {"fbank": short.cpu().numpy()})[0]
    short_diff = float(np.abs(short_torch - short_onnx).max())
    print("  ствол на куске 3 с: наибольшая разница %.2e" % short_diff)

    # ---- описание для движка
    trunk_out_frames = int(frames_torch.shape[-1])
    params = pipe.parameters(instantiated=True)
    clus = params.get("clustering", {})
    info = {
        "source": SOURCE,
        "snapshot": snap.name,
        "exported": date.today().isoformat(),
        "pyannote_audio": pa_version,
        "torch": torch.__version__,
        "sample_rate": sr,
        "segmentation": {
            "file": seg_path.name,
            "duration": float(spec.duration),
            "num_samples": seg_samples,
            "num_frames": seg_frames,
            "frames": {"start": float(rf.start), "duration": float(rf.duration),
                       "step": float(rf.step)},
            "num_speakers": len(spec.classes),
            "powerset": mapping.tolist(),
            "step_ratio": float(pipe.segmentation_step),
            "min_duration_off": float(params.get("segmentation", {}).get("min_duration_off", 0.0)),
        },
        "embedding": {
            "file": trunk_path.name,
            "head": "embedding_head.npz",
            "dimension": int(emb_model.dimension),
            "min_num_samples": int(pipe._embedding.min_num_samples),
            "exclude_overlap": bool(pipe.embedding_exclude_overlap),
            "frames_per_chunk": trunk_out_frames,
            "fbank": {
                "num_mel_bins": int(hp.num_mel_bins),
                "frame_length_ms": float(hp.frame_length),
                "frame_shift_ms": float(hp.frame_shift),
                "window": str(hp.window_type),
                "dither": float(hp.dither),
                "scale": 32768.0,
                "center": "global",
            },
        },
        "clustering": {
            "method": "VBx",
            "threshold": float(clus.get("threshold", 0.6)),
            "Fa": float(clus.get("Fa", 0.07)),
            "Fb": float(clus.get("Fb", 0.8)),
            "lda_dimension": int(pipe._plda.lda_dimension),
            "min_active_ratio": 0.2,
            "metric": "cosine",
            "plda": "plda.npz",
            "transform": "xvec_transform.npz",
        },
        "files": {},
        "sources": {},
    }
    for p in sorted(out.iterdir()):
        if p.suffix in (".onnx", ".npz"):
            info["files"][p.name] = sha256(p)
    for p in (snap / "segmentation" / "pytorch_model.bin", snap / "embedding" / "pytorch_model.bin",
              snap / "plda" / "plda.npz", snap / "plda" / "xvec_transform.npz"):
        info["sources"][p.parent.name + "/" + p.name] = sha256(p)
    with open(out / "diar.json", "w", encoding="utf-8") as fh:
        json.dump(info, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print("записано:", (out / "diar.json").name)

    bad = []
    if seg_diff > SEG_MAX_DIFF:
        bad.append("сегментация расходится на %.2e" % seg_diff)
    if float(cos.min()) < TRUNK_MIN_COS:
        bad.append("ствол отпечатков: косинус %.7f" % float(cos.min()))
    if bad:
        print("ПЛОХО:", "; ".join(bad))
        return 1
    print("готово: сети совпадают с pyannote")
    return 0


if __name__ == "__main__":
    sys.exit(main())
