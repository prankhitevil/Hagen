# -*- coding: utf-8 -*-
"""Проверка 48: SharePoint и Teams — поиск, пачка, остановка. Без живого входа.

Живой вход по коду проверяется руками (нужна рабочая учётная запись). Здесь
Microsoft Graph подменён: ответы того же вида, что у настоящего, включая
грабли, собранные старым проектом.

  1. Поиск листает страницы, слушает Retry-After, убирает повторы, берёт дату
     встречи из имени файла, фильтрует «только записи встреч».
  2. Вход по коду идёт своим потоком: очередь задач в это время свободна.
  3. Пометка 📝: расшифровка Teams в Stream.
  4. Скачивание по настройкам формы: «что хранить», «брать готовый текст»;
     понятное объяснение отказа 403.
  5. Пачка через службу: записи обработаны, поиск помечает их «✓ уже есть»;
     «Остановить» снимает пачку; сбой не оставляет пустой папки.
"""
import io
import json
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
import isolate  # noqa: E402  настоящая база голосов не трогается
from harness import LINES, FAIL, say, check, finish  # noqa: E402

isolate.voices()


from hagen import jobs, store  # noqa: E402
from hagen.sources import sharepoint as sp  # noqa: E402


# ------------------------------------------------------------ поддельный Graph
class Resp:
    def __init__(self, status=200, data=None, content=b"", headers=None):
        self.status_code = status
        self._data = data
        self.content = content if content else (json.dumps(data).encode() if data is not None else b"")
        self.headers = headers or {}

    def json(self):
        if self._data is None:
            raise ValueError("не JSON")
        return self._data

    @property
    def text(self):
        return self.content.decode("utf-8", "replace")

    def iter_bytes(self, n):
        for i in range(0, len(self.content), n):
            yield self.content[i:i + n]

    def read(self):
        return self.content


class Stream:
    def __init__(self, resp):
        self.resp = resp

    def __enter__(self):
        return self.resp

    def __exit__(self, *a):
        return False


CALLS = []


class Client:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def post(self, url, headers=None, json=None, data=None):
        CALLS.append(("POST", url, json or data))
        return ROUTER("POST", url, json or data)

    def get(self, url, headers=None, timeout=None):
        CALLS.append(("GET", url, None))
        return ROUTER("GET", url, None)

    def stream(self, method, url, headers=None):
        CALLS.append(("STREAM", url, None))
        return Stream(ROUTER("STREAM", url, None))


sp._client = lambda read_timeout=60.0: Client()
PAUSES = []
sp._sleep = lambda seconds, handle=None: (PAUSES.append(seconds), time.sleep(0.05))


def hit(iid, name, size, modified, web="https://contoso.sharepoint.com/personal/p/Documents/Recordings/x"):
    return {"resource": {"id": iid, "name": name, "size": size, "lastModifiedDateTime": modified,
                         "parentReference": {"driveId": "d1"}, "webUrl": web}}


MB = 1048576
PAGE1 = [
    hit("v1", "Планёрка-20260515_150245-Meeting Recording.mp4", 300 * MB, "2026-05-20T10:00:00Z"),
    hit("t1", "Планёрка-20260515_150245-Meeting Recording.vtt", 30000, "2026-05-20T10:00:00Z"),
    hit("f1", "Футбол финал.mp4", 500 * MB, "2026-05-02T10:00:00Z",
        web="https://contoso.sharepoint.com/sites/hr/Shared Documents/Video/f.mp4"),
    hit("v1copy", "Планёрка-20260515_150245-Meeting Recording.mp4", 300 * MB, "2026-05-21T10:00:00Z"),
]
PAGE2 = [
    hit("v2", "Еженедельное-20260402_100000-Запись собрания.mp4", 200 * MB, "2026-04-03T09:00:00Z"),
    hit("v3", "Созвон-20260410_120000-Meeting Recording.mp4", 0, "2026-04-10T13:00:00Z"),
    hit("v4", "Июньская-20260601_120000-Meeting Recording.mp4", 100 * MB, "2026-05-31T23:00:00Z"),
]
STATE = {"page2_429": True, "polls": 0, "dl_status": 200}


