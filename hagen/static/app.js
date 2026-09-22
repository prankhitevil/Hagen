/* Hagen — интерфейс. Без фреймворков.
   Звук захватывает САМА СЛУЖБА, двумя раздельными дорожками:
     микрофон          -> WASAPI, устройство выбирается в списке;
     звук собеседников -> петля вывода Windows (WASAPI loopback).
   Страница только показывает и управляет: захват звука браузером на этой
   машине не работает («Could not start audio source»), и от него отказались. */
'use strict';

const $ = (id) => document.getElementById(id);

const S = {
  recordings: [],
  settings: {},
  categories: [],
  projects: [],           // подсказки поля «Проект»: что уже вводили
  recTags: [],            // подсказки поля «Теги»
  projectFolders: {},     // {проект: папка в сейфе} — куда уходят его заметки
  listFilter: { project: '', tag: '' },  // отбор списка записей (20.09)
  selected: [],           // отмеченные Ctrl+щелчком записи — чтобы удалить пачкой
  // Список сложен по папкам проектов. null — ещё не знаем: значение возьмётся
  // из настроек при первом чтении состояния. Дальше это выбор человека, и
  // перечитывание состояния его не отменяет.
  groupByProject: null,
  foldedProjects: {},     // какие папки проектов свёрнуты: {проект: true}
  copyFormat: 'md',       // как копировали документ в прошлый раз (20.09)
  share: null,            // что можно отправить у открытой записи (20.09)
  linkDraft: [],          // отмеченное в окне «Связать с» до нажатия «Сохранить»
  caps: {},
  vault: {},
  pane: 'transcript',     // открытая вкладка записи: transcript / doc / props
  mic: {},                // выключатель микрофона Windows: {available, muted, device}
  fixes: {},              // словарь из правок: постоянные замены и предложения
  asr: { state: 'loading' },  // модель эфира: loading / ready / error / offline
  loopback: [],           // устройства вывода с признаком «будет эхо»
  jobs: [],
  current: null,          // {meta, segments, session}
  currentId: null,
  recordingId: null,
  editMode: false,        // правка стенограммы руками включена
  needs: [],              // тяжёлые части: что уже скачано, а что нет
  templates: {},
  engines: [],
  voices: [],
  call: {},
  drafts: {},
  // Свёрнуты ли стенограмма и документ — у КАЖДОЙ записи своё (замечено
  // 16.09): {номер записи: {transcript: true, minutes: false}}.
  // Раньше свёрнутость была признаком самой панели на экране и переезжала
  // с записи на запись.
  folds: {},
  levels: { mic: 0, far: 0 },
  tplChosen: 'protocol',
  speakerCtx: null,
  starting: false,   // идёт запуск записи: кнопки должны быть заблокированы
  mode: 'rec',       // 'rec' — запись с микрофона, 'video' — готовые видео
  showRecord: false, // в режиме «Видео»: показывать выбранную запись, не форму
  models: {},        // списки моделей по сервисам, полученные по кнопке
};

// состояние наружу — удобно смотреть в консоли браузера при разборе проблем
window.S = S;
// Раздел «Видео» живёт в отдельном файле и открывает записи этой функцией.
// ВАЖНО: присваиваем саму функцию, а не обёртку `(id) => openRecording(id)`.
// В обычном скрипте объявление `function openRecording` уже лежит в window, и
// такая обёртка заменила бы его собой — вызов уходил в бесконечную рекурсию
// («Maximum call stack size exceeded»), и записи перестали открываться щелчком.
window.openRecording = openRecording;

/* ====================================================== утилиты */

function fmtDur(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  const p = (n) => String(n).padStart(2, '0');
  return `${p(h)}:${p(m)}:${p(s)}`;
}
function fmtClock(sec) {
  sec = Math.max(0, Math.round(sec || 0));
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}
function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' ГБ';
  if (n >= 1048576) return Math.round(n / 1048576) + ' МБ';
  return Math.max(1, Math.round(n / 1024)) + ' КБ';
}
function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const months = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  const today = new Date();
  const same = d.toDateString() === today.toDateString();
  const y = new Date(today.getTime() - 86400000);
  const yest = d.toDateString() === y.toDateString();
  const time = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  if (same) return `сегодня, ${time}`;
  if (yest) return `вчера, ${time}`;
  return `${d.getDate()} ${months[d.getMonth()]}, ${time}`;
}
// «1 ручная правка», «2 ручные правки», «11 ручных правок»
function plural(n, one, few, many) {
  const k = Math.abs(Number(n) || 0) % 100;
  const t = k % 10;
  if (k > 10 && k < 20) return `${n} ${many}`;
  if (t === 1) return `${n} ${one}`;
  if (t >= 2 && t <= 4) return `${n} ${few}`;
  return `${n} ${many}`;
}

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
function notice(text, level) {
  const box = $('notices');
  // Тот же текст уже на экране (дважды щёлкнули) — не складываем стопкой.
  if ([...box.children].some((x) => x.textContent === text)) return;
  const el = document.createElement('div');
  el.className = 'notice' + (level ? ' ' + level : '');
  el.textContent = text;
  box.appendChild(el);
  setTimeout(() => el.remove(), level === 'err' ? 8000 : 4200);
}

async function api(path, opts) {
  const o = Object.assign({ headers: {} }, opts || {});
  if (o.body && !(o.body instanceof FormData)) {
    o.headers['Content-Type'] = 'application/json';
    o.body = JSON.stringify(o.body);
  }
  const r = await fetch(path, o);
  const ct = r.headers.get('content-type') || '';
  const data = ct.includes('json') ? await r.json().catch(() => null) : await r.text();
  if (!r.ok) {
    const msg = (data && data.detail) || (data && data.error) || `Ошибка ${r.status}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data;
}

/* ====================================================== отрисовка */

function paintMeters() {
  $('meter-mic').style.width = Math.round((S.levels.mic || 0) * 100) + '%';
  $('meter-far').style.width = Math.round((S.levels.far || 0) * 100) + '%';
  // свёрнутая строка устройств во время записи — те же уровни
  $('meter-mic-mini').style.width = $('meter-mic').style.width;
  $('meter-far-mini').style.width = $('meter-far').style.width;
  const rec = S.recordingId && S.recordingId === S.currentId;
  if (!rec) return;
  // Обрыв устройства важнее «тишины»: сторож службы уже переподключает его,
  // и человек должен видеть это, а не гадать, почему полоска замерла.
  $('mic-hint').textContent = S.micLost ? 'переподключаю…' : (S.micSilent ? 'тихо' : '');
  $('far-hint').textContent = S.farLost ? 'переподключаю…'
    : ((S.farOn && S.farSilent) ? 'тишина — не то устройство?' : '');
  $('far-hint').title = S.farLost
    ? 'Звук собеседников прервался. Программа подключает его заново, перерыв в стенограмме будет тишиной.'
    : (S.farSilent ? 'На выбранном устройстве нет звука. Проверьте, куда Teams выводит звонок.' : '');
}

/* Отбор списка по проекту и тегу (20.09). Проект и теги уже приходят с каждой
   записью, поэтому отбор — дело показа: службу об этом не спрашиваем. */
function listFilter(list) {
  const p = (S.listFilter.project || '').toLowerCase();
  const t = (S.listFilter.tag || '').toLowerCase();
  return list.filter((m) => {
    if (p && String(m.project || '').toLowerCase() !== p) return false;
    if (t && !(m.tags || []).some((x) => String(x).toLowerCase() === t)) return false;
    return true;
  });
}

/* Проекты и теги, которые есть у записей на экране. Берём из самих записей, а
   не из подсказок настроек: в отборе не должно быть проектов, по которым в
   списке всё равно ничего не найдётся. */
function paintListFilter() {
  const all = S.recordings || [];
  const list = S.mode === 'video' ? all.filter(isVideoRec) : all.filter((m) => !isVideoRec(m));
  const projects = [], tags = [];
  list.forEach((m) => {
    const p = String(m.project || '').trim();
    if (p && !projects.some((x) => x.toLowerCase() === p.toLowerCase())) projects.push(p);
    (m.tags || []).forEach((t) => {
      t = String(t).trim();
      if (t && !tags.some((x) => x.toLowerCase() === t.toLowerCase())) tags.push(t);
    });
  });
  projects.sort((a, b) => a.localeCompare(b, 'ru'));
  tags.sort((a, b) => a.localeCompare(b, 'ru'));
  const fill = (sel, values, cur, none) => {
    const el = $(sel);
    if (!el) return;
    // Выбранное значение держим в списке, даже если под него уже ничего нет:
    // иначе отбор молча снялся бы, а список остался коротким.
    const shown = cur && !values.some((v) => v.toLowerCase() === cur.toLowerCase())
      ? values.concat([cur]) : values;
    el.innerHTML = [`<option value="">${none}</option>`].concat(
      shown.map((v) => `<option value="${esc(v)}"${v.toLowerCase() === (cur || '').toLowerCase() ? ' selected' : ''}>${esc(v)}</option>`)).join('');
    el.value = cur || '';
  };
  fill('flt-project', projects, S.listFilter.project, 'все проекты');
  fill('flt-tag', tags, S.listFilter.tag, 'все теги');
  const btn = $('btn-group');
  if (btn) {
    btn.setAttribute('aria-pressed', S.groupByProject ? 'true' : 'false');
    btn.classList.toggle('on', !!S.groupByProject);
    btn.title = S.groupByProject ? 'Показать списком, без папок проектов'
      : 'Складывать записи по проектам';
  }
}

/* Отметки в списке (20.09): Ctrl+щелчок набирает несколько записей, чтобы
   удалить их разом. Обычный щелчок, как и раньше, просто открывает запись и
   отметки снимает — иначе они копились бы незаметно для человека. */
function isPicked(id) {
  return (S.selected || []).includes(id);
}

function togglePicked(id) {
  const picked = (S.selected || []).slice();
  // Первый Ctrl+щелчок при открытой записи берёт в отметку и её: так ведёт
  // себя список файлов в Windows, и удивлять человека тут незачем.
  if (!picked.length && S.currentId && S.currentId !== id) picked.push(S.currentId);
  const at = picked.indexOf(id);
  if (at >= 0) picked.splice(at, 1); else picked.push(id);
  S.selected = picked;
  paintSidebar();
}

function pickedIds() {
  // Ничего не отмечено — работаем с открытой записью, как раньше.
  const picked = (S.selected || []).filter((id) =>
    (S.recordings || []).some((m) => m.id === id));
  return picked.length ? picked : (S.currentId ? [S.currentId] : []);
}

/* Щелчки по строкам списка — одним местом на оба вида, плоский и по папкам. */
function bindRecItems(box) {
  box.querySelectorAll('.rec-item').forEach((el) => {
    el.onclick = (ev) => {
      const id = el.dataset.id;
      if (ev.ctrlKey || ev.metaKey) {
        ev.preventDefault();
        togglePicked(id);
        return;
      }
      S.selected = [];
      openRecording(id);
    };
  });
}

/* Одна строка списка. Вынесена отдельно, потому что рисуется из двух мест:
   плоским списком и внутри папки проекта. */
function recItemHtml(m) {
  const isRec = m.status === 'recording';
  // Вторая строка — дата, длительность и участники.
  // Категорию в каждой строке не повторяем: она одна почти у всех записей
  // и ничего не различает; отбор по ней будет над списком.
  const v = m.diarized ? (m.voices || null) : null;
  let who = '', whoHint = '';
  if (v && v.mine && v.others) {
    who = `${v.mine} + ${v.others}`;
    whoHint = `${v.mine} в комнате, ${v.others} на связи`;
  } else if (v && (v.mine || v.others)) {
    who = String(v.mine || v.others);
    whoHint = `голосов в записи: ${who}`;
  }
  const sub = [fmtDate(m.created_at), isRec ? fmtClock(m.duration_s) : fmtDur(m.duration_s),
    who ? `<span title="${esc(whoHint)}">${who}</span>` : ''].filter(Boolean).join(' · ');
  // Третья строка — только исключения: обычное состояние ничего не сообщает,
  // а «✓ говорящие» у каждой записи просто занимало место.
  let note = '';
  if (isRec) note = '<span class="dot-st bad"></span>идёт запись';
  else if (m.diarize_status === 'running' || m.diarize_status === 'queued') {
    note = '<span class="dot-st go"></span>идёт разметка';
  } else if (m.status === 'processing' || m.status === 'queued') {
    note = '<span class="dot-st go"></span>идёт обработка';
  } else if (m.status === 'stopped') {
    note = '<span class="dot-st bad"></span>обработка остановлена';
  } else if (m.status === 'recorded' && !m.diarized && m.duration_s) {
    note = '<span class="dot-st none"></span>говорящие не размечены';
  }
  return `<div class="rec-item${m.id === S.currentId ? ' active' : ''}${isPicked(m.id) ? ' picked' : ''}" data-id="${m.id}">
      <div class="rec-item-title"><span class="nm">${esc(m.title)}</span></div>
      <div class="rec-item-sub">${sub}</div>
      ${note ? `<div class="rec-item-note">${note}</div>` : ''}</div>`;
}

/* Список, сложенный по проектам: заголовок папки, счётчик, сворачивание.
   Записи без проекта — последней группой: их обычно больше всех, и наверху
   они отодвинули бы сами проекты. */
function groupedHtml(list) {
  const groups = [];
  const byName = new Map();
  list.forEach((m) => {
    const name = String(m.project || '').trim();
    const key = name.toLowerCase();
    if (!byName.has(key)) {
      const g = { name: name, items: [] };
      byName.set(key, g);
      groups.push(g);
    }
    byName.get(key).items.push(m);
  });
  groups.sort((a, b) => {
    if (!a.name !== !b.name) return a.name ? -1 : 1;
    return a.name.localeCompare(b.name, 'ru');
  });
  return groups.map((g) => {
    const key = g.name.toLowerCase();
    const folded = !!S.foldedProjects[key];
    const folder = g.name ? (S.projectFolders || {})[
      Object.keys(S.projectFolders || {}).find((k) => k.toLowerCase() === key) || ''] : '';
    const hint = g.name
      ? (folder ? `Проект «${g.name}», заметки в ${folder}` : `Проект «${g.name}»`)
      : 'Записи без проекта';
    return `<div class="rec-group">
      <div class="rec-group-head${folded ? ' folded' : ''}" data-project="${esc(key)}" title="${esc(hint)}">
        <span class="grp-caret">${folded ? '▸' : '▾'}</span>
        <span class="grp-name">${esc(g.name || 'Без проекта')}</span>
        <span class="grp-count">${g.items.length}</span>
      </div>
      ${folded ? '' : g.items.map(recItemHtml).join('')}</div>`;
  }).join('');
}

function paintSidebar() {
  const box = $('rec-list');
  const all = S.recordings || [];
  const byMode = S.mode === 'video' ? all.filter(isVideoRec) : all.filter((m) => !isVideoRec(m));
  paintListFilter();
  const list = listFilter(byMode);
  if (!list.length) {
    const filtered = byMode.length && !list.length;
    box.innerHTML = '<div class="tiny muted" style="padding:14px 10px">'
      + (filtered ? 'Под этот отбор ничего не подходит'
        : (S.mode === 'video' ? 'Обработанных видео пока нет' : 'Записей пока нет')) + '</div>';
    return;
  }
  if (S.groupByProject) {
    box.innerHTML = groupedHtml(list);
    box.querySelectorAll('.rec-group-head').forEach((el) => {
      el.onclick = () => {
        const key = el.dataset.project;
        S.foldedProjects[key] = !S.foldedProjects[key];
        paintSidebar();
      };
    });
    bindRecItems(box);
    paintPickedNote();
    return;
  }
  box.innerHTML = list.map(recItemHtml).join('');
  bindRecItems(box);
  paintPickedNote();
}

/* Сколько записей отмечено — строкой под списком. Без неё человек, отметивший
   три записи и ушедший в другой раздел, не поймёт, почему Delete спрашивает
   про три. */
function paintPickedNote() {
  const box = $('picked-note');
  if (!box) return;
  const n = (S.selected || []).length;
  box.classList.toggle('hidden', !n);
  if (!n) return;
  box.innerHTML = `Отмечено записей: ${n}. <button type="button" class="ghost small js-picked-clear">снять</button>`;
  box.querySelector('.js-picked-clear').onclick = () => { S.selected = []; paintSidebar(); };
}

/* Одна функция на все места, где показывается ход задачи:
   блок «В работе» в боковой панели и строка состояния записи. Своего расчёта
   остатка ни у кого больше нет — раньше одна и та же разметка показывалась как
   «осталось ~1 мин» в панели записи и «45 % · осталось ~12 с» в «В работе». */
function jobEta(j) {
  if (!j.eta_s) return '';
  return ' · осталось ~' + (j.eta_s > 90 ? Math.round(j.eta_s / 60) + ' мин'
    : Math.round(j.eta_s) + ' с');
}

function jobLine(j) {
  const pct = Math.round((j.progress || 0) * 100);
  const head = j.title || 'задача';
  if (j.status === 'queued') return head + ' · в очереди';
  // Процент — только там, где он настоящий. У одного вызова модели прогресса
  // нет, и «0 %» рядом с работающей задачей выглядит как поломка.
  return pct > 0 ? `${head} · ${pct} %${jobEta(j)}` : head + ' · идёт';
}

function paintJobs() {
  const act = S.jobs.filter((j) => j.status === 'queued' || j.status === 'running');
  const panel = $('jobs-panel');
  if (!act.length) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');
  $('jobs-list').innerHTML = act.map((j) => {
    const pct = Math.round((j.progress || 0) * 100);
    // Кнопка остановки нужна на каждой задаче: очередь однопоточная, и лишняя
    // обработка держит остальные — в том числе протокол живого совещания.
    // Признак отмены берём у службы (cancel_requested), а свой cancel_asked
    // держим только до её первого ответа: иначе надпись слетала бы обратно на
    // крестик при следующем опросе, будто нажатия не было.
    const stop = (j.cancel_requested || j.cancel_asked)
      ? '<span class="tiny muted job-stop">останавливаю…</span>'
      : `<button class="icon-btn job-stop js-stop-job" data-id="${esc(j.id)}" title="Остановить" aria-label="Остановить" type="button"><svg aria-hidden="true"><use href="/static/icons.svg#ic-x"></use></svg></button>`;
    return `<div class="job"><div class="job-title"><span>${esc(j.title)}</span>${stop}</div>
      <div class="bar"><div class="bar-fill" style="width:${pct}%"></div></div>
      <div class="job-note">${esc(jobLine(j))}${j.note ? ' — ' + esc(j.note) : ''}</div></div>`;
  }).join('');
  $('jobs-list').querySelectorAll('.js-stop-job').forEach((b) => {
    b.onclick = () => stopJob(b.dataset.id);
  });
}

/* Документ открытой записи не собрался. Показываем, почему, и что можно
   сделать. Если подписка Claude упёрлась в лимит — кнопка «Собрать через
   облако»: движок меняется только для этой сборки, общая настройка остаётся.
   Молча в облако программа не шлёт никогда — только по этой кнопке. */
const DOC_KINDS = ['minutes', 'summary', 'media'];
function paintDocAlert() {
  const box = $('doc-alert');
  const id = S.currentId;
  const hide = () => { box.classList.add('hidden'); box.innerHTML = ''; };
  if (!id) return hide();
  const mine = (S.jobs || []).filter((j) => j.rec_id === id && DOC_KINDS.includes(j.kind));
  if (!mine.length) return hide();
  const last = mine.reduce((a, b) => ((b.created_at || 0) > (a.created_at || 0) ? b : a));
  if (last.status !== 'error' || last.error_kind !== 'claude_limit' || !last.retry
      || S.docAlertHidden === last.id) return hide();
  const cloudEng = ((S.caps && S.caps.engines) || []).find((e) => e.key === 'api') || {};
  const cloud = cloudEng.ready
    ? `<button class="primary small js-doc-cloud" title="Текст стенограммы уйдёт в ${esc(cloudEng.provider_title || 'выбранный сервис')} по вашему ключу"><svg aria-hidden="true"><use href="/static/icons.svg#ic-cloud"></use></svg> Собрать через облако (${esc(cloudEng.provider_title || 'по ключу')})</button>`
    : '<button class="ghost small js-doc-settings"><svg aria-hidden="true"><use href="/static/icons.svg#ic-cloud"></use></svg> Настроить облако — ключ в «Настройки → Модели»</button>';
  box.innerHTML = `<div>⛔ ${esc(last.error || last.note || 'Подписка Claude упёрлась в лимит.')}</div>
    <div class="btn-row">${cloud}
      <button class="ghost small js-doc-again">Повторить через Claude</button>
      <button class="ghost small js-doc-hide">Скрыть</button></div>`;
  box.classList.remove('hidden');
  const resend = async (engine) => {
    const body = Object.assign({}, (last.retry && last.retry.body) || {}, { engine: engine });
    S.docAlertHidden = last.id;
    hide();
    try {
      await api(last.retry.url, { method: 'POST', body: body });
      notice(engine === 'api' ? 'Собираю через облако…' : 'Пробую снова через Claude…', 'ok');
    } catch (e) { notice(e.message, 'err'); S.docAlertHidden = null; paintDocAlert(); }
  };
  const q = (sel) => box.querySelector(sel);
  if (q('.js-doc-cloud')) q('.js-doc-cloud').onclick = () => resend('api');
  if (q('.js-doc-settings')) q('.js-doc-settings').onclick = () => openSettings();
  q('.js-doc-again').onclick = () => resend('claude_cli');
  q('.js-doc-hide').onclick = () => { S.docAlertHidden = last.id; hide(); };
}

/* Остановка задачи. Подтверждения нет намеренно: человек жмёт её, когда понял,
   что запустил не то, и лишний вопрос тут только мешает. */
async function stopJob(jobId) {
  const job = (S.jobs || []).find((j) => j.id === jobId);
  if (job) { job.cancel_asked = true; paintJobs(); }
  try {
    const res = await api(`/api/jobs/${jobId}/cancel`, { method: 'POST' });
    if (res && res.cancelled) notice('Останавливаю. Долгие шаги прервутся не сразу.', '');
    else notice('Эта задача уже завершилась', '');
  } catch (e) {
    if (job) job.cancel_asked = false;
    paintJobs();
    notice(e.message, 'err');
  }
}

/* Цвет имени собеседника (пакет тем 13.09): у названного — свой постоянный
   цвет по имени из шести, безымянные («Спикер 2», «· голос 1») — нейтральные,
   «Я» — свой цвет --me через класс .me у строки. */
const SPEAKER_SLOTS = 8;
function speakerUnnamed(nm) {
  return !nm || /^(Спикер|Говорящий|Участник|Голос|Рядом со мной)(\s|$)/i.test(nm) || / · голос \d+$/.test(nm);
}
function speakerHash(nm) {
  let h = 0;
  for (let i = 0; i < nm.length; i++) h = (h * 31 + nm.charCodeAt(i)) % 997;
  return h % SPEAKER_SLOTS;
}
/* Цвета собеседников записи: у каждого по возможности свой. Цвет берётся от
   имени (одно имя — один цвет из записи в запись), а если он уже занят другим
   собеседником этой записи — следующий свободный (замечено 13.09:
   двое разных людей были одним цветом). */
function speakerSlots(segs) {
  const map = {};
  const taken = new Set();
  (segs || []).forEach((s) => {
    const nm = String(s.speaker || '');
    if (speakerUnnamed(nm) || speakerClass(s) === 'me' || nm in map) return;
    let slot = speakerHash(nm);
    for (let k = 0; k < SPEAKER_SLOTS && taken.has(slot); k++) slot = (slot + 1) % SPEAKER_SLOTS;
    map[nm] = slot;
    taken.add(slot);
  });
  return map;
}
function speakerColor(seg, slots) {
  const nm = String(seg.speaker || '');
  if (speakerUnnamed(nm) || speakerClass(seg) === 'me') return '';
  const slot = slots && nm in slots ? slots[nm] : speakerHash(nm);
  return ' c' + (slot + 1);
}

function speakerClass(seg) {
  // Цвет владельца — только у реплик, которые и правда его: дорожка микрофона
  // и говорящий «me». Сосед по кабинету («me~1»), реплика, отданная другому
  // человеку руками, и эхо, узнанное по голосу, — это уже не «я», и красить их
  // цветом владельца нельзя (17.09: одно имя оказывалось двух цветов, а два
  // разных имени — одного).
  return seg.track === 'mic' && String(seg.speaker_key || '') === 'me' ? 'me' : 'them';
}

function capsRows() {
  const c = S.caps || {};
  if (!Object.keys(c).length) return '<div class="tiny muted">проверяю готовность…</div>';
  const rows = [];
  rows.push('<div>Распознавание речи: <b>локально, GigaAM</b></div>');
  rows.push('<div>Разметка говорящих: ' + (c.diarize
    ? '<b>готова</b>'
    : '<b>не готова</b> — ' + esc(c.diarize_note || '')) + '</div>');
  rows.push('<div>Протокол: ' + (c.claude_cli
    ? '<b>Claude CLI найден</b>' : 'нужен ключ API или Claude CLI') + '</div>');
  rows.push('<div>Встреча из Outlook: ' + (c.outlook ? '<b>доступна</b>' : 'недоступна') + '</div>');
  rows.push('<div>Замечать звонки: ' + (c.call_detect ? '<b>включено</b>' : 'выключено') + '</div>');
  rows.push('<div>Хранилище: ' + esc((S.vault && S.vault.root) || '') + '</div>');
  return rows.join('');
}

// ——— Правка стенограммы руками (16.09) ———————————————————————————
// Разметка голосов ошибается предсказуемо: хвост фразы уезжает к соседу.
// В режиме правки каждое слово — кнопка: щелчок разрезает реплику перед ним.

function segWords(seg) {
  // Как и служба: сначала слова со временем, иначе просто делим текст.
  const timed = (seg.words || [])
    .map((w) => String((w && w.text) || '').trim()).filter(Boolean);
  return timed.length ? timed : String(seg.text || '').split(/\s+/).filter(Boolean);
}

function editWords(seg) {
  return segWords(seg).map((w, i) => (i === 0
    ? `<span class="w w-first" data-i="0">${esc(w)}</span>`
    : `<span class="w" data-i="${i}" title="Alt+щелчок — разрезать реплику перед этим словом">${esc(w)}</span>`)).join(' ');
}

// Выделенные слова: мышью по тексту. Alt+↑ отдаёт их реплике выше, Alt+↓ — ниже.
function selectedWords() {
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !String(sel.toString() || '').trim()) return null;
  const line = (sel.anchorNode && sel.anchorNode.parentElement
    && sel.anchorNode.parentElement.closest('.line'));
  const to = (sel.focusNode && sel.focusNode.parentElement
    && sel.focusNode.parentElement.closest('.line'));
  if (!line || line !== to) return null;
  const nums = [...line.querySelectorAll('.txt .w')]
    .filter((w) => sel.containsNode(w, true)).map((w) => Number(w.dataset.i));
  if (!nums.length) return null;
  return { id: line.dataset.id, first: Math.min(...nums), last: Math.max(...nums) };
}

// Выделенное можно отдать не только соседу: Enter показывает всех говорящих
// записи, и кусок уходит выбранному (решение 16.09).
function hideGiveMenu() {
  const box = $('give-menu');
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
}

// pick задан — отдаём готовый кусок (щелчок по имени: вся реплика целиком),
// иначе берём выделенное мышью.
async function openGiveMenu(pick, anchor) {
  pick = pick || selectedWords();
  if (!pick) {
    notice('Сначала выделите слова в реплике — их и отдам выбранному человеку.');
    return;
  }
  const line = document.querySelector(`.line[data-id="${pick.id}"]`);
  const mine = line ? line.dataset.key : '';
  let list = [];
  try {
    const res = await api(`/api/recordings/${S.currentId}/transcript/speakers`);
    list = (res.speakers || []).filter((s) => s.key !== mine);
  } catch (err) {
    notice(String(err.message || err), 'err');
    return;
  }
  const box = $('give-menu');
  if (!list.length) {
    notice('В этой записи больше никого нет — отдавать слова некому.');
    return;
  }
  const head = anchor ? 'Чья это реплика?' : 'Кому отдать выделенное?';
  box.innerHTML = `<div class="gm-head">${head}</div>` + list.map((s) =>
    `<button type="button" class="gm-item" data-key="${esc(s.key)}">${esc(displaySpeaker(s.name))}</button>`).join('');
  const rect = anchor ? anchor.getBoundingClientRect()
    : window.getSelection().getRangeAt(0).getBoundingClientRect();
  box.classList.remove('hidden');
  box.style.left = `${Math.max(8, Math.min(rect.left, window.innerWidth - box.offsetWidth - 8))}px`;
  box.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - box.offsetHeight - 8)}px`;
  box.querySelectorAll('.gm-item').forEach((b) => {
    b.onclick = () => giveWords(pick, b.dataset.key);
  });
}

async function giveWords(pick, key) {
  hideGiveMenu();
  try {
    const res = await api(`/api/recordings/${S.currentId}/transcript/assign`, {
      method: 'POST',
      body: { segment_id: pick.id, first: pick.first, last: pick.last, speaker_key: key },
    });
    if (S.current) { S.current.segments = res.segments || []; paintTranscript(); }
    notice(`Отдал: ${res.to}`);
    refreshUndo();
  } catch (err) {
    notice(String(err.message || err), 'err');
  }
}

async function moveWords(where) {
  const pick = selectedWords();
  if (!pick) {
    notice('Сначала выделите слова в реплике — их и передам соседу.');
    return;
  }
  try {
    const res = await api(`/api/recordings/${S.currentId}/transcript/move`, {
      method: 'POST',
      body: { segment_id: pick.id, first: pick.first, last: pick.last, where: where },
    });
    if (S.current) { S.current.segments = res.segments || []; paintTranscript(); }
    notice(`Передал: ${res.to || (where === 'prev' ? 'соседу выше' : 'соседу ниже')}`);
    refreshUndo();
  } catch (err) {
    notice(String(err.message || err), 'err');
  }
}

// Правка самого текста реплики: двойной щелчок открывает строку для ввода,
// Enter сохраняет, Escape отменяет (решение 16.09).
function editPhrase(line) {
  if (!line || line.querySelector('.txt-edit')) return;
  const box = line.querySelector('.txt');
  const was = (S.current.segments.find((s) => String(s.id) === line.dataset.id) || {}).text || '';
  const ta = document.createElement('textarea');
  ta.className = 'txt-edit';
  ta.value = was;
  box.replaceWith(ta);
  ta.style.height = `${Math.max(ta.scrollHeight, 28)}px`;
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
  let done = false;
  const close = () => { if (!done) { done = true; paintTranscript(); } };
  ta.onkeydown = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); done = true; savePhrase(line.dataset.id, ta.value); }
  };
  ta.onblur = close;
}

async function savePhrase(segId, text) {
  try {
    const res = await api(`/api/recordings/${S.currentId}/transcript/text`, {
      method: 'POST', body: { segment_id: segId, text: text },
    });
    if (S.current) { S.current.segments = res.segments || []; }
    if (res.changed && !res.kept_times) {
      notice('Текст поправлен. Слов стало другое число — время слов стёрто, дальше границы считаются на глаз.');
    }
    refreshUndo();
  } catch (err) {
    notice(String(err.message || err), 'err');
  }
  paintTranscript();
}

function setEditMode(on) {
  S.editMode = !!on;
  const b = $('btn-edit-transcript');
  b.classList.toggle('on', S.editMode);
  b.querySelector('.lbl').textContent = S.editMode ? 'Готово' : 'Править';
  $('edit-hint').classList.toggle('hidden', !S.editMode);
  hideGiveMenu();
  paintTranscript();
  if (S.editMode) refreshUndo();
}

