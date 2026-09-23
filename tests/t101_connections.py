# -*- coding: utf-8 -*-
"""Проверка 101: подключения к облаку — адрес, ключ, модель (21.09).

Прежде сервис выбирался из списка в полтора десятка строк. Они отличались
только адресом, а выбор в списке сохранялся сразу — просмотр списка молча
переключал сервис. Теперь подключений два: документы и облачное распознавание,
у каждого свои адрес, ключ и модель. Как разговаривать, служба узнаёт по адресу.

Ни одного запроса в сеть. Что проверяем:
  1. вид сервиса по адресу: OpenAI, Anthropic, GigaChat, YandexGPT (и его
     совместимый слой под /v1), ручной вид сильнее адреса;
  2. чего не хватает подключению: адреса, годного адреса, ключа, каталога;
  3. наружу ключ не уходит — только маска;
  4. перенос со старого списка: выбранный сервис, «выбран без ключа, а ключ у
     одного», свой сервис, каталог Яндекса, распознавание, новая установка,
     второй запуск ничего не трогает;
  5. список моделей привязан к адресу;
  6. документы: обработчик по виду, модель по умолчанию только там, где имена
     известны, предел куска у YandexGPT;
  7. подсказка «?» у адреса: RouterAI и OpenRouter есть, все адреса годные;
  8. где распознавать файлы — выбор из настроек, и проверка частей перед
     обработкой видео его слушается.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t101_connections.py
"""
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.settings()


from hagen import config, minutes, providers  # noqa: E402

KEY = "ключ-подлиннее-восьми"

say("=== 1. Вид сервиса по адресу ===")
for url, want in (("https://polza.ai/api/v1", "openai"),
                  ("https://openrouter.ai/api/v1", "openai"),
                  ("https://api.anthropic.com/v1", "anthropic"),
                  ("https://gigachat.devices.sberbank.ru/api/v1", "gigachat"),
                  ("https://llm.api.cloud.yandex.net", "yandexgpt"),
                  ("https://llm.api.cloud.yandex.net/v1", "openai"),
                  ("", "openai")):
    got = providers.kind_for(url)
    check("%s → %s" % (url or "пусто", want), got == want, got)

isolate.settings(api_base_url="https://api.example.com/v1", api_kind="anthropic")
c = providers.connection("docs")
check("ручной вид сильнее адреса", c["kind"] == "anthropic" and c["kind_manual"], c)
isolate.settings(api_base_url="https://api.example.com/v1/", api_kind="мусор")
c = providers.connection("docs")
check("неизвестный ручной вид — по адресу", c["kind"] == "openai" and not c["kind_manual"], c)
check("косая черта в конце адреса срезана", c["base_url"] == "https://api.example.com/v1",
      c["base_url"])
check("сервис назван по имени сервера", c["service"] == "api.example.com", c["service"])
check("у распознавания вид всегда OpenAI",
      providers.connection("asr")["kind"] == "openai")

say("")
say("=== 2. Чего не хватает ===")
isolate.settings()
check("без адреса", "адрес" in providers.problem("docs").lower(), providers.problem("docs"))
for bad in ("http://пример.ru/v1", "https://127.0.0.1/v1", "https://192.168.1.10/v1",
            "https://localhost/v1"):
    isolate.settings(api_base_url=bad, api_keys={"docs": KEY})
    got = providers.problem("docs")
    check("адрес %s не принят" % bad[:26], "не годится" in got, got)
isolate.settings(api_base_url="https://polza.ai/api/v1")
check("без ключа", "ключ" in providers.problem("docs"), providers.problem("docs"))
isolate.settings(api_base_url="https://llm.api.cloud.yandex.net", api_keys={"docs": KEY})
check("YandexGPT без каталога", "каталог" in providers.problem("docs"), providers.problem("docs"))
isolate.settings(api_base_url="https://polza.ai/api/v1", api_keys={"docs": KEY})
check("всё на месте — нечего сказать", providers.problem("docs") == "", providers.problem("docs"))
check("ключ документов распознаванию не достаётся", "ключ" in providers.problem("asr"),
      providers.problem("asr"))

say("")
say("=== 3. Ключ наружу не уходит ===")
pub = providers.public_connections()
check("в открытом виде ключа нет", KEY not in str(pub), pub)
check("маска и признак есть", pub["docs"]["has_key"] and pub["docs"]["key_hint"]
      == providers.mask(KEY), pub["docs"])
check("у распознавания признак «нет ключа»", pub["asr"]["has_key"] is False)
check("вид подписан для окна", pub["docs"]["kind_title"] == "как OpenAI", pub["docs"])

