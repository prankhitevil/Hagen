# -*- coding: utf-8 -*-
"""Проверка 49: страница программы загружается без ошибок JavaScript.

Python-проверки не видят ошибок в app.js и video.js: опечатка в скрипте
ломает окно целиком, а тесты службы остаются зелёными. Здесь служба
поднимается внутри этого процесса (без отдельных процессов — Kaspersky),
страница открывается в настоящем WebView2 в спрятанном окне, и новые части
интерфейса отрисовываются на подставных данных:

  * вопрос автоматики звонков с кнопками и обратным отсчётом;
  * блок «Claude упёрся в лимит» с кнопками;
  * SharePoint: фильтры, список с галочками и пометками, счётчик;
  * подсказки «переподключается…» у полосок уровня.
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
# Настоящие settings.json и база голосов не читаются: раньше get/public падали
# в настоящие настройки, и проверка краснела, когда меняли сочетание
# диктовки или свои замены (найдено 14.09 на рабочем ноутбуке).
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
        # Консоль не знает этих букв (у нас бывает cp1251) — пишем без них.
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

# Настоящий settings.json проверка не трогает: выбор темы и плотности на
# странице сохраняется сюда, а служба читает его отсюда же.
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

# Служба при старте занимает горячую клавишу диктовки, если она включена в
# настоящих настройках. Отбирать рабочее сочетание проверка не
# должна — тем более что настоящий «Hagen» может быть запущен рядом.
# Саму диктовку проверяет t56, здесь нужен только вид её настроек.
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

INJECT = r"""
(function(){
  const out = {errors: [], checks: {}};
  const t = (name, fn) => { try { out.checks[name] = fn(); } catch (e) { out.errors.push(name + ': ' + e.message); } };
  t('функции на месте', () => ['paintPrompt','paintDocAlert','paintMeters','videoInit','videoSpEvent','videoJobChanged']
      .filter((f) => typeof window[f] !== 'function' && typeof eval(f) !== 'function').length === 0);
  t('вопрос с кнопками', () => {
    S.prompt = {id:'p1', kind:'call_ended', text:'Звонок закончился. Остановить?',
      buttons:[{id:'stop',label:'Остановить'},{id:'keep',label:'Писать дальше'}],
      deadline: Date.now()/1000 + 30, default:'stop'};
    paintPrompt();
    const box = document.getElementById('call-toast');
    return !box.classList.contains('hidden') && box.querySelectorAll('button').length === 2
      && /Без ответа через \d+ с/.test(document.getElementById('call-meeting').textContent);
  });
  t('вопрос снимается', () => { S.prompt = null; paintPrompt();
    return document.getElementById('call-toast').classList.contains('hidden'); });
  t('блок лимита', () => {
    S.currentId = 'rec-x';
    S.caps = Object.assign({}, S.caps, {engines:[{key:'api', ready:true, provider_title:'Anthropic'}]});
    S.jobs = [{id:'j1', rec_id:'rec-x', kind:'minutes', status:'error', created_at: 5,
      error_kind:'claude_limit', error:'Подписка Claude упёрлась в пятичасовой лимит. Сбросится в 14:00.',
      retry:{url:'/api/recordings/rec-x/minutes', body:{template:'protocol'}}}];
    paintDocAlert();
    const box = document.getElementById('doc-alert');
    return !box.classList.contains('hidden') && /Собрать через облако/.test(box.textContent)
      && /14:00/.test(box.textContent);
  });
  t('SharePoint: список и счётчик', () => {
    document.querySelector('.mode-btn[data-mode="video"]') && document.querySelector('.mode-btn[data-mode="video"]').click();
    document.querySelector('.src-btn[data-src="sharepoint"]').click();
    const m1 = document.getElementById('sp-m1');
    return m1.options.length === 12;
  });
  t('подсказка «переподключается»', () => {
    S.recordingId = 'rec-x'; S.currentId = 'rec-x'; S.farLost = true; S.farOn = true;
    paintMeters();
    const ok = document.getElementById('far-hint').textContent === 'переподключается…';
    S.recordingId = null; S.farLost = false;
    return ok;
  });
  return JSON.stringify(out);
})()
"""

RENDER_SP = r"""
(function(){
  try {
    V.sp.items = [
      {id:'a', driveId:'d', name:'Планёрка-20260515_150245-Meeting Recording.mp4', date:'2026-05-15',
       has_video:true, has_transcript:true, size: 314572800, duration_s: 3600, processed: null},
      {id:'b', driveId:'d', name:'Созвон.mp4', date:'2026-04-10', has_video:false, has_transcript:null,
       processed:{rec_id:'r1', title:'Созвон'}}];
    V.sp.hidden = 4; V.sp.sel = new Set([0]);
    vSpRender();
    const rows = document.querySelectorAll('#sp-results .sp-item');
    return JSON.stringify({rows: rows.length, count: document.getElementById('sp-count').textContent,
      btn: document.getElementById('btn-sp-process').textContent,
      marks: rows[0].querySelector('.sp-marks').textContent,
      done: rows[1].textContent.indexOf('уже есть') >= 0});
  } catch (e) { return JSON.stringify({error: e.message}); }
})()
"""

UI_DOCS = r"""
(function(){
  window.__ui2 = null;
  (async () => {
    const out = {errors: [], checks: {}};
    const t = async (name, fn) => { try { out.checks[name] = await fn(); } catch (e) { out.errors.push(name + ': ' + e.message); } };
    const $ = (id) => document.getElementById(id);
    await t('шестерёнка настроек — картинка', () => !!document.querySelector('#btn-settings svg'));
    await t('кнопки «Краткое резюме» больше нет', () => !$('btn-summary'));
    // «авторазметка ✓» с экрана записи убрана (этап 6, п. 6.1): это общая
    // настройка, а стояла среди кнопок записи и читалась как её состояние.
    await t('подписи «авторазметка» на экране записи нет', () =>
      !$('auto-diarize-note') && typeof window.paintAutoDiarize === 'undefined');
    // Строка внизу панели говорит, чем делаются транскрипты и документы
    // (замечено 17.09): раньше там было только про распознавание.
    await t('строка состояния программы: транскрипты и протоколы', () => {
      S.caps = Object.assign({}, S.caps, {engines: [
        {key: 'claude_cli', title: 'Claude CLI', ready: true},
        {key: 'api', title: 'Сторонний сервис по ключу', ready: true, provider_title: 'Polza'}]});
      S.settings = Object.assign({}, S.settings, {minutes_engine: 'claude_cli'});
      paintProgramState();
      const cli = $('work-state').textContent;
      S.settings = Object.assign({}, S.settings, {minutes_engine: 'api'});
      paintProgramState();
      const api2 = $('work-state').textContent;
      return (cli === 'транскрипты локально, протоколы — CLI'
        && api2 === 'транскрипты локально, протоколы — по API (Polza)') || JSON.stringify([cli, api2]); });
    await t('имя владельца вместо «Я»', () => {
      const keep = S.settings.owner_name;
      S.settings.owner_name = 'Иван П.'; const a = displaySpeaker('Я'), b = displaySpeaker('Иван');
      S.settings.owner_name = keep;
      return a === 'Иван П.' && b === 'Иван'; });
    await openRecording('__REC__');
    await loadMinutesFor('__REC__');
    // Этап 6, п. 6.1: у готовой записи блока записи нет (он занимал 250 точек
    // и «Старт» дописывал запись), у новой — наоборот, нет вкладок и панелей.
    const hid = (id) => $(id).classList.contains('hidden');
    await t('готовая запись: блока записи нет, вкладки на месте', () =>
      (hid('live-controls') && !hid('rec-tabs') && !hid('rec-actions') && !hid('rec-status'))
      || JSON.stringify({live: hid('live-controls'), tabs: hid('rec-tabs'),
                         actions: hid('rec-actions'), status: hid('rec-status')}));
    await t('пустая запись: блок записи есть, вкладок нет', () => {
      const meta = S.current.meta, segs = S.current.segments;
      const keep = meta.duration_s;
      meta.duration_s = 0; S.current.segments = [];
      paintAll();
      const ok = !hid('live-controls') && hid('rec-tabs') && hid('rec-actions') && hid('rec-status');
      meta.duration_s = keep; S.current.segments = segs;
      paintAll();
      return ok || 'блок записи и вкладки не поменялись местами'; });
    await t('предупреждение про весь звук — полосой и не постоянно', () =>
      (hid('far-warning') && !!$('far-warning').closest('.ctl-warn')
       && !/петл/i.test($('far-warning').textContent)) || $('far-warning').textContent.slice(0, 60));
    await t('у записи две вкладки документов, открыт последний', () => {
      // После перекройки экрана (этап 6) документы живут на своей вкладке, и
      // при открытии записи она не выскакивает сама: человек читает
      // стенограмму. Вкладку надо выбрать — вот и выбираем.
      const tabs = document.querySelectorAll('#minutes-tabs .doc-tab');
      if (tabs.length !== 2) return 'вкладок документов: ' + tabs.length;
      showPane('doc');
      return /САММАРИ-49/.test($('minutes-text').textContent)
        && !$('pane-doc').classList.contains('hidden'); });
    await t('вкладка «Протокол» показывает протокол', () => {
      document.querySelector('#minutes-tabs .doc-tab[data-k="protocol"]').click();
      return /ПРОТОКОЛ-49/.test($('minutes-text').textContent); });
    // Найдено 17.09: со «Стенограммы» вкладка документа не
    // открывалась — документ выбирался, а на экране ничего не менялось.
    await t('документ отрисован: заголовок, список, выделение, таблица', () => {
      document.querySelector('#minutes-tabs .doc-tab[data-k="protocol"]').click();
      const box = $('minutes-text');
      const got = {h: box.querySelectorAll('h3').length, li: box.querySelectorAll('li').length,
        b: box.querySelectorAll('b').length, td: box.querySelectorAll('td').length,
        raw: /##|\*\*/.test(box.textContent)};
      return (got.h === 1 && got.li === 2 && got.b === 1 && got.td === 2 && !got.raw)
        || JSON.stringify(got); });
    await t('таймкод в документе ведёт на реплику', async () => {
      const link = $('minutes-text').querySelector('.js-goto-ts');
      if (!link) return 'ссылки нет';
      link.click();
      await new Promise((r) => setTimeout(r, 200));
      return (S.pane === 'transcript' && !!$('transcript').querySelector('.line.flash'))
        || JSON.stringify({pane: S.pane, flash: !!$('transcript').querySelector('.line.flash')}); });
    await t('вкладка документа открывается со «Стенограммы»', () => {
      showPane('transcript');
      document.querySelector('#minutes-tabs .doc-tab[data-k="meeting"]').click();
      return (S.pane === 'doc' && !$('pane-doc').classList.contains('hidden')
        && /САММАРИ-49/.test($('minutes-text').textContent)
        && document.querySelector('#minutes-tabs .doc-tab[data-k="meeting"]').classList.contains('active'))
        || JSON.stringify({pane: S.pane, text: $('minutes-text').textContent.slice(0, 40)}); });
    await t('«Скрыть» документа прячет текст, кнопки остаются', () => {
      $('btn-hide-minutes').click();
      const ok = $('minutes-text').classList.contains('folded') && $('btn-hide-minutes').textContent === 'Показать'
        && !$('pane-doc').classList.contains('hidden') && $('btn-hide-minutes').offsetParent !== null
        && getComputedStyle($('minutes-text')).display === 'none';
      $('btn-hide-minutes').click();
      return ok && !$('minutes-text').classList.contains('folded') && $('btn-hide-minutes').textContent === 'Скрыть'; });
    // Кнопки стенограммы живут в своей группе панели действий, поэтому сначала
    // возвращаемся на вкладку стенограммы (этап 6).
    showPane('transcript');
    // Вместо двух «Свернуть» — одна кнопка «Скрыть» с меню из двух пунктов
    // (решение 17.09): «Текст» и «Имена».
    const hideItem = (cls) => {
      $('btn-hide').click();
      const b = document.querySelector('#hide-menu .' + cls);
      b.click();
    };
    await t('«Скрыть → Текст» прячет стенограмму, кнопки остаются', () => {
      if ($('btn-hide').disabled) return 'кнопка выключена';
      hideItem('js-text');
      const ok = getComputedStyle($('transcript')).display === 'none'
        && $('btn-hide').offsetParent !== null && $('btn-hide').classList.contains('on')
        && !$('btn-copy-transcript').disabled && !!$('btn-hide').querySelector('svg');
      hideItem('js-text');
      return (ok && getComputedStyle($('transcript')).display !== 'none')
        || JSON.stringify({ display: getComputedStyle($('transcript')).display,
                            on: $('btn-hide').classList.contains('on') }); });
    await t('«Скрыть → Имена» закрашивает имена, текст остаётся', () => {
      hideItem('js-names');
      const who = $('transcript').querySelector('.who');
      const st = getComputedStyle(who);
      // getComputedStyle живой: снимаем значения, пока имена закрашены.
      const snap = {cls: $('transcript').className, mask: !!S.maskNames,
        color: st.webkitTextFillColor, bg: st.backgroundColor,
        shown: getComputedStyle($('transcript')).display};
      hideItem('js-names');
      const ok = snap.mask && /masked/.test(snap.cls)
        && snap.color === 'rgba(0, 0, 0, 0)' && snap.bg !== 'rgba(0, 0, 0, 0)'
        && snap.shown !== 'none';
      return (ok && !$('transcript').classList.contains('masked')) || JSON.stringify(snap); });
    await t('в меню «Скрыть» видно, что уже скрыто', () => {
      hideItem('js-names');
      $('btn-hide').click();
      const txt = document.querySelector('#hide-menu .js-names').textContent;
      document.querySelector('#hide-menu .js-names').click();
      return txt.indexOf('✓') === 0 || txt; });
    await t('скрытость — у каждой записи своя, не общая (16.09)', () => {
      const mine = S.currentId;
      hideItem('js-text');
      $('btn-hide-minutes').click();
      const folded = foldOf('transcript') && foldOf('minutes')
        && $('transcript').classList.contains('folded') && $('minutes-text').classList.contains('folded');
      S.currentId = 'другая-запись';          // как будто открыли соседнюю запись
      paintFolds();
      const other = !$('transcript').classList.contains('folded')
        && !$('minutes-text').classList.contains('folded')
        && $('btn-hide-minutes').textContent === 'Скрыть'
        && !$('btn-hide').classList.contains('on');
      S.currentId = mine;                      // вернулись к своей — снова скрыто
      paintFolds();
      const back = $('transcript').classList.contains('folded')
        && $('minutes-text').classList.contains('folded')
        && $('btn-hide').classList.contains('on');
      hideItem('js-text');
      $('btn-hide-minutes').click();
      return (folded && other && back) || JSON.stringify({folded, other, back}); });
    await openMinutes();
    await t('окно «Сделать документ»: 5 видов и выбор типа записи', () =>
      !$('dlg-minutes').classList.contains('hidden')
      && document.querySelectorAll('#tpl-list .tpl').length === 5
      && $('doc-video-kind').options.length === 4 && S.tplChosen === 'protocol');
    await t('тип «лекция» подставляет конспект', () => {
      $('doc-video-kind').value = 'lecture'; $('doc-video-kind').onchange();
      return S.tplChosen === 'lecture' && $('question-field').classList.contains('hidden'); });
    await t('«вопрос» открывает поле вопроса', () => {
      document.querySelector('#tpl-list .tpl[data-k="question"]').click();
      return !$('question-field').classList.contains('hidden'); });
    hideDialogs();
    await loadPrompts();
    // Решение 22.09: виды документов — списком строк, текст правится в своём окне.
    await t('«Документы»: пять видов списком и имя', () =>
      document.querySelectorAll('#prompt-list .prompt-row').length === 5 && !!$('set-owner'));
    await t('вид открывается в своём окне; правка помечается, «Вернуть исходный» возвращает', () => {
      document.querySelector('#prompt-list .prompt-row').click();
      const opened = !$('dlg-prompt').classList.contains('hidden');
      const ta = $('prompt-text');
      ta.value = ta.value + ' ещё'; ta.oninput();
      const changed = /Изменён/.test($('prompt-state').textContent);
      $('btn-prompt-reset').click();
      const back = /Исходный/.test($('prompt-state').textContent);
      hideDialogs();
      return (opened && changed && back && $('dlg-prompt').classList.contains('hidden'))
        || JSON.stringify({opened, changed, back}); });
    // --- правка стенограммы: настоящие щелчки и клавиши (16.09) ---
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const until = async (fn, ms) => {
      const end = Date.now() + (ms || 4000);
      while (Date.now() < end) { if (fn()) return true; await sleep(50); }
      return false;
    };
    const nLines = () => document.querySelectorAll('#transcript .line').length;
    const words = () => document.querySelectorAll('#transcript .line .txt .w');
    await t('режим правки включается и слова становятся кнопками', async () => {
      if ($('btn-edit-transcript').disabled) return 'кнопка выключена';
      $('btn-edit-transcript').click();
      await sleep(100);
      return S.editMode && words().length > 0 && !$('edit-hint').classList.contains('hidden');
    });
    await t('простой щелчок по слову не режет реплику', async () => {
      const was = nLines();
      words()[1].dispatchEvent(new MouseEvent('click', {bubbles: true}));
      await sleep(400);
      return nLines() === was || ('стало ' + nLines() + ' из ' + was);
    });
    await t('Alt+щелчок режет реплику', async () => {
      const was = nLines();
      const w = [...words()].find((x) => Number(x.dataset.i) === 1);
      w.dispatchEvent(new MouseEvent('click', {bubbles: true, altKey: true}));
      const ok = await until(() => nLines() === was + 1);
      return ok || ('стало ' + nLines() + ' из ' + was);
    });
    // Главная ловушка: в русской раскладке клавиша Z даёт «я», и проверка по
    // букве не срабатывала вовсе (найдено 16.09).
    await t('Ctrl+Z в русской раскладке отменяет разрез', async () => {
      const was = nLines();
      document.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'я', code: 'KeyZ', ctrlKey: true, bubbles: true}));
      const ok = await until(() => nLines() === was - 1);
      return ok || ('осталось ' + nLines() + ' из ' + was);
    });
    await t('Ctrl+Z в латинской раскладке тоже отменяет', async () => {
      const w = [...words()].find((x) => Number(x.dataset.i) === 1);
      const was = nLines();
      w.dispatchEvent(new MouseEvent('click', {bubbles: true, altKey: true}));
      if (!await until(() => nLines() === was + 1)) return 'разрез не прошёл';
      document.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'z', code: 'KeyZ', ctrlKey: true, bubbles: true}));
      const ok = await until(() => nLines() === was);
      return ok || ('осталось ' + nLines() + ' из ' + was);
    });
    await t('кнопка «Отменить правку» отменяет и показывает запас', async () => {
      const was = nLines();
      const w = [...words()].find((x) => Number(x.dataset.i) === 1);
      w.dispatchEvent(new MouseEvent('click', {bubbles: true, altKey: true}));
      if (!await until(() => nLines() === was + 1)) return 'разрез не прошёл';
      if (!await until(() => !$('btn-undo-edit').classList.contains('hidden'))) return 'кнопки нет';
      $('btn-undo-edit').click();
      const ok = await until(() => nLines() === was);
      return (ok && $('btn-undo-edit').classList.contains('hidden'))
        || ('осталось ' + nLines() + ' из ' + was);
    });
    await t('вне режима правки Ctrl+Z ничего не отменяет', async () => {
      const w = [...words()].find((x) => Number(x.dataset.i) === 1);
      const was = nLines();
      w.dispatchEvent(new MouseEvent('click', {bubbles: true, altKey: true}));
      if (!await until(() => nLines() === was + 1)) return 'разрез не прошёл';
      $('btn-edit-transcript').click();                     // «Готово»
      document.dispatchEvent(new KeyboardEvent('keydown',
        {key: 'я', code: 'KeyZ', ctrlKey: true, bubbles: true}));
      await sleep(500);
      const kept = nLines() === was + 1;
      $('btn-edit-transcript').click();                     // снова «Править»
      if (!await until(() => !$('btn-undo-edit').classList.contains('hidden'))) return 'запас пропал';
      $('btn-undo-edit').click();                           // прибрали за собой
      await until(() => nLines() === was);
      $('btn-edit-transcript').click();
      return kept || 'отменилось без режима правки';
    });
    window.__ui2 = JSON.stringify(out);
  })().catch((e) => { window.__ui2 = JSON.stringify({errors: ['сбой: ' + e.message], checks: {}}); });
  return 'started';
})()
"""

# Запись с двумя документами — для вкладок, «Свернуть» и окна документа
from hagen import minutes as minutes_mod  # noqa: E402
from hagen import store  # noqa: E402

for old in store.list_all():
    if str(old.get("title") or "").startswith("Проверка 49"):
        store.delete(old["id"])
REC = store.create(title="Проверка 49", mode="online", source="live", category="Встречи")["id"]
# Запись завершена: у новой и у идущей записи экран другой — блок записи вместо
# вкладок (этап 6, п. 6.1), и кнопок стенограммы на нём нет.
store.update(REC, {"status": "recorded", "duration_s": 9.0})
store.replace_segments(REC, [store.make_segment("mic", 0.0, 3.0, "Начинаем."),
                             store.make_segment("far", 4.0, 9.0,
                                                "Добрый день коллеги а теперь передаю слово")])
# Документ с разметкой: по нему проверяется отрисовка (п. 6.4) — заголовки,
# списки, выделение, таблица и таймкод-ссылка.
minutes_mod._save_result(REC, "protocol",
                         "## Итоги\n\nПРОТОКОЛ-49\n\n"
                         "- первый пункт **важно** [00:00:04]\n"
                         "- второй пункт\n\n"
                         "| Кто | Что |\n|---|---|\n| Орлов | акт |\n", "claude_cli")
minutes_mod._save_result(REC, "meeting", "## Главное\n\nСАММАРИ-49", "claude_cli")
UI_DOCS = UI_DOCS.replace("__REC__", REC)

result = {}
window = webview.create_window("t49", "http://127.0.0.1:%d/" % PORT, hidden=True, width=1200, height=800)

# «Найти» без входа: вход подменён — код выдаётся, через секунду «подтверждён»
from hagen.sources import sharepoint as sp_mod  # noqa: E402

SP = {"logged": False}
sp_mod.logged_in = lambda: SP["logged"]
sp_mod.pending_login = lambda: None
sp_mod.begin_login = lambda: {"url": "https://login.microsoft.com/device", "code": "TEST-4242",
                              "expires_in": 900}


def _fake_wait(handle=None):
    time.sleep(1.5)
    SP["logged"] = True
    return True


sp_mod.wait_login = _fake_wait
sp_mod.search_ex = lambda *a, **k: {"hidden": 0, "items": [
    {"id": "z1", "driveId": "d", "name": "Планёрка-20260915_100000-Meeting Recording.mp4",
     "date": "2026-09-15", "has_video": True, "has_transcript": True, "size": 1048576 * 50,
     "duration_s": 600, "web_url": ""}]}
sp_mod.check_transcripts = lambda items, handle=None: {}

UI_VOICES = r"""
(function(){
  window.__ui3 = null;
  (async () => {
    const out = {errors: [], checks: {}};
    const t = async (name, fn) => { try { out.checks[name] = await fn(); } catch (e) { out.errors.push(name + ': ' + e.message); } };
    const $ = (id) => document.getElementById(id);
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    const typeName = async (text) => {
      $('speaker-name').value = text;
      $('speaker-name').dispatchEvent(new Event('input'));
      // окно проверки спрятано, а в спрятанном окне Chromium замедляет таймеры
      // до секунды — пауза перед поиском (150 мс) срабатывает позже
      await sleep(2000);
    };
    await openRecording('__REC2__');
    await sleep(300);
    await t('переключатель «со мной в комнате были ещё люди»', () => !$('room-field').classList.contains('hidden'));
    await t('плашка разделения: при догадке — кнопка «Да, это мой голос — запомнить»', () => {
      const m = S.current.meta;
      const keep = [m.splits, S.current.segments];
      S.current.segments = (S.current.segments || []).concat([{id: 'x1', track: 'mic', start: 1, end: 2,
        text: 'сосед', speaker: 'Рядом со мной · голос 1', speaker_key: 'me~1'}]);
      const show = (info) => { m.splits = {me: Object.assign({voices: 2, keys: ['me~1']}, info)}; paintSpeakerAlerts();
        return $('speaker-alerts'); };
      const withGuess = !!show({owner_guess: true, owner_note: 'ваш голос — самый частый в микрофоне'}).querySelector('.js-owner-yes');
      const confirmed = !show({owner_guess: false, owner_note: 'ваш голос запомнен'}).querySelector('.js-owner-yes');
      const old = show({owner_note: 'ваш голос — самый частый в микрофоне'});
      const oldHint = !old.querySelector('.js-owner-yes') && /снимите и снова поставьте/.test(old.textContent);
      const pill = show({owner_guess: false, owner_note: 'ваш голос узнан по образцу (99 %)'}).querySelector('.sa-ok');
      const knownPill = !!pill && /✓ ваш голос узнан по образцу \(99 %\)/.test(pill.textContent);
      const addBox = show({owner_guess: false, owner_add: true, owner_note: 'ваш голос узнан по образцу (62 %)'});
      const addBtn = [...addBox.querySelectorAll('.js-owner-yes')].map((x) => x.textContent);
      const ownerAdd = addBtn.length === 1 && addBtn[0] === 'Добавить и этот образец';
      // собеседник узнан неуверенно — «Добавить образец»; уверенно — нет
      const spKeep = m.speakers;
      m.splits = {};
      m.speakers = {SPEAKER_00: {name: 'Иван Петров', person_id: 'p1', score: 0.74, sample_offer: true},
                    SPEAKER_01: {name: 'Мария', person_id: 'p2', score: 0.98, sample_offer: false}};
      S.current.segments = (S.current.segments || []).concat([
        {id: 'y1', track: 'far', start: 3, end: 4, text: 'а', speaker: 'Иван Петров', speaker_key: 'SPEAKER_00'},
        {id: 'y2', track: 'far', start: 5, end: 6, text: 'б', speaker: 'Мария', speaker_key: 'SPEAKER_01'}]);
      paintSpeakerAlerts();
      const offers = [...$('speaker-alerts').querySelectorAll('.sa-offer')].map((x) => x.dataset.key);
      const others = offers.length === 1 && offers[0] === 'SPEAKER_00'
        && /«Иван Петров»\s*74 %/.test($('speaker-alerts').textContent)
        && !!$('speaker-alerts').querySelector('.js-add-sample');
      m.speakers = spKeep;
      m.splits = keep[0]; S.current.segments = keep[1]; paintSpeakerAlerts();
      return (withGuess && confirmed && oldHint && knownPill && ownerAdd && others)
        || JSON.stringify({withGuess, confirmed, oldHint, knownPill, ownerAdd, others}); });
    await t('окно «Перечитать точнее»: число — про собеседников; микрофон разделится заново сам', () => {
      const m = S.current.meta;
      const keep = [m.room_shared, m.splits];
      m.room_shared = true; m.splits = {me: {voices: 2, keys: ['me~1']}};
      openVoices('repass');
      const lead = $('repass-lead').textContent, hint = $('repass-hint').textContent;
      hideDialogs();
      m.room_shared = keep[0]; m.splits = keep[1];
      return (/разделятся заново сами/.test(lead) && /не это число/.test(hint)) || JSON.stringify({lead, hint}); });
    await t('обновление записи перерисовывает галочку «со мной в комнате…»', async () => {
      const m = Object.assign({}, S.current.meta, {room_shared: false, splits: {}});
      $('chk-room').checked = true;
      await handleEvent({type: 'recording', meta: m});
      return $('chk-room').checked === false || 'галочка осталась'; });
    await t('одинаковая подсказка не складывается стопкой при двойном щелчке', () => {
      $('notices').innerHTML = '';
      notice('проверка 49: одна подсказка'); notice('проверка 49: одна подсказка');
      const n = [...$('notices').children].filter((x) => x.textContent === 'проверка 49: одна подсказка').length;
      $('notices').innerHTML = '';
      return n === 1 || n; });
    await t('рядом галочка «меня в этой записи не было», по отметке записи', () => {
      if ($('absent-field').classList.contains('hidden')) return 'скрыта';
      const was = S.current.meta.owner_absent;
      S.current.meta.owner_absent = true; paintRoomToggle();
      const on = $('chk-absent').checked;
      S.current.meta.owner_absent = false; paintRoomToggle();
      const off = !$('chk-absent').checked;
      S.current.meta.owner_absent = was; paintRoomToggle();
      return (on && off && typeof setOwnerAbsent === 'function') || JSON.stringify({on, off}); });
    await t('предложение разделить голоса переговорки', () => !$('speaker-alerts').classList.contains('hidden')
      && /Переговорная 3/.test($('speaker-alerts').textContent) && !!$('speaker-alerts').querySelector('.js-split'));
    openSpeakerDialog('SPEAKER_00', 'Спикер 1');
    await sleep(800);
    await t('сразу подсказка: похожий по голосу', () => /Иван Петров/.test($('speaker-ac').textContent)
      && /голос похож на \d+ %/.test($('speaker-ac').textContent));
    await typeName('петр');
    await t('ключ имени как в службе: порядок слов и приписки не важны',
      () => nameKey('Петров  Иван (ООО)') === 'иван петров' || nameKey('Петров  Иван (ООО)'));
    await t('подсказки по вхождению букв и «новый человек»', () => {
      const items = [...document.querySelectorAll('#speaker-ac .ac-item')].map((x) => x.textContent.replace(/\s+/g, ' '));
      return (items.some((x) => /Иван Петров/.test(x)) && items.some((x) => /Петров И\./.test(x))
        && items.some((x) => /Новый человек/.test(x))) || items.join(' | '); });
    await t('окно имени выше прежнего, список подсказок не обрезан краем окна', () => {
      const dlg = $('dlg-speaker').getBoundingClientRect().height;
      const open = $('dlg-speaker').classList.contains('ac-open');
      const body = getComputedStyle($('dlg-speaker').querySelector('.dlg-body')).overflowY;
      return (dlg >= Math.min(520, window.innerHeight * 0.9) - 2 && open && body === 'visible') || `${dlg} ${open} ${body}`; });
    await t('стрелки и Enter выбирают из списка', () => {
      const ev = (k) => $('speaker-name').dispatchEvent(new KeyboardEvent('keydown', {key: k, bubbles: true}));
      ev('ArrowDown'); ev('Enter');
      return !!S.speakerCtx.picked && /Из базы голосов/.test($('speaker-picked').textContent); });
    await typeName('Petrov Ivan');
    S.speakerCtx.acClosed = true; paintSpeakerAc();
    await saveSpeakerDialog({});
    await sleep(500);
    await t('похожее имя — вопрос с кнопками прямо в окне', () => !$('speaker-conflict').classList.contains('hidden')
      && /похожее имя/.test($('speaker-conflict').textContent)
      && $('speaker-conflict').querySelectorAll('button').length === 2
      && !$('dlg-speaker').classList.contains('hidden'));
    await t('кнопка «разделить голоса» в окне', () => !$('btn-speaker-split').classList.contains('hidden'));
    hideDialogs();
    await loadVoices();
    await t('«Голоса»: карточки людей и возможные дубли', () =>
      document.querySelectorAll('#voices-list .voice-card').length === 2
      && /Возможно, это одни и те же люди/.test($('voices-dupes').textContent));
    await t('в карточке — образец с названием записи', () => {
      document.querySelector('#voices-list .voice-card .js-vopen').click();
      return true; });
    await sleep(600);
    await t('карточка раскрылась', () => /Проверка 49 — голоса/.test($('voices-list').textContent)
      && /подписан вручную/.test($('voices-list').textContent));
    // --- темы и плотность (пакет Claude Design) ---
    const bg = () => getComputedStyle(document.body).backgroundColor;
    // Кнопка того же вида, что «Перечитать точнее», но видна всегда: ту
    // прячем, когда звонки и файлы идут одной моделью.
    const btnH = () => $('btn-diarize').getBoundingClientRect().height;
    await t('по умолчанию — светлая тема и плотность «Плотно»', () =>
      (document.documentElement.dataset.theme === 'light' && document.documentElement.dataset.density === 'compact')
      || JSON.stringify(document.documentElement.dataset));
    await t('в «Настройки → Вид» пять тем и четыре плотности', () =>
      document.querySelectorAll('[data-theme-pick]').length === 5 && document.querySelectorAll('[data-density-pick]').length === 4
      && document.querySelector('[data-theme-pick="light"]').classList.contains('sel'));
    const light = bg();
    let darkBg = '';
    await t('тёмная тема меняет цвета сразу', async () => {
      document.querySelector('[data-theme-pick="dark"]').click();
      await sleep(400);
      darkBg = bg();
      return (document.documentElement.dataset.theme === 'dark' && darkBg !== light) || `${light} → ${darkBg}`; });
    await t('все пять тем отличаются основой', async () => {
      document.querySelector('[data-theme-pick="warm"]').click(); await sleep(200);
      const warm = bg();
      document.querySelector('[data-theme-pick="neutral"]').click(); await sleep(200);
      const neutral = bg();
      document.querySelector('[data-theme-pick="bright"]').click(); await sleep(200);
      const bright = bg();
      const all = [light, darkBg, warm, neutral, bright];
      return new Set(all).size === 5 || all.join(' | '); });
    await t('в «Нейтральной» цвет один — у полоски и черты', async () => {
      document.querySelector('[data-theme-pick="neutral"]').click(); await sleep(200);
      const css = getComputedStyle(document.documentElement);
      const mark = css.getPropertyValue('--accent-mark').trim();
      const accent = css.getPropertyValue('--accent').trim();
      const fill = css.getPropertyValue('--accent-fill').trim();
      document.querySelector('[data-theme-pick="bright"]').click(); await sleep(200);
      return (mark === '#2a5fa8' && accent === '#1b1b1b' && fill === '#33373d')
        || JSON.stringify({mark, accent, fill}); });
    await t('выбранная тема ушла в настройки', () => S.settings.ui_theme === 'bright' || S.settings.ui_theme);
    const hCompact = btnH();
    await t('«Крупно» делает кнопки выше, «Мелко» — ниже', async () => {
      document.querySelector('[data-density-pick="large"]').click(); await sleep(300);
      const hLarge = btnH();
      document.querySelector('[data-density-pick="tiny"]').click(); await sleep(300);
      const hTiny = btnH();
      document.querySelector('[data-density-pick="compact"]').click();
      document.querySelector('[data-theme-pick="light"]').click();
      await sleep(300);
      return (hLarge > hCompact && hTiny < hCompact) || `${hTiny} / ${hCompact} / ${hLarge}`; });
    await t('иконки вместо эмодзи в кнопках', () =>
      !!document.querySelector('#btn-copy-transcript use') && !!document.querySelector('#btn-diarize use')
      && !/[📄👥🔍📋]/u.test($('btn-copy-transcript').textContent + $('btn-diarize').textContent));
    await t('Старт и Стоп в одной ячейке: виден один', () => {
      const vis = (id) => getComputedStyle($(id)).display !== 'none';
      return (!!$('btn-start').closest('.rec-toggle') && vis('btn-start') !== vis('btn-stop')) || `${vis('btn-start')} ${vis('btn-stop')}`; });
    // Этап 6: вместо семи рядов под стенограммой — панель действий, у каждой
    // вкладки своя группа. Видна ровно одна. Решение 22.09: удаления в панели
    // нет (оно в «⋯» шапки), акцентных кнопок тоже — акцент один, «+ Документ».
    await t('панель действий: одна группа, без «Удалить» и без акцента', () => {
      const shown = [...document.querySelectorAll('#rec-actions .act-group')]
        .filter((g) => !g.classList.contains('hidden')).map((g) => g.id);
      const pane = document.querySelector('#rec-tabs .rtab.active').dataset.pane;
      const okPanel = !document.getElementById('btn-delete')
        && document.querySelectorAll('#rec-actions button.primary').length === 0
        && $('btn-add-doc').classList.contains('primary');
      return (shown.length === 1 && shown[0] === 'act-' + pane && okPanel)
        || JSON.stringify([shown, pane, okPanel]); });
    await t('названный собеседник получает свой цвет, безымянный — нет', () => {
      const a = speakerColor({ speaker: 'Иван Петров', track: 'far', speaker_key: 'SPEAKER_00' });
      const b = speakerColor({ speaker: 'Спикер 2', track: 'far', speaker_key: 'SPEAKER_01' });
      const c = speakerColor({ speaker: 'Я', track: 'mic', speaker_key: 'me' });
      return (/^ c[1-8]$/.test(a) && b === '' && c === '') || JSON.stringify([a, b, c]); });
    await t('восемь разных собеседников записи — восемь разных цветов', () => {
      const names = ['Петров Иван', 'Алексей Одинцов', 'Куренков Сергей', 'Иван Петров', 'Мария Сидорова',
        'Ольга Новикова', 'Олег Кузнецов', 'Анна Смирнова'];
      const segs = names.map((n, i) => ({ speaker: n, track: 'far', speaker_key: 'sub' + i }));
      const slots = speakerSlots(segs);
      return new Set(names.map((n) => speakerColor({ speaker: n, track: 'far' }, slots))).size === 8
        || JSON.stringify(slots); });
    await t('«имя?» — только у безымянных', () => speakerUnnamed('Спикер 3') && !speakerUnnamed('Петров Иван'));
    // Этап 9.1: список разделов слева закреплён, ездит только содержимое.
    await t('разделы «Настроек» не прокручиваются вместе с содержимым', () => {
      const body = document.querySelector('#dlg-settings .dlg-body');
      const panes = document.querySelector('#dlg-settings .set-panes');
      return (getComputedStyle(body).overflowY === 'hidden'
        && getComputedStyle(panes).overflowY === 'auto')
        || JSON.stringify({body: getComputedStyle(body).overflowY,
                           panes: getComputedStyle(panes).overflowY}); });
    await t('кнопки «Очистить базу» и «Вернуть из копии» на месте', () =>
      !!$('btn-voices-clear') && !!$('btn-voices-restore') && $('voices-backup-select').options.length >= 1);
    // --- продвинутые настройки разметки (15.09) ---
    await t('раздел «Тонкие настройки» переключается', () => {
      const tab = document.querySelector('#dlg-settings .tab[data-tab="t-advanced"]');
      tab.click();
      return (tab.textContent.trim() === 'Тонкие настройки'
        && !$('t-advanced').classList.contains('hidden')) || tab.textContent; });
    await t('поля «Тонких настроек» заполняются: пусто там, где «как у модели»', () => {
      Object.assign(S.settings, {diarize_window_step_s: 2, diarize_cluster_threshold: null,
        diarize_cluster_fb: 0.4, diarize_min_voice_s: null, split_min_piece_s: 2});
      fillAdvanced();
      return ($('set-dt-step').value === '2' && $('set-dt-threshold').value === ''
        && $('set-dt-fb').value === '0.4' && $('set-dt-minvoice').value === '') || JSON.stringify(
        [$('set-dt-step').value, $('set-dt-threshold').value, $('set-dt-fb').value]); });
    await t('«по умолчанию» и сбор: пустое поле уходит как null', () => {
      $('set-dt-threshold').value = '0.55';
      document.querySelector('#t-advanced .js-dt-reset[data-for="set-dt-fb"]').click();
      document.querySelector('#t-advanced .js-dt-reset[data-for="set-dt-step"]').click();
      const got = collectAdvanced();
      return (got.diarize_cluster_threshold === 0.55 && got.diarize_cluster_fb === null
        && got.diarize_window_step_s === 2 && got.diarize_min_voice_s === null
        && got.split_min_piece_s === 2) || JSON.stringify(got); });
    await t('поле и кнопка «по умолчанию» стоят в одну строку', () => {
      // размеры есть только у показанного окна: открываем на время замера
      const dlg = $('dlg-settings');
      const wasHidden = dlg.classList.contains('hidden');
      dlg.classList.remove('hidden');
      try {
        const inp = $('set-dt-fb').getBoundingClientRect();
        const btn = document.querySelector('#t-advanced .js-dt-reset[data-for="set-dt-fb"]').getBoundingClientRect();
        return (inp.width > 40 && inp.width < 200 && Math.abs(inp.top - btn.top) < 12)
          || JSON.stringify([inp.width, inp.top, btn.top]);
      } finally {
        if (wasHidden) dlg.classList.add('hidden');
      } });
    await t('«Запись и звонки»: оба срока на экране вместо текста с зашитыми 30 с и 10 минутами', () => {
      const pane = $('t-call').textContent;
      return (!!$('set-call-stop') && !!$('set-call-resume') && !/через 30 с/.test(pane)
        && !/в течение 10 минут/.test(pane)) || pane.slice(0, 200); });
    await t('«Запись и звонки»: сроки заполняются из настроек', async () => {
      Object.assign(S.settings, {call_end_confirm_s: 45, call_resume_window_s: 900});
      await openSettings();
      hideDialogs();
      return ($('set-call-stop').value === '45' && $('set-call-resume').value === '900')
        || JSON.stringify([$('set-call-stop').value, $('set-call-resume').value]); });
    await t('разделы «Настроек» — списком слева, все на виду', () => {
      const dlg = $('dlg-settings');
      const wasHidden = dlg.classList.contains('hidden');
      dlg.classList.remove('hidden');
      try {
        // Этап 9, п. 9.1: девять вкладок в две строки заменены вертикальным
        // списком; прокручивается только содержимое раздела.
        const nav = document.querySelector('#dlg-settings .set-nav');
        if (!nav) return 'списка разделов нет';
        const tabs = [...nav.querySelectorAll('.tab')];
        // Считаем видимые: разделы выключенных возможностей с экрана убраны
        // совсем (п. 9.2), и размеров у них нет.
        const shown = tabs.filter((x) => x.offsetParent !== null);
        const cols = new Set(shown.map((x) => Math.round(x.getBoundingClientRect().left))).size;
        const panes = document.querySelector('#dlg-settings .set-panes');
        // Одиннадцатый раздел — «О программе»: версия и обновление.
        return (tabs.length === 11 && shown.length >= 6 && cols === 1 && !!panes
          && nav.scrollWidth <= nav.clientWidth + 1)
          || JSON.stringify({n: tabs.length, shown: shown.length, cols, panes: !!panes,
            scroll: nav.scrollWidth, client: nav.clientWidth});
      } finally {
        if (wasHidden) dlg.classList.add('hidden');
      } });
    // Этап 9.2: возможности отключаемые, а не два режима программы.
    await t('«Возможности»: выключил — элементов нет, включил — на месте', async () => {
      // Смотрим на саму пометку, а не на видимость: блок «Свойств» и панель
      // документа скрыты и просто потому, что открыта другая вкладка.
      const seen = (sel) => {
        const el = document.querySelector(sel);
        return !!el && !el.classList.contains('feature-off');
      };
      await setFeature('obsidian_links', true);
      await setFeature('todoist', true);
      const on = seen('[data-feature="obsidian_links"]') && seen('#btn-tasks-out');
      await setFeature('obsidian_links', false);
      await setFeature('todoist', false);
      const off = !seen('[data-feature="obsidian_links"]') && !seen('#btn-tasks-out');
      // Данные не трогаются: поля на месте, просто спрятаны вместе с блоком.
      const kept = !!$('rec-project') && !!$('rec-tags');
      await setFeature('obsidian_links', true);
      const back = seen('[data-feature="obsidian_links"]');
      await setFeature('obsidian_links', false);
      return (on && off && kept && back) || JSON.stringify({on, off, kept, back}); });
    await t('«Включить всё» включает все возможности', async () => {
      await enableAllFeatures();
      const f = S.settings.features || {};
      const offCount = FEATURES.filter((it) => !f[it.key]).length;
      return offCount === 0 || JSON.stringify(f); });
    await t('кнопки «Сохранить» в настройках нет: применяется сразу', () =>
      !$('btn-save-settings') && typeof applySettings === 'function'
      && /Закрыть/.test($('dlg-settings').querySelector('.dlg-foot').textContent));
    // --- диктовка текста (ветка typer) ---
    await t('раздел «Диктовка» переключается', () => {
      const tab = document.querySelector('#dlg-settings .tab[data-tab="t-dictate"]');
      tab.click();
      return (tab.textContent.trim() === 'Диктовка'
        && !$('t-dictate').classList.contains('hidden')) || tab.textContent; });
    await t('в списке микрофонов есть «тот же, что и для записи»', () => {
      S.devices = {mics: [{index: 3, name: 'Brio 500'}]};
      S.settings.dictate_device_index = 3;
      fillDictate();
      const opts = [...$('set-dictate-mic').options].map((o) => o.textContent);
      return (opts.length === 2 && /тот же/.test(opts[0]) && opts[1] === 'Brio 500'
        && $('set-dictate-mic').value === '3') || JSON.stringify(opts); });
    // Настоящую историю диктовок здесь НЕ чистим: она не наша.
    // Очистку проверяет t56 на подменённом файле.
    await t('история диктовок раскрывается', async () => {
      await showDictateHistory();
      return $('dictate-history').className === 'dict-hist'
        && $('dictate-history').textContent.trim().length > 0; });
    await t('капсулу можно выключить', () => {
      const was = S.settings.dictate_pill;
      S.settings.dictate_pill = false;
      S.dictate = {stage: 'listening'}; paintDictate();
      const off = $('dictate-pill').classList.contains('hidden');
      S.settings.dictate_pill = was === undefined ? true : was;
      S.dictate = {stage: 'idle'}; paintDictate();
      return off; });
    await t('сочетание клавиш показано словами', async () => {
      await sleep(300);
      return /Ctrl/.test($('btn-dictate-hotkey').textContent) || $('btn-dictate-hotkey').textContent; });
    await t('сочетание назначается нажатием клавиш', async () => {
      $('btn-dictate-hotkey').click();
      document.dispatchEvent(new KeyboardEvent('keydown',
        {code: 'KeyJ', ctrlKey: true, altKey: true, bubbles: true}));
      await sleep(400);
      return ($('btn-dictate-hotkey').dataset.hotkey === 'ctrl+alt+j'
        && $('btn-dictate-hotkey').textContent === 'Ctrl + Alt + J') || $('btn-dictate-hotkey').textContent; });
    await t('Ctrl+Win выбирается кнопкой и честно предупреждает о перехвате', async () => {
      document.querySelector('#t-dictate .js-hk[data-hk="ctrl+win"]').click();
      await sleep(400);
      const warned = !$('dictate-hook-warn').classList.contains('hidden');
      return ($('btn-dictate-hotkey').textContent === 'Ctrl + Win' && warned)
        || `${$('btn-dictate-hotkey').textContent} / предупреждение ${warned}`; });
    await t('у обычного сочетания предупреждения нет', async () => {
      document.querySelector('#t-dictate .js-hk[data-hk="ctrl+space"]').click();
      await sleep(400);
      return ($('btn-dictate-hotkey').textContent === 'Ctrl + Space'
        && $('dictate-hook-warn').classList.contains('hidden'))
        || $('btn-dictate-hotkey').textContent; });
    await t('Ctrl+Win ловится и нажатием: две клавиши, без третьей', async () => {
      $('btn-dictate-hotkey').click();
      document.dispatchEvent(new KeyboardEvent('keydown',
        {code: 'MetaLeft', key: 'Meta', ctrlKey: true, metaKey: true, bubbles: true}));
      document.dispatchEvent(new KeyboardEvent('keyup',
        {code: 'MetaLeft', key: 'Meta', ctrlKey: true, metaKey: true, bubbles: true}));
      await sleep(400);
      return $('btn-dictate-hotkey').dataset.hotkey === 'ctrl+win'
        || $('btn-dictate-hotkey').dataset.hotkey; });
    await t('негодное сочетание объясняется, а не молчит', async () => {
      $('btn-dictate-hotkey').click();
      document.dispatchEvent(new KeyboardEvent('keydown', {code: 'KeyJ', bubbles: true}));
      await sleep(400);
      return /Ctrl/.test($('dictate-state').textContent) || $('dictate-state').textContent; });
    // Словарь замен теперь один на программу (решение 18.09):
    // у диктовки своего списка нет, есть ссылка на общий.
    await t('у диктовки нет своего словаря — ссылка на общий', () => {
      const own = document.querySelector('#dictate-rules') || $('btn-dictate-rule');
      const link = $('btn-go-dict');
      return (!own && !!link && !collectDictate().dictate_replacements)
        || JSON.stringify({own: !!own, link: !!link}); });
    await t('в словаре у замены видно, где она действует', () => {
      S.fixes = {rules: [{from: 'джира', to: 'Jira', where: 'dictation'}], suggestions: []};
      paintFixes();
      const sel = document.querySelector('#fix-rules .fix-scope');
      return (!!sel && sel.value === 'dictation' && sel.options.length === 3)
        || (sel ? sel.value : 'выбора нет'); });
    await t('капсула показывает, что программа слушает', () => {
      S.settings.dictate_pill = true;
      S.dictate = {stage: 'listening', latched: false}; paintDictate();
      const on = !$('dictate-pill').classList.contains('hidden')
        && /Слушаю/.test($('dictate-pill-text').textContent);
      S.dictate = {stage: 'thinking'}; paintDictate();
      const think = /Распознаю/.test($('dictate-pill-text').textContent)
        && $('dictate-pill').classList.contains('thinking');
      S.dictate = {stage: 'idle'}; paintDictate();
      return (on && think && $('dictate-pill').classList.contains('hidden')) || `${on} ${think}`; });
    await t('во время записи капсула честно говорит, что диктовка спит', () => {
      S.dictate = {stage: 'sleeping'}; paintDictate();
      const got = $('dictate-pill-text').textContent;
      S.dictate = {stage: 'idle'}; paintDictate();
      return /спит/.test(got) || got; });
    document.querySelector('#dlg-settings .tab[data-tab="t-general"]').click();
    window.__ui3 = JSON.stringify(out);
  })().catch((e) => { window.__ui3 = JSON.stringify({errors: ['сбой: ' + e.message], checks: {}}); });
  return 'started';
})()
"""

# База голосов — временная копия: настоящую data/voices.json проверка не трогает
import tempfile  # noqa: E402

import numpy as np  # noqa: E402

from hagen import diarize as diarize_mod  # noqa: E402
from hagen import voices as voices_mod  # noqa: E402

VOICES_TMP = Path(tempfile.mkdtemp(prefix="t49_"))
REAL_VOICES = voices_mod.VOICES_PATH
voices_mod.VOICES_PATH = VOICES_TMP / "voices.json"
voices_mod._cache, voices_mod._cache_stamp = None, None
_vec = np.random.default_rng(49).normal(size=256)
REC2 = store.create(title="Проверка 49 — голоса", mode="online", source="live", category="Встречи")["id"]
store.update(REC2, {"status": "recorded", "duration_s": 12.0})
store.replace_segments(REC2, [
    store.make_segment("mic", 0.0, 2.0, "Начнём."),
    store.make_segment("far", 3.0, 6.0, "Добрый день.", speaker="Спикер 1", speaker_key="SPEAKER_00"),
    store.make_segment("far", 7.0, 9.0, "Мы в переговорке.", speaker="Переговорная 3", speaker_key="sub9"),
])
store.update(REC2, {"speakers": {"SPEAKER_00": {"name": None}, "sub9": {"name": "Переговорная 3",
                                                                          "multi_voice": {"voices": 2}}}})
diarize_mod.save_result(REC2, {"turns": [], "embeddings": {"SPEAKER_00": (_vec * 1.01).tolist()}})
_ivan = voices_mod.create_person("Иван Петров")
voices_mod.add_sample(None, _vec.tolist(), person_id=_ivan["id"], rec_id=REC2, speaker_key="OLD")
voices_mod.create_person("Петров И.")
UI_VOICES = UI_VOICES.replace("__REC2__", REC2)

LOGIN_JS = r"""
(function(){
  document.querySelector('.src-btn[data-src="sharepoint"]').click();
  document.getElementById('btn-sp-search').click();
  return 'clicked';
})()
"""
LOGIN_STATE = r"""
JSON.stringify({
  loginShown: !document.getElementById('sp-login').classList.contains('hidden'),
  code: document.getElementById('sp-code').textContent,
  results: document.getElementById('sp-results').textContent,
  rows: document.querySelectorAll('#sp-results .sp-item').length,
  pending: V.sp.pendingSearch
})
"""


def scenario():
    errors_js = "window.__t49errs = []; window.addEventListener('error', (e) => window.__t49errs.push(e.message));"
    for _ in range(100):
        try:
            ready = window.evaluate_js("typeof S !== 'undefined' && typeof videoInit === 'function' && !!document.getElementById('call-toast')")
        except Exception:
            ready = False
        if ready:
            break
        time.sleep(0.2)
    result["ready"] = bool(ready)
    time.sleep(2.0)                          # loadState и события успевают отработать
    try:
        window.evaluate_js(errors_js)
        result["inject"] = json.loads(window.evaluate_js(INJECT))
        result["sp"] = json.loads(window.evaluate_js(RENDER_SP))
        window.evaluate_js(UI_DOCS)
        for _ in range(80):
            time.sleep(0.25)
            got = window.evaluate_js("window.__ui2")
            if got:
                result["docs"] = json.loads(got)
                break
        window.evaluate_js(UI_VOICES)
        # Блок длинный (голоса, темы, плотности, диктовка) и внутри много
        # коротких пауз: 20 секунд ему стало мало, и проверка падала не по делу.
        for _ in range(200):
            time.sleep(0.25)
            got = window.evaluate_js("window.__ui3")
            if got:
                result["voices"] = json.loads(got)
                break
        window.evaluate_js("S.showRecord = false; S.mode = 'video'; paintAll();")
        window.evaluate_js("V.sp.items = []; V.sp.sel = new Set();")
        window.evaluate_js(LOGIN_JS)
        time.sleep(0.8)
        result["login_before"] = json.loads(window.evaluate_js(LOGIN_STATE))
        after = {}
        for _ in range(40):
            time.sleep(0.25)
            after = json.loads(window.evaluate_js(LOGIN_STATE))
            if after.get("rows"):
                break
        result["login_after"] = after
        time.sleep(0.5)
        result["late_errors"] = window.evaluate_js("JSON.stringify(window.__t49errs)")
    except Exception as err:
        result["fatal"] = str(err)
    window.destroy()


webview.start(scenario, gui="edgechromium", private_mode=True)
srv.should_exit = True
th.join(timeout=20)

say("=== Страница в настоящем WebView2 ===")
check("страница загрузилась, скрипты разобраны без ошибок", result.get("ready"), result.get("fatal", ""))
inj = result.get("inject") or {}
check("ошибок при отрисовке нет", not inj.get("errors"), inj.get("errors"))
for name, ok in (inj.get("checks") or {}).items():
    check(name, ok is True, ok)
docs = result.get("docs") or {}
check("новые части окна отработали", bool(docs.get("checks")), docs or "не дождались")
check("ошибок в новых частях нет", not docs.get("errors"), docs.get("errors"))
for name, ok in (docs.get("checks") or {}).items():
    check(name, ok is True, ok)
store.delete(REC)
say("")
say("=== Голоса: окно имени, конфликты, «Голоса» в настройках ===")
vres = result.get("voices") or {}
check("части про голоса отработали", bool(vres.get("checks")), vres or "не дождались")
check("ошибок в частях про голоса нет", not vres.get("errors"), vres.get("errors"))
for name, ok in (vres.get("checks") or {}).items():
    check(name, ok is True, ok)
store.delete(REC2)
voices_mod.VOICES_PATH = REAL_VOICES
voices_mod._cache, voices_mod._cache_stamp = None, None
import shutil  # noqa: E402

shutil.rmtree(VOICES_TMP, ignore_errors=True)
check("выбор темы и плотности дошёл до сохранения настроек",
      {"ui_theme": "dark"} in SAVED and {"ui_theme": "bright"} in SAVED and {"ui_density": "large"} in SAVED,
      SAVED[-6:])
config_mod.save, config_mod.get, config_mod.public = _real_save, _real_get, _real_public
from fastapi.testclient import TestClient  # noqa: E402

VIEW.update({"ui_theme": "dark", "ui_density": "tiny"})
config_mod.get = lambda k, d=None: VIEW[k] if k in VIEW else _real_get(k, d)
with TestClient(server.app, base_url="http://127.0.0.1:8787") as _cli:
    _page = _cli.get("/").text
config_mod.get = _real_get
check("служба сразу отдаёт страницу в сохранённой теме (без мигания)",
      'data-theme="dark" data-density="tiny"' in _page, _page[:120])
sp = result.get("sp") or {}
check("SharePoint: две строки", sp.get("rows") == 2, sp)
check("SharePoint: счётчик", "Найдено: 2" in str(sp.get("count")) and "выбрано: 1" in str(sp.get("count")),
      sp.get("count"))
check("SharePoint: кнопка с числом выбранных", "(1)" in str(sp.get("btn")), sp.get("btn"))
check("SharePoint: пометки 🎬📝", sp.get("marks") == "🎬📝", sp.get("marks"))
check("SharePoint: «✓ уже есть»", sp.get("done") is True, sp)
lb = result.get("login_before") or {}
la = result.get("login_after") or {}
check("«Найти» без входа показывает ссылку и код", lb.get("loginShown") and lb.get("code") == "TEST-4242", lb)
check("человеку сказано, что поиск запустится сам", "запустится сам" in str(lb.get("results")), lb.get("results"))
check("после подтверждения входа поиск прошёл без второго нажатия", la.get("rows") == 1, la)
check("код после входа убран", la.get("loginShown") is False, la)
check("поздних ошибок JavaScript нет", result.get("late_errors") in ("[]", None), result.get("late_errors"))

say("")
say("ИТОГО провалов: %d" % len(FAIL))
for f in FAIL:
    say("   - " + f)
io.open(PROJECT / "tests" / "t49_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(1 if FAIL else 0)
