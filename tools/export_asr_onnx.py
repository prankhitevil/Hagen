# -*- coding: utf-8 -*-
r"""Выгрузить модель GigaAM из весов torch в ONNX для onnx-asr.

    .venv\Scripts\python.exe tools\export_asr_onnx.py v3_e2e_rnnt
    .venv\Scripts\python.exe tools\export_asr_onnx.py v3_e2e_ctc --out ..\gigaam-export
    .venv\Scripts\python.exe tools\export_asr_onnx.py v3_e2e_rnnt --check

Инструмент разработчика (решение 23.09): программе torch не нужен — она читает
готовый ONNX с Hugging Face (istupakov/gigaam-v3-onnx) пакетом onnx-asr. torch
остаётся в мастерской ради одного: когда выйдет новая модель GigaAM, а готового
ONNX для неё ещё нет, выгрузить её самим и выложить в выпуске.

Чем ставить (в .venv мастерской, руками; в requirements.txt и в опись выпуска
это не попадает):

    .venv\Scripts\python.exe -m pip install torch==2.14.0 torchaudio==2.11.0 ^
        --index-url https://download.pytorch.org/whl/cpu
    .venv\Scripts\python.exe -m pip install "gigaam @ git+https://github.com/salute-developers/GigaAM.git" onnx
    (после pip удалить лишние .exe из .venv\Scripts — правило про антивирус)

Что делается:
  1. веса (.ckpt) и словарь скачиваются пакетом gigaam в папку --ckpt
     (по умолчанию models\gigaam), модель выгружается его же средством to_onnx;
  2. файлы раскладываются так, как их ждёт onnx-asr (имена и сигнатуры графов —
     как в istupakov/gigaam-v3-onnx): у декодера RNNT переименовываются входы и
     выходы состояния (hi/ci → h.1/c.1, ho/co → h/c), словарь SentencePiece
     переписывается в vocab.txt («токен номер» построчно, последний — <blk>),
     рядом кладётся config.json;
  3. --check: выгруженное сверяется с тем, что программа скачала с Hugging Face
     (если оно есть в models\onnx-asr\gigaam-v3): один и тот же звук
     tests\meeting.wav распознаётся обеими сборками, тексты печатаются.

Как проверить результат: положить файлы из --out в models\onnx-asr\gigaam-v3
(программа читает модели оттуда) и прогнать tests\t89_one_model_real.py.
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

#: Какие файлы у какой модели и как их зовёт onnx-asr.
LAYOUT = {
    "v3_e2e_ctc": {"parts": [""], "classes": 257},
    "v3_e2e_rnnt": {"parts": ["_encoder", "_decoder", "_joint"], "classes": 1025},
}
#: Имена входов и выходов состояния декодера RNNT: как выгружает gigaam → как ждёт onnx-asr.
DECODER_RENAME = {"hi": "h.1", "ci": "c.1", "ho": "h", "co": "c"}
CONFIG = {"model_type": "gigaam", "version": "v3", "features_size": 64,
          "subsampling_factor": 4, "max_tokens_per_step": 3}


def say(msg: str = "") -> None:
    print(msg, flush=True)


def export(name: str, ckpt: Path, work: Path, threads: int) -> None:
    """Скачать веса и выгрузить модель средствами gigaam в work."""
    import torch

    import gigaam

    ckpt.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    torch.set_num_threads(threads)
    say("гружу %s (веса — %s)…" % (name, ckpt))
    model = gigaam.load_model(name, device="cpu", fp16_encoder=False, download_root=str(ckpt))
    say("выгружаю в ONNX — это несколько минут…")
    model.to_onnx(dir_path=str(work))
    del model


def rename_decoder(path: Path) -> None:
    """Переименовать входы и выходы состояния декодера под onnx-asr."""
    import onnx

    model = onnx.load(str(path))
    for tensor in list(model.graph.input) + list(model.graph.output):
        if tensor.name in DECODER_RENAME:
            tensor.name = DECODER_RENAME[tensor.name]
    for node in model.graph.node:
        node.input[:] = [DECODER_RENAME.get(n, n) for n in node.input]
        node.output[:] = [DECODER_RENAME.get(n, n) for n in node.output]
    onnx.save(model, str(path))


def write_vocab(tokenizer_model: Path, classes: int, out: Path) -> None:
    """Словарь SentencePiece → vocab.txt в формате onnx-asr: «токен номер», последний — <blk>."""
    import sentencepiece as spm

    sp = spm.SentencePieceProcessor(model_file=str(tokenizer_model))
    if sp.get_piece_size() != classes - 1:
        raise SystemExit("в словаре %d токенов, а у модели %d классов без пустого"
                         % (sp.get_piece_size(), classes - 1))
    with io.open(out, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(sp.get_piece_size()):
            fh.write("%s %d\n" % (sp.id_to_piece(i), i))
        fh.write("<blk> %d\n" % (classes - 1))


def lay_out(name: str, ckpt: Path, work: Path, out: Path) -> list[Path]:
    """Разложить выгруженное так, как ждёт onnx-asr."""
    out.mkdir(parents=True, exist_ok=True)
    made = []
    for part in LAYOUT[name]["parts"]:
        src = work / (name + part + ".onnx")
        dst = out / src.name
        shutil.copy2(src, dst)
        if part == "_decoder":
            rename_decoder(dst)
        made.append(dst)
    vocab = out / (name + "_vocab.txt")
    write_vocab(ckpt / (name + "_tokenizer.model"), LAYOUT[name]["classes"], vocab)
    made.append(vocab)
    cfg = out / "config.json"
    with io.open(cfg, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(CONFIG, fh, indent=4)
        fh.write("\n")
    made.append(cfg)
    return made


def check(name: str, out: Path) -> None:
    """Сверить выгруженное с тем, что скачано с Hugging Face, на tests\\meeting.wav."""
    import numpy as np
    import onnx_asr

    from hagen import asr, audio_io

    pcm, sr = audio_io.read_wav(PROJECT / "tests" / "meeting.wav")
    piece = np.ascontiguousarray(np.asarray(pcm, dtype=np.float32).reshape(-1)[: 20 * sr])
    model_name = "gigaam-v3-e2e-ctc" if name.endswith("ctc") else "gigaam-v3-e2e-rnnt"
    mine = onnx_asr.load_model(model_name, path=str(out), providers=["CPUExecutionProvider"])
    text_mine = str(mine.recognize(piece, sample_rate=sr))
    say("выгруженное:     %s" % text_mine)
    engine = "fast" if name.endswith("ctc") else "ox_fp32"
    if asr.ox_available(engine)[0]:
        theirs = onnx_asr.load_model(model_name, path=str(asr.OX_DIR), providers=["CPUExecutionProvider"])
        text_theirs = str(theirs.recognize(piece, sample_rate=sr))
        say("с Hugging Face:  %s" % text_theirs)
        say("совпало слово в слово: %s" % ("да" if text_mine.strip() == text_theirs.strip() else "НЕТ"))
    else:
        say("(файлов с Hugging Face в %s нет — сравнить не с чем)" % asr.OX_DIR)


def main() -> int:
    ap = argparse.ArgumentParser(description="Выгрузка GigaAM в ONNX для onnx-asr")
    ap.add_argument("name", choices=sorted(LAYOUT), help="какую модель выгружать")
    ap.add_argument("--ckpt", default=str(PROJECT / "models" / "gigaam"), help="куда качать веса torch")
    ap.add_argument("--work", default="", help="рабочая папка выгрузки (по умолчанию — рядом с --out)")
    ap.add_argument("--out", default=str(PROJECT.parent / "gigaam-export"),
                    help="куда сложить файлы для onnx-asr")
    ap.add_argument("--threads", type=int, default=7)
    ap.add_argument("--check", action="store_true", help="сверить с файлами, скачанными с Hugging Face")
    args = ap.parse_args()

    out = Path(args.out)
    work = Path(args.work) if args.work else out.with_name(out.name + "-raw")
    export(args.name, Path(args.ckpt), work, args.threads)
    made = lay_out(args.name, Path(args.ckpt), work, out)
    say("готово: %s" % out)
    for p in made:
        say("   %-32s %8.1f МБ" % (p.name, p.stat().st_size / 2**20))
    if args.check:
        check(args.name, out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