say("")
say("=== 4. Перенос со старого списка ===")


def adopt(**old):
    isolate.settings(**old)
    done = providers.adopt_old_services()
    return done, providers.connection("docs"), providers.connection("asr")


done, d, a = adopt(api_provider="anthropic", api_keys={"anthropic": KEY})
check("выбранный сервис с ключом переехал", done and d["base_url"] == "https://api.anthropic.com/v1"
      and d["key"] == KEY and d["kind"] == "anthropic", d)
check("модель пустая — подберётся по виду", d["model"] == ""
      and minutes._model_for("anthropic", "strong") == "claude-opus-5", d["model"])
check("отметка о переносе", config.get("services_adopted") is True)
check("второй запуск ничего не делает", providers.adopt_old_services() is False)

done, d, a = adopt(api_provider="openrouter", asr_provider="polza", api_keys={"polza": KEY},
                   api_models={"polza": "z-ai/glm-5.3-flash"})
check("выбран без ключа, а ключ у одного — берётся тот",
      d["base_url"] == "https://polza.ai/api/v1" and d["key"] == KEY, d)
check("его модель переехала", d["model"] == "z-ai/glm-5.3-flash", d["model"])
check("распознавание — polza с тем же ключом",
      a["base_url"] == "https://polza.ai/api/v1" and a["key"] == KEY, a)

done, d, a = adopt(api_provider="openrouter", api_keys={"polza": KEY, "groq": KEY})
check("ключей несколько — остаётся выбранный", d["base_url"] == "https://openrouter.ai/api/v1"
      and d["key"] == "", d)

done, d, a = adopt(api_provider="polza", api_keys={"polza": KEY})
check("модель, которую прежний список подставлял сам, прописана явно",
      d["model"] == "anthropic/claude-sonnet-4.6", d["model"])

done, d, a = adopt(api_provider="yandexgpt",
                   api_keys={"yandexgpt": KEY, "yandexgpt_folder": "b1gкаталог"})
check("каталог Яндекса переехал в своё поле", d["folder"] == "b1gкаталог"
      and d["kind"] == "yandexgpt" and providers.problem("docs") == "", d)

done, d, a = adopt(api_provider="свой", api_keys={"свой": KEY},
                   providers=[{"id": "свой", "title": "Свой", "kind": "anthropic",
                               "base_url": "https://llm.example.org/v1/"}])
check("свой сервис переехал со своим видом", d["base_url"] == "https://llm.example.org/v1"
      and d["kind"] == "anthropic" and config.get("api_kind") == "anthropic", d)

done, d, a = adopt()
check("новая установка: перенос отмечен, адреса документов нет",
      done and d["base_url"] == "" and config.get("services_adopted") is True, d)
check("распознавание — адрес по умолчанию, без ключа",
      a["base_url"] == "https://polza.ai/api/v1" and a["key"] == "", a)
check("ключи в файле остались как были", (config.get("api_keys") or {}) == {},
      config.get("api_keys"))

say("")
say("=== 5. Список моделей привязан к адресу ===")
isolate.settings(api_base_url="https://polza.ai/api/v1", api_keys={"docs": KEY})
asked = []


def fake_get(url, **kw):
    asked.append(url)
    if (kw.get("params") or {}).get("type") == "stt":      # как у polza: только распознавание
        return {"data": [{"id": "openai/whisper-1"}]}
    return {"data": [{"id": "anthropic/claude-sonnet-4.6"}, {"id": "openai/whisper-1"}]}


_real_get = providers.http_get
providers.http_get = fake_get
try:
    res = providers.list_models("docs", refresh=True)
    check("спрошено по адресу подключения",
          asked and all(u == "https://polza.ai/api/v1/models" for u in asked), asked)
    check("чат и распознавание разведены",
          [m["id"] for m in res["chat"]] == ["anthropic/claude-sonnet-4.6"]
          and [m["id"] for m in res["stt"]] == ["openai/whisper-1"], res)
    check("кэш на месте", bool(providers.cached_models("docs")))
    config.save({"api_base_url": "https://api.example.com/v1"})
    check("сменили адрес — прежний список не показываем", providers.cached_models("docs") is None)
    config.save({"api_base_url": "http://127.0.0.1/v1"})
    try:
        providers.list_models("docs", refresh=True)
        check("по негодному адресу не спрашиваем", False, "спросили")
    except RuntimeError as err:
        check("по негодному адресу не спрашиваем", "не годится" in str(err), str(err))
finally:
    providers.http_get = _real_get

say("")
say("=== 6. Документы ===")
sent = []


