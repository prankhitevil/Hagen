# -*- coding: utf-8 -*-
"""Проверка 93: копирование документа в трёх видах (20.09).

Документ внутри хранится в Markdown — это одна правда. На выходе три отрисовки
под три места, куда его вставляют:

  * Markdown — как есть, для заметок и Obsidian;
  * оформленный текст — для Word и Outlook: заголовки, списки и таблицы. Кладём
    парой «HTML + простой текст», Word берёт из буфера первое;
  * Telegram — его упрощённая разметка: жирный и курсив есть, таблиц и
    заголовков нет, списки точками, таблицы строками.

Отрисовка живёт на странице: документ рисуется тем же `mdBlocks`, которым он
показан на экране, второго рисовальщика Markdown в программе нет. Поэтому
проверяется настоящая страница в WebView2, как в t49.

Что проверяем:
  1. Markdown копируется как есть, со всей разметкой;
  2. оформленный текст: заголовки, списки и таблица стали HTML, наших крючков
     (классов вычёркивания, кнопок-таймкодов) в нём не осталось;
  3. Telegram: заголовки жирным, списки точками, таблица строками, разделитель
     таблицы выброшен;
  4. выбранный вид запоминается в настройках;
  5. меню выбора открывается и отмечает прошлый выбор.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t93_copy_formats.py
"""
import io
import json
import socket
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))
import isolate  # noqa: E402

isolate.voices()
isolate.settings()

LINES = []
FAIL = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


def check(name, ok, detail=""):
    if not ok:
        FAIL.append(name)
    say(("   ok    " if ok else "   ПЛОХО ") + name + (": " + str(detail) if detail != "" else ""))


import uvicorn  # noqa: E402
import webview  # noqa: E402

from hagen import config as config_mod  # noqa: E402
from hagen import server  # noqa: E402

SAVED = []
VIEW = {"ui_theme": "light", "ui_density": "compact"}
_real_save, _real_get, _real_public = config_mod.save, config_mod.get, config_mod.public


def _fake_save(patch):
    SAVED.append(dict(patch))
    VIEW.update(patch)
    return dict(VIEW)


config_mod.save = _fake_save
config_mod.get = lambda k, d=None: VIEW[k] if k in VIEW else _real_get(k, d)
config_mod.public = lambda: dict(_real_public(), **VIEW)

server._start_dictation = lambda: None

with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
server.set_actual_port(PORT)
srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning"))
th = threading.Thread(target=srv.run, daemon=True)
th.start()
for _ in range(200):
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            break
    time.sleep(0.1)