def ROUTER(method, url, body):
    if url.endswith("/search/query"):
        req = body["requests"][0]
        STATE.setdefault("kql", req["query"]["queryString"])
        if req["from"] == 0:
            return Resp(data={"value": [{"hitsContainers": [{"hits": PAGE1, "moreResultsAvailable": True}]}]})
        if STATE["page2_429"]:
            STATE["page2_429"] = False
            return Resp(429, data={"error": "throttled"}, headers={"Retry-After": "1"})
        return Resp(data={"value": [{"hitsContainers": [{"hits": PAGE2, "moreResultsAvailable": False}]}]})
    if url.endswith("/devicecode"):
        return Resp(data={"device_code": "DEVICE-SECRET", "user_code": "ABCD-1234",
                          "verification_uri": "https://microsoft.com/devicelogin",
                          "expires_in": 900, "interval": 2})
    if url.endswith("/token"):
        scope = (body or {}).get("scope", "")
        if "sharepoint.com/.default" in scope:
            return Resp(data={"access_token": "eyJsp.a.b", "expires_in": 3600})
        if (body or {}).get("grant_type", "").endswith("device_code"):
            STATE["polls"] += 1
            if STATE["polls"] < 6:
                return Resp(400, data={"error": "authorization_pending"})
        return Resp(data={"access_token": "eyJgraph.a.b", "refresh_token": "R", "expires_in": 3600})
    if "/media/transcripts" in url and "streamContent" not in url:
        has = "/items/v2/" in url
        return Resp(data={"value": [{"id": "tr1"}] if has else []})
    if "streamContent" in url:
        return Resp(data={"entries": [{"startOffset": "00:00:01.0000000", "endOffset": "00:00:04.0000000",
                                       "text": "Начинаем планёрку.", "speakerDisplayName": "Иван Петров"}]})
    if "/children" in url:
        return Resp(data={"value": []})
    if "/drives/d1/items/v2" in url and method == "GET":
        return Resp(data={"size": 3 * MB, "parentReference": {"id": "p", "driveId": "d1"},
                          "webUrl": "https://contoso.sharepoint.com/personal/p/Documents/Recordings/v2.mp4",
                          "@microsoft.graph.downloadUrl": "https://dl.example/v2?tempauth=SECRET"})
    if method == "STREAM":
        if STATE["dl_status"] != 200:
            return Resp(STATE["dl_status"], data={"error": "no"})
        return Resp(content=b"\x00" * (2 * MB), headers={"Content-Length": str(2 * MB)})
    return Resp(404, data={"error": "нет такого адреса в подделке: %s" % url})


say("=== 1. Поиск ===")
sp._state.update({"access": "eyJgraph.a.b", "access_until": time.time() + 3600, "refresh": "R"})
res = sp.search_ex("", 2026, [4, 5], meetings_only=True)
names = [i["name"] for i in res["items"]]
check("страниц две — вторая тоже прочитана", any("Еженедельное" in n for n in names), names)
check("после 429 ждали столько, сколько просил Microsoft", 1.0 in PAUSES, PAUSES)
check("видео и его расшифровка — одна строка", sum("Планёрка" in n for n in names) == 1, names)
check("копия того же файла не задвоена", len([i for i in res["items"] if "Планёрка" in i["name"]]) == 1)
check("не встреча (футбол) скрыта и посчитана", "Футбол финал.mp4" not in names and res["hidden"] == 1,
      res["hidden"])
check("июньская встреча отброшена по дате ВСТРЕЧИ, хотя файл изменён в мае",
      not any("Июньская" in n for n in names), names)
plan = next(i for i in res["items"] if "Планёрка" in i["name"])
check("дата встречи взята из имени файла", plan["date"] == "2026-05-15", plan["date"])
check("у планёрки есть видео и расшифровка", plan["has_video"] and plan["has_transcript"] is True)
empty = next(i for i in res["items"] if "Созвон" in i["name"])
check("пустой контейнер Teams — без видео", empty["has_video"] is False and "🎬" not in empty["label"])
weekly = next(i for i in res["items"] if "Еженедельное" in i["name"])
check("расшифровка неизвестна, пока не проверили", weekly["has_transcript"] is None)
check("KQL с запасом на месяц после выбранного", "LastModifiedTime<2026-07-01" in STATE["kql"], STATE["kql"])
all_items = sp.search_ex("", 2026, [4, 5], meetings_only=False)
check("без фильтра видно и не встречи", any("Футбол" in i["name"] for i in all_items["items"]))

