# -*- coding: utf-8 -*-
"""Проверка 33: облачное распознавание. Ни одного запроса в сеть.

Сервис подменяется: проверяем, ЧТО именно уходит в запросе и как разбирается
ответ. Ключа у нас нет, а ошибиться в сборке запроса можно молча — отметки
времени по словам не придут, и говорящие разметятся грубо.
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from harness import LINES, FAIL, say, expect as check, finish  # noqa: E402

from hagen import asr_cloud, config, providers  # noqa: E402


OVER = {}
_real_get = config.get
config.get = lambda k, d=None: OVER[k] if k in OVER else _real_get(k, d)


def setup(**kw):
    OVER.clear()
    OVER.update({
        "api_keys": {"asr": "ключ-подлиннее-восьми"},
        "asr_base_url": "https://polza.ai/api/v1",
        "asr_model": "openai/whisper-1",
        "asr_fallback": "openai/whisper-large-v3-turbo",
        "asr_lang": "ru",
        "chunk_min": 15,
    })
    OVER.update(kw)


SENT = []
VERBOSE = {
    "text": "Добрый день. Начнём с бюджета.",
    "segments": [
        {"start": 0.0, "end": 2.0, "text": "Добрый день."},
        {"start": 2.1, "end": 5.0, "text": "Начнём с бюджета."},
    ],
    "words": [
        {"word": "Добрый", "start": 0.0, "end": 0.5},
        {"word": "день", "start": 0.5, "end": 1.0},
        {"word": "Начнём", "start": 2.1, "end": 2.6},
        {"word": "бюджета", "start": 3.0, "end": 3.8},
    ],
}


def fake_post(url, **kw):
    SENT.append({"url": url, "data": list(kw.get("data") or []),
                 "headers": kw.get("headers") or {},
                 "files": sorted((kw.get("files") or {}).keys()),
                 "timeout": kw.get("timeout")})
    return dict(VERBOSE)


providers.http_post = fake_post

say("=== 1. Готовность ===")
setup()
ok, why = asr_cloud.available()
check("готово с ключом и моделью", ok, True)
setup(api_keys={})
ok, why = asr_cloud.available()
check("без ключа не готово", ok, False)
check("сказано про ключ", "ключ" in why.lower(), True)
setup(asr_model="")
check("без модели не готово", asr_cloud.available()[0], False)
setup(asr_base_url="")
ok, why = asr_cloud.available()
check("без адреса не готово", ok, False)
check("сказано про адрес", "адрес" in why.lower(), True)
setup(asr_base_url="http://127.0.0.1:8000/v1")
check("адрес на своём компьютере не принят", asr_cloud.available()[0], False)
setup(api_keys={"docs": "ключ-подлиннее-восьми"})
check("ключ документов распознаванию не годится", asr_cloud.available()[0], False)

say("")
say("=== 2. Что уходит в запросе ===")
setup()
SENT.clear()
data, model, words_ok, rough = asr_cloud._transcribe_chunk(
    Path(__file__), "openai/whisper-1", "openai/whisper-large-v3-turbo")
check("запрос один", len(SENT), 1)
sent = SENT[0]
check("адрес собран из сервиса", sent["url"],
      "https://polza.ai/api/v1/audio/transcriptions")
check("файл приложен", sent["files"], ["file"])
fields = dict(sent["data"])
check("модель передана", fields.get("model"), "openai/whisper-1")
check("формат подробный", fields.get("response_format"), "verbose_json")
check("язык передан", fields.get("language"), "ru")
grans = [v for k, v in sent["data"] if k == "timestamp_granularities[]"]
check("отметки по словам запрошены", sorted(grans), ["segment", "word"])
check("ключ в заголовке, не в теле",
      sent["headers"].get("Authorization", "").startswith("Bearer "), True)
check("модель осталась основной", model, "openai/whisper-1")
check("слова обещаны", words_ok, True)
check("не грубый режим", rough, False)

say("")
say("=== 3. Язык учитывается ===")
setup(asr_lang="en")
SENT.clear()
asr_cloud._transcribe_chunk(Path(__file__), "openai/whisper-1", "")
check("английский передан", dict(SENT[0]["data"]).get("language"), "en")

say("")
say("=== 4. Модель без поддержки словных отметок ===")
setup()
SENT.clear()
data, model, words_ok, rough = asr_cloud._transcribe_chunk(
    Path(__file__), "openai/whisper-large-v3-turbo", "")
grans = [v for k, v in SENT[0]["data"] if k == "timestamp_granularities[]"]
check("лишний параметр не послан", grans, [])
check("слова не обещаны", words_ok, False)

say("")
say("=== 5. Сервис ругается на параметр — повтор без него ===")
setup()
SENT.clear()
calls = {"n": 0}


def post_granularity_error(url, **kw):
    calls["n"] += 1
    if calls["n"] == 1:
        raise RuntimeError("polza отказал (код 400). timestamp_granularities[] "
                           "is only supported for whisper-1")
    return fake_post(url, **kw)


providers.http_post = post_granularity_error
data, model, words_ok, rough = asr_cloud._transcribe_chunk(
    Path(__file__), "openai/whisper-1", "openai/whisper-large-v3-turbo")
check("попыток две", calls["n"], 2)
check("модель не менялась", model, "openai/whisper-1")
check("слова честно помечены как отсутствующие", words_ok, False)
grans = [v for k, v in SENT[-1]["data"] if k == "timestamp_granularities[]"]
check("во втором запросе параметра нет", grans, [])

say("")
say("=== 6. Основная не ответила — идёт запасная ===")
setup()
SENT.clear()
calls["n"] = 0


def post_first_fails(url, **kw):
    calls["n"] += 1
    if dict(kw.get("data") or []).get("model") == "openai/whisper-1":
        raise RuntimeError("polza отвечает ошибкой на своей стороне (код 502).")
    return fake_post(url, **kw)


providers.http_post = post_first_fails
data, model, words_ok, rough = asr_cloud._transcribe_chunk(
    Path(__file__), "openai/whisper-1", "openai/whisper-large-v3-turbo")
check("взята запасная", model, "openai/whisper-large-v3-turbo")
check("слов у запасной нет", words_ok, False)

providers.http_post = fake_post

say("")
say("=== 7. Разбор ответа: реплики и слова ===")
segs = asr_cloud._segments_from(dict(VERBOSE), offset=0.0)
check("реплик", len(segs), 2)
check("текст первой", segs[0]["text"], "Добрый день.")
check("дорожка файла", segs[0]["track"], "file")
check("слова первой реплики", [w["text"] for w in segs[0]["words"]],
      ["Добрый", "день"])
check("слова второй реплики", [w["text"] for w in segs[1]["words"]],
      ["Начнём", "бюджета"])

say("")
say("=== 8. Сдвиг времени по кускам ===")
segs = asr_cloud._segments_from(dict(VERBOSE), offset=900.0)
check("начало сдвинуто на 15 минут", segs[0]["start"], 900.0)
check("и слова тоже", segs[0]["words"][0]["start"], 900.0)
check("вторая реплика", round(segs[1]["start"], 1), 902.1)

say("")
say("=== 9. Ответ «только текст» ===")
segs = asr_cloud._segments_from({"text": "Одним куском без времени."}, offset=60.0)
check("одна реплика", len(segs), 1)
check("время — начало куска", segs[0]["start"], 60.0)
check("слов нет", segs[0].get("words"), None)

say("")
say("=== 10. Цена ===")
setup()
providers._models_cache["asr"] = {
    "role": "asr", "base_url": "https://polza.ai/api/v1", "at": 0,
    "stt": [{"id": "openai/whisper-1", "title": "whisper-1",
             "price": {"per_minute": 0.432, "currency": "RUB"}}],
    "chat": [],
}
check("цена часа записи", asr_cloud.estimate_cost(3600.0), "примерно 25.9 ₽")
setup(asr_base_url="https://api.example.com/v1")
check("список от прежнего адреса цену не даёт",
      "неизвестна" in asr_cloud.estimate_cost(3600.0), True)
setup()
providers._models_cache.pop("asr", None)
check("без списка моделей — честно",
      "неизвестна" in asr_cloud.estimate_cost(3600.0), True)

sys.exit(finish("t33"))
