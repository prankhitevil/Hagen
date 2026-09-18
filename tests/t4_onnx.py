# -*- coding: utf-8 -*-
"""Проверка 4: экспорт GigaAM в ONNX, скорость torch против onnxruntime,
подача массива вместо файла и отметки времени по словам."""
import io
import json
import sys
import time
import traceback
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

CKPT = PROJECT / "models" / "gigaam"
ONNX = PROJECT / "models" / "onnx"
ONNX.mkdir(parents=True, exist_ok=True)

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
    io.open(PROJECT / "tests" / "t4_result.txt", "w", encoding="utf-8").write("\n".join(LINES))


def main():
    import numpy as np
    import torch

    torch.set_num_threads(7)
    import gigaam
    from gigaam.onnx_utils import infer_onnx, load_onnx

    from hagen import audio_io

    pcm, sr = audio_io.read_wav(PROJECT / "tests" / "example.wav")
    dur = len(pcm) / float(sr)
    say("аудио: %.2f c" % dur)
    report = {}

    for name in ("v3_e2e_ctc", "v3_e2e_rnnt"):
        say("")
        say("################ %s ################" % name)
        info = {}

        # --- torch, загрузка из кэша
        t0 = time.time()
        model = gigaam.load_model(name, device="cpu", fp16_encoder=False,
                                  download_root=str(CKPT))
        t_load = time.time() - t0
        info["torch_load_s"] = round(t_load, 2)
        say("torch загрузка из кэша: %.1f c" % t_load)

        # --- torch по файлу
        t0 = time.time()
        res = model.transcribe(str(PROJECT / "tests" / "example.wav"))
        t_file = time.time() - t0
        say("torch по файлу: %.2f c (RTF=%.3f)" % (t_file, t_file / dur))
        say("   текст: %s" % res.text)
        info["torch_rtf"] = round(t_file / dur, 3)
        info["torch_text"] = res.text

        # --- torch по массиву (без временного файла)
        arr_ok = False
        try:
            wav = torch.from_numpy(np.ascontiguousarray(pcm)).unsqueeze(0)
            length = torch.full([1], wav.shape[-1], dtype=torch.long)
            t0 = time.time()
            with torch.inference_mode():
                enc, enc_len = model.forward(wav, length)
                text_arr, words_arr = model._decode(enc, enc_len, length, True)[0]
            t_arr = time.time() - t0
            arr_ok = True
            say("torch по массиву: %.2f c (RTF=%.3f)" % (t_arr, t_arr / dur))
            say("   текст: %s" % text_arr)
            say("   совпадает с файловым путём: %s" % (text_arr.strip() == res.text.strip()))
            if words_arr:
                say("   слов с отметками времени: %d, первые 6:" % len(words_arr))
                for w in words_arr[:6]:
                    say("      %6.2f - %6.2f  %s" % (w.start, w.end, w.text))
            info["array_input"] = True
            info["array_rtf"] = round(t_arr / dur, 3)
            info["word_timestamps"] = bool(words_arr)
        except Exception as err:
            say("torch по массиву НЕ РАБОТАЕТ: %s: %s" % (type(err).__name__, str(err)[:300]))
            info["array_input"] = False

        # --- экспорт в ONNX
        t0 = time.time()
        try:
            model.to_onnx(dir_path=str(ONNX))
            t_exp = time.time() - t0
            say("экспорт в ONNX: %.1f c" % t_exp)
            info["onnx_export_s"] = round(t_exp, 1)
            files = sorted(p.name for p in ONNX.glob(name + "*"))
            sizes = {p.name: round(p.stat().st_size / 1e6, 1) for p in ONNX.glob(name + "*")}
            say("   файлы: %s" % files)
            say("   размеры, МБ: %s" % sizes)
            info["onnx_files"] = sizes
        except Exception as err:
            say("ЭКСПОРТ НЕ УДАЛСЯ: %s: %s" % (type(err).__name__, str(err)[:400]))
            info["onnx_export_s"] = None
            report[name] = info
            del model
            continue

        del model

        # --- onnxruntime
        try:
            t0 = time.time()
            sessions, cfg = load_onnx(str(ONNX), name, provider="CPUExecutionProvider")
            t_oload = time.time() - t0
            say("onnxruntime загрузка: %.1f c" % t_oload)
            info["onnx_load_s"] = round(t_oload, 2)

            t0 = time.time()
            texts = infer_onnx([np.ascontiguousarray(pcm)], cfg, sessions, progress=False)
            t_onnx = time.time() - t0
            say("onnxruntime распознавание: %.2f c (RTF=%.3f)" % (t_onnx, t_onnx / dur))
            say("   текст: %s" % texts[0])
            say("   совпадает с torch: %s" % (str(texts[0]).strip() == res.text.strip()))
            info["onnx_rtf"] = round(t_onnx / dur, 3)
            info["onnx_text"] = str(texts[0])

            # повторный прогон: без разогрева модель первый раз медленнее
            t0 = time.time()
            texts2 = infer_onnx([np.ascontiguousarray(pcm)], cfg, sessions, progress=False)
            t_warm = time.time() - t0
            say("onnxruntime после разогрева: %.2f c (RTF=%.3f)" % (t_warm, t_warm / dur))
            info["onnx_rtf_warm"] = round(t_warm / dur, 3)

            # короткий кусок — как в живом эфире
            short = np.ascontiguousarray(pcm[: int(3.0 * sr)])
            t0 = time.time()
            infer_onnx([short], cfg, sessions, progress=False)
            t_short = time.time() - t0
            say("кусок 3 с (как в эфире): %.3f c -> задержка приемлема: %s"
                % (t_short, "ДА" if t_short < 1.0 else "НЕТ"))
            info["onnx_3s_latency_s"] = round(t_short, 3)
        except Exception as err:
            say("ONNXRUNTIME НЕ РАБОТАЕТ: %s: %s" % (type(err).__name__, str(err)[:400]))
            say(traceback.format_exc()[-800:])

        report[name] = info

    io.open(PROJECT / "tests" / "t4_result.json", "w", encoding="utf-8").write(
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