# Документ со всем, что умеет отличаться между видами: заголовки, жирный,
# курсив, список, нумерованный список, таблица, разделитель, таймкод.
COPY_JS = r"""
(async function(){
  const out = {errors: [], checks: {}};
  const t = async (name, fn) => {
    try { out.checks[name] = await fn(); } catch (e) { out.errors.push(name + ': ' + e.message); }
  };
  const MD = [
    '# Протокол совещания',
    '',
    '## Участники',
    '- **Иван Петров** — поставщик',
    '- Орлов — *покупатель*',
    '',
    '## Задачи',
    '| Задача | Ответственный | Срок |',
    '|---|---|---|',
    '| Прислать смету | Иван Петров | 25.09 |',
    '',
    '## Решения',
    '1. Смету согласовать до пятницы.',
    '',
    '---',
    'Обсуждали в [00:12:30] и позже.',
  ].join('\n');
  S.docs = [{key: 'protocol', title: 'Протокол', markdown: MD}];
  S.docShown = 'protocol';
  S.currentId = null;
  paintDocBody(currentDoc());

  // Буфер обмена в спрятанном окне недоступен — подменяем его и смотрим, что
  // туда кладут. Проверяем отрисовку, а не сам буфер Windows.
  const wrote = {text: null, items: null};
  const realClip = navigator.clipboard;
  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: {
    writeText: async (x) => { wrote.text = x; wrote.items = null; },
    write: async (items) => { wrote.items = items; wrote.text = null; },
  }});
  const blobText = async (item, type) => {
    const b = await item.getType(type);
    return await b.text();
  };

  await t('Markdown копируется как есть', async () => {
    await copyDocAs('md');
    return (wrote.text === MD) || String(wrote.text).slice(0, 120);
  });

  await t('оформленный текст кладётся парой HTML и простого текста', async () => {
    await copyDocAs('rich');
    if (!wrote.items || !wrote.items.length) return 'пары нет: ' + String(wrote.text).slice(0, 80);
    const types = wrote.items[0].types || [];
    return ([...types].includes('text/html') && [...types].includes('text/plain'))
      || JSON.stringify([...types]);
  });
  await t('в оформленном тексте заголовки, списки и таблица стали HTML', async () => {
    const html = await blobText(wrote.items[0], 'text/html');
    return (/<h2>/.test(html) && /<ul>/.test(html) && /<ol>/.test(html)
      && /<table>/.test(html) && /<b>Иван Петров<\/b>/.test(html)
      && /<i>покупатель<\/i>/.test(html)) || html.slice(0, 200);
  });
  await t('наших крючков в оформленном тексте не осталось', async () => {
    const html = await blobText(wrote.items[0], 'text/html');
    return (!/class=/.test(html) && !/data-i=/.test(html) && !/<button/.test(html)
      && !/js-goto-ts/.test(html)) || html.slice(0, 200);
  });
  await t('таймкод в оформленном тексте остался текстом', async () => {
    const html = await blobText(wrote.items[0], 'text/html');
    return /00:12:30/.test(html) || html.slice(-160);
  });

  await t('Telegram: заголовки жирным, без решёток', async () => {
    await copyDocAs('tg');
    const tg = wrote.text || '';
    return (/\*\*Протокол совещания\*\*/.test(tg) && !/^#/m.test(tg)) || tg.slice(0, 160);
  });
  await t('Telegram: список точками', () => {
    const tg = wrote.text || '';
    return /• \*\*Иван Петров\*\* — поставщик/.test(tg) || tg.slice(0, 200);
  });
  await t('Telegram: нумерованный список остался номерами', () => {
    return /1\. Смету согласовать/.test(wrote.text || '') || String(wrote.text).slice(0, 200);
  });
  await t('Telegram: таблица строками, разделитель выброшен', () => {
    const tg = wrote.text || '';
    return (/Прислать смету — Иван Петров — 25\.09/.test(tg) && !/\|/.test(tg)
      && !/---\|/.test(tg)) || tg;
  });
  await t('Telegram: пустых строк подряд нет', () => {
    return !/\n\n\n/.test(wrote.text || '') || JSON.stringify(wrote.text);
  });

  await t('выбранный вид запоминается', async () => {
    await copyDocAs('tg');
    await new Promise((r) => setTimeout(r, 400));
    return S.copyFormat === 'tg' || S.copyFormat;
  });
  await t('меню выбора открывается и отмечает прошлый выбор', () => {
    document.getElementById('btn-copy-minutes').click();
    const menu = document.getElementById('copy-menu');
    if (!menu) return 'меню не открылось';
    const marked = [...menu.querySelectorAll('button')]
      .filter((b) => b.textContent.startsWith('✓')).map((b) => b.dataset.fmt);
    const all = [...menu.querySelectorAll('button')].map((b) => b.dataset.fmt);
    menu.querySelector('button').click();
    return (all.length === 3 && marked.length === 1 && marked[0] === 'tg')
      || JSON.stringify({all, marked});
  });
  await t('щелчок мимо меню его закрывает', () => {
    document.getElementById('btn-copy-minutes').click();
    document.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
    return !document.getElementById('copy-menu') || 'меню осталось на экране';
  });

  Object.defineProperty(navigator, 'clipboard', {configurable: true, value: realClip});
  window.__t93 = JSON.stringify(out);
})();
"""

window = webview.create_window("t93", "http://127.0.0.1:%d/" % PORT,
                               hidden=True, width=1200, height=800)
result = {}


def scenario():
    errors_js = ("window.__t93errs = []; "
                 "window.addEventListener('error', (e) => window.__t93errs.push(e.message));")
    try:
        for _ in range(100):
            ready = window.evaluate_js(
                "typeof S !== 'undefined' && typeof copyDocAs === 'function'"
                " && !!document.getElementById('btn-copy-minutes')")
            if ready:
                break
            time.sleep(0.2)
        result["ready"] = bool(ready)
        if not ready:
            window.destroy()
            return
        window.evaluate_js(errors_js)
        window.evaluate_js(COPY_JS)
        for _ in range(60):
            time.sleep(0.2)
            got = window.evaluate_js("window.__t93")
            if got:
                result["copy"] = json.loads(got)
                break
        result["late_errors"] = window.evaluate_js("JSON.stringify(window.__t93errs)")
    except Exception as err:
        result["fatal"] = str(err)
    window.destroy()


webview.start(scenario, gui="edgechromium", private_mode=True)
srv.should_exit = True
th.join(timeout=20)

say("=== Копирование документа: Markdown, Word, Telegram ===")
check("страница загрузилась", result.get("ready"), result.get("fatal", ""))
res = result.get("copy") or {}
check("проверки отработали", bool(res.get("checks")), res or "не дождались")
check("ошибок при отрисовке нет", not res.get("errors"), res.get("errors"))
for name, ok in (res.get("checks") or {}).items():
    check(name, ok is True, ok)
check("выбранный вид дошёл до настроек",
      any(p.get("copy_format") == "tg" for p in SAVED), SAVED[-4:])
check("поздних ошибок JavaScript нет", result.get("late_errors") in ("[]", None),
      result.get("late_errors"))

config_mod.save, config_mod.get, config_mod.public = _real_save, _real_get, _real_public

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t93_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