say("")
say("=== 3. Пометка 📝 ===")
flags = sp.check_transcripts([{"id": "v2", "driveId": "d1", "web_url": weekly["web_url"]},
                              {"id": "v3", "driveId": "d1", "web_url": empty["web_url"]}])
check("у записи с расшифровкой Teams — есть", flags.get("v2") is True, flags)
check("у записи без неё — нет", flags.get("v3") is False, flags)

say("")
say("=== 4. Скачивание по настройкам формы ===")
tmp = PROJECT / "data" / "_t48"
import shutil  # noqa: E402

shutil.rmtree(tmp, ignore_errors=True)
item = {"id": "v2", "driveId": "d1", "name": "Еженедельное-20260402_100000-Запись собрания.mp4",
        "kind": "video", "web_url": weekly["web_url"]}
CALLS.clear()
got = sp.fetch(item, tmp / "a", opts={"store_media": "none", "prefer_transcript": True})
streams = [c for c in CALLS if c[0] == "STREAM"]
check("текст есть, хранить нечего — видео не качаем", got["transcript"] and not got["media"] and not streams,
      (got["media"], len(streams)))
vtt = Path(got["transcript"]).read_text(encoding="utf-8") if got["transcript"] else ""
check("расшифровка Teams с именем говорящего", "<v Иван Петров>" in vtt, vtt[:120])
got = sp.fetch(item, tmp / "b", opts={"store_media": "video", "prefer_transcript": True})
check("«видео со звуком» — видео скачано", got["media"] and Path(got["media"]).exists())
got = sp.fetch(item, tmp / "c", opts={"store_media": "none", "prefer_transcript": False})
check("галочка «брать готовый текст» снята — текста нет, видео скачано",
      not got["transcript"] and got["media"])
STATE["dl_status"] = 403
# Живой случай 13.09: запись в OneDrive организатора — видео 403, расшифровка есть
err = ""
got = {}
try:
    got = sp.fetch(item, tmp / "d", opts={"store_media": "video", "prefer_transcript": True})
except Exception as e:
    err = str(e)
check("403 на видео при готовой расшифровке — не ошибка, работаем по тексту",
      not err and got.get("transcript") and got.get("media") == "", err[:160] or got.get("media"))
check("отказ по видео отмечен", (got.get("extra") or {}).get("video_forbidden") is True, got.get("extra"))
err = ""
try:
    sp.fetch(item, tmp / "e", opts={"store_media": "video", "prefer_transcript": False})
except Exception as e:
    err = str(e)
check("403 без расшифровки — ошибка с объяснением про OneDrive организатора",
      "OneDrive организатора" in err and "метка конфиденциальности" in err, err[:160])
check("секретная ссылка в текст ошибки не попала", "tempauth" not in err and "SECRET" not in err)
STATE["dl_status"] = 200
shutil.rmtree(tmp, ignore_errors=True)

say("")
say("=== 2 и 5. Сквозь службу ===")
from fastapi.testclient import TestClient  # noqa: E402

from hagen import server  # noqa: E402

ORIGIN = {"Origin": "http://127.0.0.1:8787"}
events = []
real_pub = server.hub.publish
server.hub.publish = lambda p: (events.append(p), real_pub(p))
made = []

