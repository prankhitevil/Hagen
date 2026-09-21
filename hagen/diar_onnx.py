# -*- coding: utf-8 -*-
"""Разметка говорящих без pyannote: сети community-1 в ONNX и группировка VBx.

Модель та же, что у pyannote `speaker-diarization-community-1`: те же две сети
(выгружены в ONNX инструментом tools/export_diar_onnx.py и сверены с
оригиналом), та же группировка VBx с тем же PLDA и теми же параметрами.
Меняется только исполнитель: onnxruntime и numpy вместо torch и pyannote.
Поэтому этому движку не нужны ни токен, ни Hugging Face, ни сам pyannote, а
отпечатки голоса совпадают с прежними — база голосов и пороги отсева эха общие
для обоих движков.

Конвейер повторяет SpeakerDiarization из pyannote.audio 4.0 шаг в шаг:
  1. окна по 10 с → сеть сегментации → по каждому кадру «кто из трёх мест
     говорит»;
  2. сколько голосов звучит в каждый момент — склейка окон;
  3. отпечаток голоса на каждое место в каждом окне: ствол сети считается один
     раз на окно, маски мест подаются в усреднение по кадрам;
  4. группировка мест в людей: AHC → VBx → распределение с ограничением
     «два места одного окна — два разных человека»;
  5. сборка разметки по подсчёту из шага 2 и перевод в интервалы.

Работает на склейке речи, которую готовит diarize.py; времена в ответе — в
шкале этой склейки.

Перенесённые куски pyannote.audio (MIT License, Copyright (c) 2020- CNRS,
Copyright (c) 2025- pyannoteAI): Inference.slide и aggregate,
SpeakerDiarization.get_embeddings и reconstruct, speaker_count,
to_diarization, Binarize, BaseClustering.filter_embeddings и
constrained_argmax, VBxClustering, StatsPool; полный текст лицензии MIT — в
THIRD-PARTY-NOTICES.md. Fbank повторяет torchaudio.compliance.kaldi.fbank
(BSD-2-Clause) с настройками модели.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import config

log = logging.getLogger("hagen.diar_onnx")

NAME = "onnx"
NEEDS_TOKEN = False

MODEL_DIR = config.MODELS_DIR / "diar"
FILES = ("diar.json", "segmentation.onnx", "embedding.onnx", "embedding_head.npz",
         "plda.npz", "xvec_transform.npz")

SR = 16000
#: Замеренный RTF по ДЛИТЕЛЬНОСТИ РЕЧИ при шаге окна 2 с: 21.09 на четырёх
#: встречах 0,16–0,20 (Ryzen 5 5600, 9 потоков). Накладные расходы — загрузка
#: сетей, поиск речи, сборка.
RTF = 0.18
OVERHEAD_S = 5.0
#: Окон за один проход сети. Больше — быстрее, но памяти на отпечатки уходит
#: около 1,3 МБ на окно.
BATCH = 16
#: Пол для логарифма в fbank — машинный эпсилон float32, как у Kaldi.
_EPS32 = float(np.finfo(np.float32).eps)

_model: "_Model | None" = None
_model_lock = threading.Lock()
_threads_used = 4


# ---------------------------------------------------------------- готовность


def ready() -> tuple[bool, str]:
    """Готов ли движок. Возвращает (готово, причина-по-русски)."""
    missing = [name for name in FILES if not (MODEL_DIR / name).exists()]
    if missing:
        return False, ("Нет файлов модели разметки в %s: %s. Они лежат в "
                       "репозитории — возможно, папка models скопирована не целиком."
                       % (MODEL_DIR, ", ".join(missing)))
    try:
        import importlib.util

        if importlib.util.find_spec("onnxruntime") is None:
            return False, "Не установлен пакет onnxruntime."
    except Exception:
        pass
    return True, "Модель community-1 (ONNX), токен не нужен."


def model_name() -> str:
    return "community-1 (ONNX)"


def load(threads: int = 4, progress: Callable | None = None) -> "_Model":
    """Загрузить модель один раз на процесс. progress здесь не нужен: грузится
    меньше секунды; параметр — для одинаковой двери у движков."""
    global _model, _threads_used
    with _model_lock:
        if _model is None:
            ok, reason = ready()
            if not ok:
                raise RuntimeError(reason)
            _threads_used = max(1, int(threads))
            _model = _Model(MODEL_DIR, _threads_used)
        return _model


def run(pcm: np.ndarray, min_speakers: int | None = None, max_speakers: int | None = None,
        tuning: dict | None = None, hook: Callable | None = None) -> dict[str, Any]:
    """Разметить склейку речи. Возвращает turns, exclusive_turns, labels,
    centroids — в шкале склейки."""
    return load(_threads_used).run(pcm, min_speakers, max_speakers, tuning, hook)


def embed(wave: np.ndarray) -> np.ndarray | None:
    """Отпечаток всего куска звука без масок."""
    return load(_threads_used).embed(wave)


def tuning_defaults() -> dict[str, float]:
    """Заводские значения ручек разметки — как у community-1."""
    info = _read_info(MODEL_DIR)
    seg = info["segmentation"]
    clu = info["clustering"]
    return {
        "step_s": float(seg["step_ratio"]) * float(seg["duration"]),
        "threshold": float(clu["threshold"]),
        "Fa": float(clu["Fa"]),
        "Fb": float(clu["Fb"]),
        "min_voice_s": float(clu["min_active_ratio"]) * float(seg["duration"]),
    }


def _read_info(folder: Path) -> dict[str, Any]:
    with open(folder / "diar.json", "r", encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- модель


def _mel_banks(num_bins: int, nfft: int, sr: int, low: float = 20.0,
               high: float = 0.0) -> np.ndarray:
    """Треугольные мел-фильтры Kaldi, (nfft // 2 + 1, num_bins)."""
    nyquist = 0.5 * sr
    if high <= 0.0:
        high += nyquist
    width = sr / nfft

    def mel(f):
        return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)

    lo, hi = mel(low), mel(high)
    delta = (hi - lo) / (num_bins + 1)
    b = np.arange(num_bins, dtype=np.float64)[:, None]
    left, center, right = lo + b * delta, lo + (b + 1.0) * delta, lo + (b + 2.0) * delta
    m = mel(width * np.arange(nfft // 2, dtype=np.float64))[None, :]
    up = (m - left) / (center - left)
    down = (right - m) / (right - center)
    banks = np.maximum(0.0, np.minimum(up, down))
    banks = np.pad(banks, ((0, 0), (0, 1)))          # последний столбец — нули
    return banks.T


def _nearest_index(src: int, dst: int) -> np.ndarray:
    """Какой кадр маски брать для каждого кадра ствола — как F.interpolate(nearest).

    Считается во float32, как у torch: на границе целого иначе мог бы уехать
    один кадр.
    """
    if src == dst:
        return np.arange(dst)
    scale = np.float32(src) / np.float32(dst)
    idx = np.floor(np.arange(dst, dtype=np.float32) * scale).astype(np.int64)
    return np.minimum(idx, src - 1)


class _Model:
    """Сети, PLDA и всё, что о них надо знать. Состояния между вызовами нет."""

    def __init__(self, folder: Path, threads: int):
        import onnxruntime as ort

        from .vbx import PLDA

        self.info = _read_info(folder)
        seg = self.info["segmentation"]
        emb = self.info["embedding"]
        clu = self.info["clustering"]

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        prov = ["CPUExecutionProvider"]
        self.seg = ort.InferenceSession(str(folder / seg["file"]), opts, providers=prov)
        self.trunk = ort.InferenceSession(str(folder / emb["file"]), opts, providers=prov)

        head = np.load(folder / emb["head"])
        self.head_w = head["weight"].astype(np.float64)      # (256, 5120)
        self.head_b = head["bias"].astype(np.float64)
        self.plda = PLDA(folder / clu["transform"], folder / clu["plda"],
                         int(clu["lda_dimension"]))

        self.chunk_s = float(seg["duration"])
        self.chunk_n = int(seg["num_samples"])
        self.chunk_frames = int(seg["num_frames"])
        fr = seg["frames"]
        self.frame_dur = float(fr["duration"])
        self.frame_step = float(fr["step"])
        self.mapping = np.asarray(seg["powerset"], dtype=np.float32)   # (7, 3)
        self.min_duration_off = float(seg.get("min_duration_off", 0.0))

        self.dimension = int(emb["dimension"])
        self.min_num_samples = int(emb["min_num_samples"])
        self.exclude_overlap = bool(emb["exclude_overlap"])
        fb = emb["fbank"]
        self.win = int(SR * fb["frame_length_ms"] * 0.001)
        self.shift = int(SR * fb["frame_shift_ms"] * 0.001)
        self.nfft = 1 << (self.win - 1).bit_length()
        self.scale = float(fb["scale"])
        self.window = np.hamming(self.win)
        self.mel = _mel_banks(int(fb["num_mel_bins"]), self.nfft, SR)

    # ---------------------------------------------------------------- признаки

    def fbank(self, wave: np.ndarray) -> np.ndarray:
        """Звук [-1, 1] → (кадры, 80) log-mel по Kaldi, центрированные по времени."""
        x = np.asarray(wave, dtype=np.float64) * self.scale
        n = x.shape[0]
        if n < self.win:
            return np.zeros((0, self.mel.shape[1]), dtype=np.float32)
        m = 1 + (n - self.win) // self.shift
        idx = np.arange(self.win)[None, :] + self.shift * np.arange(m)[:, None]
        frames = x[idx]
        frames = frames - frames.mean(axis=1, keepdims=True)          # remove_dc_offset
        prev = np.concatenate([frames[:, :1], frames[:, :-1]], axis=1)
        frames = (frames - 0.97 * prev) * self.window                 # предыскажение, окно
        spec = np.abs(np.fft.rfft(frames, n=self.nfft, axis=1)) ** 2
        mel = np.log(np.maximum(spec @ self.mel, _EPS32))
        mel = mel - mel.mean(axis=0, keepdims=True)
        return mel.astype(np.float32)

    def frames_of(self, fbanks: np.ndarray) -> np.ndarray:
        """(B, T, 80) → (B, 2560, T') признаки ствола по кадрам."""
        out = self.trunk.run(None, {"fbank": np.ascontiguousarray(fbanks, dtype=np.float32)})[0]
        b, c, f, t = out.shape
        return out.reshape(b, c * f, t).astype(np.float64)

    def pool(self, feats: np.ndarray, weights: np.ndarray | None) -> np.ndarray:
        """Взвешенные среднее и разброс по кадрам → линейный слой.

        feats (B, D, T); weights (B, S, F) или None. Ответ (B, S, 256) или
        (B, 256). Повторяет StatsPool: маска приводится к числу кадров ствола
        ближайшим соседом, разброс несмещённый.
        """
        if weights is None:
            mean = feats.mean(axis=-1)
            std = feats.std(axis=-1, ddof=1)
            stats = np.concatenate([mean, std], axis=-1)
            return stats @ self.head_w.T + self.head_b
        w = np.asarray(weights, dtype=np.float64)
        w = w[:, :, _nearest_index(w.shape[-1], feats.shape[-1])]      # (B, S, T)
        wt = np.transpose(w, (0, 2, 1))                                 # (B, T, S)
        v1 = w.sum(axis=2)[:, None, :] + 1e-8                           # (B, 1, S)
        xw = feats @ wt                                                 # (B, D, S)
        mean = xw / v1
        # sum w (x - mean)^2 через суммы: в float64 потеря точности ничтожна,
        # а матричное умножение в десятки раз быстрее поэлементного
        dx2 = (feats * feats) @ wt - 2.0 * mean * xw + mean * mean * (v1 - 1e-8)
        v2 = np.square(w).sum(axis=2)[:, None, :]
        var = np.maximum(dx2, 0.0) / (v1 - v2 / v1 + 1e-8)
        stats = np.concatenate([mean, np.sqrt(var)], axis=1)            # (B, 2D, S)
        return np.transpose(stats, (0, 2, 1)) @ self.head_w.T + self.head_b

    # ---------------------------------------------------------------- шаги

    def segment(self, pcm: np.ndarray, step_s: float, hook: Callable | None) -> np.ndarray:
        """Шаг 1. (окна, кадры, 3) — кто из трёх мест говорит, 0 или 1."""
        size = self.chunk_n
        step = int(round(step_s * SR))
        n = pcm.shape[0]
        full = 1 + (n - size) // step if n >= size else 0
        has_last = n < size or (n - size) % step > 0
        starts = [i * step for i in range(full)] + ([full * step] if has_last else [])
        total = len(starts)
        out = np.empty((total, self.chunk_frames, self.mapping.shape[1]), dtype=np.float32)
        _hook(hook, "segmentation", completed=0, total=total)
        for b0 in range(0, total, BATCH):
            batch = np.zeros((min(BATCH, total - b0), 1, size), dtype=np.float32)
            for j, s in enumerate(starts[b0:b0 + BATCH]):
                piece = pcm[s:s + size]
                batch[j, 0, :piece.shape[0]] = piece
            logp = self.seg.run(None, {"waveform": batch})[0]           # (b, кадры, 7)
            onehot = np.eye(self.mapping.shape[0], dtype=np.float32)[logp.argmax(axis=-1)]
            out[b0:b0 + batch.shape[0]] = onehot @ self.mapping
            _hook(hook, "segmentation", completed=b0 + batch.shape[0], total=total)
        return out

    def aggregate(self, scores: np.ndarray, step_s: float, skip_average: bool,
                  missing: float = 0.0) -> np.ndarray:
        """Склейка окон в одну шкалу кадров записи (Inference.aggregate).

        Кадры NaN (место не приписано ни к кому) в сумму не идут.
        """
        chunks, per_chunk, classes = scores.shape
        half = 0.5 * self.frame_dur

        def closest(t: float) -> int:
            return int(np.rint((t - half) / self.frame_step))

        num = closest(self.chunk_s + (chunks - 1) * step_s + half) + 1
        acc = np.zeros((num, classes), dtype=np.float32)
        cnt = np.zeros((num, classes), dtype=np.float32)
        seen = np.zeros((num, classes), dtype=np.float32)
        for c in range(chunks):
            score = scores[c]
            mask = 1.0 - np.isnan(score)
            score = np.nan_to_num(score, nan=0.0)
            s = closest(c * step_s + half)
            acc[s:s + per_chunk] += score * mask
            cnt[s:s + per_chunk] += mask
            seen[s:s + per_chunk] = np.maximum(seen[s:s + per_chunk], mask)
        avg = acc if skip_average else acc / np.maximum(cnt, np.float32(1e-12))
        avg[seen == 0.0] = missing
        return avg

    def speaker_count(self, seg: np.ndarray, step_s: float) -> np.ndarray:
        """Шаг 2. Сколько голосов звучит в каждом кадре записи."""
        summed = np.sum(seg, axis=-1, keepdims=True)
        avg = self.aggregate(summed, step_s, skip_average=False)
        return np.rint(avg[:, 0]).astype(np.uint8)

    def embeddings(self, pcm: np.ndarray, seg: np.ndarray, step_s: float,
                   hook: Callable | None) -> np.ndarray:
        """Шаг 3. (окна, 3, 256) — отпечаток на каждое место в каждом окне."""
        chunks, frames, speakers = seg.shape
        if self.exclude_overlap:
            min_frames = math.ceil(frames * self.min_num_samples / self.chunk_n)
            clean = seg * (np.sum(seg, axis=2, keepdims=True) < 2)
        else:
            min_frames = -1
            clean = seg
        used = np.where((clean.sum(axis=1) > min_frames)[:, None, :], clean, seg)
        weights = np.transpose(used, (0, 2, 1))                          # (окна, 3, кадры)

        out = np.empty((chunks, speakers, self.dimension), dtype=np.float64)
        n = pcm.shape[0]
        total = math.ceil(chunks / BATCH)
        _hook(hook, "embeddings", completed=0, total=total)
        for i, b0 in enumerate(range(0, chunks, BATCH), 1):
            fbanks = []
            for c in range(b0, min(chunks, b0 + BATCH)):
                start = int(round(c * step_s * SR))
                end = int(round((c * step_s + self.chunk_s) * SR))
                wave = np.zeros(end - start, dtype=np.float32)
                piece = pcm[start:min(end, n)]
                wave[:piece.shape[0]] = piece
                fbanks.append(self.fbank(wave))
            feats = self.frames_of(np.stack(fbanks))
            out[b0:b0 + feats.shape[0]] = self.pool(feats, weights[b0:b0 + feats.shape[0]])
            _hook(hook, "embeddings", completed=i, total=total)
        return out

    def cluster(self, emb: np.ndarray, seg: np.ndarray, threshold: float, Fa: float,
                Fb: float, min_active_ratio: float, num: int | None, min_c: int,
                max_c: float) -> tuple[np.ndarray, np.ndarray]:
        """Шаг 4. Места окон → люди. Возвращает (номер человека (окна, 3), центроиды)."""
        from scipy.cluster.hierarchy import fcluster, linkage
        from scipy.spatial.distance import cdist

        from .vbx import cluster_vbx

        chunks, speakers, dim = emb.shape
        frames = seg.shape[1]
        single = np.sum(seg, axis=2, keepdims=True) == 1
        clean = np.sum(seg * single, axis=1)
        active = clean >= min_active_ratio * frames
        valid = ~np.any(np.isnan(emb), axis=2)
        ci, si = np.where(active * valid)
        train = emb[ci, si]

        if train.shape[0] < 2:
            hard = np.zeros((chunks, speakers), dtype=np.int8)
            centroids = np.mean(train, axis=0, keepdims=True) if train.shape[0] else \
                np.zeros((1, dim))
            return hard, centroids

        normed = train / np.linalg.norm(train, axis=1, keepdims=True)
        tree = linkage(normed, method="centroid", metric="euclidean")
        ahc = fcluster(tree, threshold, criterion="distance") - 1
        _, ahc = np.unique(ahc, return_inverse=True)

        fea = self.plda(train)
        q, sp = cluster_vbx(ahc, fea, self.plda.phi, Fa=Fa, Fb=Fb, max_iters=20)
        W = q[:, sp > 1e-7]
        centroids = W.T @ train.reshape(-1, dim) / W.sum(0, keepdims=True).T

        constrained = True
        auto = centroids.shape[0]
        if auto < min_c:
            num = min_c
        elif auto > max_c:
            num = int(max_c)
        if num and num != auto:
            # как у pyannote: при заданном числе голосов ограничение снимается,
            # иначе оно искусственно добавляет голоса
            constrained = False
            labels = _kmeans(normed, int(num))
            centroids = np.vstack([np.mean(train[labels == k], axis=0) for k in range(int(num))])

        dist = cdist(emb.reshape(-1, dim), centroids, metric="cosine")
        soft = 2.0 - dist.reshape(chunks, speakers, -1)
        if constrained:
            soft[seg.sum(1) == 0] = soft.min() - 1.0
            hard = _constrained_argmax(soft)
        else:
            hard = np.argmax(soft, axis=2)
        return hard.reshape(chunks, speakers), centroids

    def reconstruct(self, seg: np.ndarray, hard: np.ndarray, count: np.ndarray,
                    step_s: float) -> np.ndarray:
        """Шаг 5. (кадры, люди) — 0 или 1: кто говорит в каждом кадре записи."""
        chunks, frames, _ = seg.shape
        k_total = int(np.max(hard)) + 1
        clustered = np.full((chunks, frames, max(k_total, 1)), np.nan, dtype=np.float32)
        for c in range(chunks):
            for k in np.unique(hard[c]):
                if k == -2:
                    continue
                clustered[c, :, k] = np.max(seg[c][:, hard[c] == k], axis=1)
        act = self.aggregate(clustered, step_s, skip_average=True)
        top = int(np.max(count)) if count.size else 0
        if act.shape[1] < top:
            act = np.pad(act, ((0, 0), (0, top - act.shape[1])))
        n = min(act.shape[0], count.shape[0])
        act, cnt = act[:n], count[:n].astype(np.int64)
        order = np.argsort(-act, axis=-1)
        ranks = np.empty_like(order)
        np.put_along_axis(ranks, order, np.broadcast_to(np.arange(act.shape[1]), act.shape), axis=-1)
        return (ranks < cnt[:, None]).astype(np.float32)

    def intervals(self, binary: np.ndarray) -> list[tuple[float, float, int]]:
        """Покадровая разметка → интервалы (Binarize, пороги 0,5).

        Начало и конец интервала — середины кадров, как у pyannote.
        """
        frames, people = binary.shape
        if frames == 0:
            return []
        # шкала кадров начинается с начала первого окна, то есть с нуля
        mids = np.arange(frames) * self.frame_step + 0.5 * self.frame_dur
        out: list[tuple[float, float, int]] = []
        for k in range(people):
            on = binary[:, k] > 0.5
            if not on.any():
                continue
            edges = np.diff(on.astype(np.int8))
            starts = list(np.where(edges == 1)[0] + 1)
            ends = list(np.where(edges == -1)[0] + 1)
            if on[0]:
                starts.insert(0, 0)
            for a, b in zip(starts, ends):
                out.append((float(mids[a]), float(mids[b]), k))
            if on[-1] and mids[-1] > mids[starts[-1]]:
                out.append((float(mids[starts[-1]]), float(mids[-1]), k))
        if self.min_duration_off > 0.0:
            out = _fill_gaps(out, self.min_duration_off)
        out.sort(key=lambda r: (r[0], r[1]))
        return out

    # ---------------------------------------------------------------- целиком

    def run(self, pcm: np.ndarray, min_speakers: int | None = None,
            max_speakers: int | None = None, tuning: dict | None = None,
            hook: Callable | None = None) -> dict[str, Any]:
        """Разметить склейку речи. pcm — float32 моно 16 кГц."""
        t = _normalize_tuning(tuning, self.chunk_s)
        num, min_s, max_s = _num_speakers(None, min_speakers, max_speakers)
        arr = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1))

        seg = self.segment(arr, t["step_s"], hook)
        count = self.speaker_count(seg, t["step_s"])
        _hook(hook, "speaker_counting")
        if count.size == 0 or int(np.max(count)) == 0:
            return {"turns": [], "exclusive_turns": [], "labels": [],
                    "centroids": np.zeros((0, self.dimension))}

        emb = self.embeddings(arr, seg, t["step_s"], hook)
        hard, centroids = self.cluster(emb, seg, t["threshold"], t["Fa"], t["Fb"],
                                       t["min_voice_s"] / self.chunk_s, num, min_s, max_s)

        if math.isfinite(max_s):
            count = np.minimum(count, int(max_s)).astype(np.int8)
        else:
            count = count.astype(np.int8)
        hard = hard.astype(np.int64)
        hard[np.sum(seg, axis=1) == 0] = -2

        regular = self.intervals(self.reconstruct(seg, hard, count, t["step_s"]))
        exclusive = self.intervals(
            self.reconstruct(seg, hard, np.minimum(count, 1).astype(np.int8), t["step_s"]))
        _hook(hook, "discrete_diarization")

        # Метки — SPEAKER_00, 01… по порядку номеров, у которых есть речь.
        present = sorted({k for _, _, k in regular})
        names = {k: "SPEAKER_%02d" % i for i, k in enumerate(present)}
        rows = max(len(present), (present[-1] + 1) if present else 0)
        if centroids.shape[0] < rows:
            centroids = np.pad(centroids, ((0, rows - centroids.shape[0]), (0, 0)))
        cents = centroids[present] if present else np.zeros((0, self.dimension))

        def turns(items):
            return [{"start": round(a, 3), "end": round(b, 3), "speaker": names.get(k, str(k))}
                    for a, b, k in items]

        return {
            "turns": turns(regular),
            "exclusive_turns": turns(exclusive),
            "labels": [names[k] for k in present],
            "centroids": cents,
        }

    def embed(self, wave: np.ndarray) -> np.ndarray | None:
        """Отпечаток всего куска звука без масок (для отсева эха по голосу)."""
        wave = np.asarray(wave, dtype=np.float32).reshape(-1)
        if wave.shape[0] < self.min_num_samples:
            return None
        fb = self.fbank(wave)
        if fb.shape[0] == 0:
            return None
        try:
            vec = self.pool(self.frames_of(fb[None]), None)[0]
        except Exception as err:                       # noqa: BLE001
            log.debug("отпечаток куска не посчитался: %s", err)
            return None
        return vec if np.isfinite(vec).all() else None


