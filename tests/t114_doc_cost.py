# -*- coding: utf-8 -*-
"""Проверка 114: стоимость документа у сервиса по ключу (23.09).

Цены в программе не хранятся: показываем только то, что сервис сам назвал в
списке моделей. Три места: подсказка под полем «Модель» (протокол часового
звонка), оценка в окне «Сделать документ» по длине стенограммы и фактический
расход в подписи под документом — по usage из ответа сервиса.

Ни одного запроса в сеть. Что проверяем:
  1. разбор цен: polza (за миллион, с валютой) и OpenRouter (за токен,
     доллары, «-1» — не цена); без цен — пусто;
  2. цена модели по кэшу списка;
  3. оценка: у Claude CLI пусто; одна модель с ценой — считается; длинная
     стенограмма — больше запросов и дороже; без цены быстрой модели — пусто;
     «hour_cost» у списка моделей только там, где цена есть;
  4. копилка: с ценой и без, смесь валют, форма подписи;
  5. usage из ответов четырёх видов сервисов попадает в копилку только внутри
     сборки; подпись документа получает токены и стоимость; у Claude CLI
     подпись прежняя;
  6. окно «Сделать документ» получает оценку для каждого вида документа.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t114_doc_cost.py
"""
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402
from harness import say, check, finish  # noqa: E402

isolate.voices()
isolate.settings()


from hagen import config, minutes, providers, store  # noqa: E402

KEY = "ключ-подлиннее-восьми"
BASE = "https://polza.ai/api/v1"
POLZA = {"in_per_million": 100.0, "out_per_million": 300.0, "currency": "RUB"}


def cache_models(chat):
    """Положить список моделей в кэш, как будто его спросили у сервиса."""
    with providers._models_lock:
        providers._models_cache["docs"] = {
            "role": "docs", "base_url": BASE, "at": 10 ** 12,
            "chat": [dict(m, title=m["id"]) for m in chat], "stt": []}


say("=== 1. Разбор цен из списка моделей ===")
p = providers._price_of({"id": "m", "pricing": {"prompt_per_million": "100", "completion_per_million": 300,
                                                 "currency": "RUB"}})
check("polza: за миллион и валюта", p == POLZA, p)
# Так polza отдаёт модели чата на самом деле (живой запрос 23.09): цена — в top_provider.
p = providers._price_of({"id": "anthropic/claude-haiku-4.5", "top_provider": {
    "name": "drouter", "context_length": 200000,
    "pricing": {"prompt_per_million": "50.56800000", "completion_per_million": "50.56800000",
                "input_cache_read_per_million": "50.56800000", "currency": "RUB"}}})
check("polza: цена в top_provider",
      p == {"in_per_million": 50.568, "out_per_million": 50.568, "currency": "RUB"}, p)
p = providers._price_of({"id": "m", "pricing": {"prompt": "0.000003", "completion": "0.000015"}})
check("OpenRouter: за токен → за миллион, доллары",
      abs(p.get("in_per_million", 0) - 3.0) < 1e-9 and abs(p.get("out_per_million", 0) - 15.0) < 1e-9
      and p.get("currency") == "USD", p)
p = providers._price_of({"id": "m", "pricing": {"prompt": "-1", "completion": "-1"}})
check("OpenRouter: «-1» — не цена", p == {}, p)
p = providers._price_of({"id": "m", "pricing": {"prompt": "0", "completion": "0"}})
check("бесплатная модель — цена ноль, а не «неизвестно»", p.get("in_per_million") == 0.0, p)
check("без pricing — пусто", providers._price_of({"id": "m"}) == {})
check("мусор вместо числа — пропущен",
      providers._price_of({"id": "m", "pricing": {"prompt_per_million": "abc"}}) == {})

say("")
say("=== 2. Цена модели по кэшу ===")
isolate.settings(api_base_url=BASE, api_keys={"docs": KEY}, minutes_engine="api",
                 api_model="anthropic/claude-sonnet-4.6")
cache_models([{"id": "anthropic/claude-sonnet-4.6", "price": POLZA},
              {"id": "free/model", "price": {}}])
check("модель с ценой", providers.model_price("docs", "anthropic/claude-sonnet-4.6") == POLZA)
check("модель без цены — пусто", providers.model_price("docs", "free/model") == {})
check("неизвестная модель — пусто", providers.model_price("docs", "nope") == {})
check("пустое имя — пусто", providers.model_price("docs", "") == {})

say("")
say("=== 3. Оценка до отправки ===")
check("Claude CLI — оценки нет", minutes.estimate_cost(60000, "protocol", "claude_cli") == {})
est = minutes.estimate_cost(minutes.CHARS_PER_HOUR, "protocol")
check("час звонка одной моделью с ценой — посчитано",
      est.get("cost", 0) > 0 and est.get("currency") == "RUB" and est.get("requests") == 1, est)
check("текст оценки начинается с «≈» и кончается рублём",
      est.get("text", "").startswith("≈ ") and est["text"].endswith("₽"), est.get("text"))
