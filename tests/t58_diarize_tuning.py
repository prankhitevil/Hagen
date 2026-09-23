# -*- coding: utf-8 -*-
"""Проверка 58: продвинутые настройки разметки и разрез фразы по длинному куску.

Решения 15.09 после разбора звонка с пятью голосами:
  * пять ручек в «Настройки → Тонкие настройки»: шаг окна, порог группировки, штраф
    за лишнего говорящего, минимум речи для нового голоса, разрез фразы;
  * пустое поле — как у модели, применяется со следующей разметки без перезапуска;
  * фраза режется, если кусок другого голоса сам по себе не короче порога (2 с),
    даже когда его доля меньше прежних 35 %. Найдено на настоящей записи:
    16,6-секундная фраза с 3,2 с первого человека не резалась совсем.

Что проверяем:
  1. Умолчания в настройках.
  2. apply_tuning на подставном пайплайне: пусто — как у модели; значения
     доходят до группировки; снятие возвращает заводские; минимум речи
     переводится в долю окна; шаг окна меняется без перезапуска.
  3. Разрез фразы: длинный чужой кусок режет при малой доле; порог 0 — старое
     правило; короткий чужой кусок не режет; доля больше трети режет как раньше.
  4. Настоящая модель community-1: ручки доходят до неё, разметка идёт.
  5. Интерфейс: вкладка, пять полей, пустое поле уходит как null.

Настоящие settings.json и база голосов не трогаются (tests\\isolate.py).
Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t58_diarize_tuning.py
"""
import copy
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

from hagen import config, diar_pyannote, diarize  # noqa: E402


ADV_KEYS = ("diarize_window_step_s", "diarize_cluster_threshold", "diarize_cluster_fb",
            "diarize_min_voice_s", "split_min_piece_s")


def reset_settings():
    config.save({k: config.DEFAULTS[k] for k in ADV_KEYS})


say("=== 1. Умолчания ===")
check("шаг окна 2 с", config.DEFAULTS.get("diarize_window_step_s") == 2.0)
check("порог — как у модели", config.DEFAULTS.get("diarize_cluster_threshold", "нет") is None)
check("штраф — как у модели", config.DEFAULTS.get("diarize_cluster_fb", "нет") is None)
check("минимум речи — как у модели", config.DEFAULTS.get("diarize_min_voice_s", "нет") is None)
check("разрез фразы 2 с", config.DEFAULTS.get("split_min_piece_s") == 2.0)

say("")
say("=== 2. apply_tuning на подставном пайплайне ===")


class FakeSeg:
    def __init__(self):
        self.step = 1.0
        self.duration = 10.0


class FakeClustering:
    def __init__(self):
        self.calls = []

    def filter_embeddings(self, embeddings, segmentations=None, min_active_ratio=0.2):
        self.calls.append(min_active_ratio)
        return min_active_ratio


class FakePipe:
    def __init__(self):
        self._segmentation = FakeSeg()
        self.segmentation_step = 0.1
        self.clustering = FakeClustering()
        self.params = {"segmentation": {"min_duration_off": 0.0},
                       "clustering": {"threshold": 0.6, "Fa": 0.07, "Fb": 0.8}}
        self.instantiated = []

    def parameters(self, instantiated=False):
        return copy.deepcopy(self.params)

    def instantiate(self, params):
        self.instantiated.append(copy.deepcopy(params))
        self.params = copy.deepcopy(params)
        return self


def ratio_now(pipe):
    return pipe.clustering.filter_embeddings(None, segmentations=None)


def apply_tuning(pipe):
    """Ручки из настроек — в пайплайн, как это делает движок перед разметкой."""
    return diar_pyannote.apply_tuning(pipe, diarize.tuning())


reset_settings()
fp = FakePipe()
diar_pyannote._remember_base(fp)
config.save({"diarize_window_step_s": None})
info = apply_tuning(fp)
check("пусто: шаг как у модели (1 с)", fp._segmentation.step == 1.0, info)
check("пусто: порог и штраф как у модели",
      fp.params["clustering"]["threshold"] == 0.6 and fp.params["clustering"]["Fb"] == 0.8, fp.params)
check("пусто: без лишнего пересоздания параметров", fp.instantiated == [], len(fp.instantiated))
check("пусто: минимум речи как у модели (20 % окна)", ratio_now(fp) == 0.2)

config.save({"diarize_window_step_s": 2.0, "diarize_cluster_threshold": 0.5,
             "diarize_cluster_fb": 0.4, "diarize_min_voice_s": 1.0})