# ---------------------------------------------------------------- помощники


def _hook(hook: Callable | None, step: str, completed: int | None = None,
          total: int | None = None) -> None:
    """Сообщить о ходе работы; отмену пробрасываем — её бросает сам hook."""
    if hook is None:
        return
    if completed is None:
        hook(step, None)
    else:
        hook(step, None, completed=completed, total=total)


def _num_speakers(num: int | None, lo: int | None,
                  hi: int | None) -> tuple[int | None, int, float]:
    """Ограничения на число голосов, как set_num_speakers у pyannote."""
    lo = num or lo or 1
    hi = num or hi or math.inf
    if lo > hi:
        raise ValueError("min_speakers больше max_speakers: %s > %s" % (lo, hi))
    if lo == hi:
        num = int(lo)
    return num, int(lo), hi


def _normalize_tuning(tuning: dict | None, chunk_s: float) -> dict[str, float]:
    """Ручки разметки → значения для шагов; пусто — заводские, мусор обрезается."""
    base = tuning_defaults()
    t = dict(base)
    for key, value in (tuning or {}).items():
        if value is None:
            continue
        try:
            t[key] = float(value)
        except (TypeError, ValueError):
            continue
    step = t["step_s"] if t["step_s"] > 0 else base["step_s"]
    t["step_s"] = min(max(0.1, step), chunk_s)          # шаг больше окна — дыры
    t["threshold"] = min(max(t["threshold"], 0.05), 2.0)
    t["Fb"] = min(max(t["Fb"], 0.01), 50.0)
    sec = t["min_voice_s"]
    ratio = base["min_voice_s"] / chunk_s if sec <= 0 else min(max(sec / chunk_s, 0.01), 1.0)
    t["min_voice_s"] = ratio * chunk_s
    return t