async function refreshUndo() {
  // Кнопка «Отменить правку» есть только когда правда есть что отменять.
  const btn = $('btn-undo-edit');
  let info = {};
  if (S.editMode && S.currentId) {
    try {
      info = await api(`/api/recordings/${S.currentId}/transcript/undo`) || {};
    } catch (err) { info = {}; }
  }
  btn.classList.toggle('hidden', !info.what);
  if (info.what) {
    btn.textContent = info.steps > 1 ? `Отменить правку (${info.steps})` : 'Отменить правку';
    btn.title = `Ctrl+Z — отменить последнее действие правки, сейчас это «${info.what}».`
      + ` Шагов назад в запасе: ${info.steps}. Глубина и срок — «Настройки → Продвинутые».`;
  }
}

async function splitPhrase(segId, wordIndex) {
  try {
    const res = await api(`/api/recordings/${S.currentId}/transcript/split`, {
      method: 'POST', body: { segment_id: segId, word_index: wordIndex },
    });
    if (S.current) { S.current.segments = res.segments || []; paintTranscript(); }
    if (res.exact === false) {
      notice('Разрезал. Время слов неизвестно — границу поставил на глаз, по длине слов.');
    }
    refreshUndo();
  } catch (err) {
    notice(String(err.message || err), 'err');
  }
}

async function undoEdit() {
  try {
    const res = await api(`/api/recordings/${S.currentId}/transcript/undo`, { method: 'POST' });
    if (S.current) { S.current.segments = res.segments || []; paintTranscript(); }
    notice(res.steps_left
      ? `Отменил: ${res.what}. Ещё шагов назад: ${res.steps_left}`
      : `Отменил: ${res.what}`);
  } catch (err) {
    notice(String(err.message || err), 'err');
  }
  refreshUndo();
}

function paintTranscript() {
  const box = $('transcript');
  const segs = (S.current && S.current.segments) || [];
  if (!S.current) {
    // запись не выбрана: вместо пустоты показываем, что готово к работе
    box.innerHTML =
      '<div class="tr-empty">' +
      '<div class="empty-icon"><svg aria-hidden="true"><use href="/static/icons.svg#ic-mic"></use></svg></div>' +
      '<h3 style="margin:8px 0 4px;color:var(--text)">Готов к записи</h3>' +
      '<p>Нажмите <b>«● Старт»</b>, чтобы начать, или перетащите видео- либо аудиофайл.</p>' +
      '<div class="caps" style="max-width:560px;margin:16px auto 0">' + capsRows() + '</div>' +
      '</div>';
    return;
  }
  if (!segs.length) {
    box.innerHTML = '<div class="tr-empty">Пока ничего не распознано. Нажмите «Старт» и говорите.</div>';
    return;
  }
  const asked = new Set();
  const slots = speakerSlots(segs);
  // ширина столбца имени — по самому длинному имени (в символах), не больше 260 px
  const longest = segs.reduce((n, s) => Math.max(n, displaySpeaker(s.speaker).length), 0);
  box.style.setProperty('--line-who-fit', `max(var(--line-who), min(260px, calc(${longest}ch + 14px)))`);
  // Прокручивать к новой реплике только если человек и так был внизу: тот, кто
  // отлистал вверх перечитать, не должен сбрасываться каждой фразой.
  const atBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
  // Имя — только при смене говорящего, таймкод — у первого куска абзаца.
  // Раньше одно имя повторялось десятками строк подряд, а
  // глазу не за что было зацепиться. Сами реплики остаются отдельными: по ним
  // работают правка, разрез и перенос слов.
  const gapMax = Number((S.settings && S.settings.transcript_merge_gap_s) ?? 2);
  box.innerHTML = segs.map((s, i) => {
    const prev = i > 0 ? segs[i - 1] : null;
    const sameWho = !!prev && String(prev.speaker_key || '') === String(s.speaker_key || '')
      && prev.track === s.track && displaySpeaker(prev.speaker) === displaySpeaker(s.speaker);
    // Пауза меньше порога — тот же абзац; длиннее — новый, со своим таймкодом.
    const glued = sameWho && (Number(s.start) - Number(prev.end || prev.start)) < gapMax;
    const sug = s.suggestion && !s.speaker_locked ? s.suggestion : null;
    let extra = '';
    if (sug && !asked.has(s.speaker_key)) {
      asked.add(s.speaker_key);
      const pct = Math.round((sug.score || 0) * 100);
      extra = `<div class="sugg-inline" data-key="${esc(s.speaker_key)}" data-name="${esc(sug.name)}">
        Похоже на «${esc(sug.name)}» (совпадение ${pct}%) — это он?
        <button class="ghost small js-yes" data-pid="${esc(sug.person_id || '')}">Да</button>
        <button class="ghost small js-no">Нет, другой</button></div>`;
    }
    // «имя?» — только у безымянных: у имён из Teams и подписанных вопрос лишний
    const ask = (!s.speaker_locked && speakerUnnamed(s.speaker)
      && (s.track !== 'mic' || String(s.speaker_key || '').includes('~')))
      ? '<span class="ask" title="указать имя">имя?</span>' : '';
    return `<div class="line ${speakerClass(s)}${sameWho ? ' cont' : ''}${glued ? ' glued' : ''}" data-id="${s.id}" data-key="${esc(s.speaker_key || '')}">
      <span class="ts">${fmtClock(s.start)}</span>
      <span class="who${speakerColor(s, slots)}" data-key="${esc(s.speaker_key || '')}" title="${esc(displaySpeaker(s.speaker))} — щёлкните, чтобы подписать">${sameWho ? '' : esc(displaySpeaker(s.speaker)) + ask}</span>
      <span class="txt${s.edited ? ' edited' : ''}"${s.edited ? ' title="текст правлен вручную"' : ''}>${S.editMode ? editWords(s) : esc(s.text)}</span>
    </div>${extra}`;
  }).join('');
  box.classList.toggle('editing', !!S.editMode);
  if (S.editMode) {
    box.querySelectorAll('.line').forEach((line) => {
      line.ondblclick = (e) => {
        if (e.target.closest('.who')) return;   // двойной щелчок по имени — не правка текста
        editPhrase(line);
      };
    });
  }
  if (S.editMode) {
    box.querySelectorAll('.w').forEach((el) => {
      el.onclick = (e) => {
        // Разрез — только с Alt: простым щелчком человек целится в выделение,
        // и случайное попадание по слову не должно резать реплику (16.09).
        if (!e.altKey || !Number(el.dataset.i)) return;
        e.stopPropagation();
        e.preventDefault();
        splitPhrase(el.closest('.line').dataset.id, Number(el.dataset.i));
      };
    });
  }

  box.querySelectorAll('.who, .ask').forEach((el) => {
    el.onclick = (e) => {
      e.stopPropagation();
      const key = el.dataset.key || el.closest('.line').dataset.key;
      const line = el.closest('.line');
      // В режиме правки щелчок по имени меняет говорящего У ЭТОЙ реплики
      // целиком (17.09): эхо колонок приписывает владельцу чужие фразы от
      // первого до последнего слова, и выделять их мышью незачем.
      if (S.editMode && line) {
        const n = line.querySelectorAll('.txt .w').length;
        if (n) { openGiveMenu({ id: line.dataset.id, first: 0, last: n - 1 }, el); return; }
      }
      openSpeakerDialog(key, line ? line.querySelector('.who').textContent.replace('имя?', '').trim() : '');
    };
  });
  box.querySelectorAll('.js-yes').forEach((b) => {
    b.onclick = async () => {
      const host = b.closest('.sugg-inline');
      await confirmSuggestion(host.dataset.key, { name: host.dataset.name, person_id: b.dataset.pid });
    };
  });
  box.querySelectorAll('.js-no').forEach((b) => {
    b.onclick = async () => {
      const host = b.closest('.sugg-inline');
      await api(`/api/recordings/${S.currentId}/speaker/reject`, {
        method: 'POST', body: { speaker_key: host.dataset.key },
      });
    };
  });
  if (S.recordingId === S.currentId && atBottom) box.scrollTop = box.scrollHeight;
}

function paintDraft() {
  const texts = Object.entries(S.drafts || {}).filter(([, v]) => v && v.trim());
  const box = $('draft-box');
  if (!texts.length || S.recordingId !== S.currentId) { box.classList.add('hidden'); return; }
  box.classList.remove('hidden');
  $('draft-text').textContent = texts
    .map(([tr, v]) => (texts.length > 1 ? (tr === 'mic' ? displaySpeaker('Я') + ': ' : 'участники: ') : '') + v)
    .join('   |   ');
}

/* Состояние кнопок пересчитываем ВСЕГДА, даже когда запись не выбрана.
   Иначе после неудачного старта «+ Новая запись» навсегда остаётся серой. */
function paintButtons() {
  const m = S.current && S.current.meta;
  // «Эта запись пишется прямо сейчас» — для заголовка и подписей.
  const isRec = !!(m && m.status === 'recording' && S.recordingId === m.id);
  const busy = !!S.recordingId || S.starting;
  $('btn-new').disabled = busy;
  $('btn-start').disabled = busy;
  // «Стоп» считается по самому факту записи, а НЕ по открытой карточке.
  // Иначе, открыв во время совещания любую другую запись, человек видел бы
  // кнопку серой и не смог остановить запись, пока не найдёт нужную заметку.
  // stopRecording и так останавливает S.recordingId, а не открытую запись.
  $('btn-stop').disabled = !S.recordingId || S.starting;
  // Во время записи выбор устройств сворачивается в строку с уровнями;
  // развернули кнопкой — остаётся развёрнутым до конца этой записи.
  if (!S.recordingId) S.devicesUnfolded = false;
  $('live-controls').classList.toggle('folded', !!S.recordingId && !S.devicesUnfolded);
  const devName = (id) => { const o = $(id).selectedOptions && $(id).selectedOptions[0]; return o ? o.textContent : ''; };
  $('ctl-summary-dev').textContent = [devName('mic-select'), $('chk-far').checked ? devName('far-select') : '']
    .filter(Boolean).join(' · ');
  $('rec-mode').disabled = busy;
  $('chk-far').disabled = busy;
  $('badge-recording').classList.toggle('hidden',
    !(S.recordingId && $('chk-far').checked));
  return isRec;
}

/* Новая запись и готовая запись — разные состояния экрана.
   У новой и у идущей записи главное — блок записи: «Старт», «Стоп»,
   устройства, уровни. Вкладок, панели действий и строки состояния нет: пока
   писать нечего, они пустые. У готовой записи наоборот: блока записи нет,
   потому что «Старт» начинает НОВУЮ запись (п. 6.10), а места ему нужно
   250 точек — при окне в 800 точек стенограмме не оставалось ничего. */
function recIsFresh(m) {
  // В записи ещё ничего нет: ни звука, ни текста. Такую открывают, чтобы начать.
  return !m.duration_s && !((S.current && S.current.segments) || []).length && !m.media_removed;
}

function paintRecShell() {
  const m = S.current && S.current.meta;
  const isVideo = !!m && isVideoRec(m) && !S.recordingId && S.mode === 'video';
  // Пока идёт запись, блок виден на любой открытой записи: иначе человек
  // остаётся без кнопки «Стоп» — потерять её нельзя ни на одном экране.
  const live = !!S.recordingId || !m || m.status === 'recording' || recIsFresh(m);
  const show = live && !isVideo;
  $('live-controls').classList.toggle('hidden', !show);
  $('rec-mode-field').classList.toggle('hidden', !show);
  $('rec-tabs').classList.toggle('hidden', live);
  $('rec-actions').classList.toggle('hidden', live);
  $('rec-status').classList.toggle('hidden', live);
  if (live && S.pane !== 'transcript') showPane('transcript');
}

/* Строка состояния записи. Раньше служебные сообщения жили в
   пяти местах: бейдж в шапке, слово «сохранено» у кнопки, полоса прогресса
   посреди панели записи, блок «В работе», тосты. Здесь — только про открытую
   запись, и формулировки безличные: «идёт разметка», а не «размечаю».
   Точка + текст, без фона и рамки: статус не должен выглядеть нажимаемым. */
function statDot(kind, text) {
  return text ? `<span class="dot-st ${kind}"></span>${text}` : '';
}

/* Меню записи «⋯»: редкие действия со всей записью целиком.
   Заметка открывается сама, а не её папка; удаление идёт через тот же диалог
   с перечнем, что и раньше. */
function closeRecMenu() {
  const old = document.getElementById('rec-menu');
  if (old) old.remove();
  document.removeEventListener('mousedown', onRecMenuOutside, true);
}

function onRecMenuOutside(ev) {
  const box = document.getElementById('rec-menu');
  if (box && !box.contains(ev.target) && ev.target !== $('btn-rec-menu')) closeRecMenu();
}

async function revealRec(what, openFile) {
  try {
    await api(`/api/recordings/${S.currentId}/reveal`,
      { method: 'POST', body: { what: what, open: !!openFile } });
  } catch (e) { notice(e.message, 'err'); }
}

function openRecMenu() {
  if (document.getElementById('rec-menu')) { closeRecMenu(); return; }
  const m = S.current && S.current.meta;
  if (!m) return;
  const box = document.createElement('div');
  box.id = 'rec-menu';
  box.className = 'rec-menu';
  const hasNote = !!m.vault_path;
  box.innerHTML = `
    <button type="button" class="js-note"${hasNote ? ' title="Файл заметки откроется программой, назначенной в Windows для .md"' : ' disabled title="Файла заметки ещё нет: запись не сохранена"'}>Открыть файл заметки</button>
    <button type="button" class="js-folder">Открыть служебную папку</button>
    <button type="button" class="js-del danger">Удалить запись…</button>`;
  document.body.appendChild(box);
  const r = $('btn-rec-menu').getBoundingClientRect();
  box.style.top = `${Math.round(r.bottom + 4)}px`;
  box.style.left = `${Math.round(Math.max(8, r.right - box.offsetWidth))}px`;
  box.querySelector('.js-note').onclick = () => { closeRecMenu(); revealRec('note', true); };
  box.querySelector('.js-folder').onclick = () => { closeRecMenu(); revealRec('data', false); };
  box.querySelector('.js-del').onclick = () => { closeRecMenu(); $('btn-delete').click(); };
  document.addEventListener('mousedown', onRecMenuOutside, true);
}

/* Панель действий не переносится на вторую строку: что не
   помещается по ширине окна — уходит в меню «⋯» в конце панели. Прячем с
   конца левой группы: слева стоят частые действия, справа — «Удалить» и
   акцентная кнопка, их прятать нельзя. */
function actionsMenuClose() {
  const m = document.getElementById('acts-menu');
  if (m) m.remove();
  document.removeEventListener('mousedown', actionsMenuOutside, true);
}

function actionsMenuOutside(ev) {
  const box = document.getElementById('acts-menu');
  const btn = document.getElementById('acts-more');
  if (box && !box.contains(ev.target) && ev.target !== btn) actionsMenuClose();
}

function fitActions() {
  const group = document.querySelector('#rec-actions .act-group:not(.hidden)');
  if (!group || !group.clientWidth) return;
  const more = $('acts-more');
  // Сначала возвращаем всё на место: ширина окна могла вырасти.
  group.querySelectorAll('.act-hidden').forEach((b) => b.classList.remove('act-hidden'));
  more.classList.add('hidden');
  const spacer = group.querySelector('.fb-spacer');
  const left = [...group.children].filter((el) => el !== more && el !== spacer
    && (!spacer || el.compareDocumentPosition(spacer) & Node.DOCUMENT_POSITION_FOLLOWING));
  const fits = () => group.scrollWidth <= group.clientWidth + 1;
  if (fits()) return;
  more.classList.remove('hidden');
  for (let i = left.length - 1; i >= 0 && !fits(); i -= 1) {
    if (left[i].tagName === 'BUTTON') left[i].classList.add('act-hidden');
  }
}

function openActionsMenu() {
  if (document.getElementById('acts-menu')) { actionsMenuClose(); return; }
  const group = document.querySelector('#rec-actions .act-group:not(.hidden)');
  const hidden = [...group.querySelectorAll('button.act-hidden')];
  if (!hidden.length) return;
  const box = document.createElement('div');
  box.id = 'acts-menu';
  box.className = 'rec-menu';
  hidden.forEach((b) => {
    const item = document.createElement('button');
    item.type = 'button';
    item.textContent = b.textContent.trim();
    item.disabled = b.disabled;
    if (b.title) item.title = b.title;
    if (b.classList.contains('danger')) item.className = 'danger';
    item.onclick = () => { actionsMenuClose(); b.click(); };
    box.appendChild(item);
  });
  document.body.appendChild(box);
  const r = $('acts-more').getBoundingClientRect();
  box.style.left = `${Math.round(Math.max(8, r.left))}px`;
  box.style.top = `${Math.round(r.top - box.offsetHeight - 4)}px`;
  document.addEventListener('mousedown', actionsMenuOutside, true);
}

function paintRecStatus() {
  const m = S.current && S.current.meta;
  const hint = $('save-hint'), state = $('rec-state'), job = $('rec-job');
  if (!hint || !state || !job) return;
  if (!m) {
    hint.innerHTML = ''; state.innerHTML = ''; job.innerHTML = '';
    return;
  }
  hint.innerHTML = m.vault_path
    ? statDot('ok', 'сохранено')
    : statDot('none', 'заметка не сохранена');
  let label = '', kind = 'ok';
  if (m.status === 'processing') { label = 'идёт обработка'; kind = 'go'; }
  else if (m.status === 'stopped') { label = 'обработка остановлена'; kind = 'bad'; }
  else if (m.diarize_status === 'running') { label = 'идёт разметка'; kind = 'go'; }
  else if (m.diarized) { label = 'говорящие размечены'; kind = 'ok'; }
  else if (m.status === 'recorded') { label = 'говорящие не размечены'; kind = 'none'; }
  state.innerHTML = statDot(kind, label);
  // Ход задачи именно по этой записи: проценты и остаток считает одна функция
  // на всю программу, своего расчёта здесь нет.
  const mine = (S.jobs || []).find((j) => j.rec_id === m.id
    && (j.status === 'running' || j.status === 'queued'));
  job.innerHTML = mine ? statDot('go', jobLine(mine)) : '';
}

function paintHead() {
  const isRec = paintButtons();
  paintDocAlert();
  const m = S.current && S.current.meta;
  if (!m) {
    // запись не выбрана: показываем пустую карточку, но интерфейс не прячем
    $('rec-title').value = '';
    $('rec-title').placeholder = 'Название записи';
    $('rec-title').disabled = true;
    $('rec-date').textContent = '';
    $('rec-duration').textContent = '00:00:00';
    $('rec-stand').classList.add('hidden');
    paintRecStatus();
    ['btn-save', 'btn-diarize', 'btn-retry', 'btn-echo', 'btn-add-doc', 'btn-delete',
      'btn-copy-transcript', 'btn-hide', 'btn-edit-transcript', 'btn-del-cat', 'btn-share']
      .forEach((id) => { $(id).disabled = true; });
    $('diar-progress').classList.add('hidden');
    paintRecShell();
    return;
  }
  ['btn-copy-transcript', 'btn-hide', 'btn-edit-transcript', 'btn-del-cat'].forEach((id) => { $(id).disabled = false; });
  $('rec-title').disabled = false;
  $('rec-title').placeholder = 'Название записи';
  $('btn-save').disabled = false;
  $('btn-delete').disabled = false;
  if (document.activeElement !== $('rec-title')) $('rec-title').value = m.title || '';
  $('rec-date').textContent = fmtDate(m.created_at);
  $('rec-duration').textContent = fmtDur(m.duration_s);
  // Каким вариантом распознавания шла запись и как он себя вёл (стенд 18.09).
  const stand = m.asr_stand;
  $('rec-stand').classList.toggle('hidden', !stand);
  $('rec-stand-title').textContent = stand ? (stand.title || '') : '';
  $('rec-stand').title = stand ? (stand.text || '') : '';
  $('rec-mode').value = m.mode === 'offline' ? 'offline' : 'online';

  $('cat-select').value = m.category || '';
  $('rec-project').value = m.project || '';
  $('rec-tags').value = (m.tags || []).join(', ');
  paintTagChips();
  paintProjectFolder();
  paintNotes();
  paintLinkChips();
  paintContinues();
  paintRecStatus();

  // Блок записи, вкладки, панель действий и строка состояния — одним решением.
  paintRecShell();
  requestAnimationFrame(fitActions);

  const busy = (S.jobs || []).some((j) => j.rec_id === m.id && (j.status === 'running' || j.status === 'queued'));
  const hasText = !!(S.current.segments || []).length;
  // Звук удалён — размечать голоса и перечитывать нечем, а документы по
  // стенограмме собираются по-прежнему: ради этого звук и удаляли.
  const noMedia = !!m.media_removed;
  $('btn-diarize').disabled = busy || !m.duration_s || noMedia;
  $('btn-retry').disabled = busy || !m.duration_s || isRec || noMedia;
  // «Перечитать точнее» есть, только когда звонки идут другой моделью, чем
  // файлы (решение 21.09): перечитывать той же моделью незачем.
  $('btn-retry').classList.toggle('hidden', !!(S.asr && S.asr.reread === false));
  $('btn-add-doc').disabled = busy || !hasText;
  // «Отправить» (20.09): отправлять нечего, пока нет ни документа, ни
  // стенограммы. Идущую запись тоже не отправляем — она ещё не готова.
  $('btn-share').disabled = isRec || (!hasText && !(S.docs || []).length);
  // Пересчёт эха текст не распознаёт заново, поэтому нужен только текст —
  // но дорожка собеседников должна быть: эхо ищется сравнением с ней.
  if ($('btn-echo')) {
    const twoTracks = (S.current.segments || []).some((x) => x.track === 'far');
    $('btn-echo').disabled = busy || isRec || !hasText || !twoTracks;
    $('btn-echo').classList.toggle('hidden', !twoTracks);
  }
  const noMediaWhy = 'Видео и звук удалены — для этого нужен звук';
  $('btn-retry').title = noMedia ? noMediaWhy
    : 'Распознать запись заново точной моделью: дольше, но меньше ошибок в словах';
  if (!S.caps.diarize) {
    $('btn-diarize').disabled = true;
    $('btn-diarize').title = S.caps.diarize_note || 'разметка говорящих не готова';
  } else {
    $('btn-diarize').title = noMedia ? noMediaWhy
      : 'Разделить собеседников по голосам и подписать знакомых по базе голосов';
  }

  const dj = (S.jobs || []).find((j) => j.rec_id === m.id && j.kind === 'diarize' &&
    (j.status === 'running' || j.status === 'queued'));
  const dp = $('diar-progress');
  if (dj) {
    dp.classList.remove('hidden');
    $('diar-bar').style.width = Math.round((dj.progress || 0) * 100) + '%';
    const eta = dj.eta_s ? `, осталось ~${Math.max(1, Math.round(dj.eta_s / 60))} мин` : '';
    $('diar-note').textContent = `${dj.note || 'размечаю'}${eta}`;
  } else {
    dp.classList.add('hidden');
  }
}

function paintCaps() {
  $('empty-caps').innerHTML = capsRows();
}

/* Где у записи что лежит: видео, текстовая копия, заметка. Список приходит от
   службы и содержит только существующие файлы — страница ничего не угадывает. */