info = apply_tuning(fp)
check("шаг окна 2 с дошёл", fp._segmentation.step == 2.0 and abs(fp.segmentation_step - 0.2) < 1e-9, info)
check("порог 0,5 дошёл до группировки", fp.params["clustering"]["threshold"] == 0.5, fp.params)
check("штраф 0,4 дошёл до группировки", fp.params["clustering"]["Fb"] == 0.4, fp.params)
check("Fa и сегментация не тронуты",
      fp.params["clustering"]["Fa"] == 0.07 and fp.params["segmentation"] == {"min_duration_off": 0.0})
check("минимум речи 1 с = 10 % окна", abs(ratio_now(fp) - 0.1) < 1e-9, ratio_now(fp))
check("в ответе видно, что применилось",
      info.get("threshold") == 0.5 and info.get("Fb") == 0.4 and info.get("min_voice_s") == 1.0, info)

config.save({"diarize_window_step_s": 1.0})
apply_tuning(fp)
check("шаг окна меняется без перезапуска", fp._segmentation.step == 1.0)

config.save({"diarize_cluster_threshold": None, "diarize_cluster_fb": None, "diarize_min_voice_s": None})
apply_tuning(fp)
check("очистили поля — порог и штраф заводские",
      fp.params["clustering"]["threshold"] == 0.6 and fp.params["clustering"]["Fb"] == 0.8, fp.params)
check("очистили поля — минимум речи заводской", ratio_now(fp) == 0.2, ratio_now(fp))

config.save({"diarize_cluster_threshold": "мусор", "diarize_min_voice_s": -3})
apply_tuning(fp)
check("мусор в поле — как у модели, без падения",
      fp.params["clustering"]["threshold"] == 0.6 and ratio_now(fp) == 0.2)


class NoClusterParams(FakePipe):
    """Запасная модель 3.1: у группировки нет Fb."""

    def __init__(self):
        super().__init__()
        self.params = {"clustering": {"method": "centroid", "min_cluster_size": 12,
                                      "threshold": 0.7045}}


reset_settings()
fp31 = NoClusterParams()
diar_pyannote._remember_base(fp31)
config.save({"diarize_cluster_fb": 0.4})
apply_tuning(fp31)
check("у модели без штрафа ключ Fb не появляется", "Fb" not in fp31.params["clustering"], fp31.params)
reset_settings()

say("")
say("=== 3. Разрез фразы ===")


def words_span(texts, start, end):
    step = (end - start) / len(texts)
    return [{"text": t, "start": start + i * step, "end": start + (i + 1) * step - 0.02}
            for i, t in enumerate(texts)]


def phrase(first_s, second_s, gap=0.6):
    """Фраза: сначала A говорит first_s секунд, потом B — second_s."""
    w1 = words_span(["Ладно,", "коллеги.", "Если", "всё,", "хорошего", "дня."], 0.0, first_s)
    b0 = first_s + gap
    w2 = words_span(["Сергей", "Владимирович.", "Хорошего", "дня", "вам.", "У", "меня",
                     "там", "приказ", "по", "зачистке."], b0, b0 + second_s)
    seg = {"id": "s1", "track": "far", "start": 0.0, "end": b0 + second_s,
           "text": " ".join(w["text"] for w in w1 + w2), "words": w1 + w2,
           "speaker": "Спикер 1", "speaker_key": "far:1"}
    turns = [{"start": 0.0, "end": first_s + 0.1, "speaker": "SPEAKER_A"},
             {"start": b0 - 0.05, "end": b0 + second_s, "speaker": "SPEAKER_B"}]
    return seg, turns


def split_result(seg, turns):
    out = diarize.relabel([copy.deepcopy(seg)], turns, "far")
    return [(" ".join(str(x.get("text")).split()[:2]), x.get("speaker_key")) for x in out]


seg, turns = phrase(3.2, 12.5)            # как на звонке 9:30: 21 % чужой речи
config.save({"split_min_piece_s": 2.0})
got = split_result(seg, turns)
check("16-секундная фраза с 3,2 с другого голоса режется на две", len(got) == 2, got)
check("первый кусок — «Ладно, коллеги», второй — «Сергей Владимирович»",
      len(got) == 2 and got[0][0].startswith("Ладно") and got[1][0].startswith("Сергей")
      and got[0][1] != got[1][1], got)

config.save({"split_min_piece_s": 0})
got = split_result(seg, turns)
check("порог 0 — старое правило: фраза целиком", len(got) == 1, got)