def _constrained_argmax(soft: np.ndarray) -> np.ndarray:
    """Места одного окна — разным людям: назначение по наибольшей похожести."""
    from scipy.optimize import linear_sum_assignment

    soft = np.nan_to_num(soft, nan=np.nanmin(soft))
    chunks, speakers, _ = soft.shape
    hard = -2 * np.ones((chunks, speakers), dtype=np.int8)
    for c, cost in enumerate(soft):
        rows, cols = linear_sum_assignment(cost, maximize=True)
        hard[c, rows] = cols
    return hard


def _kmeans(x: np.ndarray, k: int, n_init: int = 3, max_iter: int = 300,
            seed: int = 42) -> np.ndarray:
    """k-means++ с фиксированным зерном: результат повторяется от запуска к запуску.

    Нужен, только когда число голосов задано явно, а VBx нашёл другое. Даёт то
    же, что sklearn.KMeans по смыслу, но не бит в бит: начальные точки
    выбираются своим генератором.
    """
    rng = np.random.default_rng(seed)
    n = x.shape[0]
    k = max(1, min(k, n))
    tol = 1e-4 * float(np.mean(np.var(x, axis=0)))
    best, best_inertia = None, math.inf
    for _ in range(n_init):
        centers = [x[rng.integers(n)]]
        for _j in range(1, k):
            d2 = np.min(((x[:, None, :] - np.asarray(centers)[None]) ** 2).sum(-1), axis=1)
            total = d2.sum()
            pick = rng.integers(n) if total <= 0 else rng.choice(n, p=d2 / total)
            centers.append(x[pick])
        c = np.asarray(centers)
        for _it in range(max_iter):
            labels = np.argmin(((x[:, None, :] - c[None]) ** 2).sum(-1), axis=1)
            new = np.vstack([x[labels == j].mean(axis=0) if np.any(labels == j) else c[j]
                             for j in range(k)])
            shift = float(((new - c) ** 2).sum())
            c = new
            if shift <= tol:
                break
        labels = np.argmin(((x[:, None, :] - c[None]) ** 2).sum(-1), axis=1)
        inertia = float(((x - c[labels]) ** 2).sum())
        if inertia < best_inertia:
            best, best_inertia = labels, inertia
    return best


def _fill_gaps(items: list[tuple[float, float, int]], gap: float) -> list[tuple[float, float, int]]:
    """Склеить интервалы одного человека с паузой короче gap (min_duration_off)."""
    out: list[tuple[float, float, int]] = []
    for a, b, k in sorted(items, key=lambda r: (r[2], r[0])):
        if out and out[-1][2] == k and a - out[-1][1] < gap:
            out[-1] = (out[-1][0], max(out[-1][1], b), k)
        else:
            out.append((a, b, k))
    return out