def fake_call(kind):
    def call(c, prompt, text, key, model):
        sent.append((kind, c["base_url"], key, model))
        return "# Документ"
    return call


_real_calls = dict(minutes.CALLS)
minutes.CALLS = {k: fake_call(k) for k in minutes.CALLS}
try:
    isolate.settings(api_base_url="https://api.anthropic.com/v1", api_keys={"docs": KEY})
    minutes._run_engine("api", "инструкция", "стенограмма", role="fast")
    check("Anthropic по адресу — свой обработчик, быстрая модель",
          sent[-1] == ("anthropic", "https://api.anthropic.com/v1", KEY, "claude-haiku-4-5"),
          sent[-1:])
    isolate.settings(api_base_url="https://polza.ai/api/v1", api_keys={"docs": KEY})
    try:
        minutes._run_engine("api", "инструкция", "стенограмма")
        check("у сервиса как OpenAI модель не угадывается", False, sent[-1:])
    except RuntimeError as err:
        check("у сервиса как OpenAI модель не угадывается", "модель" in str(err), str(err))
    eng = minutes.available_engines()[1]
    check("и движок не готов, сказано про модель", eng["ready"] is False
          and "модель" in eng["reason"], eng)
    config.save({"api_model": "anthropic/claude-sonnet-4.6"})
    minutes._run_engine("api", "инструкция", "стенограмма")
    check("указанная модель делает всё",
          sent[-1] == ("openai", "https://polza.ai/api/v1", KEY, "anthropic/claude-sonnet-4.6"),
          sent[-1:])
    eng = minutes.available_engines()[1]
    check("движок готов, в строке сервис и модель", eng["ready"] and "polza.ai" in eng["reason"]
          and "anthropic/claude-sonnet-4.6" in eng["reason"] and KEY not in eng["reason"], eng)
    check("плашка называет сервис", "polza.ai" in minutes.cloud_warning("api"))
    isolate.settings(api_base_url="https://polza.ai/api/v1")
    try:
        minutes._run_engine("api", "инструкция", "стенограмма")
        check("без ключа не отправляем", False, sent[-1:])
    except RuntimeError as err:
        check("без ключа не отправляем", "ключ" in str(err), str(err))
    isolate.settings(api_base_url="https://llm.api.cloud.yandex.net",
                     api_keys={"docs": KEY}, api_folder="b1g", minutes_engine="api")
    check("у YandexGPT кусок меньше", minutes._chunk_limit("api") == 40000,
          minutes._chunk_limit("api"))
finally:
    minutes.CALLS = _real_calls

say("")
say("=== 7. Подсказка «?»: подходящие адреса ===")
hosts = {providers.service_name(s["base_url"]): s for s in providers.SUGGESTED}
check("RouterAI в подсказке", hosts.get("routerai.ru", {}).get("base_url")
      == "https://routerai.ru/api/v1", sorted(hosts))
check("OpenRouter в подсказке — с оговоркой",
      "иностранный" in hosts.get("openrouter.ai", {}).get("note", ""), sorted(hosts))
check("polza, LLM Router и Groq на месте",
      {"polza.ai", "llm-router.org", "api.groq.com"} <= set(hosts), sorted(hosts))
bad_urls = []
for s in providers.SUGGESTED:
    try:
        providers.validate_base_url(s["base_url"])
    except ValueError as err:
        bad_urls.append((s["base_url"], str(err)))
check("все адреса подсказки проходят проверку адреса", bad_urls == [], bad_urls)
check("у каждого есть название и пометка",
      all(s.get("title") and s.get("note") for s in providers.SUGGESTED))
check("GigaChat из подсказки разговаривает по-своему",
      providers.kind_for(hosts["gigachat.devices.sberbank.ru"]["base_url"]) == "gigachat")

say("")
say("=== 8. Где распознавать — из настроек ===")
from hagen import media  # noqa: E402
from hagen.api import deps  # noqa: E402

isolate.settings(asr_files="cloud")
check("задание без выбора — как в настройках", media.asr_where({}) == "cloud")
check("задание может сказать своё", media.asr_where({"asr_files": "local"}) == "local")
asked_parts = []
_real_part = deps.require_part
deps.require_part = lambda key: asked_parts.append(key)
try:
    deps.require_media_parts({"asr_lang": "ru"})
    check("в облаке местная модель не требуется", asked_parts == [], asked_parts)
    config.save({"asr_files": "local"})
    deps.require_media_parts({"asr_lang": "ru"})
    check("на этом компьютере — требуется", len(asked_parts) == 1, asked_parts)
finally:
    deps.require_part = _real_part

sys.exit(finish("t101"))