config.save({"split_min_piece_s": 4.0})
got = split_result(seg, turns)
check("кусок короче порога (3,2 < 4) — фраза целиком", len(got) == 1, got)

config.save({"split_min_piece_s": None})
got = split_result(seg, turns)
check("пустое поле — заводские 2 с, режется", len(got) == 2, got)

seg2, turns2 = phrase(6.0, 7.0)           # больше трети — резалось и раньше
config.save({"split_min_piece_s": 0})
check("доля больше трети режет и при старом правиле", len(split_result(seg2, turns2)) == 2)
reset_settings()

say("")
say("=== 4. Настоящая модель community-1 через pyannote ===")
# Тот же путь для движка ONNX проверяет t100 (раздел «Ручки»).
wav = PROJECT / "tests" / "meeting.wav"
pipe = None
why = "нет tests\\meeting.wav"
if not diar_pyannote.installed():
    why = "pyannote не установлен — в этом релизе разметка идёт через ONNX"
elif wav.exists():
    # Токена во временных настройках нет, а настоящие проверке читать нельзя.
    # Скачанная модель грузится с диска и без него — ставим её в движок сами.
    name = config.DEFAULTS["diarize_model"]
    try:
        pipe = diar_pyannote._load_one(name, "")
        diar_pyannote.use_pipeline(pipe, name)
        diarize.use_engine("pyannote")
    except Exception as err:
        why = "модель %s не загрузилась с диска: %s" % (name, str(err)[:120])
if pipe is None:
    say("   (пропуск: %s)" % why)
else:
    from hagen import audio_io  # noqa: E402

    base = getattr(pipe, "_hagen_base", None) or {}
    check("заводские значения модели запомнены",
          base.get("params", {}).get("clustering", {}).get("Fb") is not None
          and base.get("filter") is not None, list(base))
    seen = []
    real_filter = base["filter"]

    def spy(*a, **kw):
        seen.append(kw.get("min_active_ratio", 0.2))
        return real_filter(*a, **kw)

    base["filter"] = spy
    try:
        config.save({"diarize_cluster_threshold": 0.55, "diarize_cluster_fb": 0.5,
                     "diarize_min_voice_s": 1.0, "diarize_window_step_s": 2.0})
        pcm, sr = audio_io.read_wav(wav)
        res = diarize.diarize_pcm(pcm, sr=sr)
        cl = pipe.parameters(instantiated=True)["clustering"]
        check("порог 0,55 и штраф 0,5 в модели", cl.get("threshold") == 0.55 and cl.get("Fb") == 0.5, cl)
        check("минимум речи 1 с дошёл до отбора отпечатков", seen and abs(seen[-1] - 0.1) < 1e-9, seen[-3:])
        check("разметка с новыми значениями прошла", len(res.get("labels") or []) >= 1, res.get("labels"))
        seen.clear()
        reset_settings()
        diarize.diarize_pcm(pcm, sr=sr)
        cl = pipe.parameters(instantiated=True)["clustering"]
        check("вернули пустые — модель снова на заводских 0,6 / 0,8",
              cl.get("threshold") == 0.6 and cl.get("Fb") == 0.8, cl)
        check("минимум речи снова заводской", seen and seen[-1] == 0.2, seen[-3:])
    finally:
        base["filter"] = real_filter
        reset_settings()
        diarize.use_engine(None)

say("")
say("=== 5. Интерфейс ===")
html = io.open(PROJECT / "hagen" / "static" / "index.html", encoding="utf-8").read()
js = io.open(PROJECT / "hagen" / "static" / "app.js", encoding="utf-8").read()
check("вкладка «Тонкие настройки» есть", 'data-tab="t-advanced"' in html and 'id="t-advanced"' in html)
pane = html[html.find('id="t-advanced"'):]
for fid in ("set-dt-step", "set-dt-threshold", "set-dt-fb", "set-dt-minvoice", "set-dt-split"):
    check("поле %s на вкладке" % fid, 'id="%s"' % fid in pane)
check("у каждого поля разметки есть «по умолчанию»",
      all('data-for="%s"' % fid in pane
          for fid in ("set-dt-step", "set-dt-threshold", "set-dt-fb",
                      "set-dt-minvoice", "set-dt-split")),
      pane.count("js-dt-reset"))
for key in ADV_KEYS:
    check("страница читает и пишет %s" % key, "'%s'" % key in js)
check("пустое поле уходит как null", "? null : n" in js)
check("пояснения больше/меньше у порога",
      "Больше — чаще склеивает" in pane and "Меньше — чаще делит" in pane)

reset_settings()
sys.exit(finish("t58"))