function paintPaths() {
  const box = $('paths-box');
  const items = (S.current && S.current.paths) || [];
  if (!items.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.classList.remove('hidden');
  box.innerHTML = items.map((it) =>
    `<div class="path-row">
       <span class="path-title">${esc(it.title)}</span>
       <span class="path-val" title="${esc(it.path)}">${esc(it.path)}</span>
       <button class="ghost small js-reveal" data-key="${esc(it.key)}" type="button">Открыть папку</button>
     </div>`).join('');
  box.querySelectorAll('.js-reveal').forEach((b) => {
    b.onclick = async () => {
      try {
        await api(`/api/recordings/${S.currentId}/reveal`,
          { method: 'POST', body: { what: b.dataset.key } });
      } catch (e) { notice(e.message, 'err'); }
    };
  });
}

function paintCategories() {
  const sel = $('cat-select');
  const cur = (S.current && S.current.meta && S.current.meta.category) || '';
  const cats = (S.categories || []).slice();
  // Категорию убрали из списка, а у открытой записи она осталась — показываем
  // как есть, иначе список молча показывал бы чужую категорию.
  if (cur && !cats.includes(cur)) cats.push(cur);
  sel.innerHTML = cats.map((c) =>
    `<option value="${esc(c)}"${c === cur ? ' selected' : ''}>${esc(c)}${(S.categories || []).includes(c) ? '' : ' (убрана из списка)'}</option>`).join('');
}

/* Подсказки «Проект» и «Теги»: то, что уже вводили (17.09).
   Список живёт в настройках и пополняется сам. Проект — через datalist, его
   браузер подставляет сам. Теги — чипсами: щелчок дописывает тег в поле, потому
   что datalist на поле со списком через запятую подсказывает бесполезно. */
async function loadLabels() {
  try {
    const res = await api('/api/labels');
    S.projects = res.projects || [];
    S.recTags = res.tags || [];
    S.projectFolders = res.folders || {};
  } catch (e) {
    S.projects = S.projects || []; S.recTags = S.recTags || [];
    S.projectFolders = S.projectFolders || {};
  }
  const dl = $('dl-projects');
  if (dl) dl.innerHTML = (S.projects || []).map((p) => `<option value="${esc(p)}"></option>`).join('');
  paintTagChips();
  paintProjectFolder();
}

/* Папка проекта в сейфе (20.09). Связка живёт у проекта, а не у записи,
   поэтому подсказка меняется вслед за полем «Проект», а не за записью. */
function currentProjectFolder() {
  const name = ($('rec-project').value || '').trim().toLowerCase();
  if (!name) return '';
  const map = S.projectFolders || {};
  const key = Object.keys(map).find((k) => k.toLowerCase() === name);
  return key ? map[key] : '';
}

function paintProjectFolder() {
  const hint = $('project-folder-hint');
  const btn = $('btn-project-folder');
  if (!hint || !btn) return;
  const name = ($('rec-project').value || '').trim();
  // Без проекта привязывать нечего: папка принадлежит проекту, не записи.
  btn.disabled = !name;
  const folder = currentProjectFolder();
  hint.textContent = !name ? ''
    : (folder ? `заметки проекта → ${folder}` : 'папка не привязана, заметка — по категории');
}

function paintTagChips() {
  const box = $('tag-chips');
  if (!box) return;
  const used = ($('rec-tags').value || '').split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
  const free = (S.recTags || []).filter((t) => !used.includes(t.toLowerCase())).slice(0, 8);
  box.innerHTML = free.length
    ? free.map((t) => `<button type="button" class="tag-chip" data-tag="${esc(t)}">${esc(t)}</button>`).join('')
    : '';
  box.querySelectorAll('.tag-chip').forEach((b) => {
    b.onclick = () => {
      const cur = ($('rec-tags').value || '').trim();
      $('rec-tags').value = cur ? `${cur.replace(/,\s*$/, '')}, ${b.dataset.tag}` : b.dataset.tag;
      saveLabels();
    };
  });
}

/* Сохранить проект и теги. Заметка перерисовывается на стороне службы. */
async function saveLabels() {
  if (!S.currentId) return;
  const project = ($('rec-project').value || '').trim();
  const tags = ($('rec-tags').value || '').split(',').map((s) => s.trim()).filter(Boolean);
  try {
    const meta = await api(`/api/recordings/${S.currentId}`, {
      method: 'PATCH', body: { project: project, tags: tags },
    });
    S.current.meta = meta;
    // Служба чистит значения (пробелы в тегах, длина) — показываем, что вышло.
    $('rec-project').value = meta.project || '';
    $('rec-tags').value = (meta.tags || []).join(', ');
    await loadLabels();
  } catch (e) { notice(e.message, 'err'); }
}

/* Задачи наружу, в Todoist (17.09). Единственное действие программы, которое
   уходит за пределы компьютера и не отменяется кнопкой «отмена». Поэтому здесь
   три заслона, и главный из них стоит в службе, а не тут: показ списка до
   отправки, явное согласие, отметка «уже отправлено» против дублей. */
async function openTasks() {
  if (!S.currentId) return;
  const panel = $('tasks-panel');
  $('tasks-list').innerHTML = '<div class="tiny muted">читаю документ…</div>';
  panel.classList.remove('hidden');
  let res;
  try { res = await api(`/api/tasks/${S.currentId}`); }
  catch (e) { $('tasks-list').innerHTML = `<div class="tiny err-line">${esc(e.message)}</div>`; return; }
  S.tasks = res;
  const warn = $('tasks-warn');
  warn.classList.toggle('hidden', res.configured);
  if (!res.configured) {
    warn.textContent = 'Токен Todoist не задан — отправлять некуда. Настройки → Обработка.';
  }
  if (!(res.items || []).length) {
    $('tasks-list').innerHTML = '<div class="tiny muted">В документе нет разделов с поручениями.</div>';
  } else {
    $('tasks-list').innerHTML = res.items.map((it, i) => `
      <label class="task-row${it.sent ? ' sent' : ''}">
        <input type="checkbox" data-i="${i}"${it.sent ? ' disabled' : ''}>
        <span class="task-text">${esc(it.clean)}</span>
        ${it.sent ? `<span class="tiny muted">уже отправлено${it.sent_at ? ' · ' + esc(String(it.sent_at).slice(0, 10)) : ''}</span>` : ''}
      </label>`).join('');
  }
  paintTaskProjects(res.project);
  $('btn-tasks-send').disabled = !res.configured;
}

function paintTaskProjects(chosen) {
  const sel = $('tasks-project');
  const list = S.todoistProjects || (chosen && chosen.id ? [chosen] : []);
  sel.innerHTML = ['<option value="">по умолчанию (Inbox)</option>']
    .concat(list.map((p) => `<option value="${esc(p.id)}"${chosen && p.id === chosen.id ? ' selected' : ''}>${esc(p.name)}</option>`))
    .join('');
}

async function sendTasks() {
  const res = S.tasks || {};
  const picked = Array.from($('tasks-list').querySelectorAll('input[type=checkbox]:checked'))
    .map((c) => (res.items || [])[Number(c.dataset.i)])
    .filter(Boolean);
  if (!picked.length) { notice('Не отмечено ни одного поручения', 'err'); return; }
  if (picked.length > (res.max_batch || 25)) {
    notice(`За раз отправляем не больше ${res.max_batch || 25}`, 'err'); return;
  }
  const sel = $('tasks-project');
  const where = sel.value ? sel.options[sel.selectedIndex].text : 'Inbox';
  // Второе, осознанное согласие: список уже виден, здесь называем число и место.
  const list = picked.map((p) => '• ' + p.clean).join('\n');
  if (!confirm(`Отправить в Todoist, проект «${where}», задач: ${picked.length}\n\n${list}\n\nОтменить отправку из программы будет нельзя.`)) return;
  $('btn-tasks-send').disabled = true;
  try {
    const out = await api(`/api/tasks/${S.currentId}/send`, {
      method: 'POST',
      body: { texts: picked.map((p) => p.text), project_id: sel.value, confirmed: true },
    });
    const n = (out.created || []).length;
    const bad = (out.failed || [])[0];
    if (bad) notice(`Отправлено ${n}, дальше сбой: ${bad.error}`, 'err');
    else notice(n ? `Отправлено задач: ${n}` : 'Все выбранные уже были отправлены', 'ok');
    await openTasks();
  } catch (e) { notice(e.message, 'err'); }
  finally { $('btn-tasks-send').disabled = false; }
}

/* «Продолжение предыдущей записи» (17.09). Серию программа не угадывает: у
   еженедельной планёрки и случайной встречи с тем же названием разная судьба,
   и ошибиться дороже, чем не подсказать. Выбирает человек. */
function paintContinues() {
  const sel = $('rec-continues');
  if (!sel) return;
  const cur = (S.current && S.current.meta && S.current.meta.continues) || '';
  const others = (S.recordings || [])
    .filter((r) => r.id !== S.currentId)
    .slice(0, 40);
  const opts = ['<option value="">не связывать</option>'];
  // Выбранная запись могла уехать за предел списка — показываем её всё равно,
  // иначе связь молча выглядела бы снятой.
  if (cur && !others.some((r) => r.id === cur)) {
    const m = (S.recordings || []).find((r) => r.id === cur);
    opts.push(`<option value="${esc(cur)}" selected>${esc(m ? m.title : cur)}</option>`);
  }
  others.forEach((r) => {
    opts.push(`<option value="${esc(r.id)}"${r.id === cur ? ' selected' : ''}>${esc(r.title)} · ${esc(fmtDate(r.created_at))}</option>`);
  });
  sel.innerHTML = opts.join('');
  sel.value = cur;
}

/* «Связать с» (17.09): поиск заметок сейфа и выбранные связи.
   Связи хранятся в данных записи, а не в файле: заметка перерисовывается
   целиком, и дописанное руками в Obsidian пропало бы при сохранении. */
let linkTimer = null;

function currentLinks() {
  return (S.current && S.current.meta && S.current.meta.links) || [];
}

function paintLinkChips() {
  const box = $('link-chips');
  if (!box) return;
  const links = currentLinks();
  box.innerHTML = links.length
    ? links.map((l, i) => `<button type="button" class="tag-chip link-chip" data-i="${i}" title="Убрать связь">${esc(l.title)} ×</button>`).join('')
    : '<span class="muted">связей нет</span>';
  box.querySelectorAll('.link-chip').forEach((b) => {
    b.onclick = () => {
      const links = currentLinks().slice();
      links.splice(Number(b.dataset.i), 1);
      saveLinks(links);
    };
  });
}

/* Окно выбора связей: отбор по названию вверху, галочки по списку.
   Связей у записи может быть несколько, поэтому выбор именно множественный.
   Отмеченное держим в S.linkDraft и пишем в запись одним разом по «Сохранить»:
   так человек может передумать, не наплодив лишних пересохранений заметки. */
function openLinksDialog() {
  if (!S.currentId) return;
  S.linkDraft = currentLinks().slice();
  $('links-filter').value = '';
  $('links-list').innerHTML = '<div class="tiny muted">Введите хотя бы две буквы названия.</div>';
  paintLinksPicked();
  $('overlay').classList.remove('hidden');
  $('dlg-links').classList.remove('hidden');
  $('links-filter').focus();
}

function linkKey(n) {
  return String(n.path || n.title || '').toLowerCase();
}

function paintLinksPicked() {
  const picked = S.linkDraft || [];
  $('links-picked').innerHTML = picked.length
    ? 'Отмечено: ' + picked.map((l) => esc(l.title)).join(' · ')
    : 'Пока ничего не отмечено.';
}

async function searchNotes() {
  const box = $('links-list');
  const q = ($('links-filter').value || '').trim();
  if (q.length < 2) {
    box.innerHTML = '<div class="tiny muted">Введите хотя бы две буквы названия.</div>';
    return;
  }
  let notes = [];
  try {
    notes = (await api(`/api/vault/notes?q=${encodeURIComponent(q)}`)).notes || [];
  } catch (e) {
    box.innerHTML = `<div class="tiny err-line">${esc(e.message)}</div>`;
    return;
  }
  // Уже отмеченное показываем в списке первым, даже если под отбор не попало:
  // иначе снять связь можно было бы только вспомнив её название.
  const keys = notes.map(linkKey);
  const shown = (S.linkDraft || []).filter((l) => !keys.includes(linkKey(l))).concat(notes);
  if (!shown.length) {
    box.innerHTML = '<div class="tiny muted">Ничего не нашлось.</div>';
    return;
  }
  const on = (S.linkDraft || []).map(linkKey);
  box.innerHTML = shown.map((n, i) => `
    <label class="link-row">
      <input type="checkbox" data-i="${i}"${on.includes(linkKey(n)) ? ' checked' : ''}>
      <span class="link-title">${esc(n.title)}</span>
      <span class="tiny muted">${esc(n.path)}</span>
    </label>`).join('');
  box.querySelectorAll('input[type=checkbox]').forEach((c) => {
    c.onchange = () => {
      const n = shown[Number(c.dataset.i)];
      const draft = (S.linkDraft || []).filter((l) => linkKey(l) !== linkKey(n));
      if (c.checked) draft.push({ title: n.title, path: n.path });
      S.linkDraft = draft;
      paintLinksPicked();
    };
  });
}

async function saveLinks(links) {
  if (!S.currentId) return;
  try {
    const meta = await api(`/api/recordings/${S.currentId}`, {
      method: 'PATCH', body: { links: links },
    });
    S.current.meta = meta;
    paintLinkChips();
  } catch (e) { notice(e.message, 'err'); }
}

/* Голосовые заметки к записи (20.09). Надиктованное клавишей заметки ложится
   в запись, открытую на экране, — поэтому служба должна знать, какая это
   запись: клавишу ловит она, а что открыто в окне, знает только страница. */
async function tellOpenRecording() {
  try {
    await api('/api/dictate/open-recording', {
      method: 'POST', body: { rec_id: S.currentId || '' },
    });
  } catch (e) { /* не дошло — заметка просто вставится, как обычная диктовка */ }
}

function paintNotes() {
  const box = $('notes-list');
  if (!box) return;
  const notes = (S.current && S.current.meta && S.current.meta.notes) || [];
  box.innerHTML = notes.length ? notes.map((n, i) => `
    <div class="note-row${n.kind === 'task' ? ' task' : ''}">
      <span class="note-mark">${n.kind === 'task' ? 'поручение' : 'заметка'}</span>
      <span class="note-text">${esc(n.text)}</span>
      <button type="button" class="ghost small js-note-del" data-i="${i}" title="Убрать заметку">×</button>
    </div>`).join('') : '<span class="muted tiny">заметок нет</span>';
  box.querySelectorAll('.js-note-del').forEach((b) => {
    b.onclick = async () => {
      const left = notes.slice();
      left.splice(Number(b.dataset.i), 1);
      try {
        const meta = await api(`/api/recordings/${S.currentId}`, {
          method: 'PATCH', body: { notes: left },
        });
        S.current.meta = meta;
        paintNotes();
      } catch (e) { notice(e.message, 'err'); }
    };
  });
  const hk = (S.dictate || {}).note_hotkey || '';
  const tk = (S.dictate || {}).task_hotkey || '';
  $('notes-hint').textContent = (hk || tk)
    ? `Надиктовать: ${[hk && `заметка — ${hk}`, tk && `поручение — ${tk}`].filter(Boolean).join(', ')}.`
      + ' Заметки уходят в документы как ваше слово, в первую очередь.'
    : 'Сочетания для голосовых заметок не назначены — «Настройки → Диктовка».';
}

/* Окно выбора папки проекта (20.09). Выбор одиночный — у проекта одна папка,
   поэтому список с переключателями, а не с галочками, как у «Связать с». */
let folderTimer = null;

function openProjectFolderDialog() {
  const name = ($('rec-project').value || '').trim();
  if (!name) return;
  S.folderDraft = currentProjectFolder();
  $('pf-project').textContent = `Проект: ${name}`;
  $('pf-filter').value = '';
  $('overlay').classList.remove('hidden');
  $('dlg-project-folder').classList.remove('hidden');
  searchFolders();
  $('pf-filter').focus();
}

async function searchFolders() {
  const box = $('pf-list');
  const q = ($('pf-filter').value || '').trim();
  let folders = [];
  try {
    folders = (await api(`/api/vault/folders?q=${encodeURIComponent(q)}`)).folders || [];
  } catch (e) {
    box.innerHTML = `<div class="tiny err-line">${esc(e.message)}</div>`;
    return;
  }
  // Привязанная папка показывается первой, даже если под отбор не попала:
  // иначе её не видно и непонятно, что вообще выбрано.
  const cur = S.folderDraft || '';
  const shown = cur && !folders.some((f) => f.path === cur)
    ? [{ title: cur.split('/').pop(), path: cur }].concat(folders) : folders;
  if (!shown.length) {
    box.innerHTML = '<div class="tiny muted">В сейфе не нашлось ни одной папки.</div>';
    return;
  }
  box.innerHTML = shown.map((f, i) => `
    <label class="link-row">
      <input type="radio" name="pf-pick" data-i="${i}"${f.path === cur ? ' checked' : ''}>
      <span class="link-title">${esc(f.title)}</span>
      <span class="tiny muted">${esc(f.path)}</span>
    </label>`).join('');
  box.querySelectorAll('input[type=radio]').forEach((c) => {
    c.onchange = () => { S.folderDraft = shown[Number(c.dataset.i)].path; };
  });
}

async function saveProjectFolder(folder) {
  const name = ($('rec-project').value || '').trim();
  if (!name) return;
  try {
    const res = await api('/api/projects/folder', {
      method: 'POST', body: { project: name, folder: folder || '' },
    });
    S.projectFolders = res.folders || {};
    paintProjectFolder();
    hideDialogs();
    notice(folder ? `Заметки проекта «${name}» будут в ${folder}`
      : `Связка с папкой снята: заметки проекта «${name}» — по категории`, 'ok');
  } catch (e) { notice(e.message, 'err'); }
}

/* Имя владельца вместо «Я»: в данных реплики микрофона всегда «Я». */
function displaySpeaker(name) {
  const owner = ((S.settings && S.settings.owner_name) || '').trim();
  return (name === 'Я' && owner) ? owner : (name || '');
}

/* Свёрнутость стенограммы и документа — у каждой записи своя (16.09). */
function foldOf(what) {
  return !!((S.folds[S.currentId] || {})[what]);
}

function setFold(what, folded) {
  if (!S.currentId) return;
  S.folds[S.currentId] = Object.assign({}, S.folds[S.currentId], { [what]: !!folded });
  paintFolds();
}

/* «Скрыть» (решение 17.09): одна кнопка вместо двух «Свернуть».
   В меню два случая. «Текст» — спрятать стенограмму целиком, когда кто-то
   подошёл. «Имена» — закрасить имена плашкой того же цвета, чтобы можно было
   показать или отправить снимок экрана, не раскрывая, кто говорил. */
function hideMenuClose() {
  const m = document.getElementById('hide-menu');
  if (m) m.remove();
  document.removeEventListener('mousedown', hideMenuOutside, true);
}

function hideMenuOutside(ev) {
  const box = document.getElementById('hide-menu');
  if (box && !box.contains(ev.target) && ev.target !== $('btn-hide')
      && !$('btn-hide').contains(ev.target)) hideMenuClose();
}

function openHideMenu() {
  if (document.getElementById('hide-menu')) { hideMenuClose(); return; }
  const box = document.createElement('div');
  box.id = 'hide-menu';
  box.className = 'rec-menu';
  const mark = (on) => (on ? '✓ ' : '');
  const textOn = foldOf('transcript');
  box.innerHTML = `
    <button type="button" class="js-text" title="Спрятать текст стенограммы: кнопки и вкладки остаются">${mark(textOn)}Текст</button>
    <button type="button" class="js-names" title="Закрасить имена плашкой — для снимка экрана">${mark(!!S.maskNames)}Имена</button>`;
  document.body.appendChild(box);
  const r = $('btn-hide').getBoundingClientRect();
  box.style.left = `${Math.round(Math.max(8, r.left))}px`;
  box.style.top = `${Math.round(r.top - box.offsetHeight - 4)}px`;
  box.querySelector('.js-text').onclick = () => {
    hideMenuClose();
    setFold('transcript', !foldOf('transcript'));
  };
  box.querySelector('.js-names').onclick = () => {
    hideMenuClose();
    S.maskNames = !S.maskNames;
    paintFolds();
  };
  document.addEventListener('mousedown', hideMenuOutside, true);
}

function paintFolds() {
  const t = foldOf('transcript');
  $('transcript').classList.toggle('folded', t);
  // Кнопка одна на оба случая, поэтому подпись не меняется: что именно
   // скрыто — видно по галочкам в её меню, а сама кнопка подсвечена.
  $('btn-hide').classList.toggle('on', t || !!S.maskNames);
  $('transcript').classList.toggle('masked', !!S.maskNames);
  const m = foldOf('minutes');
  $('minutes-text').classList.toggle('folded', m);
  $('btn-hide-minutes').textContent = m ? 'Показать' : 'Скрыть';
}

function paintAll() {
  // Интерфейс показываем всегда: он не должен появляться и исчезать
  // в зависимости от того, выбрана запись или нет.
  $('empty-state').classList.add('hidden');
  const video = S.mode === 'video' && !S.showRecord;
  $('rec-view').classList.toggle('hidden', video);
  $('video-view').classList.toggle('hidden', !video);
  paintSidebar(); paintJobs(); paintCategories();
  paintHead(); paintSpeakerAlerts(); paintRoomToggle(); paintTranscript(); paintDraft();
  paintMeters(); paintSources();
  paintPaths(); paintFolds();
}

/* Два режима: запись с микрофона и обработка готовых видео. Переключатель
   меняет экран и фильтрует список слева. Выбор записи в списке всегда
   показывает саму запись — у видео она такая же, как у совещания. */
function setMode(mode) {
  S.mode = mode === 'video' ? 'video' : 'rec';
  S.showRecord = false;
  document.querySelectorAll('.mode-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.mode === S.mode));
  $('side-title').textContent = S.mode === 'video' ? 'ВИДЕО' : 'ЗАПИСИ';
  $('btn-new').classList.toggle('hidden', S.mode === 'video');
  // В разделе «Видео» на этом месте — возврат к добавлению ссылки или файла.
  $('btn-video-home').classList.toggle('hidden', S.mode !== 'video');
  if (S.mode === 'video' && window.videoReloadOptions) window.videoReloadOptions();
  paintAll();
}

/* Запись это видео или живая? Нужно только для фильтра списка. */
function isVideoRec(m) {
  return m.source === 'link' || m.source === 'file';
}

/* ====================================================== данные */

async function loadState() {
  const st = await api('/api/state');
  S.recordings = st.recordings || [];
  S.settings = st.settings || {};
  applyView(S.settings);
  S.jobs = st.jobs || [];
  S.caps = st.capabilities || {};
  S.vault = st.vault || {};
  S.categories = st.categories || [];
  S.call = st.call || {};
  S.recordingId = st.active_recording || null;
  $('chk-far').checked = S.settings.record_far !== false;
  // Вид списка берём из настроек один раз, при первом чтении состояния: дальше
  // им распоряжается человек, и ответ службы не должен перебивать его выбор.
  if (S.groupByProject === null) S.groupByProject = !!S.settings.list_group_by_project;
  if (COPY_FORMATS[S.settings.copy_format]) S.copyFormat = S.settings.copy_format;
  S.asr = S.caps.asr || { state: 'loading' };
  applyFeatures();
  paintProgramState();
  if (!S.currentId && S.recordingId) await openRecording(S.recordingId);
  else paintAll();
  S.prompt = st.prompt || null;
  paintPrompt();
}

/* Тема и плотность («Настройки → Вид», пакет Claude Design 13.09). Служба
   ставит их на <html> уже при выдаче страницы — здесь только смена на ходу. */
const VIEW_THEMES = ['light', 'warm', 'neutral', 'dark', 'bright'];
const VIEW_DENSITIES = ['tiny', 'compact', 'normal', 'large'];
function applyView(s) {
  const theme = VIEW_THEMES.includes(s && s.ui_theme) ? s.ui_theme : 'light';
  const density = VIEW_DENSITIES.includes(s && s.ui_density) ? s.ui_density : 'compact';
  document.documentElement.dataset.theme = theme;
  document.documentElement.dataset.density = density;
  document.querySelectorAll('[data-theme-pick]').forEach((b) => b.classList.toggle('sel', b.dataset.themePick === theme));
  document.querySelectorAll('[data-density-pick]').forEach((b) => b.classList.toggle('sel', b.dataset.densityPick === density));
}

async function saveView(patch) {
  S.settings = Object.assign({}, S.settings, patch);
  applyView(S.settings);
  try { await api('/api/settings', { method: 'POST', body: patch }); } catch (e) { notice(e.message, 'err'); }
}

async function openRecording(id) {
  if (!id) return;
  try {
    // Блок протокола закрываем СРАЗУ, до запроса: пока он летит, на экране не
    // должен висеть протокол предыдущей записи.
    if (S.currentId !== id) { hideMinutes(); if (S.editMode) setEditMode(false); }
    S.current = await api(`/api/recordings/${id}`);
    S.currentId = id;
    // Выбранную запись показываем в любом режиме: у видео она такая же.
    // Вернуться к форме обработки — кнопкой «Видео» в панели слева.
    S.showRecord = true;
    if (S.current.session && S.current.session.drafts) S.drafts = S.current.session.drafts;
    paintAll();
    // Клавиша голосовой заметки кладёт текст в ЭТУ запись (20.09), а знает об
    // открытом на экране только страница — говорим службе сами.
    tellOpenRecording();
    await loadMinutesFor(id);
  } catch (e) {
    notice(e.message, 'err');
  }
}

/* Удаление в одном из трёх объёмов: всё, только видео и звук, только из
   истории. Первый и третий убирают запись с экрана, второй — оставляет:
   стенограмма на месте, и по ней ещё можно собрать протокол или саммари. */
/* Окно «Что удалить?» — одно на оба случая: кнопка в карточке и клавиша Delete
   (20.09). Отмечено несколько записей — вопрос задаётся один раз на всех, а
   объём работы («всё», «только видео и звук», «только из списка») тот же. */
function openDeleteDialog() {
  const ids = pickedIds();
  if (!ids.length) return;
  const metas = ids.map((id) => (S.recordings || []).find((m) => m.id === id)).filter(Boolean);
  const many = ids.length > 1;

  if (many) {
    const names = metas.slice(0, 3).map((m) => `«${m.title}»`).join(', ');
    const tail = metas.length > 3 ? ` и ещё ${metas.length - 3}` : '';
    $('del-what').textContent = `Записей: ${ids.length} — ${names}${tail}.`;
  } else {
    const m = (S.current && S.current.meta) || metas[0] || {};
    const where = [];
    if (m.video_path) where.push('видео лежит рядом с записью');
    if (m.vault_path) where.push('заметка сохранена в хранилище');
    $('del-what').textContent = `Запись «${m.title}»`
      + (where.length ? `. Сейчас: ${where.join(', ')}.` : '.');
  }

  // Если у всех отмеченных медиа уже нет, второй пункт бессмыслен.
  const mediaChoice = $('del-choices').querySelector('[data-scope="media"]');
  const gone = metas.length
    ? metas.every((m) => m.media_removed)
    : !!((S.current && S.current.meta) || {}).media_removed;
  mediaChoice.classList.toggle('off', gone);
  $('del-media-note').textContent = gone
    ? 'Видео и звук уже удалены.'
    : 'Стенограммы останутся — по ним можно сделать протокол или саммари.';
  // Третий пункт обещает ровно то, что правда: папка записи уходит вместе со
  // стенограммой, а снаружи остаётся только то, что и правда лежит снаружи.
  const stays = [];
  if (metas.some((m) => m.video_path)) stays.push('видео');
  if (metas.some((m) => m.vault_path)) stays.push('заметки в хранилище');
  $('del-history-note').textContent = stays.length
    ? `Записи и их стенограммы уходят. Остаётся: ${stays.join(', ')}.`
    : 'Записи уходят вместе со стенограммами. Снаружи ничего не сохранено.';
  showDialog('dlg-delete');
}

/* Удалить всё отмеченное (20.09). Объём работы тот же, что и для одной записи,
   и спрашивается он один раз на всех: три подряд одинаковых вопроса — это не
   осторожность, а мучение. Идём по одной и в конце говорим, что получилось. */
async function deleteMany(scope, ids) {
  hideDialogs();
  let ok = 0;
  const failed = [];
  for (const id of ids) {
    try {
      await api(`/api/recordings/${id}?scope=${encodeURIComponent(scope)}`, { method: 'DELETE' });
      ok += 1;
    } catch (e) {
      const m = (S.recordings || []).find((x) => x.id === id);
      failed.push((m && m.title) || id);
    }
  }
  S.selected = [];
  if (ids.includes(S.currentId) && scope !== 'media') { S.current = null; S.currentId = null; }
  await loadState();
  if (S.currentId) await refreshCurrent();
  paintAll();
  const what = scope === 'media' ? 'Видео и звук убраны'
    : (scope === 'history' ? 'Убраны из списка вместе со стенограммой' : 'Удалены');
  if (failed.length) {
    notice(`${what}: ${ok}. Не вышло: ${failed.join(', ')}.`, 'err');
  } else {
    notice(`${what}: ${ok} ${ok === 1 ? 'запись' : 'записей'}.`, 'ok');
  }
}

async function deleteRecording(scope) {
  // Отмечено несколько — удаляем их, а не только открытую.
  const ids = pickedIds();
  if (ids.length > 1) return deleteMany(scope, ids);
  const id = ids[0];
  if (!id) return;
  S.selected = [];
  hideDialogs();
  try {
    const res = await api(`/api/recordings/${id}?scope=${encodeURIComponent(scope)}`,
      { method: 'DELETE' });
    if (scope === 'media') {
      await openRecording(id);
      await loadState();
      // Говорим по факту: занятый плеером или антивирусом файл не удаляется,
      // и обещать освободившееся место в этом случае нельзя.
      if (res.ok === false) {
        notice(`Не удалось убрать: ${(res.left || []).join(', ')}. Файл держит `
          + 'другая программа — обычно плеер или антивирус. Закройте её и повторите.', 'err');
      } else {
        const freed = res.freed_bytes ? `, освободилось ${fmtSize(res.freed_bytes)}` : '';
        notice(`Видео и звук удалены${freed}. Стенограмма осталась.`, 'ok');
      }
      return;
    }
    S.current = null; S.currentId = null;
    await loadState();
    if (scope === 'history') {
      const kept = [];
      if (res.kept_video) kept.push('видео');
      if (res.kept_note) kept.push('заметка');
      notice(kept.length
        ? `Убрал из списка вместе со стенограммой. Осталось: ${kept.join(', ')}.`
        : 'Убрал из списка вместе со стенограммой.', 'ok');
    } else {
      notice('Удалено всё: запись, файлы и заметка.', 'ok');
    }
  } catch (e) { notice(e.message, 'err'); }
}

async function refreshCurrent() {
  if (S.currentId) {
    try { S.current = await api(`/api/recordings/${S.currentId}`); } catch (e) {}
  }
}

/* ====================================================== запись */

async function listDevices(probe) {
  try {
    // проба открывает устройства — это действие, поэтому POST
    const res = await api('/api/devices' + (probe ? '?probe=true' : ''), probe ? { method: 'POST' } : undefined);
    S.devices = res;
    const mic = $('mic-select');
    mic.innerHTML = '<option value="">по умолчанию</option>' + (res.mics || []).map((d) => {
      const marks = [];
      // Живой вход или пустышка — это важнее всех прочих пометок, поэтому первым.
      if (d.probe) {
        if (!d.probe.ok) marks.push('НЕ ОТКРЫВАЕТСЯ');
        else if (d.silent) marks.push('ПУСТЫШКА: ровная тишина');
        else if (!d.alive) marks.push('тихо — скажите что-нибудь');
        else marks.push('есть звук');
      }
      if (d.is_communications) marks.push('устройство связи');
      else if (d.is_default) marks.push('по умолчанию');
      return `<option value="${esc(d.name)}">${esc(d.name)}${marks.length ? ' — ' + marks.join(', ') : ''}</option>`;
    }).join('');
    mic.value = res.mic_selected == null ? '' : String(res.mic_selected);

    const far = $('far-select');
    far.innerHTML = '<option value="">рекомендованное</option>' + (res.loopback || []).map((d) => {
      const marks = [];
      if (d.is_communications) marks.push('устройство связи');
      else if (d.is_default_output) marks.push('вывод по умолчанию');
      return `<option value="${d.index}">${esc(d.name)}${marks.length ? ' — ' + marks.join(', ') : ''}</option>`;
    }).join('');
    far.value = res.far_selected == null ? '' : String(res.far_selected);
    S.loopback = res.loopback || [];
    paintEchoWarning();

    if (res.mic_probe) {
      // «Ровный ноль» и «тихо» — разные беды, и путать их дорого: при нуле
      // устройство не микрофон вовсе, сколько в него ни говори.
      const p = res.mic_probe;
      if (!p.ok) {
        $('mic-hint').textContent = 'не открылся';
        $('mic-hint').title = p.reason || '';
      } else if (!(p.peak > 0)) {
        $('mic-hint').textContent = 'пустышка';
        $('mic-hint').title = 'Устройство открылось, но отдаёт ровную тишину — '
          + 'это не микрофон (так ведут себя «микрофоны» колонок и виртуальные '
          + 'устройства Steam). Выберите другой вход из списка.';
      } else if (p.peak < 0.002) {
        $('mic-hint').textContent = 'тихо';
        $('mic-hint').title = 'Микрофон живой, но сигнал слабый. Скажите что-нибудь и проверьте ещё раз.';
      } else {
        $('mic-hint').textContent = '';
        $('mic-hint').title = '';
      }
    }
    if (res.far_probe) {
      $('far-hint').textContent = res.far_probe.ok ? '' : 'не открылась';
      $('far-hint').title = res.far_probe.ok ? '' : (res.far_probe.reason || '');
    }
  } catch (e) {
    notice('Не удалось получить список устройств: ' + e.message, 'err');
  }
}

async function startRecording() {
  if (S.recordingId || S.starting) { return; }
  S.starting = true;
  paintHead();
  notice('Включаю устройства…');

  // Если открыта живая заметка — дописываем в неё, а не заводим новую.
  // Так «Старт» и «Стоп» можно нажимать сколько нужно: стенограмма растёт.
  const cur = S.current && S.current.meta;
  const resumeId = (cur && cur.source === 'live' && cur.id) ? cur.id : undefined;

  let payload;
  try {
    payload = await api('/api/recordings', {
      method: 'POST',
      body: {
        rec_id: resumeId,
        mode: $('rec-mode').value,
        category: $('cat-select').value || undefined,
      },
    });
  } catch (e) {
    S.starting = false;
    notice(e.message, 'err');
    paintAll();
    return;
  }

  S.current = payload;
  S.currentId = payload.meta.id;
  S.recordingId = payload.meta.id;
  S.captureStatus = payload.capture || {};
  // Устройства открываются в фоне: итог придёт событием capture.
  // Флаг снимаем сразу, чтобы «Стоп» был доступен и запись можно было прервать.
  S.starting = false;
  paintAll();
  startTicker();
}

async function stopRecording() {
  const id = S.recordingId;
  if (!id) return;
  $('btn-stop').disabled = true;
  notice('Останавливаю, дописываю последнюю фразу…');
  S.recordingId = null;
  try {
    const res = await api(`/api/recordings/${id}/stop`, { method: 'POST' });
    if (S.currentId === id) {
      S.current = { meta: res.meta, segments: res.segments, session: res.session };
    }
  } catch (e) { notice(e.message, 'err'); }
  stopTicker();
  await loadState();
  if (S.currentId !== id) await openRecording(id);
  paintAll();
  const hasText = !!(S.current && (S.current.segments || []).length);
  if (hasText) {
    try { await api(`/api/recordings/${id}/save`, { method: 'POST' }); } catch (e) {}
    await refreshCurrent(); paintAll();
  } else {
    notice('Записывать было нечего — заметку не сохраняю');
  }
}

let ticker = null;
function startTicker() {
  stopTicker();
  ticker = setInterval(() => {
    if (!S.recordingId) return;
    const m = S.current && S.current.meta;
    if (m && m.id === S.recordingId) {
      m.duration_s = (m.duration_s || 0) + 1;
      $('rec-duration').textContent = fmtDur(m.duration_s);
      const row = document.querySelector(`.rec-item[data-id="${m.id}"] .rec-item-sub span:nth-child(2)`);
      if (row) row.textContent = fmtClock(m.duration_s);
    }
  }, 1000);
}
function stopTicker() { if (ticker) { clearInterval(ticker); ticker = null; } }

/* ====================================================== говорящие */

/* Окно «Кто это говорит?» (решения 13.09).
   Поле имени с подсказками из базы голосов: по вхождению букв в любое слово
   имени, у каждого — насколько похож голос этого говорящего. Сохранение идёт
   через проверку: если служба видит конфликт (похожее имя, чужой голос, тот
   же человек у другого говорящего), она отвечает вопросом с кнопками, и
   выбранная кнопка уходит обратно вместе с остальными ответами. */

function nameKey(s) {
  return String(s || '').toLowerCase().replace(/ё/g, 'е').replace(/[([{][^)\]}]*[)\]}]/g, ' ')
    .replace(/[^\p{L}\p{N}-]+/gu, ' ').trim().split(/\s+/).filter(Boolean).sort().join(' ');
}

function openSpeakerDialog(key, currentName) {
  if (!key) return;
  if (key === 'me') {
    const split = ((S.current && S.current.meta && S.current.meta.splits) || {}).me;
    notice(split && split.owner_guess
      ? 'Это вы. Чтобы программа запомнила ваш голос — «Да, это мой голос — запомнить» над стенограммой.'
      : split
        ? 'Это вы. Если это не ваш голос — подпишите «Это я» у другого голоса.'
        : 'Это ваша дорожка. Если рядом с вами говорили другие люди — включите «со мной в комнате были ещё люди».');
    return;
  }
  const meta = S.current && S.current.meta;
  S.speakerCtx = { key: key, decision: {}, picked: null, items: [], active: -1, seq: 0, role: null };
  $('speaker-current').textContent = `Сейчас подписано: ${currentName || key}`;
  $('speaker-name').value = '';
  $('speaker-remember').checked = true;
  $('speaker-picked').textContent = '';
  $('speaker-notes').textContent = '';
  hideConflict();
  const split = key.includes('~');
  const hasAudio = !!(meta && !meta.media_removed);
  $('btn-speaker-split').classList.toggle('hidden', split || !hasAudio);
  $('btn-speaker-unsplit').classList.toggle('hidden', !split);
  showDialog('dlg-speaker');
  speakerSearch();
  setTimeout(() => $('speaker-name').focus(), 50);
}

function speakerHint(p) {
  const parts = [];
  if (p.voice != null) parts.push(`голос похож на ${Math.round(p.voice * 100)} %`);
  else if (p.has_voice) parts.push(`образцов голоса: ${p.count}`);
  else parts.push('голоса в базе нет');
  if (p.matched) parts.push(`также «${p.matched}»`);
  if (p.kind === 'shared') parts.push('общее устройство');
  return parts.join(' · ');
}

async function speakerSearch() {
  const ctx = S.speakerCtx;
  if (!ctx) return;
  const q = $('speaker-name').value.trim();
  const seq = ++ctx.seq;
  let res;
  try {
    res = await api(`/api/recordings/${S.currentId}/speaker/search?key=${encodeURIComponent(ctx.key)}&q=${encodeURIComponent(q)}`);
  } catch (e) { return; }
  if (S.speakerCtx !== ctx || seq !== ctx.seq) return;
  const items = [];
  if (res.is_mic) items.push({ kind: 'me', label: `Это я (${res.owner})`, hint: 'реплики станут вашими' });
  (res.people || []).forEach((p) => items.push({ kind: 'person', id: p.id, label: p.name, hint: speakerHint(p), voice: p.voice }));
  (res.attendees || []).forEach((n) => items.push({ kind: 'attendee', label: n, hint: 'приглашён на встречу, в базе пока нет' }));
  if (q && !(res.people || []).some((p) => nameKey(p.name) === nameKey(q) || (p.aliases || []).some((a) => nameKey(a) === nameKey(q)))) {
    items.push({ kind: 'new', label: `＋ Новый человек «${q}»`, hint: 'завести в базе голосов' });
  }
  ctx.items = items;
  ctx.active = -1;
  ctx.hasVoice = !!res.has_voice;
  if (!res.has_voice) $('speaker-notes').textContent = 'Голос этого говорящего ещё не размечен — запомнится только имя.';
  // Роль показываем один раз, при открытии карточки: дальше человек правит
  // поля, и перерисовка на каждую букву в имени стирала бы правку.
  if (!ctx.role) paintSpeakerRole(res.role || {});
  paintSpeakerAc();
}

