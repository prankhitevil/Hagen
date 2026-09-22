# -*- coding: utf-8 -*-
"""Проверка 91: отбор и группировка списка записей (20.09).

Список растёт, и плоская лента перестаёт помогать. Отбор («показать только
эти») и группировка («сложить по папкам проектов») — разные вещи, и нужны обе.
Проект и теги приходят с каждой записью, поэтому и отбор, и группировка живут
на странице: службу о них не спрашивают.

Проверяется настоящая страница в WebView2 на подставных записях (как t49):
логика отбора в JavaScript из Python не видна, а опечатка в ней ломает список
целиком, оставляя проверки службы зелёными.

Что проверяем:
  1. отбор по проекту, по тегу и обоими сразу;
  2. отбор, под который ничего не подошло, говорит об этом, а не «записей нет»;
  3. выбранное значение остаётся в списке отбора, даже если под него пусто;
  4. группировка: папки проектов, счётчик, записи без проекта — последними;
  5. сворачивание папки прячет её записи, счётчик остаётся;
  6. отбор и группировка работают вместе;
  7. складывание по проектам запоминается в настройках;
  8. сервис по ключу — поля «адрес, ключ, модель» вместо списка сервисов;
     «где распознавать» — в настройках, облачных полей нет при «на этом
     компьютере»; модель — выпадающий список с отбором по вводу.

Запуск из корня проекта:  .venv\\Scripts\\python.exe tests\\t91_list_groups.py
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

# Настройки в памяти: страница сохраняет сюда «складывать по проектам», и
# настоящий settings.json проверка не трогает.
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

# Горячую клавишу диктовки не занимаем: рядом может работать сам Hagen.
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

# Подставные записи: два проекта, один без проекта, теги пересекаются.
LIST_JS = r"""
(async function(){
  const out = {errors: [], checks: {}};
  const t = async (name, fn) => {
    try { out.checks[name] = await fn(); } catch (e) { out.errors.push(name + ': ' + e.message); }
  };
  const rec = (id, title, project, tags) => ({
    id: id, title: title, project: project, tags: tags,
    created_at: '2026-09-20T10:00:00', duration_s: 600, status: 'recorded',
    source: 'live', diarized: true, voices: {mine: 1, others: 2},
  });
  S.mode = 'rec';
  S.currentId = null;
  S.projectFolders = {'АКП Digital': 'Projects/АКП'};
  S.recordings = [
    rec('r1', 'Планёрка по смете', 'АКП Digital', ['смета', 'планёрка']),
    rec('r2', 'Сроки поставки', 'АКП Digital', ['смета']),
    rec('r3', 'Договор подряда', 'Ремонт офиса', ['договор']),
    rec('r4', 'Разговор без проекта', '', ['планёрка']),
  ];
  const titles = () => [...document.querySelectorAll('#rec-list .rec-item .nm')]
    .map((x) => x.textContent);
  const groups = () => [...document.querySelectorAll('#rec-list .rec-group-head')]
    .map((x) => [x.querySelector('.grp-name').textContent,
                 x.querySelector('.grp-count').textContent]);

  await t('без отбора видны все записи', () => {
    S.listFilter = {project: '', tag: ''};
    S.groupByProject = false;
    paintSidebar();
    return titles().length === 4 || JSON.stringify(titles());
  });
  await t('отбор по проекту оставляет только его записи', () => {
    S.listFilter = {project: 'АКП Digital', tag: ''};
    paintSidebar();
    const got = titles();
    return (got.length === 2 && got.includes('Планёрка по смете')
      && got.includes('Сроки поставки')) || JSON.stringify(got);
  });
  await t('отбор по тегу идёт поперёк проектов', () => {
    S.listFilter = {project: '', tag: 'планёрка'};
    paintSidebar();
    const got = titles();
    return (got.length === 2 && got.includes('Планёрка по смете')
      && got.includes('Разговор без проекта')) || JSON.stringify(got);
  });
  await t('проект и тег вместе сужают список', () => {
    S.listFilter = {project: 'АКП Digital', tag: 'планёрка'};
    paintSidebar();
    const got = titles();
    return (got.length === 1 && got[0] === 'Планёрка по смете') || JSON.stringify(got);
  });
  await t('регистр в отборе не важен', () => {
    S.listFilter = {project: 'акп digital', tag: 'СМЕТА'};
    paintSidebar();
    return titles().length === 2 || JSON.stringify(titles());
  });
  await t('пустой отбор говорит про отбор, а не «записей нет»', () => {
    S.listFilter = {project: 'Ремонт офиса', tag: 'смета'};
    paintSidebar();
    const text = document.getElementById('rec-list').textContent;
    return (titles().length === 0 && /отбор/.test(text)) || text.slice(0, 120);
  });
  await t('выбранный проект остаётся в списке отбора, даже когда под него пусто', () => {
    S.listFilter = {project: 'Ушедший проект', tag: ''};
    paintSidebar();
    const opts = [...document.getElementById('flt-project').options].map((o) => o.value);
    return (opts.includes('Ушедший проект')
      && document.getElementById('flt-project').value === 'Ушедший проект') || JSON.stringify(opts);
  });
  await t('в отборе только те проекты и теги, что есть у записей', () => {
    S.listFilter = {project: '', tag: ''};
    paintSidebar();
    const projects = [...document.getElementById('flt-project').options].map((o) => o.value).filter(Boolean);
    const tags = [...document.getElementById('flt-tag').options].map((o) => o.value).filter(Boolean);
    return (projects.length === 2 && projects.includes('АКП Digital')
      && projects.includes('Ремонт офиса') && tags.length === 3)
      || JSON.stringify({projects, tags});
  });

  await t('группировка складывает список по проектам', () => {
    S.listFilter = {project: '', tag: ''};
    S.groupByProject = true;
    S.foldedProjects = {};
    paintSidebar();
    const g = groups();
    return (g.length === 3 && g[0][0] === 'АКП Digital' && g[0][1] === '2'
      && g[1][0] === 'Ремонт офиса' && g[1][1] === '1') || JSON.stringify(g);
  });
  await t('записи без проекта — последней папкой', () => {
    const g = groups();
    return (g[g.length - 1][0] === 'Без проекта' && g[g.length - 1][1] === '1') || JSON.stringify(g);
  });
  await t('в папках видны все записи', () => titles().length === 4 || JSON.stringify(titles()));
  await t('свёрнутая папка прячет свои записи, счётчик остаётся', () => {
    S.foldedProjects = {'акп digital': true};
    paintSidebar();
    const got = titles();
    const g = groups();
    return (got.length === 2 && !got.includes('Планёрка по смете')
      && g[0][1] === '2') || JSON.stringify({got, g});
  });
  await t('щелчок по заголовку папки сворачивает и разворачивает её', () => {
    S.foldedProjects = {};
    paintSidebar();
    document.querySelector('#rec-list .rec-group-head').click();
    const folded = titles().length;
    document.querySelector('#rec-list .rec-group-head').click();
    const back = titles().length;
    return (folded === 2 && back === 4) || JSON.stringify({folded, back});
  });
  await t('отбор и группировка работают вместе', () => {
    S.listFilter = {project: '', tag: 'смета'};
    S.foldedProjects = {};
    paintSidebar();
    const g = groups();
    return (g.length === 1 && g[0][0] === 'АКП Digital' && g[0][1] === '2')
      || JSON.stringify(g);
  });
  await t('у папки проекта в подсказке видна его папка в сейфе', () => {
    const head = document.querySelector('#rec-list .rec-group-head');
    return /Projects\/АКП/.test(head.title) || head.title;
  });
  await t('запись открывается щелчком и из папки', () => {
    let opened = '';
    const real = window.openRecording;
    window.openRecording = (id) => { opened = id; };
    try {
      paintSidebar();
      document.querySelector('#rec-list .rec-item').click();
    } finally { window.openRecording = real; }
    return !!opened || 'щелчок никуда не привёл';
  });
  await t('кнопка складывания показывает своё состояние', () => {
    S.groupByProject = false;
    paintSidebar();
    const off = document.getElementById('btn-group').getAttribute('aria-pressed');
    S.groupByProject = true;
    paintSidebar();
    const on = document.getElementById('btn-group').getAttribute('aria-pressed');
    return (off === 'false' && on === 'true') || JSON.stringify({off, on});
  });
  await t('складывание по проектам уходит в настройки', async () => {
    S.groupByProject = false;
    paintSidebar();
    document.getElementById('btn-group').click();
    await new Promise((r) => setTimeout(r, 400));
    return S.groupByProject === true || 'кнопка не переключила вид';
  });

  // Отметки в списке (20.09): Ctrl+щелчок набирает несколько записей, чтобы
  // удалить их разом; Delete открывает то же окно, что и кнопка «Удалить».
  const ctrlClick = (i) => {
    const items = [...document.querySelectorAll('#rec-list .rec-item')];
    items[i].dispatchEvent(new MouseEvent('click', {ctrlKey: true, bubbles: true}));
  };
  await t('Ctrl+щелчок отмечает запись, а не открывает её', () => {
    S.listFilter = {project: '', tag: ''};
    S.groupByProject = false;
    S.selected = [];
    S.currentId = null;
    paintSidebar();
    let opened = '';
    const real = window.openRecording;
    window.openRecording = (id) => { opened = id; };
    try { ctrlClick(0); } finally { window.openRecording = real; }
    return (S.selected.length === 1 && !opened) || JSON.stringify({sel: S.selected, opened});
  });
  await t('отмеченная запись помечена в списке', () => {
    const marked = document.querySelectorAll('#rec-list .rec-item.picked').length;
    return marked === 1 || 'помечено строк: ' + marked;
  });
  await t('второй Ctrl+щелчок добавляет, а не заменяет', () => {
    ctrlClick(1);
    return S.selected.length === 2 || JSON.stringify(S.selected);
  });
  await t('повторный Ctrl+щелчок снимает отметку', () => {
    ctrlClick(1);
    return S.selected.length === 1 || JSON.stringify(S.selected);
  });
  await t('сколько отмечено — написано под списком', () => {
    const note = document.getElementById('picked-note');
    return (!note.classList.contains('hidden') && /Отмечено записей: 1/.test(note.textContent))
      || note.textContent;
  });
  await t('обычный щелчок снимает отметки и открывает запись', () => {
    let opened = '';
    const real = window.openRecording;
    window.openRecording = (id) => { opened = id; };
    try {
      document.querySelector('#rec-list .rec-item').click();
    } finally { window.openRecording = real; }
    return (S.selected.length === 0 && !!opened) || JSON.stringify({sel: S.selected, opened});
  });
  await t('при открытой записи первый Ctrl+щелчок берёт обе', () => {
    S.selected = [];
    S.currentId = 'r1';
    paintSidebar();
    ctrlClick(1);
    return (S.selected.length === 2 && S.selected.includes('r1'))
      || JSON.stringify(S.selected);
  });
  await t('без отметок удаляется открытая запись', () => {
    S.selected = [];
    S.currentId = 'r3';
    return JSON.stringify(pickedIds()) === '["r3"]' || JSON.stringify(pickedIds());
  });
  await t('Delete открывает то же окно, что и кнопка «Удалить»', () => {
    hideDialogs();               // не зависим от того, что осталось от прошлых проверок
    S.selected = ['r1', 'r2'];
    S.currentId = 'r1';
    S.current = {meta: (S.recordings || []).find((m) => m.id === 'r1')};
    const picked = JSON.stringify(pickedIds());
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Delete', bubbles: true}));
    const shown = !document.getElementById('dlg-delete').classList.contains('hidden');
    const text = document.getElementById('del-what').textContent;
    hideDialogs();
    return (shown && /Записей: 2/.test(text))
      || JSON.stringify({shown, text, picked, sel: S.selected,
        recs: (S.recordings || []).map((m) => m.id)});
  });
  await t('в поле ввода Delete окно не открывает', () => {
    hideDialogs();
    const input = document.getElementById('rec-project');
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Delete', bubbles: true}));
    const shown = !document.getElementById('dlg-delete').classList.contains('hidden');
    hideDialogs();
    return !shown || 'окно открылось из поля ввода';
  });
  S.selected = [];

  // «Домой» в разделе «Видео» (20.09): открытая запись занимает весь экран, и
  // добавить вторую ссылку было неоткуда — приходилось уходить в «Запись» и
  // возвращаться. Кнопка стоит там же, где «Новая запись» в другом разделе.
  const hidden = (id) => document.getElementById(id).classList.contains('hidden');
  await t('в разделе «Запись» кнопки «домой» нет', () => {
    setMode('rec');
    return (hidden('btn-video-home') && !hidden('btn-new')) || 'кнопки перепутаны';
  });
  await t('в разделе «Видео» она заменяет «Новую запись»', () => {
    setMode('video');
    return (!hidden('btn-video-home') && hidden('btn-new')) || 'кнопки перепутаны';
  });
  await t('она видна и с открытой карточки видео', () => {
    S.showRecord = true;
    paintAll();
    return !hidden('btn-video-home') || 'с карточки кнопку не видно';
  });
  await t('щелчок возвращает к добавлению ссылки или файла', () => {
    S.showRecord = true;
    paintAll();
    document.getElementById('btn-video-home').click();
    const onForm = S.showRecord === false
      && !document.getElementById('video-view').classList.contains('hidden')
      && document.getElementById('rec-view').classList.contains('hidden');
    return onForm || JSON.stringify({showRecord: S.showRecord,
      video: hidden('video-view'), rec: hidden('rec-view')});
  });
  await t('запись при этом остаётся выбранной в списке', () => {
    return S.currentId !== undefined || 'выбор записи потерян';
  });
  setMode('rec');

  // Сервис по ключу (21.09): адрес, ключ и модель вместо списка сервисов.
  // Что с подключением, говорит служба; страница только показывает.
  await t('списка сервисов больше нет', () =>
    !document.getElementById('set-provider') || 'список сервисов на месте');
  await t('поля заполняются из настроек, ключ в поле не попадает', () => {
    S.settings = Object.assign({}, S.settings, {
      api_base_url: 'https://api.anthropic.com/v1', api_model: 'claude-opus-5',
      api_kind: '', api_folder: '', asr_files: 'cloud', asr_base_url: 'https://polza.ai/api/v1',
      asr_model: 'openai/whisper-1'});
    S.caps = S.caps || {};
    S.caps.connections = {
      docs: {base_url: 'https://api.anthropic.com/v1', kind: 'anthropic', kind_manual: false,
             kind_title: 'как Anthropic', service: 'api.anthropic.com',
             model: 'claude-opus-5', has_key: true, key_hint: 'sk-a******xy', problem: ''},
      asr: {base_url: 'https://polza.ai/api/v1', kind: 'openai', kind_title: 'как OpenAI',
            service: 'polza.ai', model: 'openai/whisper-1', has_key: false, key_hint: '',
            problem: 'Не задан ключ для polza.ai.'},
    };
    fillConnections();
    const v = (id) => document.getElementById(id).value;
    return (v('set-api-url') === 'https://api.anthropic.com/v1' && v('set-model') === 'claude-opus-5'
      && v('set-apikey') === '' && v('set-cloud-asr-url') === 'https://polza.ai/api/v1')
      || JSON.stringify({url: v('set-api-url'), model: v('set-model'), key: v('set-apikey')});
  });
  await t('виден ключ маской и как разговаривает сервис', () => {
    const key = document.getElementById('apikey-state').textContent;
    const url = document.getElementById('api-url-state').textContent;
    const kind = document.getElementById('set-api-kind').options[0].textContent;
    return (/sk-a\*+xy/.test(key) && /api\.anthropic\.com/.test(url) && /как Anthropic/.test(kind))
      || JSON.stringify({key, url, kind});
  });
  await t('чего не хватает распознаванию — сказано', () => {
    const text = document.getElementById('cloud-asr-state').textContent;
    return /ключ/.test(text) || text;
  });
  await t('поле каталога — только у YandexGPT', () => {
    const hiddenNow = hidden('folder-field');
    S.caps.connections.docs = Object.assign({}, S.caps.connections.docs,
      {kind: 'yandexgpt', kind_title: 'как YandexGPT'});
    paintConnections();
    const shown = !hidden('folder-field');
    return (hiddenNow && shown) || JSON.stringify({hiddenNow, shown});
  });
  await t('ключ уходит в сохранение, только если его вписали', () => {
    const before = collectConnections();
    document.getElementById('set-apikey').value = 'новый-ключ-подлиннее';
    const after = collectConnections();
    document.getElementById('set-apikey').value = '';
    return (!before.api_keys && after.api_keys && after.api_keys.docs === 'новый-ключ-подлиннее'
      && !('asr' in after.api_keys) && after.api_base_url === 'https://api.anthropic.com/v1')
      || JSON.stringify({before, after});
  });

  // «Где распознавать» переехал из окна «Видео» в настройки (21.09); при
  // «на этом компьютере» облачных полей нет на экране и в сохранении
  // (решение 22.09: спрятанное поле ни на что не влияет).
  await t('в окне «Видео» выбора «где распознавать» больше нет', () =>
    !document.getElementById('v-where') || 'поле на месте');
  await t('на этом компьютере — облачных полей нет и в сохранение они не идут', () => {
    document.getElementById('set-asr-files').value = 'local';
    paintConnections();
    const box = document.getElementById('cloud-asr-fields');
    const patch = collectConnections();
    return (box.classList.contains('hidden') && !('asr_base_url' in patch) && !('asr_model' in patch))
      || JSON.stringify({cls: box.className, patch});
  });
  await t('в облаке — поля видны, и выбор уходит в настройки', () => {
    document.getElementById('set-asr-files').value = 'cloud';
    paintConnections();
    const box = document.getElementById('cloud-asr-fields');
    const els = [...box.querySelectorAll('input, button')];
    const patch = collectConnections();
    return (!box.classList.contains('hidden') && els.every((el) => !el.disabled)
      && patch.asr_files === 'cloud' && 'asr_base_url' in patch) || JSON.stringify({cls: box.className, where: patch.asr_files});
  });

  // Модель — выпадающий список с отбором по вводу.
  const shownModels = () => [...document.querySelectorAll('#api-models .ac-item b')]
    .map((b) => b.textContent);
  await t('список моделей открывается целиком и сужается отбором', () => {
    S.models = {docs: {chat: [
      {id: 'anthropic/claude-sonnet-4.6', title: 'Claude Sonnet 4.6', price: {}},
      {id: 'z-ai/glm-5.3-flash', title: 'GLM 5.3 Flash', price: {}},
      {id: 'openai/gpt-5.6-sol', title: 'GPT-5.6 Sol', price: {}}]}};
    const input = document.getElementById('set-model');
    input.dispatchEvent(new Event('focus'));
    const all = shownModels().length;
    input.value = 'glm';
    input.dispatchEvent(new Event('input'));
    const left = shownModels();
    return (all === 3 && left.length === 1 && left[0] === 'z-ai/glm-5.3-flash')
      || JSON.stringify({all, left});
  });
  await t('слова отбора — в любом порядке и по подписи', () => {
    const input = document.getElementById('set-model');
    input.value = 'sonnet claude';
    input.dispatchEvent(new Event('input'));
    const left = shownModels();
    return (left.length === 1 && left[0] === 'anthropic/claude-sonnet-4.6') || JSON.stringify(left);
  });
  // «?» у адреса сервиса: по щелчку — подходящие адреса, у каждого «Копировать».
  const pop = () => document.getElementById('services-pop');
  await t('«?» открывает список подходящих адресов', async () => {
    document.getElementById('btn-services').click();
    for (let i = 0; i < 50 && pop().classList.contains('hidden'); i++) {
      await new Promise((r) => setTimeout(r, 100));
    }
    const text = pop().textContent;
    const rows = pop().querySelectorAll('tr').length;
    const buttons = pop().querySelectorAll('.js-copy-url').length;
    return (!pop().classList.contains('hidden') && rows >= 10 && buttons === rows
      && /routerai\.ru\/api\/v1/.test(text) && /openrouter\.ai/.test(text))
      || JSON.stringify({hidden: pop().classList.contains('hidden'), rows, buttons});
  });
  await t('«Копировать» кладёт адрес в буфер', async () => {
    let copied = null;
    const real = navigator.clipboard.writeText;
    navigator.clipboard.writeText = async (s) => { copied = s; };
    const row = [...pop().querySelectorAll('tr')].find((r) => /RouterAI/.test(r.textContent));
    row.querySelector('.js-copy-url').click();
    await new Promise((r) => setTimeout(r, 50));
    navigator.clipboard.writeText = real;
    return copied === 'https://routerai.ru/api/v1' || String(copied);
  });
  await t('Esc закрывает список', () => {
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}));
    return pop().classList.contains('hidden') || 'список открыт';
  });

  await t('стрелкой и Enter пункт выбирается в поле, список закрывается', () => {
    const input = document.getElementById('set-model');
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
    input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
    const closed = document.getElementById('api-models').classList.contains('hidden');
    return (input.value === 'anthropic/claude-sonnet-4.6' && closed)
      || JSON.stringify({value: input.value, closed});
  });

  window.__t91 = JSON.stringify(out);
})();
"""

window = webview.create_window("t91", "http://127.0.0.1:%d/" % PORT,
                               hidden=True, width=1200, height=800)
result = {}


def scenario():
    errors_js = ("window.__t91errs = []; "
                 "window.addEventListener('error', (e) => window.__t91errs.push(e.message));")
    try:
        for _ in range(100):
            # Ждём, пока страница сама прочтёт состояние: иначе её loadState,
            # пришедший позже, затирал подставные записи пустым списком службы.
            ready = window.evaluate_js(
                "typeof S !== 'undefined' && typeof paintSidebar === 'function'"
                " && !!document.getElementById('flt-project')"
                " && Object.keys(S.settings || {}).length > 0")
            if ready:
                break
            time.sleep(0.2)
        result["ready"] = bool(ready)
        if not ready:
            window.destroy()
            return
        window.evaluate_js(errors_js)
        window.evaluate_js(LIST_JS)
        for _ in range(60):
            time.sleep(0.2)
            got = window.evaluate_js("window.__t91")
            if got:
                result["list"] = json.loads(got)
                break
        result["late_errors"] = window.evaluate_js("JSON.stringify(window.__t91errs)")
    except Exception as err:
        result["fatal"] = str(err)
    window.destroy()


webview.start(scenario, gui="edgechromium", private_mode=True)
srv.should_exit = True
th.join(timeout=20)

say("=== Отбор и группировка списка записей ===")
check("страница загрузилась, отбор на месте", result.get("ready"), result.get("fatal", ""))
res = result.get("list") or {}
check("проверки отработали", bool(res.get("checks")), res or "не дождались")
check("ошибок при отрисовке нет", not res.get("errors"), res.get("errors"))
for name, ok in (res.get("checks") or {}).items():
    check(name, ok is True, ok)
check("складывание по проектам сохранилось в настройках",
      any("list_group_by_project" in p for p in SAVED), SAVED[-4:])
check("поздних ошибок JavaScript нет", result.get("late_errors") in ("[]", None),
      result.get("late_errors"))

config_mod.save, config_mod.get, config_mod.public = _real_save, _real_get, _real_public

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t91_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