# Вход ~27 тыс. токенов по 100 ₽/млн + выход 4 тыс. по 300 ₽/млн ≈ 4 ₽.
check("порядок величины разумный (2–6 ₽)", 2.0 < est["cost"] < 6.0, est["cost"])
long = minutes.estimate_cost(minutes.CHARS_PER_HOUR * 6, "protocol")
check("шесть часов — выжимки по кускам, запросов больше и дороже",
      long.get("requests", 0) > est["requests"] and long.get("cost", 0) > est["cost"] * 3, long)
check("пустая стенограмма — пусто", minutes.estimate_cost(0, "protocol") == {})
for kind in minutes.DOC_KINDS:
    e = minutes.estimate_cost(30000, kind)
    check("вид «%s» оценивается" % kind, e.get("cost", 0) > 0, e)

# Модель не указана: сильная и быстрая берутся из профиля, у polza их нет —
# оценки нет. У Anthropic профиль есть, но цен в списке нет — тоже пусто.
isolate.settings(api_base_url=BASE, api_keys={"docs": KEY}, minutes_engine="api")
cache_models([{"id": "anthropic/claude-sonnet-4.6", "price": POLZA}])
check("модель не выбрана — оценки нет", minutes.estimate_cost(60000) == {})

# Указанная модель делает всё — и выжимки: цена одна. А если бы выжимки шла
# другой моделью без цены, оценки бы не было.
cheap = {"in_per_million": 10.0, "out_per_million": 30.0, "currency": "RUB"}
got = minutes._cost_of(200000, "protocol", POLZA, {}, 150000)
check("длинная стенограмма, у быстрой модели цены нет — пусто", got == {}, got)
got = minutes._cost_of(200000, "protocol", POLZA, cheap, 150000)
check("длинная стенограмма, обе цены есть — посчитано", got.get("requests") == 3 and got["cost"] > 0, got)

labelled = minutes.label_models({"chat": [{"id": "a", "price": POLZA}, {"id": "b", "price": {}}],
                                 "stt": [], "cached": True})
check("hour_cost у модели с ценой", labelled["chat"][0].get("hour_cost", "").startswith("≈"), labelled)
check("hour_cost пуст у модели без цены", labelled["chat"][1].get("hour_cost") == "", labelled)
check("остальное списка не тронуто", labelled.get("cached") is True and labelled["stt"] == [])
check("пустой список — пустой", minutes.label_models(None) == {} and minutes.label_models({}) == {})

say("")
say("=== 4. Копилка ===")
s = minutes.Spend()
check("пустая — подписи нет", s.text() == "" and s.as_dict().get("cost") is None)
s.add(10000, 1000, POLZA)
check("с ценой: токены и стоимость", s.text() == "токенов: 11 000 · стоимость: 1,30 ₽", s.text())
s.add(5000, 500, POLZA)
check("складывается", s.tokens == 16500 and abs(s.cost - 1.95) < 1e-9, (s.tokens, s.cost))
d = s.as_dict()
check("в карточку: токены, запросы, стоимость, валюта",
      d == {"tokens_in": 15000, "tokens_out": 1500, "requests": 2, "cost": 1.95, "currency": "RUB"}, d)
s.add(1000, 100, {})
check("один запрос без цены — остаются одни токены",
      s.text() == "токенов: 17 600" and "cost" not in s.as_dict(), s.text())
s = minutes.Spend()
s.add(1000, 100, POLZA)
s.add(1000, 100, {"in_per_million": 3.0, "out_per_million": 15.0, "currency": "USD"})
check("смесь валют не складывается — одни токены", not s.priced and s.text() == "токенов: 2 200", s.text())
check("доллары", minutes.money(0.04, "USD") == "0,04 $" and minutes.money(0.004, "USD") == "0,004 $")
check("неизвестная валюта — кодом", minutes.money(2.5, "KZT") == "2,50 KZT")

say("")
say("=== 5. usage из ответа сервиса — в подпись документа ===")
ANSWERS = {
    "openai": {"choices": [{"message": {"content": "# Документ"}}],
               "usage": {"prompt_tokens": 20000, "completion_tokens": 3000}},
    "anthropic": {"content": [{"type": "text", "text": "# Документ"}],
                  "usage": {"input_tokens": 20000, "output_tokens": 3000}},
    "gigachat": {"choices": [{"message": {"content": "# Документ"}}],
                 "usage": {"prompt_tokens": 20000, "completion_tokens": 3000}},
    "yandexgpt": {"result": {"alternatives": [{"message": {"text": "# Документ"}}],
                             "usage": {"inputTextTokens": "20000", "completionTokens": "3000"}}},
}
posted = []


def fake_post(url, **kw):
    """Ответ по виду сервиса: он приходит в service, как имя для сообщений."""
    posted.append(url)
    return ANSWERS.get(kw.get("service"), ANSWERS["openai"])