function paintSpeakerAc() {
  const ctx = S.speakerCtx;
  const box = $('speaker-ac');
  const open = !!(ctx && ctx.items.length && !ctx.acClosed);
  // пока список открыт, край окна его не обрезает (замечено 13.09)
  $('dlg-speaker').classList.toggle('ac-open', open);
  if (!open) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  box.innerHTML = ctx.items.map((it, i) =>
    `<span class="ac-item${i === ctx.active ? ' active' : ''}${it.kind === 'new' ? ' ac-new' : ''}" data-i="${i}">
      <b>${esc(it.label)}</b><span class="tiny muted">${esc(it.hint || '')}</span></span>`).join('');
  box.classList.remove('hidden');
  box.querySelectorAll('.ac-item').forEach((el) => {
    el.onmousedown = (e) => { e.preventDefault(); pickSpeakerItem(ctx.items[Number(el.dataset.i)]); };
  });
}

function pickSpeakerItem(it) {
  const ctx = S.speakerCtx;
  if (!ctx || !it) return;
  ctx.decision = {};
  hideConflict();
  if (it.kind === 'person') {
    ctx.picked = { person_id: it.id, name: it.label };
    $('speaker-name').value = it.label;
    $('speaker-picked').textContent = `Из базы голосов: ${it.label}${it.hint ? ' — ' + it.hint : ''}`;
  } else if (it.kind === 'me') {
    ctx.picked = { me: true };
    $('speaker-name').value = S.settings.owner_name || 'Я';
    $('speaker-picked').textContent = 'Реплики этого голоса станут вашими («Я»).';
  } else if (it.kind === 'new') {
    ctx.picked = { new: true };
    $('speaker-picked').textContent = `Новый человек: «${$('speaker-name').value.trim()}».`;
  } else {
    ctx.picked = null;
    $('speaker-name').value = it.label;
    $('speaker-picked').textContent = '';
  }
  ctx.acClosed = true;
  paintSpeakerAc();
}

function hideConflict() {
  $('speaker-conflict').classList.add('hidden');
  $('speaker-conflict').querySelector('.c-choices').innerHTML = '';
}

const CHOICE_DECISION = {
  new: { new_person: true }, merge: { merge: true }, namesake: { namesake: true },
  keep_both: { keep_both: true }, add: { voice: 'add' }, keep: { voice: 'keep' }, no: { voice: 'no' },
};
function choiceDecision(id) {
  if (id.startsWith('alias:')) return { alias_of: id.slice(6) };
  if (id.startsWith('use:')) return { voice: id };
  return CHOICE_DECISION[id] || {};
}

function showConflict(res) {
  const c = res.conflict || {};
  const box = $('speaker-conflict');
  box.querySelector('.c-text').textContent = c.text || 'Нужно решение';
  box.querySelector('.c-choices').innerHTML = (c.choices || []).map((ch) =>
    `<button class="${ch.primary ? 'primary' : 'ghost'} small" type="button" data-id="${esc(ch.id)}">${esc(ch.label)}</button>`).join('');
  box.querySelectorAll('button').forEach((b) => {
    b.onclick = () => saveSpeakerDialog(choiceDecision(b.dataset.id));
  });
  box.classList.remove('hidden');
  $('speaker-notes').textContent = (res.notes || []).join(' ');
}

async function postSpeaker(body) {
  const r = await fetch(`/api/recordings/${S.currentId}/speaker`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  return { status: r.status, data: data };
}

/* Роль участника (20.09): сторона и должность прямо в карточке говорящего.
   Постоянная роль живёт у человека в базе голосов, разовая — в записи. */
function paintSpeakerRole(role) {
  const box = $('speaker-role');
  if (!box) return;
  const ctx = S.speakerCtx;
  if (ctx) ctx.role = role;
  // Человека в базе ещё нет — роль привязывать не к кому. Разовую роль в
  // записи поставить всё равно можно: она держится за именем, а не за базой.
  const known = !!(role && role.person_id);
  const once = !!(role && (role.rec_side || role.rec_position)) || !known;
  $('speaker-side').innerHTML = ['<option value="">не указана</option>'].concat(
    ((role && role.sides) || []).map((s) => `<option value="${esc(s.key)}">${esc(s.title)}</option>`)).join('');
  $('speaker-role-once').checked = once;
  $('speaker-role-once').disabled = !known;
  $('speaker-side').value = once ? ((role && role.rec_side) || (known ? '' : (role && role.side) || ''))
    : ((role && role.side) || '');
  $('speaker-position').value = once ? ((role && role.rec_position) || (known ? '' : (role && role.position) || ''))
    : ((role && role.position) || '');
  paintSpeakerRoleNote();
}

function paintSpeakerRoleNote() {
  const ctx = S.speakerCtx;
  const role = (ctx && ctx.role) || {};
  const once = $('speaker-role-once').checked;
  const perm = role.side || role.position
    ? `постоянная роль: ${[sideTitle(role.side), role.position].filter(Boolean).join(', ')}`
    : 'постоянной роли нет';
  $('speaker-role-note').textContent = !role.person_id
    ? 'Человека ещё нет в базе голосов — роль запомнится только для этой записи.'
    : (once ? `Только для этой записи. У человека ${perm}.`
      : 'Роль запомнится у человека и будет действовать во всех записях.');
}

function sideTitle(key) {
  const ctx = S.speakerCtx;
  const list = ((ctx && ctx.role && ctx.role.sides) || []);
  const found = list.find((s) => s.key === key);
  return found ? found.title : '';
}

/* Сохранить роль: постоянную — человеку в базу, разовую — в запись.
   Зовётся из «Сохранить» карточки говорящего, вместе с именем. */
async function saveSpeakerRole() {
  const ctx = S.speakerCtx;
  const role = (ctx && ctx.role) || {};
  const side = $('speaker-side').value || '';
  const position = ($('speaker-position').value || '').trim();
  const once = $('speaker-role-once').checked || !role.person_id;
  const name = role.name || ($('speaker-name').value || '').trim();
  const was = once ? [role.rec_side || '', role.rec_position || '']
    : [role.side || '', role.position || ''];
  if (was[0] === side && was[1] === position) return;   // ничего не меняли
  try {
    if (once) {
      const roles = Object.assign({}, (S.current && S.current.meta && S.current.meta.roles) || {});
      if (side || position) roles[name] = { side: side, position: position };
      else delete roles[name];
      const meta = await api(`/api/recordings/${S.currentId}`, {
        method: 'PATCH', body: { roles: roles },
      });
      if (S.current) S.current.meta = meta;
    } else {
      await api(`/api/voices/${role.person_id}/role`, {
        method: 'POST', body: { side: side, position: position },
      });
    }
  } catch (e) { notice(e.message, 'err'); }
}

function applySpeakerResult(data) {
  if (data.meta && S.current) { S.current.meta = data.meta; S.current.segments = data.segments; }
  paintAll();
  const p = data.person || {};
  notice(`Говорящий подписан: ${p.owner ? 'Я' : (p.name || '')}${data.voice_saved ? ' (голос запомнен)' : ''}`, 'ok');
  if ((data.removed_from || []).length) notice(`Голос убран у: ${data.removed_from.join(', ')}`);
}

async function saveSpeakerDialog(extra) {
  const ctx = S.speakerCtx;
  if (!ctx) return;
  const name = $('speaker-name').value.trim();
  const decision = Object.assign({}, ctx.decision || {}, extra || {});
  const p = ctx.picked;
  if (p && p.me) decision.me = true;
  if (p && p.person_id && !decision.alias_of && !String(decision.voice || '').startsWith('use:')) decision.person_id = p.person_id;
  if (p && p.new && !decision.namesake) decision.new_person = true;
  if (!name && !decision.me) { notice('Введите имя', 'err'); return; }
  let res;
  try {
    res = await postSpeaker({ speaker_key: ctx.key, name: name, decision: decision, remember: $('speaker-remember').checked });
  } catch (e) { notice(e.message, 'err'); return; }
  if (S.speakerCtx !== ctx) return;
  if (res.status === 409 && res.data.conflict) {
    ctx.decision = decision;
    showConflict(res.data);
    return;
  }
  if (res.status !== 200) { notice(res.data.detail || `Ошибка ${res.status}`, 'err'); return; }
  // Роль сохраняется вместе с именем: человек правит карточку целиком, и
  // отдельной кнопки «сохранить роль» в ней быть не должно.
  await saveSpeakerRole();
  hideDialogs();
  S.speakerCtx = null;
  applySpeakerResult(res.data);
  loadVoices();
}

/* «Похоже на Ивана Петрова — это он?» → «Да»: тот же путь проверки. Нужен
   выбор — открываем окно с вопросом. */
async function confirmSuggestion(key, sug) {
  let res;
  try {
    res = await postSpeaker({ speaker_key: key, name: sug.name, decision: { person_id: sug.person_id }, remember: true });
  } catch (e) { notice(e.message, 'err'); return; }
  if (res.status === 409 && res.data.conflict) {
    openSpeakerDialog(key, sug.name);
    const ctx = S.speakerCtx;
    ctx.picked = { person_id: sug.person_id, name: sug.name };
    ctx.decision = { person_id: sug.person_id };
    $('speaker-name').value = sug.name;
    ctx.acClosed = true;
    paintSpeakerAc();
    showConflict(res.data);
    return;
  }
  if (res.status !== 200) { notice(res.data.detail || `Ошибка ${res.status}`, 'err'); return; }
  applySpeakerResult(res.data);
  loadVoices();
}

async function splitSpeaker(key) {
  try {
    await api(`/api/recordings/${S.currentId}/speaker/split`, { method: 'POST', body: { speaker_key: key } });
    notice('Разделяю голоса: распознаю реплики заново и размечаю — это займёт время', 'ok');
  } catch (e) { notice(e.message, 'err'); }
}

async function unsplitSpeaker(key) {
  try {
    const res = await api(`/api/recordings/${S.currentId}/speaker/unsplit`, { method: 'POST', body: { speaker_key: key } });
    if (S.current) { S.current.meta = res.meta; S.current.segments = res.segments; }
    paintAll();
    notice('Разделение отменено', 'ok');
  } catch (e) { notice(e.message, 'err'); }
}

/* Под одним именем несколько голосов и итоги разделения — над стенограммой. */
function paintSpeakerAlerts() {
  const box = $('speaker-alerts');
  const m = S.current && S.current.meta;
  if (!m) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  const rows = [];
  Object.entries(m.speakers || {}).forEach(([key, info]) => {
    if (info && info.multi_voice) {
      rows.push(`<div class="sa-row" data-key="${esc(key)}"><svg aria-hidden="true"><use href="/static/icons.svg#ic-people"></use></svg> Под именем «${esc(info.name || key)}» говорили
        ${info.multi_voice.voices} разных голоса — похоже на переговорку или общий компьютер. Голос этой учётки не запоминаю.
        <button class="primary small js-split" type="button">Разделить голоса</button>
        <button class="ghost small js-one" type="button">Это один человек</button></div>`);
    }
  });
  const present = new Set(((S.current && S.current.segments) || []).map((s) => s.speaker_key));
  Object.entries(m.splits || {}).forEach(([key, info]) => {
    if (!info || !(info.keys || []).some((k) => present.has(k))) return;
    const who = key === 'me' ? 'Ваша дорожка' : `«${info.name || key}»`;
    // «Я» угадано по правилу «самый частый голос» — голос можно запомнить кнопкой.
    // Разделения до 15.09 отпечаток не сохраняли: для них — как получить кнопку.
    const guess = key === 'me' && info.owner_guess === true;
    const addMore = key === 'me' && !guess && info.owner_add === true;
    const oldGuess = key === 'me' && info.owner_guess === undefined && /самый частый/.test(info.owner_note || '');
    const ownerBtn = guess
      ? '<button class="primary small js-owner-yes" type="button" data-done="Ваш голос запомнен — дальше «Я» будет ставиться по голосу" title="Запомнить ваш голос: дальше «Я» будет ставиться по нему">Да, это мой голос — запомнить</button>'
      : addMore
        ? '<button class="ghost small js-owner-yes" type="button" data-done="Образец добавлен — ваш голос будет узнаваться увереннее" title="Голос узнан не очень уверенно. Ещё один образец — из другой записи, другого микрофона — поможет узнавать увереннее">Добавить и этот образец</button>'
        : (oldGuess ? ' Чтобы запомнить ваш голос, снимите и снова поставьте «со мной в комнате были ещё люди».' : '');
    const note = info.owner_note || '';
    const known = key === 'me' && /узнан по образцу|запомнен/.test(note);
    const noteHtml = !note ? '' : (known ? ` <span class="sa-ok">✓ ${esc(note)}</span>` : ' ' + esc(note) + '.');
    rows.push(`<div class="sa-row sa-done" data-key="${esc(key)}">${who} разделена на ${info.voices} голоса — щёлкните по имени
      в стенограмме, чтобы подписать.${noteHtml}${ownerBtn}
      <button class="ghost small js-unsplit" type="button">Отменить разделение</button></div>`);
  });
  // Узнаны по голосу, но не очень уверенно (ниже 80 %) — предложить добавить
  // образец. Сам образец — только по нажатию (решение 15.09).
  const offers = Object.entries(m.speakers || {})
    .filter(([key, info]) => info && info.sample_offer && info.name && present.has(key));
  if (offers.length) {
    const items = offers.map(([key, info]) => `<span class="sa-offer" data-key="${esc(key)}">«${esc(info.name)}»
      ${info.score != null ? Math.round(info.score * 100) + ' %' : ''}
      <button class="ghost small js-add-sample" type="button" title="Запомнить ещё один образец голоса этого человека из этой записи">Добавить образец</button></span>`);
    rows.push(`<div class="sa-row sa-done">Узнаны по голосу не очень уверенно — образец из этой записи поможет
      узнавать увереннее: ${items.join(' ')}</div>`);
  }
  box.innerHTML = rows.join('');
  box.querySelectorAll('.js-add-sample').forEach((b) => {
    b.onclick = async () => {
      const key = b.closest('.sa-offer').dataset.key;
      const name = ((m.speakers || {})[key] || {}).name || key;
      b.disabled = true;
      try {
        const res = await api(`/api/recordings/${S.currentId}/speaker/add_sample`, { method: 'POST', body: { speaker_key: key } });
        if (S.current && res.meta) S.current.meta = res.meta;
        notice(`Образец голоса «${name}» добавлен`, 'ok');
        paintAll();
      } catch (e) { b.disabled = false; notice(e.message, 'err'); }
    };
  });
  box.classList.toggle('hidden', !rows.length);
  box.querySelectorAll('.js-split').forEach((b) => { b.onclick = () => splitSpeaker(b.closest('.sa-row').dataset.key); });
  box.querySelectorAll('.js-owner-yes').forEach((b) => {
    b.onclick = async () => {
      b.disabled = true;
      try {
        const res = await api(`/api/recordings/${S.currentId}/owner_voice`, { method: 'POST' });
        if (S.current && res.meta) S.current.meta = res.meta;
        notice(b.dataset.done || 'Ваш голос запомнен', 'ok');
        paintAll();
      } catch (e) { b.disabled = false; notice(e.message, 'err'); }
    };
  });
  box.querySelectorAll('.js-unsplit').forEach((b) => {
    b.onclick = () => {
      const key = b.closest('.sa-row').dataset.key;
      if (key === 'me') setRoom(false); else unsplitSpeaker(key);
    };
  });
  box.querySelectorAll('.js-one').forEach((b) => {
    b.onclick = async () => {
      try {
        const res = await api(`/api/recordings/${S.currentId}/speaker/one-person`, { method: 'POST', body: { speaker_key: b.closest('.sa-row').dataset.key } });
        if (S.current) S.current.meta = res.meta;
        paintAll();
      } catch (e) { notice(e.message, 'err'); }
    };
  });
}

function paintRoomToggle() {
  const m = S.current && S.current.meta;
  const segs = (S.current && S.current.segments) || [];
  const hasMic = segs.some((s) => s.track === 'mic');
  const show = !!(m && m.source === 'live' && hasMic && !m.media_removed);
  $('room-field').classList.toggle('hidden', !show);
  $('absent-field').classList.toggle('hidden', !show);
  if (!show) return;
  $('chk-room').checked = !!(m.room_shared || (m.splits && m.splits.me && (m.splits.me.keys || []).length));
  $('chk-room').disabled = !!(S.recordingId && S.recordingId === m.id);
  $('chk-absent').checked = !!m.owner_absent;
  $('chk-absent').disabled = $('chk-room').disabled;
}

async function setOwnerAbsent(on) {
  // Включение — долгая работа (разметка голосов дорожки), выключение — мгновенное.
  try {
    const res = await api(`/api/recordings/${S.currentId}/owner_absent`, { method: 'POST', body: { on: !!on } });
    if (on) notice('Разделяю голоса микрофона — «Я» не поставлю никому', 'ok');
    if (res.meta && S.current) { S.current.meta = res.meta; await refreshCurrent(); }
    paintAll();
  } catch (e) {
    notice(e.message, 'err');
    paintRoomToggle();
  }
}

async function setRoom(on) {
  // Включение — это долгая работа точной моделью, поэтому сначала окно с
  // вопросом; пока в нём не нажали, ничего не запускается. Выключение
  // (отменить разделение) спрашивать незачем.
  if (on) { openVoices('room'); return; }
  try {
    const res = await api(`/api/recordings/${S.currentId}/room`, { method: 'POST', body: { on: false } });
    if (res.meta && S.current) { S.current.meta = res.meta; await refreshCurrent(); }
    paintAll();
  } catch (e) {
    notice(e.message, 'err');
    paintRoomToggle();
  }
}

/* ====================================================== окна */

function showDialog(id) {
  $('overlay').classList.remove('hidden');
  $(id).classList.remove('hidden');
}
function hideDialogs() {
  $('overlay').classList.add('hidden');
  document.querySelectorAll('.dialog').forEach((d) => d.classList.add('hidden'));
  // Окно могли закрыть крестиком или Esc, ничего не выбрав: галочка «со мной
  // в комнате были ещё люди» не должна оставаться нажатой просто так.
  try { paintRoomToggle(); } catch (e) { /* окно ещё не собрано */ }
}

/* Окно «сколько голосов» — одно на два случая, обе работы долгие и обе идут
   точной моделью, поэтому ни одна не начинается без нажатия:
     repass — «Перечитать точнее»: голоса собеседников;
     room   — «Со мной в комнате были ещё люди»: голоса в моей дорожке.
   Считаются ГОЛОСА, а не участники звонка: трое в переговорке на том конце —
   это три голоса. Число, названное в прошлый раз (или найденное разметкой),
   подсвечено. */
function openVoices(mode) {
  const m = (S.current && S.current.meta) || {};
  const live = (m.source || 'live') === 'live';
  S.voicesMode = mode === 'room' ? 'room' : 'repass';
  const room = S.voicesMode === 'room';
  $('repass-title').textContent = room
    ? 'Со мной в комнате были ещё люди' : 'Перечитать точнее';
  const micSplit = !!(m.room_shared || m.owner_absent || (m.splits && m.splits.me));
  $('repass-lead').textContent = room
    ? 'Ваши реплики будут распознаны заново точной моделью и разделены по голосам.'
    : 'Текст распознается заново точной моделью, и реплики разойдутся по говорящим.'
      + (micSplit ? ' Голоса вашего микрофона после этого разделятся заново сами.' : '');
  $('repass-hint').textContent = room
    ? 'Сколько человек говорило рядом с вами, считая вас?'
    : (live
      ? 'Сколько разных голосов у собеседников в звонке? Считайте людей, а не участников звонка: '
        + 'трое в переговорке на том конце — это три голоса. Свой голос не считайте. '
        + 'Люди рядом с вами у микрофона — это галочка «со мной в комнате были ещё люди», не это число.'
      : 'Сколько разных голосов в записи?');
  const hint = m.voices_hint || {};
  const found = m.voices || {};
  const prev = Number(room ? (hint.mine || found.mine) : (hint.others || found.others)) || 0;
  document.querySelectorAll('#repass-numbers button').forEach((b) => {
    b.classList.toggle('sel', Number(b.textContent) === prev);
    b.title = Number(b.textContent) === prev ? 'Столько было в прошлый раз' : '';
  });
  $('repass-count').classList.add('hidden');
  $('repass-ask').classList.remove('hidden');
  warnAboutEdits(room);
  showDialog('dlg-repass');
}

/* Состояние программы в нижней строке боковой панели. Одна функция рисует его
   из S.asr, и зовут её и загрузка состояния, и событие от службы: окно,
   открытое ПОСЛЕ прогрева модели, раньше навсегда оставалось с надписью
   «модель загружается…». */
/* Вторая строка состояния программы: чем делаются транскрипты и документы.
   Раньше здесь стояло «Всё распознаётся на этом компьютере» — это правда про
   распознавание, но документы могут собираться и в облаке по ключу, а строка
   об этом молчала (замечено 17.09). */
function workLine() {
  const engines = (S.caps && S.caps.engines) || [];
  const chosen = (S.settings && S.settings.minutes_engine) || 'claude_cli';
  const eng = engines.find((e) => e.key === chosen);
  let docs;
  if (chosen === 'claude_cli') docs = 'протоколы — CLI';
  else if (eng && eng.provider_title) docs = `протоколы — по API (${eng.provider_title})`;
  else docs = 'протоколы — по API';
  if (eng && eng.ready === false) docs += ': не настроено';
  return `транскрипты локально, ${docs}`;
}

function paintProgramState() {
  const box = $('asr-state');
  if (!box) return;
  const work = $('work-state');
  if (work) work.textContent = workLine();
  paintAsrModels();
  const st = S.asr || {};
  const kind = st.state || 'loading';
  box.classList.toggle('bad', kind === 'error');
  if (kind === 'ready') {
    // Выбраны одни модели, а работают другие (нет файлов или модель отказала):
    // запись идёт, но человек должен это видеть, а не искать в журнале.
    box.textContent = st.note ? 'модель распознавания готова ⚠' : 'модель распознавания готова';
    box.title = st.note || [st.models || '',
      st.seconds ? `Загрузилась за ${st.seconds} с` : ''].filter(Boolean).join('. ');
    return;
  }
  if (kind === 'error') {
    box.title = st.error || '';
    box.innerHTML = 'модель не загрузилась — '
      + '<button type="button" class="linky js-asr-retry">повторить</button>';
    const b = box.querySelector('.js-asr-retry');
    if (b) b.onclick = retryAsr;
    return;
  }
  if (kind === 'offline') {
    box.textContent = 'служба не ответила';
    box.title = st.error || 'Программа не достучалась до своей службы';
    return;
  }
  box.textContent = 'модель загружается…';
  box.title = 'Первая фраза записи подождёт загрузку';
}

async function retryAsr() {
  S.asr = { state: 'loading' };
  paintProgramState();
  try {
    S.asr = await api('/api/asr/warmup', { method: 'POST' });
  } catch (err) {
    S.asr = { state: 'error', error: String(err.message || err) };
  }
  paintProgramState();
}

/* Выключатель системного микрофона — то же, что клавиша на ноутбуке, но видно
   состояние и можно нажать мышью (решение 17.09). Красный кружок —
   микрофон включён, серый — выключен. Гасится микрофон Windows целиком:
   значит, и в записи будет тишина. */
function paintMicButton() {
  const b = $('btn-micmute');
  if (!b) return;
  const m = S.mic || {};
  b.classList.toggle('hidden', m.available === false);
  const off = !!m.muted;
  b.classList.toggle('off', off);
  b.classList.toggle('on', !off);
  b.setAttribute('aria-pressed', off ? 'true' : 'false');
  // Подсказка говорит, слышит ли программа (п. 10.3): голос персонажа звучит
  // только в приветствии и в подобных подписях, рабочие тексты — безличные.
  b.title = (off
    ? 'Hagen не слышит (микрофон выключен) — вас не слышат и собеседники. Нажмите, чтобы включить.'
    : 'Hagen слушает. Нажмите, чтобы выключить микрофон — вас не услышат ни собеседники, ни запись.')
    + (m.device ? `\nУстройство: ${m.device}` : '');
}

async function loadMic() {
  try {
    S.mic = await api('/api/mic');
  } catch (err) {
    S.mic = { available: false };
  }
  paintMicButton();
}

async function toggleMic() {
  const b = $('btn-micmute');
  if (b) b.disabled = true;
  try {
    S.mic = await api('/api/mic', { method: 'POST', body: { toggle: true } });
    if (S.mic.available === false) notice('Микрофоном управлять не получилось', 'err');
  } catch (err) {
    notice(String(err.message || err), 'err');
  } finally {
    if (b) b.disabled = false;
    paintMicButton();
  }
}

/* Слушать совещание через динамики — значит получить двойники: микрофон
   услышит собеседников из колонок (случай 17.09, Realtek вместо
   Jabra). Windows знает форму устройства, поэтому предупреждаем заранее. */
/* Предупреждение «в запись попадёт весь звук компьютера» — не постоянная
   строка на экране, а ответ на действие: при проверке
   устройств и один раз при первом включении «писать собеседников». */
function showFarWarning() {
  $('far-warning').classList.remove('hidden');
}

function paintEchoWarning() {
  const box = $('echo-warning');
  if (!box) return;
  const chosen = $('far-select').value;
  const list = S.loopback || [];
  const dev = chosen === ''
    ? list.find((d) => d.is_communications) || list.find((d) => d.is_default_output)
    : list.find((d) => String(d.index) === String(chosen));
  const risky = !!(dev && dev.echo_risk) && $('chk-far').checked;
  box.classList.toggle('hidden', !risky);
  if (risky) {
    box.title = `${dev.name}: ${dev.echo_why}`;
  }
}

/* Переразбор собирает стенограмму заново, и ручные правки текста пропадают.
   Раньше об этом никто не предупреждал (замечено 17.09: разметил,
   нажал «перечитать», всё сбросилось). Решения «чьи это слова» переживают
   переразбор — они привязаны ко времени, а звук тот же. */
async function warnAboutEdits(room) {
  const box = $('repass-edits');
  if (!box) return;
  box.classList.add('hidden');
  box.textContent = '';
  if (!S.currentId) return;
  let c = {};
  try {
    c = await api(`/api/recordings/${S.currentId}/transcript/edits`);
  } catch (err) {
    return;                                   // не смогли спросить — молчим
  }
  if (!c.edits) return;
  const said = [`В записи ${plural(c.edits, 'ручная правка', 'ручные правки', 'ручных правок')}.`];
  said.push(c.texts
    ? `Правки текста (${c.texts}) пропадут: текст распознается заново.`
    : 'Правки текста пропадут: текст распознается заново.');
  if (c.speakers && !room) said.push(`Ваши решения о говорящих (${c.speakers}) сохранятся.`);
  box.textContent = said.join(' ');
  box.classList.remove('hidden');
}

async function runVoices(speakers) {
  const id = S.currentId;
  if (!id) return;
  const room = S.voicesMode === 'room';
  hideDialogs();
  try {
    if (room) {
      const body = speakers ? { on: true, speakers: speakers } : { on: true };
      await api(`/api/recordings/${id}/room`, { method: 'POST', body: body });
      notice('Размечаю вашу дорожку: ваш голос останется «Я», остальные — «Рядом со мной»', 'ok');
      await refreshCurrent();
      paintAll();
      return;
    }
    await api(`/api/recordings/${id}/retranscribe`,
      { method: 'POST', body: speakers ? { speakers: speakers } : {} });
    notice(speakers
      ? `Перечитываю точной моделью и размечаю на ${speakers} голосов — это идёт в фоне`
      : 'Перечитываю точной моделью — это идёт в фоне', 'ok');
  } catch (e) {
    notice(e.message, 'err');
    paintRoomToggle();
  }
}

/* Возможности. Отключаемые
   возможности, а не два режима программы: режим — это два продукта, и человек,
   которому нужна одна «продвинутая» вещь, получал бы сразу все.

   Выключенная возможность исчезает с экрана целиком — не серая, её нет.
   Данные при этом не трогаются: выключили и включили — всё на месте. Один
   реестр на всю программу: в разметке `data-feature`, здесь одна функция,
   на сервере — config.feature(). Условий по коду не заводим. */
const FEATURES = [
  { key: 'obsidian_links', name: 'Связи в заметках',
    note: 'Проект, теги, продолжение записи и связанные заметки во «Свойствах».' },
  { key: 'todoist', name: 'Задачи в Todoist',
    note: 'Кнопка «Поставить задачи» у документа и токен Todoist.' },
  { key: 'outlook', name: 'Встречи из Outlook',
    note: 'Название и участники подставляются из встречи в календаре.' },
  { key: 'calls', name: 'Замечать звонки',
    note: 'Программа видит начало звонка и предлагает начать запись.' },
  { key: 'screenshots', name: 'Снимки экрана в заметку',
    note: 'Снимки, сделанные во время записи, попадают в заметку по времени.' },
  { key: 'micmute', name: 'Выключатель микрофона',
    note: 'Кнопка в шапке панели и кружок поверх окон во время записи.' },
  { key: 'dictation', name: 'Диктовка',
    note: 'Надиктовать текст в любое окно по сочетанию клавиш.' },
  { key: 'video_getcourse', name: 'Видео: GetCourse', note: 'Источник видео на вкладке «Видео».' },
  { key: 'video_sharepoint', name: 'Видео: SharePoint и Teams', note: 'Источник видео на вкладке «Видео».' },
  { key: 'video_password', name: 'Видео: сайт по паролю', note: 'Источник видео на вкладке «Видео».' },
  { key: 'advanced', name: 'Тонкие настройки',
    note: 'Раздел «Продвинутые»: шаг окна разметки, пороги, число отмен.' },
];

/* Спрятать всё, что относится к выключенным возможностям. Одно место на всю
   страницу: элемент помечен data-feature, остальное — дело этой функции. */
function applyFeatures() {
  const f = (S.settings && S.settings.features) || {};
  document.querySelectorAll('[data-feature]').forEach((el) => {
    const on = !!f[el.dataset.feature];
    el.classList.toggle('feature-off', !on);
  });
  // Вкладка раздела настроек пропала вместе с возможностью — уводим с неё.
  const active = document.querySelector('#dlg-settings .tab.active');
  if (active && active.classList.contains('feature-off')) {
    const first = document.querySelector('#dlg-settings .tab:not(.feature-off)');
    if (first) first.click();
  }
}

function paintFeatures() {
  const box = $('features-list');
  if (!box) return;
  const f = (S.settings && S.settings.features) || {};
  box.innerHTML = FEATURES.map((it) => `
    <label class="feature-row">
      <input type="checkbox" class="js-feature" data-key="${esc(it.key)}"${f[it.key] ? ' checked' : ''}>
      <span><b>${esc(it.name)}</b><span class="tiny muted">${esc(it.note)}</span></span>
    </label>`).join('');
  box.querySelectorAll('.js-feature').forEach((el) => {
    el.onchange = () => setFeature(el.dataset.key, el.checked);
  });
}

async function setFeature(key, on) {
  const f = Object.assign({}, (S.settings && S.settings.features) || {});
  f[key] = !!on;
  try {
    S.settings = await api('/api/settings', { method: 'POST', body: { features: f } });
    applyFeatures();
    paintFeatures();
  } catch (e) { notice(e.message, 'err'); }
}

async function enableAllFeatures() {
  const f = {};
  FEATURES.forEach((it) => { f[it.key] = true; });
  try {
    S.settings = await api('/api/settings', { method: 'POST', body: { features: f } });
    applyFeatures();
    paintFeatures();
    notice('Все возможности включены', 'ok');
  } catch (e) { notice(e.message, 'err'); }
}


async function openSettings() {
  const s = S.settings;
  $('set-vault').value = s.vault_path || '';
  $('set-subfolder').value = s.vault_subfolder || '';
  $('set-hf').value = '';
  // Токен нужен только релизу с разметкой через pyannote; решает служба.
  $('hf-field').classList.toggle('hidden', !s.diarize_needs_token);
  $('hf-state').textContent = s.hf_token_set
    ? `токен задан (${s.hf_token_hint}) — поле оставьте пустым, чтобы не менять`
    : 'токен не задан: разметка говорящих не заработает';
  $('set-silence').value = s.silence_finalize_ms || 2200;
  $('set-threads').value = s.onnx_threads || 7;
  $('set-match').value = s.voice_match_threshold || 0.7;
  $('set-suggest').value = s.voice_suggest_threshold || 0.45;
  $('set-shots').checked = s.screenshots_enabled !== false;
  $('set-mic-pill').checked = s.mic_pill !== false;
  $('set-fix-after').value = s.fix_suggest_after == null ? 3 : s.fix_suggest_after;
  loadFixes();
  $('set-tray').checked = s.tray_enabled !== false;
  $('set-autostart-win').checked = !!s.autostart_windows;
  $('set-watch').checked = !!s.call_watch_enabled;
  $('set-autostart').checked = !!s.call_watch_autostart;
  $('set-call-stop').value = s.call_end_confirm_s || 30;
  $('set-call-resume').value = s.call_resume_window_s || 600;
  // Забытая запись (20.09). 0 — законное значение («не следить»), поэтому не «|| 10».
  $('set-idle-min').value = (s.idle_stop_min === undefined || s.idle_stop_min === null)
    ? 10 : s.idle_stop_min;
  $('set-idle-words').value = (s.idle_stop_words === undefined || s.idle_stop_words === null)
    ? 5 : s.idle_stop_words;
  $('set-idle-confirm').value = s.idle_stop_confirm_s || 120;
  $('set-require-mic').checked = !!s.call_watch_require_mic;
  // 0 — законное значение («только идущая встреча» и «без выдержки»), поэтому не «|| 5».
  $('set-meeting-ahead').value = (s.meeting_lookahead_min === undefined
    || s.meeting_lookahead_min === null) ? 5 : s.meeting_lookahead_min;
  $('set-call-debounce').value = (s.call_watch_debounce_s === undefined
    || s.call_watch_debounce_s === null) ? 3 : s.call_watch_debounce_s;
  $('set-procs').value = (s.call_watch_processes || []).join(', ');
  $('set-outlook').checked = !!s.outlook_enabled;
  $('set-engine').value = s.minutes_engine || 'claude_cli';
  $('set-cli-model').value = s.claude_cli_model || '';
  $('set-cli-timeout').value = s.claude_timeout_s || 600;
  fillConnections();
  paintVaultState();
  $('outlook-state').textContent = S.caps.outlook
    ? 'классический Outlook найден, встречи читаются'
    : 'COM-доступ к Outlook недоступен (возможно, запущен «новый Outlook»)';
  const eng = (S.caps.engines || []).map((e) => `${e.title}: ${e.ready ? (e.state || 'готов') : e.reason}`);
  $('engine-state').textContent = eng.join(' · ');
  refreshModelsHint();
  loadNeeds();
  fillAsrModels();
  loadAsrModels();
  toggleApiFields();
  $('set-auto-diarize').checked = !!s.diarize_auto;
  $('set-retention').value = s.audio_retention_days || 0;
  fillDictate();
  fillAdvanced();
  showYtdlpVersion();
  loadStorage();
  await loadPrompts();
  await loadVoices();
  paintFeatures();
  // Поля привязываем при открытии: часть из них (список сервисов, промпты)
  // появляется только теперь.
  bindSettingsFields();
  bindPickFolder();
  loadAbout();
  showDialog('dlg-settings');
}

/* «О программе»: версия и обновление. Проверка — только по кнопке, сама
   программа в интернет не ходит. Всё решает служба (hagen/updates.py);
   страница показывает и передаёт нажатия. */
function updateLastText(last) {
  if (!last) return '';
  const when = (last.at || '').replace('T', ' ').slice(0, 16);
  if (last.phase === 'applied' && last.confirmed) {
    return `Обновлено с ${last.from} до ${last.to} (${when}).`
      + ((last.models || []).length ? ' В этой версии обновилась модель распознавания — '
        + 'скачайте её заново в «Настройки → Модели», когда будет удобно.' : '');
  }
  if (last.phase === 'rolled_back') {
    return `Версия ${last.to} дважды не запустилась — вернулась ${last.from}. `
      + 'Библиотеки остались новыми. Если что-то не работает, поставьте выпуск заново через Ustanovka.cmd.';
  }
  if (last.phase === 'failed') return `Обновление до ${last.to} не встало: ${last.error}. Осталась версия ${last.from}.`;
  return '';
}

async function loadAbout() {
  let st;
  try { st = await api('/api/updates'); } catch (e) { return null; }
  S.updates = st;
  $('about-version').textContent = st.version || 'без номера';
  $('about-repo').textContent = st.repo ? `github.com/${st.repo}` : '—';
  const busy = (S.jobs || []).some((j) => j.kind === 'update' && ['queued', 'running'].includes(j.status));
  $('btn-upd-check').disabled = !st.can_update || busy;
  $('btn-upd-file').disabled = !st.can_update || busy;
  $('upd-staged').classList.toggle('hidden', !st.staged);
  if (st.staged) {
    const extra = [];
    if ((st.staged.wheels || []).length) extra.push(`библиотек: ${st.staged.wheels.length}`);
    if (st.staged.ffmpeg) extra.push('новый ffmpeg');
    $('upd-staged-text').textContent = `Обновление до ${st.staged.to} скачано и проверено`
      + (extra.length ? ` (${extra.join(', ')})` : '')
      + '. Закройте программу — значок у часов → «Выход» — и откройте снова: оно встанет при запуске.';
    $('upd-found').classList.add('hidden');
  }
  $('upd-state').textContent = !st.can_update ? st.reason
    : (busy ? 'Готовлю обновление — ход виден в блоке «В работе».' : updateLastText(st.last));
  return st;
}

async function checkUpdates() {
  $('btn-upd-check').disabled = true;
  $('upd-state').textContent = 'Спрашиваю GitHub…';
  $('upd-found').classList.add('hidden');
  try {
    const r = await api('/api/updates/check', { method: 'POST' });
    if (r.error) { $('upd-state').textContent = r.error; return; }
    if (!r.newer) {
      $('upd-state').textContent = r.latest ? `Стоит последняя версия: ${r.current}.` : 'Выпусков с архивом пока нет.';
      return;
    }
    const mb = r.asset && r.asset.size ? ` — архив ${Math.round(r.asset.size / 1048576)} МБ` : '';
    $('upd-found-title').textContent = `Есть версия ${r.latest}${mb}`;
    $('upd-notes').textContent = (r.notes || '').trim() || 'Описания у выпуска нет.';
    $('upd-found').classList.remove('hidden');
    $('upd-state').textContent = `Сейчас стоит ${r.current}.`;
  } catch (e) {
    $('upd-state').textContent = e.message;
  } finally {
    $('btn-upd-check').disabled = !(S.updates && S.updates.can_update);
  }
}

function bindAbout() {
  $('btn-upd-check').onclick = checkUpdates;
  $('btn-upd-get').onclick = async () => {
    $('btn-upd-get').disabled = true;
    try {
      await api('/api/updates/download', { method: 'POST' });
      $('upd-found').classList.add('hidden');
      $('upd-state').textContent = 'Скачиваю и готовлю — ход виден в блоке «В работе». Программа работает как обычно.';
    } catch (e) { notice(e.message, 'err'); } finally { $('btn-upd-get').disabled = false; }
  };
  $('btn-upd-file').onclick = () => $('upd-file').click();
  $('upd-file').onchange = async () => {
    const f = $('upd-file').files[0];
    $('upd-file').value = '';
    if (!f) return;
    const fd = new FormData();
    fd.append('file', f, f.name);
    $('upd-state').textContent = `Беру «${f.name}»…`;
    try {
      await api('/api/updates/file', { method: 'POST', body: fd });
      $('upd-state').textContent = 'Проверяю архив и докачиваю, что поменялось, — ход виден в блоке «В работе».';
    } catch (e) { $('upd-state').textContent = e.message; }
  };
  $('btn-upd-discard').onclick = async () => {
    try { await api('/api/updates/discard', { method: 'POST' }); } catch (e) { notice(e.message, 'err'); }
    loadAbout();
  };
}

/* Итог прошлого обновления — один раз, при запуске окна. */
async function noticeLastUpdate() {
  const st = await loadAbout();
  const last = st && st.last;
  if (!last || last.seen || (last.phase === 'applied' && !last.confirmed)) return;
  const text = updateLastText(last);
  if (text) notice(text, last.phase === 'applied' ? 'ok' : 'err');
  try { await api('/api/updates/seen', { method: 'POST' }); } catch (e) {}
}

/* Строка под папкой заметок: на месте ли она и сколько в ней заметок. */
function paintVaultState() {
  const v = S.vault || {};
  $('vault-state').textContent = v.exists
    ? `папка на месте, заметок: ${v.notes != null ? v.notes : '—'}`
    : 'папка будет создана при первом сохранении';
  $('vault-state').className = 'tiny ' + (v.writable === false ? '' : 'muted');
}

/* Кнопка «Выбрать…» у поля с папкой: окно выбора, как в Проводнике.
   Выбранный путь ложится в поле и сохраняется так же, как вписанный руками.
   К другому полю подключается одной разметкой: data-for="<id поля>". */
function bindPickFolder() {
  document.querySelectorAll('.js-pick-folder').forEach((btn) => {
    if (btn.dataset.bound) return;
    btn.dataset.bound = '1';
    btn.addEventListener('click', async (e) => {
      e.preventDefault();
      const input = $(btn.dataset.for);
      if (!input) return;
      const label = btn.closest('.field');
      const title = ((label && label.firstChild && label.firstChild.textContent) || '').trim();
      btn.disabled = true;
      try {
        const res = await api('/api/pick-folder', {
          method: 'POST', body: { title: title || 'Выбор папки', start: input.value.trim() } });
        if (res.path) {
          input.value = res.path;
          if (input.closest('#dlg-settings')) await applySettings(input);
        }
      } catch (err) { notice(err.message, 'err'); }
      finally { btn.disabled = false; }
    });
  });
}

/* «Настройки → Основное»: где лежит звук и видео, сколько занимает, что удалится по сроку. */
async function loadStorage() {
  let st;
  try { st = await api('/api/storage'); } catch (e) { $('storage-audio').textContent = e.message; return; }
  const a = st.audio || {};
  const v = st.videos || {};
  $('storage-audio').textContent = `${a.path} — ${fmtSize(a.bytes)}`
    + (a.records ? ` (записей со звуком: ${a.records}, из них звонков ${a.calls_records}, ${fmtSize(a.calls_bytes)})` : ', пусто');
  $('storage-videos').textContent = v.exists ? `${v.path} — ${fmtSize(v.bytes)} (файлов: ${v.files})` : `${v.path} — папки пока нет`;
  $('btn-reveal-videos').disabled = !v.exists;
  const due = st.due || {};
  $('storage-due').textContent = st.retention_days
    ? (due.records ? `Сейчас под срок попадает звук записей: ${due.records} (${fmtSize(due.bytes)}) — удалится при следующей проверке (раз в 6 часов).`
      : 'Звука старше срока сейчас нет.')
    : '0 — не удалять. Стенограммы, документы и заметки при удалении звука остаются.';
}

/* «Настройки → Обработка»: имя владельца и задача/структура каждого документа. */
async function loadPrompts() {
  let res;
  try { res = await api('/api/prompts'); } catch (e) { $('prompt-list').textContent = e.message; return; }
  S.prompts = res.prompts || [];
  $('set-owner').value = res.owner_name === 'Я' ? '' : (res.owner_name || '');
  $('set-owner-side').innerHTML = ['<option value="">не указана</option>'].concat(
    (res.sides || []).map((s) => `<option value="${esc(s.key)}">${esc(s.title)}</option>`)).join('');
  $('set-owner-side').value = res.owner_side || '';
  $('set-owner-position').value = res.owner_position || '';
  $('set-prompt-lang').value = res.prompt_lang === 'en' ? 'en' : 'ru';
  // Токен не показываем: службы отдаёт только «задан» и хвостик. Пустое поле
  // означает «не трогали», а не «убрать токен» — см. collectPrompts.
  $('set-todoist').value = '';
  $('set-todoist').placeholder = res.todoist_set ? ('задан · ' + (res.todoist_hint || '')) : 'не задан';
  $('prompt-list').innerHTML = S.prompts.map((p) => `
    <div class="box prompt-box" data-k="${esc(p.key)}">
      <div class="row"><b>${esc(p.title)}</b>
        <span class="tiny js-state">${p.overridden ? '— изменён' : '— исходный'}</span></div>
      <div class="tiny muted">${esc(p.hint)}</div>
      <textarea rows="9" class="js-prompt">${esc(p.current)}</textarea>
      <div class="row"><button class="ghost small js-reset" type="button">Вернуть исходный</button></div>
    </div>`).join('');
  $('prompt-list').querySelectorAll('.prompt-box').forEach((box) => {
    const p = S.prompts.find((x) => x.key === box.dataset.k);
    const ta = box.querySelector('.js-prompt');
    const state = () => {
      box.querySelector('.js-state').textContent = ta.value.trim() === p.default ? '— исходный' : '— изменён';
    };
    ta.oninput = state;
    box.querySelector('.js-reset').onclick = () => { ta.value = p.default; state(); };
  });
}

function collectPrompts() {
  const overrides = {};
  $('prompt-list').querySelectorAll('.prompt-box').forEach((box) => {
    overrides[box.dataset.k] = box.querySelector('.js-prompt').value;
  });
  const out = { owner_name: $('set-owner').value.trim() || 'Я', overrides,
                owner_side: $('set-owner-side').value || '',
                owner_position: $('set-owner-position').value.trim(),
                prompt_lang: $('set-prompt-lang').value };
  // Поле токена пустое — значит его не трогали, и менять сохранённый нельзя.
  // Чтобы УБРАТЬ токен, есть отдельное слово «убрать»: иначе один случайный
  // заход в настройки молча отключал бы отправку задач.
  const tok = $('set-todoist').value.trim();
  if (tok) out.todoist_token = (tok.toLowerCase() === 'убрать') ? '' : tok;
  return out;
}

/* Поля подписки и поля стороннего сервиса не показываем одновременно: при
   работе по подписке адрес и ключ ни на что не влияют и только путают. */
function toggleApiFields() {
  const isApi = $('set-engine').value === 'api';
  $('api-fields').classList.toggle('hidden', !isApi);
  $('cli-fields').classList.toggle('hidden', isApi);
}

/* Подключения к облаку — адрес, ключ и модель (решение 21.09): одно для
   документов, одно для облачного распознавания. Ключ в поле не показываем:
   пустое поле значит «не трогали», а что ключ задан, видно по маске. */
const CONNECTION_FIELDS = ['set-api-url', 'set-apikey', 'set-model', 'set-api-kind',
  'set-folder', 'set-asr-files', 'set-cloud-asr-url', 'set-cloud-asr-key',
  'set-cloud-asr-model'];

function fillConnections() {
  const s = S.settings || {};
  $('set-api-url').value = s.api_base_url || '';
  $('set-model').value = s.api_model || '';
  $('set-api-kind').value = s.api_kind || '';
  $('set-folder').value = s.api_folder || '';
  $('set-apikey').value = '';
  $('set-asr-files').value = s.asr_files === 'cloud' ? 'cloud' : 'local';
  $('set-cloud-asr-url').value = s.asr_base_url || '';
  $('set-cloud-asr-model').value = s.asr_model || '';
  $('set-cloud-asr-key').value = '';
  closeServicesPop();
  paintConnections();
}

function collectConnections() {
  const patch = {
    api_base_url: $('set-api-url').value.trim(),
    api_model: $('set-model').value.trim(),
    api_kind: $('set-api-kind').value,
    api_folder: $('set-folder').value.trim(),
    asr_files: $('set-asr-files').value,
    asr_base_url: $('set-cloud-asr-url').value.trim(),
    asr_model: $('set-cloud-asr-model').value.trim(),
  };
  // Ключи служба сливает по одному: отправляем только вписанный.
  const keys = {};
  if ($('set-apikey').value.trim()) keys.docs = $('set-apikey').value.trim();
  if ($('set-cloud-asr-key').value.trim()) keys.asr = $('set-cloud-asr-key').value.trim();
  if (Object.keys(keys).length) patch.api_keys = keys;
  return patch;
}

/* Что с подключениями, говорит служба (S.caps.connections): как разговаривает
   сервис по этому адресу, маска ключа, чего не хватает. Здесь только показ. */
function paintConnections() {
  const cs = (S.caps || {}).connections || {};
  const docs = cs.docs || {};
  const asr = cs.asr || {};
  const keyLine = (c) => (c.has_key ? `ключ задан (${c.key_hint})` : 'ключ не задан');
  $('apikey-state').textContent = keyLine(docs);
  $('cloud-asr-key-state').textContent = keyLine(asr);
  $('set-api-kind').options[0].textContent = docs.base_url && !docs.kind_manual
    ? `По адресу — ${docs.kind_title}` : 'По адресу';
  $('folder-field').classList.toggle('hidden', docs.kind !== 'yandexgpt');
  $('api-url-state').textContent = docs.base_url
    ? `${docs.service} · разговор ${docs.kind_title}` + (docs.problem ? ` · ${docs.problem}` : '')
    : 'Адрес API, обычно заканчивается на /v1. Только https и только внешний адрес.';
  // Облачные поля нужны, только когда распознаём в облаке: иначе они серые и
  // не правятся — чтобы не казалось, что ключ здесь что-то меняет.
  const local = $('set-asr-files').value !== 'cloud';
  $('cloud-asr-fields').classList.toggle('fields-off', local);
  $('cloud-asr-fields').querySelectorAll('input, button').forEach((el) => { el.disabled = local; });
  $('cloud-asr-state').textContent = local
    ? 'Файлы и видео распознаются на этом компьютере — облачные поля не используются.'
    : (asr.problem
      || (asr.model ? `${asr.service} · модель ${asr.model}` : 'Не указана модель распознавания.'));
}

/* Модели, которые служба уже спросила у сервиса по кнопке, — для списка у поля
   «Модель». Сменили адрес — прежний список служба не отдаёт. */
function modelItems(role) {
  const cached = (S.models || {})[role] || ((S.caps || {}).models_cached || {})[role] || {};
  return ((role === 'asr' ? cached.stt : cached.chat) || []).map((m) => ({
    value: m.id,
    hint: [m.title && m.title !== m.id ? m.title : '', priceLabel(m).replace(/^ — /, '')]
      .filter(Boolean).join(' · '),
  }));
}

/* Выпадающий список с отбором по вводу — для поля, где значение выбирают из
   длинного списка, но можно вписать и своё. Вид — как у подсказок имени в окне
   «Кто это говорит?». Слова отбора ищутся в любом месте имени и подписи, в
   любом порядке. items() — [{value, hint}], pick(value) — что делать с выбранным. */
const COMBO_LIMIT = 200;

function bindComboList(input, box, items, pick) {
  const st = { open: false, active: -1, filter: '', shown: [] };
  const render = () => {
    if (!st.open) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    const all = items();
    const words = st.filter.toLowerCase().split(/\s+/).filter(Boolean);
    const match = all.filter((it) => {
      const hay = `${it.value} ${it.hint || ''}`.toLowerCase();
      return words.every((w) => hay.includes(w));
    });
    st.shown = match.slice(0, COMBO_LIMIT);
    if (st.active >= st.shown.length) st.active = st.shown.length - 1;
    let html = st.shown.map((it, i) =>
      `<span class="ac-item${i === st.active ? ' active' : ''}" data-i="${i}"><b>${esc(it.value)}</b>`
      + `${it.hint ? `<span class="tiny muted">${esc(it.hint)}</span>` : ''}</span>`).join('');
    if (match.length > COMBO_LIMIT) {
      html += `<span class="ac-note tiny muted">и ещё ${match.length - COMBO_LIMIT} — уточните отбор</span>`;
    }
    if (!all.length) {
      html = '<span class="ac-note tiny muted">Список пуст — нажмите «Обновить список моделей».</span>';
    } else if (!match.length) {
      html = '<span class="ac-note tiny muted">Под отбор ничего не подошло — можно вписать своё имя.</span>';
    }
    box.innerHTML = html;
    box.classList.remove('hidden');
    box.querySelectorAll('.ac-item').forEach((el) => {
      el.onmousedown = (e) => { e.preventDefault(); choose(st.shown[Number(el.dataset.i)]); };
    });
    const act = box.querySelector('.ac-item.active');
    if (act) act.scrollIntoView({ block: 'nearest' });
  };
  const open = (filter) => { st.open = true; st.filter = filter; st.active = -1; render(); };
  const close = () => { st.open = false; render(); };
  const choose = (it) => {
    if (!it) return;
    input.value = it.value;
    close();
    pick(it.value);
  };
  input.addEventListener('focus', () => open(''));       // сразу весь список
  input.addEventListener('input', () => open(input.value.trim()));
  input.addEventListener('blur', close);
  input.addEventListener('keydown', (e) => {
    const n = st.shown.length;
    if (e.key === 'ArrowDown') {
      if (!st.open) open('');
      else if (n) { st.active = (st.active + 1) % n; render(); }
      e.preventDefault();
    } else if (e.key === 'ArrowUp' && st.open && n) {
      st.active = (st.active - 1 + n) % n;
      render();
      e.preventDefault();
    } else if (e.key === 'Enter' && st.open && st.active >= 0) {
      e.preventDefault();
      e.stopImmediatePropagation();       // Enter выбирает пункт, а не сохраняет набранное
      choose(st.shown[st.active]);
    } else if (e.key === 'Escape' && st.open) {
      e.stopPropagation();                 // закрыть список, а не окно настроек
      close();
    }
  });
  const arrow = input.parentElement.querySelector('.ac-arrow');
  if (arrow) {
    arrow.onmousedown = (e) => {
      e.preventDefault();                  // фокус остаётся в поле
      if (st.open) close();
      else if (document.activeElement !== input) input.focus();
      else open('');
    };
  }
}

/* «?» у адреса сервиса: подходящие адреса — список ведёт служба. Два столбца:
   сервис с пометкой и адрес с кнопкой «Копировать». */
async function toggleServicesPop() {
  const box = $('services-pop');
  if (!box.classList.contains('hidden')) { closeServicesPop(); return; }
  let res;
  try { res = await api('/api/connections'); } catch (e) { notice(e.message, 'err'); return; }
  const items = res.suggested || [];
  box.innerHTML = `<table class="services-table">${items.map((s, i) => `<tr>
    <td><b>${esc(s.title)}</b><div class="tiny muted">${esc(s.note || '')}</div></td>
    <td><span class="url"><code>${esc(s.base_url)}</code>
      <button type="button" class="ghost small js-copy-url" data-i="${i}"
              title="Скопировать адрес в буфер обмена">Копировать</button></span></td>
  </tr>`).join('')}</table>`;
  box.querySelectorAll('.js-copy-url').forEach((b) => {
    b.onclick = async () => {
      const url = (items[Number(b.dataset.i)] || {}).base_url || '';
      try { await navigator.clipboard.writeText(url); notice(`Скопировано: ${url}`, 'ok'); }
      catch (e) { notice('Скопировать не вышло: ' + e.message, 'err'); }
    };
  });
  box.classList.remove('hidden');
  document.addEventListener('mousedown', servicesPopOutside, true);
  document.addEventListener('keydown', servicesPopEscape, true);
}

function closeServicesPop() {
  $('services-pop').classList.add('hidden');
  document.removeEventListener('mousedown', servicesPopOutside, true);
  document.removeEventListener('keydown', servicesPopEscape, true);
}

function servicesPopOutside(ev) {
  if (!$('services-pop').contains(ev.target) && ev.target !== $('btn-services')) closeServicesPop();
}

function servicesPopEscape(ev) {
  if (ev.key !== 'Escape') return;
  ev.stopPropagation();                  // закрыть список, а не окно настроек
  closeServicesPop();
}

/* Выбранная модель сохраняется так же, как вписанная руками: уходом из поля. */
function bindModelCombos() {
  [['set-model', 'api-models', 'docs'], ['set-cloud-asr-model', 'cloud-asr-models', 'asr']]
    .forEach(([id, box, role]) => {
      const input = $(id);
      bindComboList(input, $(box), () => modelItems(role), () => input.blur());
    });
}

async function refreshModels(role, btn) {
  btn.disabled = true;
  btn.textContent = 'спрашиваю сервис…';
  try {
    await applySettings(btn);            // адрес и ключ могли вписать только что
    const res = await api(`/api/models?role=${role}&refresh=true`, { method: 'POST' });
    S.models = S.models || {};
    S.models[role] = res;
    paintConnections();
    const n = (role === 'asr' ? res.stt : res.chat) || [];
    notice(`Моделей получено: ${n.length}. Откройте список у поля «Модель» — `
      + 'начните вводить, и он сузится.', 'ok');
  } catch (e) { notice(e.message, 'err'); } finally {
    btn.disabled = false;
    btn.textContent = 'Обновить список моделей';
  }
}

/* Поля подключений сохраняются, как все поля настроек, но после сохранения
   перерисовывают строки под собой: как разговаривает сервис и чего не
   хватает, служба узнаёт только по сохранённому адресу. */
function bindConnections() {
  CONNECTION_FIELDS.forEach((id) => {
    const el = $(id);
    el.dataset.bound = '1';               // общая привязка полей настроек — мимо
    const save = async () => {
      // Сменили адрес — список моделей прежнего сервиса больше не подсказка.
      if (S.models && id === 'set-api-url') delete S.models.docs;
      if (S.models && id === 'set-cloud-asr-url') delete S.models.asr;
      if (id === 'set-asr-files') paintConnections();   // серость — сразу, не после ответа
      await applySettings(el);
      if (el.type === 'password') el.value = '';
      paintConnections();
      refreshModelsHint();
    };
    if (el.tagName === 'SELECT') { el.addEventListener('change', save); return; }
    el.addEventListener('blur', save);
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); el.blur(); }
    });
  });
}