with TestClient(server.app, base_url="http://127.0.0.1:8787") as cli:
    sp.logout()
    r = cli.post("/api/sharepoint/login", headers=ORIGIN)
    login = r.json().get("login") or {}
    check("код и ссылка выданы", login.get("code") == "ABCD-1234", login)
    check("служебный device_code наружу не ушёл", "DEVICE-SECRET" not in r.text)
    st = cli.get("/api/sharepoint/status").json()
    check("после перезагрузки страницы код можно показать снова",
          (st.get("pending") or {}).get("code") == "ABCD-1234", st)

    ran = threading.Event()
    jid = jobs.submit("probe", lambda h: ran.set(), "Проверка 48: очередь свободна")
    check("пока ждём код, очередь задач свободна", ran.wait(3.0))
    for _ in range(60):
        if sp.logged_in():
            break
        time.sleep(0.1)
    check("вход подтверждён", sp.logged_in())
    time.sleep(0.3)
    check("окно получило «вход выполнен»",
          any(e.get("type") == "sp_login" and e.get("state") == "ok" for e in events))

    # пачка: подменяем скачивание — важен путь через очередь и записи
    def fake_fetch(it, dst_dir, handle=None, opts=None):
        if it.get("id") == "boom":
            raise RuntimeError(sp.FORBIDDEN_HINT)
        for _ in range(int(it.get("slow") or 0) * 10):
            if handle is not None and handle.cancelled:
                raise RuntimeError("отменено")
            time.sleep(0.1)
        dst_dir.mkdir(parents=True, exist_ok=True)
        p = dst_dir / "t.vtt"
        p.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Иван Петров>Привет.</v>\n",
                     encoding="utf-8")
        return {"media": "", "transcript": str(p), "title": Path(it["name"]).stem,
                "url": "", "source_name": "sharepoint", "duration_s": 3.0, "extra": {}}

    sp.fetch = fake_fetch
    opts = {"video_kind": "transcript", "store_media": "none", "prefer_transcript": True,
            "make_summary": False}
    picked = [dict(i, id=i["id"]) for i in res["items"] if i["id"] in ("v1", "v2")]
    picked.append({"id": "boom", "driveId": "d1", "name": "Закрытая-20260420_100000-Meeting Recording.mp4"})
    r = cli.post("/api/sharepoint/batch", headers=ORIGIN, json=dict(opts, items=picked))
    batch = r.json()
    made += [s["rec_id"] for s in batch.get("started", [])]
    check("пачка принята: три задачи", len(batch.get("started", [])) == 3, batch)

    def wait_jobs(ids, limit=60):
        for _ in range(limit * 10):
            states = [(jobs.get(j) or {}).get("status") for j in ids]
            if all(s in ("done", "error", "cancelled") for s in states):
                return states
            time.sleep(0.1)
        return [(jobs.get(j) or {}).get("status") for j in ids]

    states = wait_jobs([s["job_id"] for s in batch["started"]])
    check("две обработаны, закрытая — с ошибкой", sorted(states) == ["done", "done", "error"], states)
    boom = next(s for s in batch["started"] if s["name"].startswith("Закрытая"))
    bmeta = store.get(boom["rec_id"]) or {}
    folder = bmeta.get("assets_folder")
    check("сбой не оставил пустую папку в хранилище видео",
          not folder or not store.assets_dir(boom["rec_id"], folder).exists(), folder)
    r = cli.post("/api/sharepoint/search", headers=ORIGIN,
                 json={"year": 2026, "month_from": 4, "month_to": 5, "meetings_only": True})
    marks = {i["id"]: bool(i.get("processed")) for i in r.json()["items"]}
    check("поиск помечает обработанные «✓ уже есть»", marks.get("v1") and marks.get("v2"), marks)
    check("необработанные без пометки", marks.get("v3") is False, marks)

    slow = [{"id": "s%d" % k, "driveId": "d1", "name": "Медленная %d-20260405_100000-Meeting Recording.mp4" % k,
             "slow": 3} for k in range(3)]
    r = cli.post("/api/sharepoint/batch", headers=ORIGIN, json=dict(opts, items=slow))
    b2 = r.json()
    made += [s["rec_id"] for s in b2.get("started", [])]
    time.sleep(0.8)
    r = cli.post("/api/sharepoint/batch/%s/cancel" % b2["batch"], headers=ORIGIN)
    check("«Остановить» снял всю пачку", r.json().get("stopped") == 3, r.json())
    states = wait_jobs([s["job_id"] for s in b2["started"]])
    check("ни одна из остановленных не доделана", "done" not in states, states)

    for rid in made:
        if store.get(rid):
            cli.delete("/api/recordings/%s?scope=all" % rid, headers=ORIGIN)
    check("тестовые записи убраны", all(store.get(x) is None for x in made))

server.hub.publish = real_pub
html = (PROJECT / "hagen" / "static" / "index.html").read_text(encoding="utf-8")
for el in ("sp-m1", "sp-m2", "sp-meetings", "btn-sp-process", "btn-sp-stop", "btn-sp-copy", "sp-count"):
    check("на странице есть %s" % el, 'id="%s"' % el in html)

sys.exit(finish("t48"))