_real_post = minutes._http_post
minutes._http_post = fake_post
_real_gc = providers.gigachat_token
providers.gigachat_token = lambda key: "токен"
try:
    for kind, url in (("openai", BASE), ("anthropic", "https://api.anthropic.com/v1"),
                      ("gigachat", "https://gigachat.devices.sberbank.ru/api/v1"),
                      ("yandexgpt", "https://llm.api.cloud.yandex.net")):
        conn = {"base_url": url, "service": kind, "kind": kind, "folder": "b1g"}
        with minutes._tracking() as spend:
            minutes.CALLS[kind](conn, "инструкция", "текст", KEY, "anthropic/claude-sonnet-4.6")
        check("%s: токены сосчитаны" % kind, spend.tokens_in == 20000 and spend.tokens_out == 3000,
              (spend.tokens_in, spend.tokens_out))
    conn = {"base_url": BASE, "service": "openai", "kind": "openai", "folder": ""}
    with minutes._tracking() as spend:
        minutes.CALLS["openai"](conn, "и", "т", KEY, "anthropic/claude-sonnet-4.6")
    check("модель с ценой — стоимость посчитана", spend.priced and abs(spend.cost - 2.9) < 1e-9, spend.cost)
    with minutes._tracking() as spend:
        minutes.CALLS["openai"](conn, "и", "т", KEY, "free/model")
    check("модель без цены — одни токены", not spend.priced and spend.tokens == 23000)
    check("вне сборки копилки нет и ничего не падает",
          minutes.CALLS["openai"](conn, "и", "т", KEY, "x") == "# Документ")
    with minutes._tracking() as spend:
        minutes._note_usage("openai", {"choices": []}, "x")
        minutes._note_usage("openai", {"usage": {"prompt_tokens": "abc"}}, "x")
        minutes._note_usage("openai", "не словарь", "x")
    check("ответ без usage или с мусором — копилка пуста", spend.tokens == 0 and spend.calls == 0)
finally:
    minutes._http_post = _real_post
    providers.gigachat_token = _real_gc

# Подпись документа.
for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 114"):
        store.delete(old["id"])
rid = store.create(title="Проверка 114 — стоимость", mode="online", source="live")["id"]
try:
    s = minutes.Spend()
    s.add(20000, 3000, POLZA)
    path = minutes._save_result(rid, "protocol", "# Протокол совещания\nтекст", "api", spend=s)
    md = path.read_text(encoding="utf-8")
    check("подпись: движок, токены и стоимость",
          "движок: api · токенов: 23 000 · стоимость: 2,90 ₽._" in md, md[-120:])
    doc = (store.get(rid) or {}).get("documents", {}).get("protocol") or {}
    check("расход в карточке записи", doc.get("spend", {}).get("cost") == 2.9, doc)
    s = minutes.Spend()
    s.add(20000, 3000, {})
    md = minutes._save_result(rid, "protocol", "# Протокол совещания\nтекст", "api", spend=s).read_text(encoding="utf-8")
    check("без цены — только токены", "движок: api · токенов: 23 000._" in md, md[-120:])
    md = minutes._save_result(rid, "protocol", "# Протокол совещания\nтекст", "claude_cli").read_text(encoding="utf-8")
    check("Claude CLI — подпись прежняя", "движок: claude_cli._" in md, md[-120:])
    doc = (store.get(rid) or {}).get("documents", {}).get("protocol") or {}
    check("у прежней подписи расхода в карточке нет", "spend" not in doc, doc)
    config.save({"sign_documents": False})
    md = minutes._save_result(rid, "protocol", "# Протокол совещания\nтекст", "api", spend=s).read_text(encoding="utf-8")
    check("короткая подпись — расход через запятую", "движок: api, токенов: 23 000._" in md, md[-120:])
    config.save({"sign_documents": True})

    say("")
    say("=== 6. Окно «Сделать документ» получает оценку ===")
    from fastapi.testclient import TestClient  # noqa: E402

    from hagen import server  # noqa: E402

    store.replace_segments(rid, [store.make_segment("mic", 0, 5, "Смету подготовлю к четвергу. " * 200)])
    isolate.settings(api_base_url=BASE, api_keys={"docs": KEY}, minutes_engine="api",
                     api_model="anthropic/claude-sonnet-4.6")
    cache_models([{"id": "anthropic/claude-sonnet-4.6", "price": POLZA}])
    with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
        res = cli.get("/api/documents/kinds?rec_id=%s" % rid).json()
        costs = res.get("costs") or {}
        check("оценка для каждого вида документа",
              set(costs) == set(minutes.DOC_KINDS) and all(v.startswith("≈") for v in costs.values()), costs)
        config.save({"minutes_engine": "claude_cli"})
        res = cli.get("/api/documents/kinds?rec_id=%s" % rid).json()
        check("у Claude CLI оценки пустые", all(v == "" for v in (res.get("costs") or {}).values()), res.get("costs"))
        res = cli.get("/api/documents/kinds").json()
        check("без записи — без оценок", res.get("costs") == {}, res.get("costs"))
finally:
    store.delete(rid)

sys.exit(finish("t114"))