/* Цена модели, если сервис её сообщил. Сразу считаем «за час записи» и «за
   протокол часового совещания»: голые цифры за миллион токенов человеку без
   привычки ничего не говорят. */
function priceLabel(m) {
  const p = m.price || {};
  const cur = p.currency === 'RUB' ? '₽' : (p.currency || '');
  if (p.per_minute) return ` — ${(p.per_minute * 60).toFixed(1)} ${cur} за час записи`;
  if (p.in_per_million && p.out_per_million) {
    const perDoc = p.in_per_million * 0.03 + p.out_per_million * 0.004;
    return ` — примерно ${perDoc.toFixed(2)} ${cur} за протокол`;
  }
  return '';
}

/* Какие модели подставятся на самом деле. Строку считает служба (она знает
   имена моделей), поэтому после смены выбора она устаревает до сохранения —
   об этом честно пишем. */
function refreshModelsHint() {
  const s = S.settings || {};
  const now = (S.caps || {}).minutes_models || '';
  const changed = $('set-engine').value !== (s.minutes_engine || 'claude_cli')
    || $('set-model').value.trim() !== (s.api_model || '')
    || $('set-cli-model').value !== (s.claude_cli_model || '');
  $('quality-state').textContent = changed
    ? `Сейчас: ${now} Новый выбор применится после «Сохранить».`
    : `Сейчас: ${now}`;
}

/* «Настройки → Голоса»: карточки людей (варианты имени, откуда каждый образец
   голоса), возможные дубли, объединение, «общее устройство». */
async function voicesAction(path, opts) {
  const o = Object.assign({ headers: {} }, opts || {});
  if (o.body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(o.body); }
  const r = await fetch(path, o);
  const data = await r.json().catch(() => ({}));
  return { status: r.status, data: data };
}

async function renameVoice(p) {
  const name = prompt('Новое имя', p.name);
  if (!name || name.trim() === p.name) return;
  const res = await voicesAction(`/api/voices/${encodeURIComponent(p.id)}`, { method: 'PATCH', body: { name: name } });
  if (res.status === 409 && res.data.other) {
    if (confirm(`«${res.data.other.name}» уже есть в базе. Объединить «${p.name}» с ним в одного человека?`)) {
      await voicesAction(`/api/voices/${encodeURIComponent(p.id)}/merge`, { method: 'POST', body: { into: res.data.other.id } });
    }
  } else if (res.status !== 200) {
    notice(res.data.detail || `Ошибка ${res.status}`, 'err');
  }
  loadVoices();
}

async function loadVoices() {
  let res = {};
  try { res = await api('/api/voices'); } catch (e) { res = {}; }
  S.voices = res.people || [];
  S.voiceDupes = res.duplicates || [];
  api('/api/voices/backups').then((b) => {
    const list = b.backups || [];
    $('voices-backup-select').innerHTML = list.length
      ? list.map((x) => `<option value="${esc(x.name)}">${esc(fmtDate(x.at))} · людей ${x.people}</option>`).join('')
      : '<option value="">копий пока нет</option>';
    $('btn-voices-restore').disabled = !list.length;
  }).catch(() => {});
  const open = S.voicesOpen || (S.voicesOpen = new Set());
  $('voices-dupes').innerHTML = S.voiceDupes.length
    ? `<div class="box dupes"><b>Возможно, это одни и те же люди</b>${S.voiceDupes.map((d, i) =>
      `<div class="dupe" data-i="${i}">«${esc(d.a.name)}» и «${esc(d.b.name)}» <span class="tiny muted">— ${esc(d.reason)}</span>
        <button class="ghost small js-merge-ab" type="button">Объединить в «${esc(d.a.name)}»</button>
        <button class="ghost small js-merge-ba" type="button">в «${esc(d.b.name)}»</button>
        <button class="ghost small js-notsame" type="button">Разные люди</button></div>`).join('')}</div>`
    : '';
  const box = $('voices-list');
  if (!S.voices.length) {
    box.innerHTML = '<div class="tiny muted">Пока пусто. Подпишите говорящего в записи или загрузите расшифровку Teams с именами.</div>';
    return;
  }
  const others = (p) => S.voices.filter((x) => x.id !== p.id)
    .map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join('');
  box.innerHTML = S.voices.map((p) => {
    const badges = [p.owner ? '<span class="vbadge">это вы</span>' : '',
      p.kind === 'shared' ? '<span class="vbadge warn">общее устройство</span>' : ''].join('');
    const voice = p.kind === 'shared' ? 'голос не запоминается'
      : (p.has_voice ? `образцов голоса: ${p.count}` : 'голоса нет');
    const odd = (p.samples || []).filter((s) => s.outlier).length;
    const samples = (p.samples || []).map((s) =>
      `<div class="vsample${s.outlier ? ' odd' : ''}">${esc(fmtDate(s.added_at) || '—')} · ${esc(s.origin_title || '')}
        ${s.rec_title ? '· ' + esc(s.rec_title) : ''}${s.outlier ? ' · ⚠ не похож на остальные' : ''}
        <button class="ghost small js-vforget" type="button" data-i="${s.index}">забыть</button></div>`).join('');
    return `<div class="voice-card" data-id="${esc(p.id)}">
      <div class="voice"><span class="vn">${esc(p.name)}</span>${badges}
        <span class="tiny muted">${voice}${odd ? ` · ⚠ ${odd} не похож(и)` : ''}</span>
        <button class="ghost small js-vopen" type="button">${open.has(p.id) ? 'свернуть' : 'подробнее'}</button></div>
      <div class="voice-more${open.has(p.id) ? '' : ' hidden'}">
        <div class="tiny">Варианты имени: ${(p.aliases || []).map((a) =>
          `<span class="alias">${esc(a)} <a href="#" class="js-valias-del" data-a="${esc(a)}" title="убрать вариант">×</a></span>`).join(' ') || '<span class="muted">нет</span>'}
          <a href="#" class="js-valias-add">＋ добавить</a></div>
        ${samples ? `<div class="vsamples">${samples}</div>` : ''}
        <div class="row">
          <button class="ghost small js-vrename" type="button">Переименовать</button>
          ${p.owner ? '' : `<label class="inline tiny" title="Переговорка или общий компьютер: голос не запоминается и никому не подставляется"><input type="checkbox" class="js-vshared" ${p.kind === 'shared' ? 'checked' : ''}> общее устройство</label>`}
          <select class="js-vmerge-to"><option value="">объединить с…</option>${others(p)}</select>
          <button class="ghost small js-vdel" type="button">Удалить</button>
        </div>
      </div></div>`;
  }).join('');
  const person = (el) => S.voices.find((x) => x.id === el.closest('.voice-card').dataset.id);
  box.querySelectorAll('.js-vopen').forEach((b) => {
    b.onclick = () => { const p = person(b); open.has(p.id) ? open.delete(p.id) : open.add(p.id); loadVoices(); };
  });
  box.querySelectorAll('.js-vdel').forEach((b) => {
    b.onclick = async () => {
      const p = person(b);
      if (!confirm(`Удалить «${p.name}» из базы вместе с образцами голоса? Копия базы сохранится.`)) return;
      await voicesAction(`/api/voices/${encodeURIComponent(p.id)}`, { method: 'DELETE' });
      loadVoices();
    };
  });
  box.querySelectorAll('.js-vrename').forEach((b) => { b.onclick = () => renameVoice(person(b)); });
  box.querySelectorAll('.js-vforget').forEach((b) => {
    b.onclick = async () => {
      const p = person(b);
      await voicesAction(`/api/voices/${encodeURIComponent(p.id)}/samples/${b.dataset.i}`, { method: 'DELETE' });
      loadVoices();
    };
  });
  box.querySelectorAll('.js-vshared').forEach((c) => {
    c.onchange = async () => {
      const p = person(c);
      if (c.checked && p.has_voice && !confirm(`Отметить «${p.name}» общим устройством? Образцы голоса этой учётки будут забыты.`)) {
        c.checked = false; return;
      }
      await voicesAction(`/api/voices/${encodeURIComponent(p.id)}/kind`, { method: 'POST', body: { kind: c.checked ? 'shared' : 'person' } });
      loadVoices();
    };
  });
  box.querySelectorAll('.js-vmerge-to').forEach((sel) => {
    sel.onchange = async () => {
      const p = person(sel);
      const into = S.voices.find((x) => x.id === sel.value);
      if (!into) return;
      if (!confirm(`Объединить «${p.name}» с «${into.name}» в одного человека? Образцы голоса и варианты имени перейдут к «${into.name}». Копия базы сохранится.`)) {
        sel.value = ''; return;
      }
      await voicesAction(`/api/voices/${encodeURIComponent(p.id)}/merge`, { method: 'POST', body: { into: into.id } });
      loadVoices();
    };
  });
  box.querySelectorAll('.js-valias-add').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      const p = person(a);
      const alias = prompt(`Другой вариант имени для «${p.name}» (например, латиницей)`);
      if (!alias) return;
      const res = await voicesAction(`/api/voices/${encodeURIComponent(p.id)}/alias`, { method: 'POST', body: { alias: alias } });
      if (res.status !== 200) notice(res.data.detail || `Ошибка ${res.status}`, 'err');
      loadVoices();
    };
  });
  box.querySelectorAll('.js-valias-del').forEach((a) => {
    a.onclick = async (e) => {
      e.preventDefault();
      const p = person(a);
      await voicesAction(`/api/voices/${encodeURIComponent(p.id)}/alias`, { method: 'POST', body: { alias: a.dataset.a, remove: true } });
      loadVoices();
    };
  });
  $('voices-dupes').querySelectorAll('.dupe').forEach((row) => {
    const d = S.voiceDupes[Number(row.dataset.i)];
    const merge = async (src, dst) => {
      if (!confirm(`Объединить «${src.name}» с «${dst.name}»? Копия базы сохранится.`)) return;
      await voicesAction(`/api/voices/${encodeURIComponent(src.id)}/merge`, { method: 'POST', body: { into: dst.id } });
      loadVoices();
    };
    row.querySelector('.js-merge-ab').onclick = () => merge(d.b, d.a);
    row.querySelector('.js-merge-ba').onclick = () => merge(d.a, d.b);
    row.querySelector('.js-notsame').onclick = async () => {
      await voicesAction('/api/voices/not-same', { method: 'POST', body: { a: d.a.id, b: d.b.id } });
      loadVoices();
    };
  });
}

/* Настройки применяются сразу (решение 18.09): кнопки
   «Сохранить» больше нет, внизу только «Закрыть». Галочки, списки и карточки
   сохраняются по щелчку, текстовые и числовые поля — когда из них уходят или
   нажимают Enter. У поля на пару секунд появляется «сохранено».

   Собираем по-прежнему весь набор разом: служба сливает настройки по ключу,
   а отправка одного поля за раз отличалась бы от отправки всех только
   объёмом — зато пришлось бы держать соответствие «поле → ключ» вторым
   списком и следить, чтобы он не разошёлся с этим. */
function collectSettings() {
  const patch = {
    vault_path: $('set-vault').value.trim(),
    vault_subfolder: $('set-subfolder').value.trim(),
    silence_finalize_ms: parseInt($('set-silence').value, 10) || 2200,
    onnx_threads: parseInt($('set-threads').value, 10) || 7,
    voice_match_threshold: parseFloat($('set-match').value) || 0.7,
    voice_suggest_threshold: parseFloat($('set-suggest').value) || 0.45,
    screenshots_enabled: $('set-shots').checked,
    mic_pill: $('set-mic-pill').checked,
    fix_suggest_after: Number($('set-fix-after').value) || 3,
    tray_enabled: $('set-tray').checked,
    autostart_windows: $('set-autostart-win').checked,
    call_watch_enabled: $('set-watch').checked,
    call_watch_autostart: $('set-autostart').checked,
    // Ноль служба прочла бы как «по умолчанию», поэтому держим разумные пределы.
    call_end_confirm_s: Math.min(600, Math.max(5, parseInt($('set-call-stop').value, 10) || 30)),
    call_resume_window_s: Math.min(7200, Math.max(30, parseInt($('set-call-resume').value, 10) || 600)),
    // Забытая запись (20.09): 0 минут — защита выключена, и это законный выбор.
    idle_stop_min: (() => {
      const n = parseInt($('set-idle-min').value, 10);
      return Number.isFinite(n) && n >= 0 ? Math.min(240, n) : 10;
    })(),
    idle_stop_words: (() => {
      const n = parseInt($('set-idle-words').value, 10);
      return Number.isFinite(n) && n >= 0 ? Math.min(200, n) : 5;
    })(),
    idle_stop_confirm_s: Math.min(3600, Math.max(30, parseInt($('set-idle-confirm').value, 10) || 120)),
    call_watch_require_mic: $('set-require-mic').checked,
    meeting_lookahead_min: (() => {
      const n = parseInt($('set-meeting-ahead').value, 10);
      return Number.isFinite(n) && n >= 0 ? Math.min(120, n) : 5;
    })(),
    call_watch_debounce_s: (() => {
      const n = parseInt($('set-call-debounce').value, 10);
      return Number.isFinite(n) && n >= 0 ? Math.min(60, n) : 3;
    })(),
    call_watch_processes: $('set-procs').value.split(',').map((s) => s.trim()).filter(Boolean),
    outlook_enabled: $('set-outlook').checked,
    minutes_engine: $('set-engine').value,
    claude_cli_model: $('set-cli-model').value,
    claude_timeout_s: parseInt($('set-cli-timeout').value, 10) || 600,
  };
  const hf = $('set-hf').value.trim();
  if (hf) patch.hf_token = hf;
  Object.assign(patch, collectConnections());
  patch.diarize_auto = $('set-auto-diarize').checked;
  // Список вариантов приходит от службы; пока не пришёл — выбор не трогаем.
  // Какие модели распознавания и для чего (решение 21.09).
  Object.assign(patch, collectAsrModels());
  patch.audio_retention_days = Math.max(0, parseInt($('set-retention').value, 10) || 0);
  Object.assign(patch, collectDictate());
  Object.assign(patch, collectAdvanced());
  return patch;
}

/* Отметка «сохранено» у поля: человек должен видеть, что его правку приняли,
   раз кнопки «Сохранить» больше нет. Держится пару секунд и уходит. */
function markSaved(el) {
  if (!el) return;
  const field = el.closest('.field') || el.parentElement;
  if (!field) return;
  let mark = field.querySelector('.saved-mark');
  if (!mark) {
    mark = document.createElement('span');
    mark.className = 'saved-mark tiny';
    field.appendChild(mark);
  }
  mark.textContent = 'сохранено';
  mark.classList.add('on');
  clearTimeout(mark._t);
  mark._t = setTimeout(() => mark.classList.remove('on'), 2200);
}

async function applySettings(from) {
  const patch = collectSettings();
  try {
    S.settings = await api('/api/settings', { method: 'POST', body: patch });
    if ($('prompt-list').children.length) {
      await api('/api/prompts', { method: 'POST', body: collectPrompts() });
    }
    if (patch.dictate_enabled) await checkDictate();
    markSaved(from);
    // Сменили выбор моделей — служба пересчитает, что скачать и что не нужно.
    if (from && /^set-(asr|live-draft)/.test(from.id || '')) await loadAsrModels();
    await loadState();
    paintVaultState();
    if (S.currentId) await refreshCurrent();
    paintAll();
  } catch (e) { notice(e.message, 'err'); }
}

/* Каждое поле настроек отправляет набор само: галочки и списки — сразу,
   текст и числа — когда из них ушли или нажали Enter. */
function bindSettingsFields() {
  const box = $('dlg-settings');
  if (!box) return;
  box.querySelectorAll('input, select, textarea').forEach((el) => {
    if (el.dataset.bound) return;
    el.dataset.bound = '1';
    const kind = (el.type || el.tagName).toLowerCase();
    if (kind === 'checkbox' || kind === 'radio' || kind === 'select' || el.tagName === 'SELECT') {
      el.addEventListener('change', () => applySettings(el));
      return;
    }
    el.addEventListener('blur', () => applySettings(el));
    el.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && el.tagName !== 'TEXTAREA') { e.preventDefault(); el.blur(); }
    });
  });
}

/* «Сделать документ»: тип записи + вид документа. Тип подсказывает документ
   (встреча — протокол или саммари, лекция — конспект, интервью — выжимка), но
   выбрать можно любой. Выбранный тип запоминается в записи. */
const KIND_DOC = { lecture: 'lecture', interview: 'interview' };
async function openMinutes() {
  if (!S.currentId) return;
  let res;
  try {
    res = await api(`/api/documents/kinds?rec_id=${encodeURIComponent(S.currentId)}`);
  } catch (e) {
    notice(e.message, 'err'); return;
  }
  S.docKinds = res.docs || [];
  $('cloud-warning').textContent = '⚠ ' + (res.warning || '');
  const kinds = res.kinds || [];
  $('doc-video-kind').innerHTML = kinds.map((k) =>
    `<option value="${esc(k.key)}" title="${esc(k.hint)}">${esc(k.title)}</option>`).join('');
  $('doc-video-kind').value = res.video_kind || 'meeting';
  const note = () => {
    const k = kinds.find((x) => x.key === $('doc-video-kind').value);
    $('doc-kind-note').textContent = k ? k.hint : '';
  };
  const pick = (key) => {
    S.tplChosen = key;
    $('tpl-list').querySelectorAll('.tpl').forEach((x) => x.classList.toggle('sel', x.dataset.k === key));
    $('question-field').classList.toggle('hidden', key !== 'question');
  };
  $('tpl-list').innerHTML = S.docKinds.map((d) =>
    `<div class="tpl" data-k="${esc(d.key)}" title="${esc(d.hint)}">
      <b>${esc(d.title)}</b><span class="tiny muted">${esc(d.hint)}</span></div>`).join('');
  $('tpl-list').querySelectorAll('.tpl').forEach((el) => { el.onclick = () => pick(el.dataset.k); });
  $('doc-video-kind').onchange = () => {
    note();
    const kind = $('doc-video-kind').value;
    pick(KIND_DOC[kind] || (res.source === 'live' ? 'protocol' : 'meeting'));
  };
  note();
  pick(res.default_doc || 'protocol');
  $('doc-shots-note').classList.toggle('hidden', !res.shots);
  $('doc-shots-note').textContent = res.shots
    ? `🖼 В записи снимков экрана: ${res.shots}. Модель получит только время и имена файлов и поставит ссылки в нужные места; сами картинки не отправляются.`
    : '';
  showDialog('dlg-minutes');
}

/* ====================================================== звонки */

/* Вопросы автоматики звонков: «Не писать», «Остановить?», «Дописать в прошлую?».
   Решает служба, окно только показывает кнопки: окно может быть спрятано в
   трей, а тот же вопрос приходит уведомлением Windows. */
let promptTimer = null;
function paintPrompt() {
  const p = S.prompt;
  const box = $('call-toast');
  if (promptTimer) { clearInterval(promptTimer); promptTimer = null; }
  if (!p) { box.classList.add('hidden'); return; }
  $('call-text').textContent = p.text || '';
  const acts = $('call-actions');
  acts.innerHTML = '';
  (p.buttons || []).forEach((b, i) => {
    const el = document.createElement('button');
    el.className = (i === 0 ? 'primary' : 'ghost') + ' small';
    el.textContent = b.label;
    el.onclick = async () => {
      box.classList.add('hidden');
      try {
        const r = await api(`/api/prompt/${p.id}`, { method: 'POST', body: { answer: b.id } });
        if (r && r.ok === false && r.reason) notice(r.reason, 'err');
      } catch (e) { notice(e.message, 'err'); }
    };
    acts.appendChild(el);
  });
  const def = (p.buttons || []).find((b) => b.id === p.default);
  const tick = () => {
    if (!p.deadline || !def) { $('call-meeting').textContent = ''; return; }
    const left = Math.max(0, Math.round(p.deadline - Date.now() / 1000));
    $('call-meeting').textContent = `Без ответа через ${left} с — «${def.label}»`;
  };
  tick();
  if (p.deadline && def) promptTimer = setInterval(tick, 1000);
  box.classList.remove('hidden');
}

/* ====================================================== WebSocket событий */

let evWs = null, evTimer = null, evWasOpen = false;
function connectEvents() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  evWs = new WebSocket(`${proto}://${location.host}/ws/events`);
  evWs.onopen = async () => {
    if (evTimer) { clearTimeout(evTimer); evTimer = null; }
    // Связь восстановилась после обрыва (сон ноутбука, перезапуск службы):
    // события за это время пропущены, поэтому перечитываем состояние целиком.
    if (evWasOpen) {
      try {
        await loadState();
        if (S.currentId) await refreshCurrent();
        paintAll();
      } catch (e) { /* служба ещё поднимается — следующая попытка через onclose */ }
    }
    evWasOpen = true;
  };
  evWs.onclose = () => { evTimer = setTimeout(connectEvents, 1500); };
  evWs.onmessage = (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch (err) { return; }
    handleEvent(ev);
  };
}

async function handleEvent(ev) {
  switch (ev.type) {
    case 'segment':
      if (ev.rec_id === S.currentId && S.current) {
        S.current.segments.push(ev.segment);
        S.current.segments.sort((a, b) => a.start - b.start || (a.track > b.track ? 1 : -1));
        paintTranscript();
      }
      break;
    case 'segments':
      if (ev.rec_id === S.currentId && S.current) {
        S.current.segments = ev.segments || [];
        paintTranscript();
      }
      break;
    case 'draft':
      if (ev.rec_id === S.currentId) { S.drafts = ev.drafts || {}; paintDraft(); }
      break;
    case 'level':
      if (ev.rec_id === S.currentId) { S.levels[ev.track] = ev.level; paintMeters(); }
      break;
    case 'levels':
      if (ev.rec_id === S.currentId) {
        S.levels = ev.levels || { mic: 0, far: 0 };
        S.micSilent = !!ev.mic_silent;
        S.farSilent = !!ev.far_silent;
        S.farOn = !!ev.far_on;
        S.micLost = !!ev.mic_lost;
        S.farLost = !!ev.far_lost;
        paintMeters();
      }
      break;
    case 'capture': {
      const st = ev.status || {};
      if (ev.rec_id === S.currentId) S.captureStatus = st;
      if (st.state === 'failed' && ev.rec_id === S.recordingId) {
        // пустую запись служба убрала сама; продолжение заметки остаётся
        S.recordingId = null;
        S.starting = false;
        stopTicker();
        if (S.currentId === ev.rec_id && st.deleted !== false) { S.current = null; S.currentId = null; }
        await loadState();
        if (S.currentId === ev.rec_id) await refreshCurrent();
      } else {
        paintHead();
      }
      break;
    }
    case 'recording': {
      const m = ev.meta;
      // Запись могли удалить, пока событие летело: без этой проверки страница
      // спотыкалась на пустом meta и переставала перерисовываться вовсе.
      if (!m || !m.id) break;
      const i = S.recordings.findIndex((r) => r.id === m.id);
      if (i >= 0) S.recordings[i] = m; else S.recordings.unshift(m);
      if (S.currentId === m.id && S.current) S.current.meta = m;
      paintSidebar(); paintHead();
      // Галочки микрофона и плашка разделения зависят от meta: без перерисовки
      // после «Перечитать точнее» галочка оставалась нажатой, а разделения уже не было.
      if (S.currentId === m.id && S.current) { paintRoomToggle(); paintSpeakerAlerts(); }
      break;
    }
    case 'recordings':
      S.recordings = await api('/api/recordings').catch(() => S.recordings);
      paintSidebar();
      break;
    case 'job': {
      const j = ev.job;
      const i = S.jobs.findIndex((x) => x.id === j.id);
      if (i >= 0) S.jobs[i] = Object.assign(S.jobs[i], j); else S.jobs.unshift(j);
      paintJobs(); paintHead();
      if (window.videoJobChanged) window.videoJobChanged();
      // обновление yt-dlp закончилось — вернуть кнопку и показать версию
      if (j.kind === 'ytdlp' && ['done', 'error', 'cancelled'].includes(j.status)) {
        if ($('btn-ytdlp')) $('btn-ytdlp').disabled = false;
        showYtdlpVersion();
      }
      // скачали тяжёлую часть — обновить список и разблокировать кнопки
      if (j.kind === 'needs' && ['done', 'error', 'cancelled'].includes(j.status)) loadNeeds();
      // подготовка обновления кончилась — «О программе» говорит, что дальше
      if (j.kind === 'update' && ['done', 'error', 'cancelled'].includes(j.status)) loadAbout();
      break;
    }
    case 'updates':
      loadAbout();
      break;
    case 'sp_login':
      if (window.videoSpEvent) window.videoSpEvent(ev);
      break;
    case 'minutes':
      if (ev.rec_id === S.currentId) showMinutes(ev.markdown, ev.template);
      break;
    case 'needs':
      S.needs = ev.parts || [];
      paintNeeds();
      loadAsrModels();      // скачали или сбросили модели — «нужное» и «ненужное» другие
      break;
    case 'notice':
      notice(ev.text, ev.level === 'ok' ? 'ok' : (ev.level === 'err' ? 'err' : ''));
      break;
    case 'call':
      S.call = ev.call || {};
      break;
    case 'mic':
      S.mic = ev.mic || {};
      paintMicButton();
      break;
    case 'dictate':
      S.dictate = ev.dictate || {};
      paintDictate();
      break;
    case 'prompt':
      S.prompt = ev.prompt || null;
      paintPrompt();
      break;
    case 'recording_state':
      // Запись могла начать или остановить служба сама — по звонку. Кнопки
      // «Старт»/«Стоп» обязаны это видеть, иначе остановить было бы нечем.
      if (ev.active && !S.recordingId) {
        S.recordingId = ev.rec_id;
        startTicker();
        await loadState();
        if (!S.currentId || S.currentId === ev.rec_id || S.mode !== 'video') await openRecording(ev.rec_id);
        paintAll();
      } else if (!ev.active && S.recordingId === ev.rec_id) {
        S.recordingId = null;
        stopTicker();
        await loadState();
        if (S.currentId === ev.rec_id) await refreshCurrent();
        paintAll();
      }
      break;
    case 'settings':
      S.settings = ev.settings || S.settings;
      applyView(S.settings);
      paintProgramState();   // сменили движок документов — строка внизу это говорит
      applyFeatures();
      // «Сбросить всё» меняет выбор моделей сама служба — список на экране за ним.
      if (!$('dlg-settings').classList.contains('hidden')) fillAsrModels();
      if (S.current) paintTranscript();       // могло смениться имя владельца
      break;
    case 'ready':
      S.asr = ev.asr || { state: 'ready' };
      paintProgramState();
      break;
  }
}

/* Вкладки записи: стенограмма, по вкладке на
   каждый созданный документ, свойства. Прокручивается только содержимое
   активной вкладки; шапка, вкладки, панель действий и строка состояния стоят
   на месте. */
function showPane(pane) {
  S.pane = pane;
  ['transcript', 'doc', 'props'].forEach((name) => {
    const box = $('pane-' + name);
    if (box) box.classList.toggle('hidden', name !== pane);
    const act = $('act-' + name);
    if (act) act.classList.toggle('hidden', name !== pane);
  });
  document.querySelectorAll('#rec-tabs .rtab').forEach((b) => {
    b.classList.toggle('active', b.dataset.pane === pane);
  });
  // Подсветка вкладки документа: гаснет, как только открыта другая вкладка —
  // иначе активными выглядели две сразу («Саммари» и «Свойства»).
  document.querySelectorAll('#minutes-tabs .doc-tab').forEach((b) => {
    b.classList.toggle('active', pane === 'doc' && b.dataset.k === S.docShown);
  });
  if (pane === 'doc') paintDocTabs();
  requestAnimationFrame(fitActions);
}

function paintRecTabs() {
  const has = ((S.docs || []).length > 0);
  const tabs = $('minutes-tabs');
  if (tabs) tabs.classList.toggle('hidden', !has);
  // Документов не осталось — уходим со вкладки документа, иначе человек
  // смотрел бы в пустоту.
  if (!has && S.pane === 'doc') showPane('transcript');
}

/* Открытый сейчас документ записи. Одно место на всех, кто его ищет:
   отрисовка вкладок, копирование и отправка. */
function currentDoc() {
  const docs = S.docs || [];
  return docs.find((d) => d.key === S.docShown) || docs[0] || null;
}

/* Документы записи: по вкладке на каждый (протокол, саммари, вопросы…). */
function paintDocTabs() {
  const docs = S.docs || [];
  $('minutes-tabs').innerHTML = docs.map((d) =>
    `<button class="doc-tab${(d.key === S.docShown && S.pane === 'doc') ? ' active' : ''}" data-k="${esc(d.key)}" type="button">${esc(d.title)}</button>`).join('');
  $('minutes-tabs').querySelectorAll('.doc-tab').forEach((b) => {
    // Щелчок по вкладке документа не только выбирает документ, но и открывает
    // саму вкладку: раньше, стоя на «Стенограмме», нажать «Саммари» было
    // некуда — документ выбирался, а на экране ничего не менялось.
    b.onclick = () => { S.docShown = b.dataset.k; showPane('doc'); };
  });
  paintDocBody(currentDoc());
  // Вкладка документа появляется, только когда документ есть (п. 6.3).
  paintRecTabs();
  paintFolds();   // свёрнутость документа — у каждой записи своя
}

/* Строка документа в сравнимом виде — ровно та же нормализация, что в службе
   (store.norm_line). Разойдутся — вычеркнутое перестанет совпадать. */
function normLine(text) {
  return String(text || '').trim()
    .replace(/^[\s>*+-]+/, '')
    .replace(/^\d+[.)]\s*/, '')
    .replace(/[*`~]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

/* Вычёркиваемые пункты (17.09): заголовки, разделители и подпись «Сформировано»
   не трогаем — это каркас документа, а не содержание. */
function isDroppable(line) {
  const b = line.trim();
  if (!b || b.startsWith('#') || b.startsWith('---')) return false;
  if (b.startsWith('_Сформировано') || b.startsWith('*Сформировано')
      || b.startsWith('_Подготовил') || b.startsWith('*Подготовил')) return false;
  return !/^[|\-: ]+$/.test(b);
}

function droppedOf(key) {
  const all = (S.current && S.current.meta && S.current.meta.dropped) || {};
  return all[key] || [];
}

/* Документ показывается отрисованным: заголовки, выделение,
   списки и таблицы вместо исходного markdown с «##» и «**». Разбор свой, без
   сторонней библиотеки: программа работает без сети, а набор нужен небольшой —
   ровно то, что кладут в документы наши же инструкции.

   Вычёркивание пунктов остаётся: каждая строка исходника — свой блок со своим
   номером, поэтому щелчок вычёркивает ровно её, как и раньше.

   Таймкоды — ссылки: щелчок переводит на стенограмму к этой реплике. */
function mdInline(text) {
  let html = esc(String(text));
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');
  html = html.replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>');
  html = html.replace(/(^|[^*\w])\*([^*\n]+)\*/g, '$1<i>$2</i>');
  // Курсив подчёркиваниями берём «до последнего подчёркивания в строке»:
  // иначе строка «_…, движок: claude_cli._» рвалась по имени движка.
  html = html.replace(/(^|\s)_(.+)_(?=$|[\s.,;:)])/g, '$1<i>$2</i>');
  // Ссылки вида [[Заметка]] показываем как есть: открывать чужую заметку
  // из документа некуда, а текст ссылки человеку понятен.
  html = html.replace(/\[\[([^\]]+)\]\]/g, '<span class="wikilink">$1</span>');
  html = html.replace(/\[(\d{1,2}:\d{2}(?::\d{2})?)\]/g, (m, t) => tsLink(t));
  html = html.replace(/(^|[\s(])(\d{1,2}:\d{2}:\d{2})(?=$|[\s).,;:])/g,
    (m, pre, t) => pre + tsLink(t));
  return html;
}

function tsSeconds(text) {
  const parts = String(text).split(':').map((n) => parseInt(n, 10));
  if (parts.some((n) => Number.isNaN(n))) return null;
  return parts.length === 3 ? parts[0] * 3600 + parts[1] * 60 + parts[2]
    : parts[0] * 60 + parts[1];
}

function tsLink(text) {
  const sec = tsSeconds(text);
  if (sec === null) return esc(text);
  return '<button type="button" class="ts-link js-goto-ts" data-sec="' + sec
    + '" title="Перейти к этой реплике в стенограмме">' + esc(text) + '</button>';
}

/* Переход по таймкоду: открываем стенограмму и подсвечиваем ближайшую реплику.
   Ближайшую, а не точную: в документе время округлено. */
function gotoTimecode(sec) {
  const segs = (S.current && S.current.segments) || [];
  if (!segs.length) return;
  let best = segs[0];
  segs.forEach((g) => {
    if (Math.abs(Number(g.start) - sec) < Math.abs(Number(best.start) - sec)) best = g;
  });
  showPane('transcript');
  const line = $('transcript').querySelector('.line[data-id="' + best.id + '"]');
  if (!line) return;
  line.scrollIntoView({ block: 'center' });
  line.classList.add('flash');
  setTimeout(() => line.classList.remove('flash'), 1600);
}

function mdBlocks(md) {
  const lines = String(md || '').split('\n');
  const out = [];
  let list = null;
  let table = null;
  const closeList = () => {
    if (list) { out.push('<' + list.tag + '>' + list.items.join('') + '</' + list.tag + '>'); list = null; }
  };
  const closeTable = () => {
    if (!table) return;
    const cells = (row) => row.replace(/^\||\|$/g, '').split('|').map((c) => c.trim());
    const head = cells(table[0]);
    const body = table.slice(1).filter((r) => !/^[|\-: ]+$/.test(r)).map(cells);
    out.push('<table><thead><tr>' + head.map((c) => '<th>' + mdInline(c) + '</th>').join('')
      + '</tr></thead><tbody>'
      + body.map((r) => '<tr>' + r.map((c) => '<td>' + mdInline(c) + '</td>').join('') + '</tr>').join('')
      + '</tbody></table>');
    table = null;
  };
  lines.forEach((raw, i) => {
    const body = String(raw).trim();
    if (!body) { closeList(); closeTable(); return; }   // пустых строк не копим
    if (body.startsWith('|')) { closeList(); table = table || []; table.push(body); return; }
    closeTable();
    const h = body.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList();
      const lvl = Math.min(4, h[1].length + 1);   // «#» документа — это h2 страницы
      out.push('<h' + lvl + '>' + mdInline(h[2]) + '</h' + lvl + '>');
      return;
    }
    if (/^([-*_])\1{2,}$/.test(body)) { closeList(); out.push('<hr>'); return; }
    const li = body.match(/^([-*+]|\d+[.)])\s+(.*)$/);
    const drop = isDroppable(body) ? ' data-i="' + i + '"' : '';
    if (li) {
      const tag = /\d/.test(li[1]) ? 'ol' : 'ul';
      if (!list || list.tag !== tag) { closeList(); list = { tag: tag, items: [] }; }
      list.items.push('<li class="doc-line"' + drop + '>' + mdInline(li[2]) + '</li>');
      return;
    }
    closeList();
    out.push('<p class="doc-line' + (drop ? '' : ' fixed') + '"' + drop + '>'
      + mdInline(body) + '</p>');
  });
  closeList();
  closeTable();
  return out.join('');
}

/* Копировать документ в том виде, в каком его будут вставлять (20.09).
   Документ внутри хранится в Markdown — это одна правда, а на выходе три
   отрисовки под три места: заметки, Word с Outlook и Telegram.

   Системного вызова здесь нет и не нужно: буфер обмена браузера сам умеет
   класть рядом простой текст и HTML, а Word с Outlook берут из него HTML. */
const COPY_FORMATS = {
  md: { title: 'Markdown', note: 'как есть — для заметок и Obsidian' },
  rich: { title: 'Оформленный текст', note: 'для Word и Outlook: заголовки, списки, таблицы' },
  tg: { title: 'Telegram', note: 'жирный и курсив, таблицы строками' },
};

/* HTML для Word и Outlook: тот же рисовальщик, что и на странице, только без
   наших крючков — классов вычёркивания и кнопок-таймкодов. Второго
   рисовальщика Markdown в программе заводить незачем. */
function docHtml(md) {
  const box = document.createElement('div');
  box.innerHTML = mdBlocks(md);
  box.querySelectorAll('.js-goto-ts').forEach((b) => {
    b.replaceWith(document.createTextNode(b.textContent || ''));
  });
  box.querySelectorAll('*').forEach((el) => {
    el.removeAttribute('class');
    el.removeAttribute('data-i');
    el.removeAttribute('title');
  });
  return box.innerHTML;
}

/* Telegram: заголовки жирным, списки точками, таблицы строками. Таблиц он не
   умеет вовсе, а заголовки у него — те же жирные строки. */
function docTelegram(md) {
  const out = [];
  String(md || '').split('\n').forEach((raw) => {
    const body = String(raw).trim();
    if (!body) { out.push(''); return; }
    if (/^([-*_])\1{2,}$/.test(body)) { out.push('—'); return; }
    const h = body.match(/^#{1,4}\s+(.*)$/);
    if (h) { out.push('**' + tgInline(h[1]) + '**'); return; }
    if (body.startsWith('|')) {
      // Разделитель таблицы («|---|---|») выбрасываем: строкой он не читается.
      if (/^[|\-: ]+$/.test(body)) return;
      const cells = body.replace(/^\||\|$/g, '').split('|').map((c) => tgInline(c.trim()));
      out.push(cells.filter(Boolean).join(' — '));
      return;
    }
    const li = body.match(/^([-*+]|\d+[.)])\s+(.*)$/);
    if (li) {
      out.push((/\d/.test(li[1]) ? li[1] + ' ' : '• ') + tgInline(li[2]));
      return;
    }
    out.push(tgInline(body));
  });
  // Пустых строк подряд быть не должно: в сообщении они съедают экран.
  return out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

function tgInline(text) {
  // Разметка Telegram — та же, что у нас, кроме `код`: его он понимает так же.
  // Вики-ссылки [[Заметка]] разворачиваем в имя: в Telegram по ним не перейти.
  return String(text || '').replace(/\[\[([^\]]+)\]\]/g, '$1');
}

/* Положить документ в буфер в выбранном виде. Оформленный текст кладётся
   парой «HTML + простой текст»: Word возьмёт первое, «Блокнот» — второе. */
async function copyDocAs(format) {
  const cur = currentDoc();
  const md = (cur && cur.markdown) || $('minutes-text').textContent || '';
  const kind = COPY_FORMATS[format] ? format : 'md';
  try {
    if (kind === 'md') {
      await navigator.clipboard.writeText(md);
    } else if (kind === 'tg') {
      await navigator.clipboard.writeText(docTelegram(md));
    } else {
      const html = docHtml(md);
      const plain = $('minutes-text').textContent || md;
      if (window.ClipboardItem && navigator.clipboard.write) {
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': new Blob([html], { type: 'text/html' }),
          'text/plain': new Blob([plain], { type: 'text/plain' }),
        })]);
      } else {
        // Без ClipboardItem оформление положить нечем — честно кладём текст.
        await navigator.clipboard.writeText(plain);
      }
    }
  } catch (e) { notice('Скопировать не вышло: ' + e.message, 'err'); return; }
  S.copyFormat = kind;
  notice(`Скопировано: ${COPY_FORMATS[kind].title}`, 'ok');
  try {
    // Ответ кладём в S.settings сразу: иначе следующее чтение состояния
    // вернуло бы прошлый выбор, и галочка в меню отскочила бы назад.
    S.settings = await api('/api/settings', { method: 'POST', body: { copy_format: kind } });
  } catch (e) { /* не сохранилось — в этот раз скопировано всё равно верно */ }
}

/* «Отправить» (20.09). Продолжение копирования: там документ ложится в буфер,
   здесь — уходит адресату. Порядок тот же, что у задач в Todoist: сначала
   показываем, что уйдёт, потом человек отмечает, и только потом открывается
   письмо — которое программа не отправляет, а показывает. */
async function openShareDialog() {
  if (!S.currentId) return;
  let res;
  try {
    res = await api(`/api/recordings/${S.currentId}/share`);
  } catch (e) { notice(e.message, 'err'); return; }
  S.share = res;
  const items = res.items || [];
  if (!items.length) {
    notice('Отправлять пока нечего: нет ни документов, ни стенограммы', 'err');
    return;
  }
  // Открытый документ отмечен сразу: обычно отправляют именно его.
  const cur = currentDoc();
  $('share-list').innerHTML = items.map((it, i) => `
    <label class="link-row">
      <input type="checkbox" data-key="${esc(it.key)}"${
  (cur && it.key === cur.key) || (!cur && i === 0) ? ' checked' : ''}>
      <span class="link-title">${esc(it.title)}</span>
      <span class="tiny muted">${it.kind === 'transcript' ? 'весь разговор по репликам' : 'документ'}</span>
    </label>`).join('');
  $('share-list').querySelectorAll('input').forEach((c) => { c.onchange = paintShareNote; });
  $('share-target').onchange = paintShareNote;
  paintShareNote();
  showDialog('dlg-share');
}

function sharePicked() {
  return [...$('share-list').querySelectorAll('input:checked')].map((c) => c.dataset.key);
}

function paintShareNote() {
  const res = S.share || {};
  const mail = $('share-target').value === 'mail';
  const picked = sharePicked();
  $('btn-share-go').textContent = mail ? 'Открыть письмо' : 'Открыть Telegram';
  $('btn-share-go').disabled = mail && !picked.length;
  if (!mail) {
    $('share-note').textContent = 'Telegram откроет выбор чата с готовым сообщением. '
      + 'Вложения он по ссылке не принимает, а длинный документ в сообщение не влезет — '
      + 'целиком он ляжет в буфер обмена. Отмечать можно только один документ.';
    return;
  }
  $('share-note').textContent = res.mail_attachments
    ? 'Отмеченное уйдёт вложением. Письмо откроется, но НЕ отправится: адресатов впишете сами.'
    : `Вложение доезжает только через классический Outlook. ${res.mail_note || ''} `
      + 'Письмо откроется ссылкой, а папку с файлами покажу рядом.';
}

async function doShare() {
  if (!S.currentId) return;
  const target = $('share-target').value;
  const keys = sharePicked();
  let text = '';
  if (target === 'telegram') {
    // Текст готовит тот же рисовальщик, что и «Копировать в разметке Telegram»:
    // второй разметки Telegram в программе нет. Он же едет в ссылке — по ней
    // Telegram открывает выбор чата с готовым сообщением.
    const items = (S.share || {}).items || [];
    const key = keys[0] || (items[0] || {}).key;
    const doc = (S.docs || []).find((d) => d.key === key);
    if (!doc) { notice('В Telegram можно отправить документ, а не стенограмму', 'err'); return; }
    text = docTelegram(doc.markdown || '');
    // Целиком документ кладём в буфер: в ссылку влезает только начало.
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) { notice('Не вышло положить текст в буфер: ' + e.message, 'err'); }
  }
  try {
    const res = await api(`/api/recordings/${S.currentId}/share`, {
      method: 'POST', body: { target: target, keys: keys, text: text },
    });
    hideDialogs();
    notice(res.note || 'Готово', 'ok');
  } catch (e) { notice(e.message, 'err'); }
}

function copyMenuClose() {
  const m = document.getElementById('copy-menu');
  if (m) m.remove();
  document.removeEventListener('mousedown', copyMenuOutside, true);
}

function copyMenuOutside(ev) {
  const box = document.getElementById('copy-menu');
  const btn = $('btn-copy-minutes');
  if (box && !box.contains(ev.target) && ev.target !== btn && !btn.contains(ev.target)) {
    copyMenuClose();
  }
}

/* Меню выбора вида. Последний выбор отмечен галочкой и стоит первым в памяти:
   человек обычно копирует в одно и то же место. */
function openCopyMenu() {
  if (document.getElementById('copy-menu')) { copyMenuClose(); return; }
  const box = document.createElement('div');
  box.id = 'copy-menu';
  box.className = 'rec-menu';
  box.innerHTML = Object.entries(COPY_FORMATS).map(([key, it]) =>
    `<button type="button" data-fmt="${key}" title="${esc(it.note)}">${
      key === S.copyFormat ? '✓ ' : ''}${esc(it.title)}</button>`).join('');
  document.body.appendChild(box);
  const r = $('btn-copy-minutes').getBoundingClientRect();
  box.style.left = `${Math.round(Math.max(8, r.left))}px`;
  box.style.top = `${Math.round(r.top - box.offsetHeight - 4)}px`;
  box.querySelectorAll('button').forEach((b) => {
    b.onclick = () => { copyMenuClose(); copyDocAs(b.dataset.fmt); };
  });
  document.addEventListener('mousedown', copyMenuOutside, true);
}

function paintDocBody(cur) {
  const box = $('minutes-text');
  if (!cur) { box.innerHTML = ''; return; }
  const marks = new Set(droppedOf(cur.key).map(normLine));
  box.innerHTML = mdBlocks(cur.markdown);
  box.querySelectorAll('.doc-line[data-i]').forEach((el) => {
    const off = marks.has(normLine(el.textContent));
    el.classList.toggle('dropped', off);
    el.title = off ? 'Вернуть пункт' : 'Вычеркнуть: в заметку не уйдёт';
    el.onclick = (ev) => {
      if (ev.target.closest('.js-goto-ts')) return;    // щелчок по таймкоду — переход
      toggleDropped(cur, el.textContent);
    };
  });
  box.querySelectorAll('.js-goto-ts').forEach((b) => {
    b.onclick = (ev) => { ev.stopPropagation(); gotoTimecode(Number(b.dataset.sec)); };
  });
  const n = marks.size;
  // Подсказка про вычёркивание — не постоянной строкой под документом, а пока
  // мышь над текстом (п. 6.4): постоянная занимала место у всех и всегда.
  const hint = (text) => { $('drop-hint').textContent = text; };
  hint(n ? 'вычеркнуто пунктов: ' + n + ' — в заметку они не уйдут' : '');
  box.onmouseenter = () => { if (!n) hint('щелчок по пункту вычёркивает его из заметки'); };
  box.onmouseleave = () => { if (!n) hint(''); };
  $('btn-drop-clear').classList.toggle('hidden', !n);
  $('btn-drop-redo').classList.toggle('hidden', !n);
}

async function toggleDropped(cur, line) {
  if (!S.currentId) return;
  const norm = normLine(line);
  const cur_list = droppedOf(cur.key).slice();
  const at = cur_list.findIndex((d) => normLine(d) === norm);
  if (at >= 0) cur_list.splice(at, 1); else cur_list.push(line.trim());
  await saveDropped(cur.key, cur_list);
}

async function saveDropped(key, lines) {
  const all = Object.assign({}, (S.current && S.current.meta && S.current.meta.dropped) || {});
  if (lines.length) all[key] = lines; else delete all[key];
  try {
    const meta = await api(`/api/recordings/${S.currentId}`, {
      method: 'PATCH', body: { dropped: all },
    });
    S.current.meta = meta;
    paintDocTabs();
  } catch (e) { notice(e.message, 'err'); }
}

async function showMinutes(md, kind) {
  const id = S.currentId;
  await loadMinutesFor(id, kind === 'video_summary' ? 'meeting' : kind);
  if (!(S.docs || []).length && md) {          // файл ещё не прочитался — покажем текст события
    S.docs = [{ key: kind || 'protocol', title: 'Документ', markdown: md }];
    S.docShown = S.docs[0].key;
    paintDocTabs();
  }
  showPane('doc');
}

function hideMinutes() {
  S.docs = [];
  $('minutes-text').textContent = '';
  $('minutes-tabs').innerHTML = '';
}

/* Документы принадлежат КОНКРЕТНОЙ записи, а блок на странице один. Поэтому при
   каждом переключении записи его надо закрыть и подгрузить заново — иначе
   протокол прошлой записи остаётся висеть под чужой стенограммой. */
async function loadMinutesFor(id, prefer) {
  const meta = S.current && S.current.meta;
  if (!id || !meta) { hideMinutes(); return; }
  try {
    const res = await api(`/api/recordings/${id}/documents`);
    if (S.currentId !== id) return;
    S.docs = res.documents || [];
    const keys = S.docs.map((d) => d.key);
    S.docShown = keys.includes(prefer) ? prefer
      : (keys.includes(res.last) ? res.last : (keys.includes(S.docShown) ? S.docShown : keys[0]));
    paintDocTabs();
  } catch (e) {
    hideMinutes();        // документов нет или не читаются — просто не показываем
  }
}

/* «+ Новая запись» готовит чистую страницу, но САМА НЕ ПИШЕТ.
   Раньше эта кнопка вызывала startRecording, то есть немедленно включала
   микрофон — одно нажатие плодило запись на пару секунд, которую потом ещё и
   размечали. Запись начинается только по «● Старт». */
function newRecordingTab() {
  if (S.recordingId) { notice('Сначала остановите текущую запись.', 'err'); return; }
  S.current = null;
  S.currentId = null;
  S.drafts = {};
  hideMinutes();
  paintAll();
  notice('Готово к записи: нажмите «● Старт» или перетащите файл.');
}

/* ====================================================== файлы и ссылки */

/* Загрузка файлов и ссылки переехали в раздел «Видео» (video.js): там у
   них появились свои настройки — язык, где распознавать, брать ли готовые
   субтитры, хранить ли видео. Здесь осталась только блокировка на время записи:
   процессор один, и расшифровка чужого файла посреди совещания съела бы эфир. */
function paintSources() {
  if (window.videoPaint) window.videoPaint(!!S.recordingId);
}


/* ====================================================== диктовка текста

   Горячая клавиша наговаривает текст в любое окно. Служба делает всю работу
   сама; окну остаётся показать капсулу «слушаю» и дать настройки.
   Капсула живёт внутри окна программы: держать её поверх чужих окон умеет
   только отдельное окно Windows — это отложено. */

const DICT_STAGES = {
  listening: ['Слушаю…', ''],
  thinking: ['Распознаю…', 'thinking'],
  sleeping: ['Идёт запись — диктовка спит', 'sleep'],
};

function paintDictate() {
  const d = S.dictate || {};
  const pill = $('dictate-pill');
  const stage = S.settings.dictate_pill === false ? null : DICT_STAGES[d.stage];
  pill.classList.toggle('hidden', !stage);
  if (!stage) return;
  let text = stage[0];
  if (d.stage === 'listening' && d.latched) text = 'Слушаю — нажмите ещё раз, чтобы закончить';
  $('dictate-pill-text').textContent = text;
  pill.className = 'dictate-pill ' + stage[1];
}

/* Словарь из ручных правок. Поправили слово в
   стенограмме несколько раз — программа предлагает делать так всегда. Список
   замен живёт в настройках, журнал пар — рядом с записями. */
async function loadFixes() {
  try {
    S.fixes = await api('/api/fixes');
  } catch (err) {
    S.fixes = { rules: [], suggestions: [] };
  }
  paintFixes();
}

function paintFixes() {
  const box = $('fix-rules');
  const sug = $('fix-suggest');
  if (!box || !sug) return;
  const data = S.fixes || {};
  const rules = data.rules || [];
  // Словарь один на программу, поэтому у каждой замены помечено, где она
  // действует: в стенограммах, в диктовке или всюду (решение 18.09).
  const SCOPES = [['both', 'везде'], ['transcript', 'в стенограммах'], ['dictation', 'в диктовке']];
  box.innerHTML = rules.length
    ? rules.map((r) => `<div class="dict-rule">
        <span class="fix-from">${esc(r.from)}</span>
        <span class="tiny muted">→</span>
        <span class="fix-to">${esc(r.to)}</span>
        <select class="fix-scope small" data-from="${esc(r.from)}"
                title="Где действует эта замена">${SCOPES.map(([v, n]) =>
          `<option value="${v}"${(r.where || 'both') === v ? ' selected' : ''}>${n}</option>`).join('')}</select>
        <button class="ghost small fix-del" type="button" data-from="${esc(r.from)}"
                title="Убрать эту замену">×</button>
      </div>`).join('')
    : '<div class="tiny muted">Пока пусто. Замены появятся сами — из ваших правок.</div>';
  box.querySelectorAll('.fix-del').forEach((b) => {
    b.onclick = () => sendFix('forget', b.dataset.from, '');
  });
  box.querySelectorAll('.fix-scope').forEach((sel) => {
    sel.onchange = () => sendFix('scope', sel.dataset.from, '', sel.value);
  });

  const list = data.suggestions || [];
  sug.classList.toggle('hidden', !list.length);
  sug.innerHTML = list.map((s) => `<div class="fix-sugg">
      <span>Вы ${plural(s.count, 'раз правили', 'раза правили', 'раз правили')}
        «<b>${esc(s.from)}</b>» на «<b>${esc(s.to)}</b>». Делать так всегда?</span>
      <button class="ghost small fix-yes" type="button"
              data-from="${esc(s.from)}" data-to="${esc(s.to)}">Делать</button>
      <button class="ghost small fix-no" type="button"
              data-from="${esc(s.from)}" data-to="${esc(s.to)}">Не предлагать</button>
    </div>`).join('');
  sug.querySelectorAll('.fix-yes').forEach((b) => {
    b.onclick = () => sendFix('add', b.dataset.from, b.dataset.to);
  });
  sug.querySelectorAll('.fix-no').forEach((b) => {
    b.onclick = () => sendFix('dismiss', b.dataset.from, b.dataset.to);
  });
}

async function sendFix(action, from, to, where) {
  try {
    S.fixes = await api('/api/fixes', {
      method: 'POST', body: { action, from, to, where: where || 'both' },
    });
    paintFixes();
    if (action === 'add') notice(`Теперь пишу «${to}»`, 'ok');
  } catch (err) {
    notice(String(err.message || err), 'err');
  }
}

// «Продвинутые»: поле → ключ настроек. Пустое поле уходит в службу как null —
// «как у модели».
const ADVANCED_FIELDS = [
  ['set-dt-step', 'diarize_window_step_s'],
  ['set-dt-threshold', 'diarize_cluster_threshold'],
  ['set-dt-fb', 'diarize_cluster_fb'],
  ['set-dt-minvoice', 'diarize_min_voice_s'],
  ['set-dt-split', 'split_min_piece_s'],
];

// Отмена правок стенограммы: пустое поле здесь — не «как у модели», а заводское
// значение, иначе пустота читалась бы как «без предела».
const UNDO_FIELDS = [
  ['set-undo-steps', 'edit_undo_steps', 20],
  ['set-undo-minutes', 'edit_undo_minutes', 30],
];

// ——— Тяжёлые части, которые качаются по требованию (решение 16.09) ———
// В сборке их нет: точная модель, английская модель и браузер для SharePoint
// нужны не всем и не сразу. Перед действием спрашиваем, а не качаем молча.

async function loadNeeds() {
  try {
    const res = await api('/api/needs');
    S.needs = res.parts || [];
  } catch (e) { S.needs = []; }
  paintNeeds();
}

function needPart(key) {
  return (S.needs || []).find((p) => p.key === key) || null;
}

/* «Настройки → Модели»: какие модели распознавания и для чего (решение 21.09).
   Страница показывает выбор и отправляет его; чего не хватает, что не нужно и
   что работает сейчас — говорит служба (/api/asr/models). Выбор срабатывает
   после перезапуска, и без строки состояния не видно, перезапускались ли. */
async function loadAsrModels() {
  try { S.asrModels = await api('/api/asr/models'); } catch (e) { return; }
  if (S.asrModels.running) S.asr = S.asrModels.running;
  paintAsrModels();
}

function fillAsrModels() {
  const s = S.settings;
  $('set-asr-count').value = String(s.asr_count || 1);
  $('set-asr-single').value = s.asr_single || 'precise';
  $('set-asr-calls').value = s.asr_calls || 'fast';
  $('set-asr-voice').value = s.asr_voice || 'precise';
  $('set-asr-reread').checked = s.asr_reread !== false;
  $('set-live-draft').checked = !!s.live_draft;
  $('set-asr-engine').value = s.asr_engine || 'onnx_asr';
  $('set-asr-weights').value = s.asr_weights || 'fp32';
  paintAsrRows();
}

function collectAsrModels() {
  return {
    asr_count: parseInt($('set-asr-count').value, 10) === 2 ? 2 : 1,
    asr_single: $('set-asr-single').value,
    asr_calls: $('set-asr-calls').value,
    asr_voice: $('set-asr-voice').value,
    asr_reread: $('set-asr-reread').checked,
    live_draft: $('set-live-draft').checked,
    asr_engine: $('set-asr-engine').value,
    asr_weights: $('set-asr-weights').value,
  };
}

/* Какие поля видны при таком выборе — сразу, не дожидаясь службы. */
function paintAsrRows() {
  const two = $('set-asr-count').value === '2';
  const fastCalls = two ? $('set-asr-calls').value === 'fast' : $('set-asr-single').value === 'fast';
  $('row-asr-single').classList.toggle('hidden', two);
  $('row-asr-two').classList.toggle('hidden', !two);
  $('set-asr-reread').disabled = !(two && fastCalls);
  $('row-live-draft').classList.toggle('hidden', !fastCalls);
  $('row-asr-engine').classList.toggle('hidden', !two && $('set-asr-single').value === 'fast');
  $('row-asr-weights').classList.toggle('hidden', $('set-asr-engine').value !== 'onnx_asr');
}

function paintAsrModels() {
  const box = $('asr-models-state');
  const m = S.asrModels;
  if (!box || !m) return;
  const miss = m.missing || [];
  $('asr-models-download').classList.toggle('hidden', !miss.length);
  $('btn-asr-download').textContent = `Скачать нужное — ${m.download_mb} МБ`;
  $('asr-download-what').textContent = miss.map((x) => x.title).join(', ');
  const un = m.unneeded || [];
  $('asr-models-unneeded').classList.toggle('hidden', !un.length);
  $('asr-unneeded-what').textContent = un.map((x) => `${x.title} (${x.disk_mb} МБ`
    + (x.busy ? ', занято — удалится после перезапуска' : '') + ')').join(', ')
    + ` — всего ${m.unneeded_mb} МБ.`;
  $('asr-models-notes').textContent = (m.notes || []).join(' ');
  const st = m.running || S.asr || {};
  let text = '';
  if (st.note) text = '⚠ ' + st.note;
  else if (m.restart) {
    text = `Сейчас работает: ${st.models}. Выбранное — ${m.title} — включится после перезапуска программы.`;
  } else if (st.models) text = `Работает: ${st.models}.`;
  box.textContent = text;
}

async function asrModelsAction(url, body, done) {
  try {
    const res = await api(url, { method: 'POST', body: body || {} });
    if (res.state) { S.asrModels = res.state; paintAsrModels(); }
    if (done) done(res);
  } catch (e) { notice(e.message, 'err'); }
}

function paintNeeds() {
  const box = $('needs-list');
  if (!box) return;
  const parts = S.needs || [];
  if (!parts.length) { box.innerHTML = '<div class="tiny muted">проверяю…</div>'; return; }
  box.innerHTML = parts.map((p) => `<div class="need-row" data-key="${esc(p.key)}">
    <div class="need-main"><b>${esc(p.title)}</b> <span class="tiny muted">${p.size_mb} МБ</span>
      <div class="tiny muted">${esc(p.why)}</div></div>
    ${p.ready ? '<span class="pill ok">на месте</span>'
              : '<button class="ghost small js-need">Скачать</button>'}
  </div>`).join('');
  box.querySelectorAll('.js-need').forEach((b) => {
    b.onclick = () => startNeed(b.closest('.need-row').dataset.key);
  });
}

async function startNeed(key) {
  const p = needPart(key);
  try {
    await api(`/api/needs/${key}`, { method: 'POST' });
    notice(`Скачиваю: ${(p && p.title) || key}. Ход виден в панели задач слева.`);
  } catch (e) { notice(e.message, 'err'); }
}

/* Проверить, есть ли нужная часть. Нет — спросить и поставить скачивание.
   Возвращает true, только если всё на месте и действие можно продолжать. */
async function ensurePart(key) {
  const p = needPart(key);
  if (!p || p.ready) return true;
  const agreed = await askDownload(p);
  if (!agreed) return false;
  await startNeed(key);
  return false;
}

function askDownload(part) {
  return new Promise((resolve) => {
    $('need-text').innerHTML = `Для этого нужна <b>${esc(part.title)}</b> — ${part.size_mb} МБ.`;
    $('need-why').textContent = part.why || '';
    let done = false;
    const finish = (val) => {
      if (done) return;
      done = true;
      $('btn-need-ok').onclick = null;
      hideDialogs();
      resolve(val);
    };
    $('btn-need-ok').onclick = () => finish(true);
    // Закрыли окно крестиком, «Отменой» или Escape — значит, не надо.
    const watch = setInterval(() => {
      if ($('dlg-need').classList.contains('hidden')) { clearInterval(watch); finish(false); }
    }, 200);
    showDialog('dlg-need');
  });
}

async function showYtdlpVersion(text) {
  const box = $('ytdlp-state');
  if (!box) return;
  if (text) { box.textContent = text; return; }
  try {
    const res = await api('/api/ytdlp');
    box.textContent = res.version ? `сейчас ${res.version}` : 'не установлен';
  } catch (e) { box.textContent = ''; }
}

async function updateYtdlp() {
  const btn = $('btn-ytdlp');
  btn.disabled = true;
  showYtdlpVersion('обновляю…');
  try {
    await api('/api/ytdlp/update', { method: 'POST' });
  } catch (e) {
    notice(e.message, 'err');
    showYtdlpVersion();
  }
  // кнопку и версию вернёт событие о завершении задачи
}

function fillAdvanced() {
  const s = S.settings;
  ADVANCED_FIELDS.forEach(([id, key]) => {
    const v = s[key];
    $(id).value = (v === null || v === undefined) ? '' : v;
  });
  UNDO_FIELDS.forEach(([id, key, def]) => {
    const v = s[key];
    $(id).value = (v === null || v === undefined) ? def : v;   // 0 — законное «без предела»
  });
}

function collectAdvanced() {
  const out = {};
  ADVANCED_FIELDS.forEach(([id, key]) => {
    const raw = String($(id).value || '').trim().replace(',', '.');
    const n = parseFloat(raw);
    out[key] = (raw === '' || !Number.isFinite(n) || n < 0) ? null : n;
  });
  UNDO_FIELDS.forEach(([id, key, def]) => {
    const raw = String($(id).value || '').trim();
    const n = parseInt(raw, 10);
    out[key] = (raw === '' || !Number.isFinite(n) || n < 0) ? def : n;
  });
  return out;
}

function fillDictate() {
  const s = S.settings;
  $('set-dictate').checked = !!s.dictate_enabled;
  $('set-dictate-mode').value = s.dictate_mode || 'smart';
  $('set-dictate-lang').value = s.dictate_lang || 'ru';
  $('set-dictate-sound').checked = s.dictate_sound !== false;
  $('set-dictate-pill').checked = s.dictate_pill !== false;
  $('set-dictate-marks').checked = !!s.dictate_marks;
  $('set-dictate-fillers').checked = s.dictate_strip_fillers !== false;
  $('set-dictate-fillers-list').value = (s.dictate_fillers || []).join(', ');
  // 0 — законное значение («держать всегда»), поэтому не «|| 30».
  $('set-precise-idle').value = (s.precise_idle_min === undefined || s.precise_idle_min === null)
    ? 30 : s.precise_idle_min;
  // Список микрофонов уже прочитан для записи — берём тот же.
  const mics = (S.devices && S.devices.mics) || [];
  const cur = s.dictate_device_index;
  $('set-dictate-mic').innerHTML = '<option value="">тот же, что и для записи</option>'
    + mics.map((d) => `<option value="${d.index}"${String(d.index) === String(cur) ? ' selected' : ''}>${esc(d.name)}</option>`).join('');
  $('dictate-history').classList.add('hidden');
  $('btn-dictate-hotkey').dataset.hotkey = s.dictate_hotkey || 'ctrl+shift+space';
  paintHotkeyButton();
  // Сочетания голосовых заметок (20.09): пустое — клавиши нет вовсе.
  paintExtraHotkey('btn-note-hotkey', s.note_hotkey || '');
  paintExtraHotkey('btn-task-hotkey', s.task_hotkey || '');
}

/* Сочетания заметки и поручения. Своё простое назначение: сочетание
   набирается нажатием, как основное, но занятость проверяет служба при
   включении — если занять не вышло, оно не появится в подсказке у заметок. */
function paintExtraHotkey(id, combo) {
  const btn = $(id);
  if (!btn) return;
  btn.dataset.hotkey = combo || '';
  btn.textContent = combo ? hotkeyText(combo) : 'не задано';
}

function hotkeyText(combo) {
  return String(combo || '').split('+')
    .map((p) => (p ? p[0].toUpperCase() + p.slice(1) : p)).join(' + ');
}

function captureExtraHotkey(id) {
  const btn = $(id);
  if (!btn) return;
  const was = btn.textContent;
  btn.textContent = 'Нажмите сочетание…';
  const finish = async (combo) => {
    stop();
    paintExtraHotkey(id, combo);
    await applySettings(btn);
  };
  const onDown = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.key === 'Escape') { stop(); btn.textContent = was; return; }
    const mods = heldMods(e);
    const key = hotkeyName(e.code || '');
    // Сочетания из одних модификаторов здесь не берём: они работают только
    // с подпиской на все нажатия, и это отдельный разговор (см. основное).
    if (key && mods.length) finish(mods.concat([key]).join('+'));
  };
  function stop() { document.removeEventListener('keydown', onDown, true); }
  document.addEventListener('keydown', onDown, true);
}

// Сочетание набора текста показано на двух вкладках — «Набор текста» и
// «Сочетания». Хранится оно одно: меняешь в любом месте, меняется везде
// (решение 16.09).
function hotkeyButtons() {
  return ['btn-dictate-hotkey', 'btn-keys-hotkey'].map((id) => $(id)).filter(Boolean);
}

function setHotkey(combo) {
  hotkeyButtons().forEach((b) => { b.dataset.hotkey = combo; });
}

async function paintHotkeyButton() {
  const btn = $('btn-dictate-hotkey');
  setHotkey(btn.dataset.hotkey);
  let res;
  try { res = await api('/api/dictate/hotkey', { method: 'POST', body: { hotkey: btn.dataset.hotkey } }); }
  catch (e) { return; }
  const text = res.ok ? res.text : (btn.dataset.hotkey || 'не задано');
  hotkeyButtons().forEach((b) => { b.textContent = text; });
  $('dictate-state').textContent = res.ok ? '' : res.error;
  $('dictate-state').className = 'tiny ' + (res.ok ? 'muted' : '');
  if ($('keys-hotkey-state')) {
    $('keys-hotkey-state').textContent = res.ok ? '' : res.error;
    $('keys-hotkey-state').className = 'tiny ' + (res.ok ? 'muted' : '');
  }
  // Сочетание из одних модификаторов работает только с подпиской на все
  // нажатия — человек должен видеть это до сохранения, а не узнавать от
  // антивируса.
  $('dictate-hook-warn').classList.toggle('hidden', !(res.ok && res.by_hook));
  return res.ok;
}

/* Какая это клавиша в нашей записи. Пусто — значит модификатор или чужая. */
function hotkeyName(code) {
  if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
  if (/^Digit[0-9]$/.test(code)) return code.slice(5);
  if (/^F([1-9]|1[0-9]|2[0-4])$/.test(code)) return code.toLowerCase();
  if (code === 'Space') return 'space';
  if (code === 'Insert') return 'insert';
  if (code === 'Home' || code === 'End') return code.toLowerCase();
  return '';
}

function heldMods(e) {
  const out = [];
  if (e.ctrlKey) out.push('ctrl');
  if (e.altKey) out.push('alt');
  if (e.shiftKey) out.push('shift');
  if (e.metaKey) out.push('win');
  return out;
}

/* Сочетание не набирается буквами, а нажимается: так не ошибёшься в написании.
   Обычное сочетание берём сразу по нажатию обычной клавиши. Сочетание из одних
   модификаторов (Ctrl+Win) — только когда клавиши отпустили: пока они нажаты,
   человек ещё может добавить к ним букву. */
function captureHotkey(ev) {
  const btn = (ev && ev.currentTarget) || $('btn-dictate-hotkey');
  const was = btn.textContent;
  btn.textContent = 'Нажмите сочетание…';
  let mods = [];
  const finish = async (combo) => {
    stop();
    setHotkey(combo);
    await paintHotkeyButton();
  };
  const onDown = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (e.key === 'Escape') { stop(); btn.textContent = was; return; }
    mods = heldMods(e);
    const key = hotkeyName(e.code || '');
    if (key) finish(mods.concat([key]).join('+'));
  };
  const onUp = (e) => {
    e.preventDefault();
    e.stopPropagation();
    if (mods.length >= 2) finish(mods.join('+'));
    else { stop(); btn.textContent = was; }
  };
  function stop() {
    document.removeEventListener('keydown', onDown, true);
    document.removeEventListener('keyup', onUp, true);
  }
  document.addEventListener('keydown', onDown, true);
  document.addEventListener('keyup', onUp, true);
}

function collectDictate() {
  // Замены сюда больше не входят: словарь один на программу и сохраняется сам,
  // когда в нём что-то меняют (решение 18.09).
  return {
    dictate_enabled: $('set-dictate').checked,
    dictate_hotkey: $('btn-dictate-hotkey').dataset.hotkey || 'ctrl+shift+space',
    note_hotkey: $('btn-note-hotkey').dataset.hotkey || '',
    task_hotkey: $('btn-task-hotkey').dataset.hotkey || '',
    dictate_mode: $('set-dictate-mode').value,
    dictate_lang: $('set-dictate-lang').value,
    dictate_sound: $('set-dictate-sound').checked,
    dictate_pill: $('set-dictate-pill').checked,
    dictate_marks: $('set-dictate-marks').checked,
    dictate_strip_fillers: $('set-dictate-fillers').checked,
    dictate_fillers: String($('set-dictate-fillers-list').value || '')
      .split(',').map((w) => w.trim()).filter(Boolean),
    precise_idle_min: (() => {
      const n = parseInt($('set-precise-idle').value, 10);
      return Number.isFinite(n) && n >= 0 ? n : 30;
    })(),
    // Пусто — «тот же микрофон, что и для записи»; служба поймёт это как null.
    dictate_device_index: $('set-dictate-mic').value === ''
      ? null : parseInt($('set-dictate-mic').value, 10),
  };
}

/* История диктовок: что и когда наговорено. Лежит на этом компьютере. */
async function showDictateHistory() {
  const box = $('dictate-history');
  let res;
  try { res = await api('/api/dictate/history'); }
  catch (e) { notice(e.message, 'err'); return; }
  const items = res.items || [];
  box.className = 'dict-hist';
  if (!items.length) {
    box.innerHTML = '<div class="tiny muted" style="padding:8px 0">Пока пусто.</div>';
    return;
  }
  box.innerHTML = items.map((it, i) => {
    const when = new Date((it.at || 0) * 1000).toLocaleString('ru-RU');
    return `<div class="dict-item">
      <div class="dh-body"><div class="dh-when">${esc(when)} · ${fmtDur(it.seconds || 0)}</div>
        <div class="dh-text">${esc(it.text || '')}</div></div>
      <button class="ghost small js-dh-copy" data-i="${i}" title="Скопировать в буфер обмена">Копировать</button>
    </div>`;
  }).join('');
  box.querySelectorAll('.js-dh-copy').forEach((b) => {
    b.onclick = async () => {
      const text = (items[parseInt(b.dataset.i, 10)] || {}).text || '';
      try { await navigator.clipboard.writeText(text); notice('Скопировано', 'ok'); }
      catch (e) { notice('Скопировать не вышло: ' + e.message, 'err'); }
    };
  });
}

async function forgetDictateHistory() {
  if (!confirm('Удалить всё надиктованное из истории? Вернуть будет нельзя.')) return;
  try { await api('/api/dictate/history', { method: 'DELETE' }); }
  catch (e) { notice(e.message, 'err'); return; }
  notice('История диктовок очищена', 'ok');
  await showDictateHistory();
}

/* После сохранения служба сразу занимает или освобождает клавишу — показываем,
   получилось ли: сочетание могло оказаться занятым другой программой. */
async function checkDictate() {
  let st;
  try { st = await api('/api/dictate'); } catch (e) { return; }
  S.dictate = st;
  if (st.error) notice(st.error, 'err');
}


/* ====================================================== привязка событий */

function bind() {
  $('btn-new').onclick = newRecordingTab;
  // «Домой» в разделе «Видео» (20.09): вернуться к форме добавления. Запись с
  // экрана не убираем — она остаётся выбранной в списке, и к ней можно
  // вернуться щелчком, ничего не потеряв.
  $('btn-video-home').onclick = () => {
    S.showRecord = false;
    if (window.videoReloadOptions) window.videoReloadOptions();
    paintAll();
  };
  $('btn-start').onclick = startRecording;
  $('btn-stop').onclick = stopRecording;
  $('btn-settings').onclick = openSettings;
  document.querySelectorAll('.dlg-close').forEach((b) => (b.onclick = hideDialogs));
  $('overlay').onclick = hideDialogs;
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') hideDialogs(); });

  // Селектор привязан к окну настроек намеренно: класс .tab когда-нибудь
  // используют ещё где-нибудь, и общий поиск по документу начнёт переключать
  // чужие вкладки.
  document.querySelectorAll('#dlg-settings .tab').forEach((t) => {
    t.onclick = () => {
      document.querySelectorAll('#dlg-settings .tab').forEach((x) =>
        x.classList.toggle('active', x === t));
      document.querySelectorAll('#dlg-settings .tabpane').forEach((p) =>
        p.classList.toggle('hidden', p.id !== t.dataset.tab));
    };
  });

  $('btn-ytdlp').onclick = updateYtdlp;
  document.querySelectorAll('#t-advanced .js-dt-reset').forEach((b) => {
    b.onclick = (ev) => {
      ev.preventDefault();          // кнопка внутри <label>: не уводить фокус в поле
      $(b.dataset.for).value = b.dataset.default;
    };
  });

  $('btn-dictate-hotkey').onclick = captureHotkey;
  // Сочетания голосовых заметок (20.09).
  $('btn-note-hotkey').onclick = () => captureExtraHotkey('btn-note-hotkey');
  $('btn-task-hotkey').onclick = () => captureExtraHotkey('btn-task-hotkey');
  $('btn-note-hotkey-clear').onclick = async () => {
    paintExtraHotkey('btn-note-hotkey', '');
    await applySettings($('btn-note-hotkey'));
  };
  $('btn-task-hotkey-clear').onclick = async () => {
    paintExtraHotkey('btn-task-hotkey', '');
    await applySettings($('btn-task-hotkey'));
  };
  if ($('btn-keys-hotkey')) $('btn-keys-hotkey').onclick = captureHotkey;
  // Готовые сочетания: клавишу Win окно программы может и не увидеть — её
  // перехватывает сама Windows. Кнопкой выбрать всегда можно.
  document.querySelectorAll('.js-hk').forEach((b) => {
    b.onclick = async () => {
      setHotkey(b.dataset.hk);
      await paintHotkeyButton();
    };
  });
  $('btn-dictate-history').onclick = showDictateHistory;
  $('btn-dictate-forget').onclick = forgetDictateHistory;
  // Клик по капсуле бросает начатую диктовку, ничего не вставляя.
  $('dictate-pill').onclick = () => api('/api/dictate/cancel', { method: 'POST', body: {} })
    .catch(() => {});

  document.querySelectorAll('.mode-btn').forEach((b) => {
    b.onclick = () => setMode(b.dataset.mode);
  });
  bindModelCombos();                     // раньше bindConnections: Enter в списке — выбор
  bindConnections();
  $('set-model').onchange = refreshModelsHint;
  $('set-cli-model').onchange = refreshModelsHint;
  $('set-engine').onchange = () => { refreshModelsHint(); toggleApiFields(); };
  // Модели распознавания: какие поля видны — сразу, что скачать и что не
  // нужно — после сохранения выбора (applySettings зовёт loadAsrModels).
  ['set-asr-count', 'set-asr-single', 'set-asr-calls', 'set-asr-engine'].forEach((id) => {
    $(id).addEventListener('change', paintAsrRows);
  });
  $('btn-asr-download').onclick = () => asrModelsAction('/api/asr/models/download', {}, (res) => {
    notice(res.job_id ? 'Скачиваю модели. Ход виден в панели задач слева.' : res.title, 'ok');
  });
  $('btn-asr-delete').onclick = () => asrModelsAction('/api/asr/models/cleanup',
    { action: 'delete' }, (res) => {
      notice(`Освобождено ${res.freed_mb || 0} МБ`
        + ((res.later || []).length ? '; занятое удалится после перезапуска программы' : ''), 'ok');
    });
  $('btn-asr-keep').onclick = () => asrModelsAction('/api/asr/models/cleanup', { action: 'keep' });
  $('btn-asr-reset').onclick = () => {
    if (!confirm('Сбросить модели распознавания? Останется одна точная модель (onnx-asr, '
      + 'полные веса) и рекомендованные настройки, остальные модели удалятся. Программа '
      + 'доработает на прежних моделях до перезапуска.')) return;
    asrModelsAction('/api/asr/models/reset', {}, () => {
      notice('Сбрасываю модели. Ход виден в панели задач слева.', 'ok');
    });
  };

  $('btn-refresh-models').onclick = async () => {
    await refreshModels('docs', $('btn-refresh-models'));
  };
  $('btn-cloud-asr-models').onclick = () => refreshModels('asr', $('btn-cloud-asr-models'));
  // «?» внутри <label>: щелчок не должен уводить фокус в поле адреса.
  $('btn-services').onclick = (e) => { e.preventDefault(); toggleServicesPop(); };
  $('btn-services').onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggleServicesPop(); }
  };

  // Найден ли CLI, выполнен ли вход, отвечает ли — решает служба, здесь только показ.
  $('btn-check-cli').onclick = async () => {
    const btn = $('btn-check-cli');
    const box = $('cli-check-state');
    btn.disabled = true;
    btn.textContent = 'проверяю…';
    box.className = 'tiny muted';
    box.textContent = 'Спрашиваю Claude CLI — до минуты.';
    try {
      const res = await api('/api/engines/claude_cli/check', { method: 'POST' });
      box.textContent = (res.ok ? '✓ ' : '⚠ ') + res.text;
      box.className = 'tiny' + (res.ok ? ' muted' : '');
    } catch (e) {
      box.textContent = '⚠ ' + e.message;
      box.className = 'tiny';
    } finally {
      btn.disabled = false;
      btn.textContent = 'Проверить';
    }
  };

  $('rec-title').onchange = async () => {
    if (!S.currentId) return;
    try {
      const meta = await api(`/api/recordings/${S.currentId}`, {
        method: 'PATCH', body: { title: $('rec-title').value.trim() },
      });
      S.current.meta = meta;
      paintSidebar();
    } catch (e) { notice(e.message, 'err'); }
  };
  $('cat-select').onchange = async () => {
    if (!S.currentId) return;
    try {
      const meta = await api(`/api/recordings/${S.currentId}`, {
        method: 'PATCH', body: { category: $('cat-select').value },
      });
      S.current.meta = meta; paintSidebar();
    } catch (e) { notice(e.message, 'err'); }
  };
  // Проект и теги сохраняются по уходу из поля и по Enter, а не на каждую
  // букву: иначе служба переписывала бы заметку на каждое нажатие.
  $('rec-project').onchange = () => { saveLabels(); };
  // Подсказка про папку меняется сразу, ещё до сохранения: человек должен
  // видеть, куда уедет заметка, пока правит имя проекта.
  $('rec-project').oninput = () => { paintProjectFolder(); };
  $('rec-tags').onchange = () => { saveLabels(); };
  $('rec-tags').oninput = () => { paintTagChips(); };
  $('btn-project-folder').onclick = () => openProjectFolderDialog();
  // Отбор и группировка списка (20.09). Отбор живёт, пока открыто окно:
  // спрятанный отбор, переживший перезапуск, выглядел бы как пропажа записей.
  // Складывание по проектам — привычка, её запоминаем в настройках.
  $('flt-project').onchange = () => {
    S.listFilter.project = $('flt-project').value || '';
    paintSidebar();
  };
  $('flt-tag').onchange = () => {
    S.listFilter.tag = $('flt-tag').value || '';
    paintSidebar();
  };
  $('btn-group').onclick = async () => {
    S.groupByProject = !S.groupByProject;
    paintSidebar();
    try {
      await api('/api/settings',
        { method: 'POST', body: { list_group_by_project: S.groupByProject } });
    } catch (e) { /* не сложилось сохранить — вид всё равно переключён */ }
  };
  $('pf-filter').oninput = () => {
    clearTimeout(folderTimer);
    folderTimer = setTimeout(searchFolders, 250);
  };
  $('btn-pf-save').onclick = () => saveProjectFolder(S.folderDraft || '');
  $('btn-pf-clear').onclick = () => saveProjectFolder('');
  $('rec-continues').onchange = async () => {
    if (!S.currentId) return;
    try {
      const meta = await api(`/api/recordings/${S.currentId}`, {
        method: 'PATCH', body: { continues: $('rec-continues').value },
      });
      S.current.meta = meta;
      notice($('rec-continues').value ? 'Связано с предыдущей записью' : 'Связь снята', 'ok');
    } catch (e) { notice(e.message, 'err'); paintContinues(); }
  };
  $('btn-links-open').onclick = () => openLinksDialog();
  // Отбор с задержкой: без неё запрос уходил бы на каждую букву, а обход сейфа
  // в тысячи заметок не бесплатный.
  $('links-filter').oninput = () => {
    clearTimeout(linkTimer);
    linkTimer = setTimeout(searchNotes, 250);
  };
  $('btn-links-save').onclick = async () => {
    await saveLinks(S.linkDraft || []);
    hideDialogs();
  };
  $('btn-new-cat').onclick = async () => {
    const name = prompt('Название новой категории (это будет подпапка в папке заметок):');
    if (!name) return;
    try {
      const res = await api('/api/vault/category', { method: 'POST', body: { name: name } });
      S.categories = res.categories || S.categories;
      paintCategories();
      $('cat-select').value = name.trim();
      $('cat-select').onchange();
    } catch (e) { notice(e.message, 'err'); }
  };
  $('btn-del-cat').onclick = async () => {
    const name = $('cat-select').value;
    if (!name) return;
    if (!confirm(`Убрать категорию «${name}» из списка?\n\nПапка и заметки в ней останутся на месте. `
      + 'Если добавить категорию с тем же именем снова — заметки будут складываться в ту же папку.')) return;
    try {
      const res = await api('/api/vault/category/remove', { method: 'POST', body: { name: name } });
      S.categories = res.categories || S.categories;
      paintCategories();
      notice(`Категория «${name}» убрана из списка. Папка осталась.`, 'ok');
      if (S.current && S.current.meta && S.current.meta.category === name && res.default) {
        $('cat-select').value = res.default;
        $('cat-select').onchange();
      }
    } catch (e) { notice(e.message, 'err'); }
  };

  $('btn-save').onclick = async () => {
    if (!S.currentId) return;
    try {
      const res = await api(`/api/recordings/${S.currentId}/save`, { method: 'POST' });
      notice('Сохранено: ' + (res.relative || res.path), 'ok');
      await refreshCurrent(); paintHead();
    } catch (e) { notice(e.message, 'err'); }
  };
  // Число голосов здесь больше не спрашиваем: авторазметка идёт без вопросов,
  // и ручная должна вести себя так же. Указать число можно в «Перечитать
  // точнее» — там оно и нужно, когда разметка ошиблась (решение 14.09).
  $('btn-diarize').onclick = async () => {
    if (!S.currentId) return;
    try {
      await api(`/api/recordings/${S.currentId}/diarize`, { method: 'POST', body: {} });
      notice('Разметка говорящих поставлена в очередь', 'ok');
    } catch (e) { notice(e.message, 'err'); }
  };
  $('btn-micmute').onclick = toggleMic;
  $('btn-rec-menu').onclick = openRecMenu;
  $('acts-more').onclick = openActionsMenu;
  $('btn-features-all').onclick = enableAllFeatures;
  $('btn-go-dict').onclick = () => {
    document.querySelector('#dlg-settings .tab[data-tab="t-general"]').click();
    const title = $('dict-title');
    if (title) title.scrollIntoView({ block: 'center' });
  };
  window.addEventListener('resize', fitActions);
  $('mic-select').onchange = async () => {
    const v = $('mic-select').value;
    await api('/api/settings', {
      method: 'POST', body: { mic_device_name: v === '' ? null : v },
    });
    notice('Микрофон выбран', 'ok');
  };
  $('far-select').onchange = async () => {
    const v = $('far-select').value;
    await api('/api/settings', {
      method: 'POST', body: { far_device_index: v === '' ? null : parseInt(v, 10) },
    });
    notice('Устройство собеседников выбрано', 'ok');
    paintEchoWarning();
  };
  $('btn-check-dev').onclick = async () => {
    notice('Проверяю устройства…');
    showFarWarning();
    await listDevices(true);
    const d = S.devices || {};
    const m = d.mic_probe || {}, f = d.far_probe || {};
    notice('Микрофон: ' + (m.ok ? ('работает, уровень ' + (m.peak || 0)) : ('не открылся — ' + (m.reason || '')))
      + ' | Собеседники: ' + (f.ok ? 'звук слышен' : ('не слышен — ' + (f.reason || ''))),
      (m.ok && f.ok) ? 'ok' : 'err');
  };
  $('chk-far').onchange = async () => {
    const on = $('chk-far').checked;
    const patch = { record_far: on };
    // Первый раз человек должен узнать, что в запись пойдёт весь звук машины.
    // Дальше эта полоса не мешается: отметка о показе живёт в настройках.
    const first = on && !(S.settings && S.settings.far_hint_shown);
    if (first) patch.far_hint_shown = true;
    S.settings = await api('/api/settings', { method: 'POST', body: patch });
    if (first) showFarWarning();
    else if (!on) $('far-warning').classList.add('hidden');
    paintHead();
    paintEchoWarning();
  };
  $('btn-echo').onclick = async () => {
    if (!S.currentId) return;
    try {
      await api(`/api/recordings/${S.currentId}/echo`, { method: 'POST' });
      notice('Пересчитываю эхо — это займёт секунды', 'ok');
    } catch (e) {
      notice(e.message, 'err');
    }
  };
  $('btn-retry').onclick = async () => {
    if (!S.currentId) return;
    // Точная модель может быть не скачана: спросим до того, как человек
    // выберет число голосов и будет ждать (решение 16.09). Какая именно часть
    // нужна, говорит служба: у варианта распознавания на onnx-asr она своя.
    if (!await ensurePart((S.asr && S.asr.precise_part) || 'precise')) return;
    openVoices('repass');
  };
  document.querySelectorAll('#repass-ask .tpl').forEach((t) => {
    t.onclick = () => {
      if (t.dataset.repass === 'auto') { runVoices(0); return; }
      $('repass-ask').classList.add('hidden');
      $('repass-count').classList.remove('hidden');
    };
  });
  const nums = $('repass-numbers');
  nums.innerHTML = '';
  for (let i = 1; i <= 10; i += 1) {
    const b = document.createElement('button');
    b.className = 'ghost small';
    b.textContent = String(i);
    b.onclick = () => runVoices(i);
    nums.appendChild(b);
  }
  document.querySelectorAll('#rec-tabs .rtab[data-pane]').forEach((b) => {
    b.onclick = () => showPane(b.dataset.pane);
  });
  $('btn-add-doc').onclick = openMinutes;
  // Документ собирается по стенограмме и исходник не трогает — поэтому работает
  // и тогда, когда видео уже удалено.
  $('btn-do-minutes').onclick = async () => {
    const body = { doc: S.tplChosen, video_kind: $('doc-video-kind').value };
    if (S.tplChosen === 'question') {
      const q = $('minutes-question').value.trim();
      if (!q) { notice('Напишите, что сделать', 'err'); return; }
      body.question = q;
    }
    hideDialogs();
    const title = ((S.docKinds || []).find((d) => d.key === S.tplChosen) || {}).title || 'документ';
    try {
      await api(`/api/recordings/${S.currentId}/document`, { method: 'POST', body: body });
      notice(`Готовлю: ${title}…`, 'ok');
    } catch (e) { notice(e.message, 'err'); }
  };
  // Одна кнопка с выбором вида (20.09): Markdown, оформленный текст, Telegram.
  $('btn-copy-minutes').onclick = () => openCopyMenu();
  $('btn-share').onclick = () => openShareDialog();
  $('btn-share-go').onclick = () => doShare();
  // «Свернуть» прячет только текст: вкладки и кнопки остаются, иначе
  // развернуть было бы нечем (замечено 13.09).
  $('btn-hide-minutes').onclick = () => setFold('minutes', !foldOf('minutes'));
  $('btn-tasks-out').onclick = () => openTasks();
  $('btn-tasks-close').onclick = () => $('tasks-panel').classList.add('hidden');
  $('btn-tasks-send').onclick = () => sendTasks();
  $('btn-tasks-reload').onclick = async () => {
    try {
      const r = await api('/api/tasks/projects/list');
      S.todoistProjects = r.projects || [];
      paintTaskProjects((S.tasks || {}).project);
      notice(`Проектов в Todoist: ${S.todoistProjects.length}`, 'ok');
    } catch (e) { notice(e.message, 'err'); }
  };
  $('btn-drop-clear').onclick = () => {
    const cur = (S.docs || []).find((d) => d.key === S.docShown) || (S.docs || [])[0];
    if (cur) saveDropped(cur.key, []);
  };
  // Пересборка учитывает вычеркнутое: список уходит модели с запретом повторять.
  $('btn-drop-redo').onclick = async () => {
    const cur = (S.docs || []).find((d) => d.key === S.docShown) || (S.docs || [])[0];
    if (!cur || !S.currentId) return;
    if (!confirm(`Собрать «${cur.title}» заново? Нынешний текст документа будет заменён.`)) return;
    try {
      await api(`/api/recordings/${S.currentId}/document`,
                { method: 'POST', body: { doc: cur.key } });
      notice('Пересобираю документ без вычеркнутого…', 'ok');
    } catch (e) { notice(e.message, 'err'); }
  };
  $('btn-edit-transcript').onclick = () => setEditMode(!S.editMode);
  // Alt+↑ / Alt+↓ — передать выделенные слова соседней реплике.
  document.addEventListener('keydown', (e) => {
    if (!S.editMode || !e.altKey || e.ctrlKey || e.shiftKey) return;
    if (e.key !== 'ArrowUp' && e.key !== 'ArrowDown') return;
    e.preventDefault();
    moveWords(e.key === 'ArrowUp' ? 'prev' : 'next');
  });
  // Ctrl+Z — отменить правку, шаг за шагом. Только в режиме правки: вне его
  // сочетание должно работать как обычно (например, в полях ввода).
  // Клавишу узнаём по её месту на клавиатуре (code), а не по букве: в русской
  // раскладке та же клавиша даёт «я», и проверка по 'z' не срабатывала вовсе
  // (найдено 16.09).
  document.addEventListener('keydown', (e) => {
    const zKey = e.code === 'KeyZ' || ['z', 'я'].includes(String(e.key).toLowerCase());
    if (!S.editMode || !e.ctrlKey || e.altKey || !zKey) return;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target.tagName || '').toUpperCase())) return;
    e.preventDefault();
    undoEdit();
  });
  // Enter — отдать выделенное любому говорящему записи.
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') hideGiveMenu();
    if (!S.editMode || e.key !== 'Enter' || e.altKey || e.ctrlKey) return;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes((e.target.tagName || '').toUpperCase())) return;
    e.preventDefault();
    openGiveMenu();
  });
  document.addEventListener('mousedown', (e) => {
    if (!e.target.closest || !e.target.closest('#give-menu')) hideGiveMenu();
  });
  $('btn-undo-edit').onclick = undoEdit;
  $('btn-hide').onclick = openHideMenu;
  $('btn-copy-transcript').onclick = () => {
    const segs = (S.current && S.current.segments) || [];
    if (!segs.length) { notice('Стенограмма пуста', 'err'); return; }
    const text = segs.map((s) => `[${fmtClock(s.start)}] ${displaySpeaker(s.speaker)}: ${s.text || ''}`).join('\n');
    navigator.clipboard.writeText(text).then(() => notice('Стенограмма скопирована', 'ok'));
  };
  $('btn-delete').onclick = () => openDeleteDialog();
  // Delete — то же окно, что и кнопка «Удалить» (20.09). Только когда человек
  // и правда в списке записей: в поле ввода, в открытом окне и в режиме правки
  // стенограммы эта клавиша занята своим делом.
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Delete' || e.ctrlKey || e.altKey || e.shiftKey) return;
    if (S.editMode) return;
    const tag = (e.target.tagName || '').toUpperCase();
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(tag) || e.target.isContentEditable) return;
    if (!$('overlay').classList.contains('hidden')) return;   // открыто какое-то окно
    if (!pickedIds().length) return;
    e.preventDefault();
    openDeleteDialog();
  });
  $('del-choices').querySelectorAll('.tpl').forEach((el) => {
    el.onclick = () => {
      if (el.classList.contains('off')) return;
      deleteRecording(el.dataset.scope);
    };
  });
  $('btn-save-speaker').onclick = () => saveSpeakerDialog({});
  // Переключение «только в этой записи» меняет, какую роль показывают поля.
  $('speaker-role-once').onchange = () => {
    const role = (S.speakerCtx && S.speakerCtx.role) || {};
    const once = $('speaker-role-once').checked;
    $('speaker-side').value = once ? (role.rec_side || '') : (role.side || '');
    $('speaker-position').value = once ? (role.rec_position || '') : (role.position || '');
    paintSpeakerRoleNote();
  };
  let speakerTimer = null;
  $('speaker-name').addEventListener('input', () => {
    const ctx = S.speakerCtx;
    if (!ctx) return;
    ctx.picked = null;
    ctx.decision = {};
    ctx.acClosed = false;
    $('speaker-picked').textContent = '';
    hideConflict();
    clearTimeout(speakerTimer);
    speakerTimer = setTimeout(speakerSearch, 150);
  });
  $('speaker-name').addEventListener('keydown', (e) => {
    const ctx = S.speakerCtx;
    if (!ctx) return;
    const n = ctx.items.length;
    if (e.key === 'ArrowDown' && n) { ctx.acClosed = false; ctx.active = (ctx.active + 1) % n; paintSpeakerAc(); e.preventDefault(); }
    else if (e.key === 'ArrowUp' && n) { ctx.acClosed = false; ctx.active = (ctx.active - 1 + n) % n; paintSpeakerAc(); e.preventDefault(); }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (!ctx.acClosed && ctx.active >= 0) pickSpeakerItem(ctx.items[ctx.active]);
      else saveSpeakerDialog({});
    } else if (e.key === 'Escape' && !ctx.acClosed) { ctx.acClosed = true; paintSpeakerAc(); e.stopPropagation(); }
  });
  $('speaker-name').addEventListener('focus', () => {
    const ctx = S.speakerCtx;
    if (ctx) { ctx.acClosed = false; paintSpeakerAc(); }
  });
  $('speaker-name').addEventListener('blur', () => {
    const ctx = S.speakerCtx;
    if (ctx) { ctx.acClosed = true; paintSpeakerAc(); }
  });
  $('btn-speaker-split').onclick = () => {
    const ctx = S.speakerCtx;
    if (!ctx) return;
    hideDialogs();
    splitSpeaker(ctx.key);
  };
  $('btn-speaker-unsplit').onclick = () => {
    const ctx = S.speakerCtx;
    if (!ctx) return;
    hideDialogs();
    const base = ctx.key.split('~')[0];
    if (base === 'me') setRoom(false); else unsplitSpeaker(base);
  };
  $('chk-room').onchange = () => setRoom($('chk-room').checked);
  $('chk-absent').onchange = () => setOwnerAbsent($('chk-absent').checked);
  document.querySelectorAll('[data-theme-pick]').forEach((b) => {
    b.onclick = () => saveView({ ui_theme: b.dataset.themePick });
  });
  document.querySelectorAll('[data-density-pick]').forEach((b) => {
    b.onclick = () => saveView({ ui_density: b.dataset.densityPick });
  });
  $('btn-devices-fold').onclick = () => {
    S.devicesUnfolded = true;
    $('live-controls').classList.remove('folded');
  };
  ['audio', 'videos'].forEach((what) => {
    $('btn-reveal-' + what).onclick = async () => {
      try { await api('/api/storage/reveal', { method: 'POST', body: { what: what } }); } catch (e) { notice(e.message, 'err'); }
    };
  });
  $('btn-voices-clear').onclick = async () => {
    const n = (S.voices || []).length;
    if (!n) { notice('База голосов и так пустая'); return; }
    if (!confirm(`Очистить базу голосов: удалить всех (${n}) и все образцы голоса?\n\nПеред очисткой сохранится копия — её можно вернуть кнопкой «Вернуть из копии».`)) return;
    const res = await voicesAction('/api/voices/clear', { method: 'POST' });
    if (res.status === 200) notice(`База голосов очищена (было ${res.data.result.removed}). Копия сохранена.`, 'ok');
    else notice(res.data.detail || `Ошибка ${res.status}`, 'err');
    loadVoices();
  };
  $('btn-voices-restore').onclick = async () => {
    const name = $('voices-backup-select').value;
    if (!name) { notice('Копий пока нет'); return; }
    const label = $('voices-backup-select').selectedOptions[0].textContent;
    if (!confirm(`Вернуть базу голосов из копии «${label}»?\n\nТекущая база перед этим тоже сохранится копией.`)) return;
    const res = await voicesAction('/api/voices/restore', { method: 'POST', body: { name: name } });
    if (res.status === 200) notice(`База возвращена из копии: людей ${res.data.result.people}`, 'ok');
    else notice(res.data.detail || `Ошибка ${res.status}`, 'err');
    loadVoices();
  };


  // Кнопка и поле ссылки теперь в разделе «Видео», обработчики — в video.js.

  // Файл можно бросать в любое место окна: удобнее, чем целиться в рамку.
  // Бросок сам переводит в раздел «Видео» — файлы обрабатываются там.
  const dz = $('dropzone');
  ['dragenter', 'dragover'].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      if (dz) dz.classList.add('over');
    }));
  ['dragleave', 'drop'].forEach((ev) =>
    document.addEventListener(ev, (e) => {
      e.preventDefault();
      if (ev === 'dragleave' && e.relatedTarget) return;
      if (dz) dz.classList.remove('over');
    }));
  // Бросок только КЛАДЁТ файлы в список раздела «Видео». Обработка начинается
  // кнопкой «Обработать»: до неё человек видит, что выбрал, и выставляет
  // настройки. Во время записи список пополнять можно — нельзя обрабатывать,
  // и кнопка в это время серая.
  document.addEventListener('drop', (e) => {
    e.preventDefault();
    const files = Array.from((e.dataTransfer && e.dataTransfer.files) || []);
    if (!files.length) return;
    if (window.videoPickFiles) window.videoPickFiles(files);
    if (S.recordingId) {
      // Идёт совещание — экран не трогаем: уводить человека от живой
      // стенограммы и кнопки «Стоп» нельзя. Файлы просто ждут в списке.
      notice('Файлы отложены в раздел «Видео» — обработаю после «Стоп».', '');
      return;
    }
    setMode('video');
  });

  if (window.videoInit) window.videoInit();

  // Предупреждать при закрытии вкладки не нужно: звук берёт служба, запись
  // продолжится. Это требование ТЗ «служба переживает закрытие вкладки».
}

/* ====================================================== старт */

(async function main() {
  bind();
  connectEvents();
  try {
    await loadState();
  } catch (e) {
    S.asr = { state: 'offline', error: String(e.message || e) };
    paintProgramState();
    notice('Служба не отвечает: ' + e.message, 'err');
  }
  loadLabels();
  bindAbout();
  noticeLastUpdate();
  // Диктовка могла уже идти, когда окно открыли: капсула должна это показать.
  try { S.dictate = await api('/api/dictate'); paintDictate(); } catch (e) {}
  await listDevices(false);
  loadMic();
  navigator.mediaDevices.addEventListener('devicechange', () => { listDevices(false); loadMic(); });
  // Опрос задач раз в 8 с дублирует события «job» — и оставлен намеренно
  // (аудит предлагал убрать его как дубль). Событие приходит по открытому
  // соединению, а оно умеет не рваться, но и не доставлять ничего: тогда блок
  // «В работе» застыл бы навсегда, и человек не узнал бы, что документ готов.
  // Цена — один короткий запрос к своей же службе раз в восемь секунд.
  setInterval(async () => {
    if (!document.hidden) {
      try { S.jobs = await api('/api/jobs'); paintJobs(); paintHead(); } catch (e) {}
      // Микрофон гасят и клавишей на ноутбуке — кнопка не должна врать.
      loadMic();
    }
  }, 8000);
})();
