/* Раздел «Видео»: готовые файлы, ссылки и сторонние площадки.
 *
 * Почему отдельным файлом: app.js рисует экран записи и переписывает разметку
 * целиком на каждое событие службы. Форма так работать не может — введённый
 * текст и фокус пропадали бы при каждой реплике. Поэтому здесь разметка
 * СТАТИЧЕСКАЯ, а меняются только значения полей и класс hidden.
 *
 * Файл подключается перед app.js и ничего не делает сам: app.js вызывает
 * videoInit() после того, как навесит свои обработчики.
 */

/* global $, api, notice, esc, S */

const V = {
  src: 'files',          // выбранный источник
  opts: null,            // ответ /api/media/options
  probed: null,          // что ответила проверка ссылки
  lines: [],             // свой лог раздела
  files: [],             // выбранные файлы, ждут кнопки «Обработать»
  busy: false,           // идёт отправка — второй раз не отправляем
  kindTouched: false,    // тип записи выбран руками — не подсказываем свой
  catTouched: false,     // категория выбрана руками — тип её больше не меняет
};

/* Какие поля формы нужны — одна таблица на всю форму: источник и «что
   сохранить» решают, что показывать. Спрятанное поле в задание не уходит.
     качество видео — только когда видео скачивается и сохраняется: у файла с
       диска качать нечего, а при «только текст» и «только звук» видео нет;
     автоматические субтитры — только у открытой ссылки (это YouTube);
     «только скачать» — у всего, что скачивается, а не у файла с диска. */
const V_FIELDS = {
  'v-height-field': () => ['link', 'gcvh', 'webauth'].includes(V.src) && $('v-store').value === 'video',
  'v-auto-subs-field': () => V.src === 'link',
  'v-only-field': () => V.src !== 'files',
};

function vPaintFields() {
  Object.entries(V_FIELDS).forEach(([id, show]) => {
    const el = $(id);
    if (el) el.classList.toggle('hidden', !show());
  });
}

function vShown(id) {
  const el = $(id);
  return !!el && !el.classList.contains('hidden');
}

function vLog(text, level) {
  V.lines.push({ text: String(text), level: level || '' });
  if (V.lines.length > 200) V.lines.shift();
  const box = $('video-log');
  if (!box) return;
  box.innerHTML = V.lines.map((l) =>
    `<div class="vlog ${l.level}">${esc(l.text)}</div>`).join('');
  box.scrollTop = box.scrollHeight;
}

/* Настройки задания: ровно то, что стоит в форме. */
function vOpts() {
  return {
    category: $('v-category').value || undefined,
    video_kind: $('v-kind').value,
    store_media: $('v-store').value,
    asr_lang: $('v-lang').value,
    // Спрятанное поле не участвует: качество — по умолчанию службы, флажки — нет.
    max_height: vShown('v-height-field') ? (parseInt($('v-height').value, 10) || 720) : undefined,
    prefer_transcript: $('v-subs').checked,
    yt_auto_subs: vShown('v-auto-subs-field') && $('v-auto-subs').checked,
    video_only: vShown('v-only-field') && $('v-only').checked,
    make_summary: $('v-summary').checked,
  };
}

/* Что тип записи подставляет. Именно ПОДСТАВЛЯЕТ: любое поле можно поменять
   руками, программа не станет возвращать своё. */
const V_KINDS = {
  meeting: { store: 'video', diarize: true, doc: 'краткое содержание встречи' },
  lecture: { store: 'none', diarize: false, doc: 'конспект' },
  interview: { store: 'audio', diarize: true, doc: 'выжимка' },
  transcript: { store: 'none', diarize: false, doc: '' },
};

/* Какой тип предложить для выбранного источника. Угадываем по опыту:
   записи Teams и SharePoint — это совещания, ссылки и курсы — чаще лекции.
   Человек всегда может поправить. */
const V_SRC_KIND = {
  files: 'meeting', sharepoint: 'meeting',
  link: 'lecture', gcvh: 'lecture', webauth: 'lecture',
};

function vKindNote() {
  const kind = $('v-kind').value;
  const k = V_KINDS[kind] || V_KINDS.meeting;
  // Подсказка — о результате, а не от лица программы.
  let text;
  if (k.diarize) text = k.doc ? `Будет разметка голосов и ${k.doc}.` : 'Будет разметка голосов, без документа.';
  else text = k.doc ? `Без разметки голосов. Будет ${k.doc}.` : 'Без разметки голосов и без документа.';
  const box = $('v-kind-note');
  if (box) box.textContent = text;
  const sum = $('v-summary');
  if (sum) {
    sum.disabled = !k.doc;
    if (!k.doc) sum.checked = false;
  }
}

/* Тип подставляет «что сохранить». Вызываем при смене типа — не при каждой
   перерисовке: иначе выбор человека молча возвращался бы к умолчанию. */
function vApplyKind() {
  const k = V_KINDS[$('v-kind').value] || V_KINDS.meeting;
  $('v-store').value = k.store;
  vApplyCategory();
  vKindNote();
  vAsrNote();
  vPaintFields();
}

/* Категория заметки по умолчанию — от типа записи (решает служба): лекция не
   должна уезжать во «Встречи». Выбрали руками — тип её больше не трогает. */
function vApplyCategory() {
  const byKind = ((V.opts || {}).category_by_kind) || {};
  const cat = byKind[$('v-kind').value];
  if (!cat || V.catTouched) return;
  const sel = $('v-category');
  // Категории из настроек может не быть в списке (например, «Видео») —
  // добавляем, иначе список молча показал бы первую попавшуюся.
  if (![...sel.options].some((o) => o.value === cat)) {
    const o = document.createElement('option');
    o.value = cat;
    o.textContent = cat;
    sel.appendChild(o);
  }
  sel.value = cat;
}

/* Подсказка о распознавании: где (выбирается в настройках, 21.09), честно про
   доступность и цену. */
function vAsrNote() {
  const o = V.opts || {};
  const lang = $('v-lang').value;
  const where = (o.defaults || {}).asr_files || 'local';
  const box = $('v-asr-note');
  if (!box) return;
  if ($('v-only').checked) {
    box.textContent = 'Выбрано «только скачать» — распознавание не запустится.';
    return;
  }
  if (where === 'cloud') {
    box.textContent = (o.cloud && o.cloud.ready)
      ? 'Распознаётся в облаке — так выбрано в «Настройки → Модели». Звук уйдёт по сети '
        + 'в выбранный сервис по вашему ключу.'
      : `В настройках выбрано облако, но оно недоступно: ${(o.cloud && o.cloud.why) || 'не настроено'}`;
    return;
  }
  if (lang === 'en') {
    box.textContent = (o.english && o.english.ready)
      ? 'Английский распознаётся на этом компьютере: час записи — около трёх минут, бесплатно.'
      : `Английская модель не готова: ${(o.english && o.english.why) || ''}`;
    // Модели нет — предложим скачать сразу, а не в середине обработки.
    if (!(o.english && o.english.ready) && window.ensurePart) window.ensurePart('english');
    return;
  }
  box.textContent = 'Русский распознаётся на этом компьютере, бесплатно. '
    + 'Где распознавать — «Настройки → Модели».';
}

async function vLoadOptions() {
  const first = V.opts === null;      // первый заход в раздел за сеанс
  try {
    V.opts = await api('/api/media/options');
  } catch (e) {
    vLog('Не удалось прочитать настройки раздела: ' + e.message, 'err');
    return;
  }
  const d = V.opts.defaults || {};
  // Поля формы заполняем ТОЛЬКО в первый раз. Раздел перечитывает настройки на
  // каждое переключение «Запись»/«Видео», и выставленные руками язык, качество
  // и категория молча возвращались бы к умолчаниям — а обработка читает их в
  // момент нажатия «Обработать».
  if (first) {
    $('v-lang').value = d.asr_lang || 'ru';
    $('v-height').value = String(d.max_height || 720);
    $('v-subs').checked = !!d.prefer_transcript;
    $('v-auto-subs').checked = !!d.yt_auto_subs;
    $('v-only').checked = !!d.video_only;
    $('v-summary').checked = !!d.make_summary;
    const cats = V.opts.categories || [];
    $('v-category').innerHTML = cats.map((c) =>
      `<option value="${esc(c)}"${c === d.category ? ' selected' : ''}>${esc(c)}</option>`).join('');
    vApplyKind();          // «что сохранить» подставляется из типа записи
  }
  vAsrNote();              // где распознавать, меняется в настройках между заходами
  // Куда складываются видео, видно в «Настройки → Общие → Место на диске»:
  // путь к папке — сведение о хранилище, а не часть формы.
  const web = V.opts.webauth || {};
  $('web-state').textContent = web.ready
    ? 'Готово: браузер установлен.'
    : (web.why || 'Для сайтов с паролем нужен браузер для входа.');
  vSpState((V.opts.sharepoint || {}).logged_in);
  vAsrNote();
}

function vSpState(loggedIn) {
  const box = $('sp-state');
  if (!box) return;
  box.textContent = loggedIn
    ? 'Вход в Microsoft выполнен — можно искать записи.'
    : 'Вход в Microsoft не выполнен. Поиск записей без него не работает.';
  $('btn-sp-login').textContent = loggedIn ? 'Войти заново' : 'Войти в Microsoft';
}

function vPickSource(name) {
  V.src = name;
  document.querySelectorAll('.src-btn').forEach((b) =>
    b.classList.toggle('active', b.dataset.src === name));
  ['files', 'link', 'gcvh', 'sharepoint', 'webauth'].forEach((s) => {
    const pane = $('src-' + s);
    if (pane) pane.classList.toggle('hidden', s !== name);
  });
  // Тип подставляем по источнику, но только пока человек не выбрал его сам:
  // свой выбор важнее наших догадок.
  if (!V.kindTouched) {
    $('v-kind').value = V_SRC_KIND[name] || 'meeting';
    vApplyKind();
  }
  vPaintFields();
  vPaintFiles();
}

/* Во время записи раздел гаснет: процессор один, и распознавание чужого файла
   посреди совещания съело бы эфир. Служба это тоже проверяет — здесь лишь
   честная подсказка, чтобы не нажимать зря. */
function videoPaint(recording) {
  const ids = ['btn-link', 'btn-link-probe', 'btn-gcvh', 'btn-webauth',
    'btn-sp-search', 'btn-sp-process', 'link-url', 'gcvh-url'];
  ids.forEach((id) => { const el = $(id); if (el) el.disabled = !!recording; });
  const dz = $('dropzone');
  if (dz) {
    dz.classList.toggle('off', !!recording);
    dz.textContent = recording
      ? 'Идёт запись — обработка видео начнётся после «Стоп»'
      : 'Перетащите сюда видео, аудио или готовые субтитры (.vtt, .srt)';
  }
  // Список файлов при этом не теряется: человек соберёт его во время записи,
  // а нажмёт «Обработать» после «Стоп».
  const proc = $('btn-process');
  if (proc) proc.disabled = !!recording || V.busy;
}

/* ------------------------------------------------ выбранные файлы */

function vSize(bytes) {
  const n = Number(bytes) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(1) + ' ГБ';
  if (n >= 1048576) return Math.round(n / 1048576) + ' МБ';
  return Math.max(1, Math.round(n / 1024)) + ' КБ';
}

/* Файлы кладутся в список и ждут. Раньше перетаскивание запускало обработку
   сразу, и настройки под ним (язык, где распознавать, качество) применить было
   уже некуда — они читаются в момент отправки. */
function vPickFiles(files) {
  // Брошенный файл — это вкладка «Файлы с диска»: её параметры и её кнопка.
  if (V.src !== 'files') vPickSource('files');
  const add = Array.from(files || []);
  let skipped = 0;
  add.forEach((f) => {
    const same = V.files.some((x) => x.name === f.name && x.size === f.size);
    if (same) { skipped += 1; return; }
    V.files.push(f);
  });
  if (skipped) vLog(`Уже в списке, пропущено: ${skipped}`, '');
  vPaintFiles();
}

function vDropFile(i) {
  V.files.splice(i, 1);
  vPaintFiles();
}

function vPaintFiles() {
  const box = $('v-files');
  const row = $('v-files-row');
  if (!box || !row) return;
  // Список и «Обработать» — только у файлов с диска: у ссылки своя кнопка.
  const has = V.files.length > 0 && V.src === 'files';
  box.classList.toggle('hidden', !has);
  row.classList.toggle('hidden', !has);
  if (!has) { box.innerHTML = ''; return; }

  box.innerHTML = V.files.map((f, i) =>
    `<div class="file-item">
       <span class="file-name">${esc(f.name)}</span>
       <span class="tiny muted">${vSize(f.size)}</span>
       <button class="icon-btn js-drop" data-i="${i}" title="Убрать из списка" aria-label="Убрать из списка" type="button"><svg aria-hidden="true"><use href="/static/icons.svg#ic-x"></use></svg></button>
     </div>`).join('');
  box.querySelectorAll('.js-drop').forEach((b) => {
    b.onclick = () => vDropFile(parseInt(b.dataset.i, 10));
  });
  const total = V.files.reduce((s, f) => s + (f.size || 0), 0);
  $('v-files-hint').textContent =
    `${V.files.length} ${V.files.length === 1 ? 'файл' : 'файла(ов)'} · ${vSize(total)}`;
}

/* Отправка: настройки читаются здесь, в момент нажатия «Обработать». */
async function vProcess() {
  if (V.busy || !V.files.length) return;
  const btn = $('btn-process');
  V.busy = true;
  if (btn) btn.disabled = true;
  const opts = JSON.stringify(vOpts());
  const queue = V.files.slice();
  const failed = [];
  let sent = 0;
  try {
    for (const f of queue) {
      // Перед каждой отправкой сверяемся со списком: «Очистить» и крестик во
      // время работы обязаны отменять то, что ещё не ушло.
      if (!V.files.some((x) => x.name === f.name && x.size === f.size)) {
        vLog(`«${f.name}» убран из списка — пропущен`);
        continue;
      }
      const fd = new FormData();
      fd.append('file', f, f.name);
      vLog(`Загружается «${f.name}»…`);
      try {
        await api('/api/media/upload?opts=' + encodeURIComponent(opts),
          { method: 'POST', body: fd });
        vLog(`«${f.name}» принят в обработку`, 'ok');
        sent += 1;
        // Запись НЕ открываем: с несколькими файлами экран прыгал бы на
        // последний. Ход работы виден в списке слева и в блоке «В работе».
        V.files = V.files.filter((x) => !(x.name === f.name && x.size === f.size));
        vPaintFiles();
      } catch (e) {
        failed.push(f.name);
        vLog(`«${f.name}»: ${e.message}`, 'err');
        notice(e.message, 'err');
      }
    }
  } finally {
    V.busy = false;
    // Доступность считаем из состояния, а не руками: пока шла отправка, могла
    // начаться запись — тогда кнопка обязана остаться серой.
    videoPaint(!!(typeof S !== 'undefined' && S.recordingId));
    vPaintFiles();
  }
  if (sent && !failed.length) notice('Взято в работу: ' + sent, 'ok');
}

async function vProbe() {
  const url = ($('link-url').value || '').trim();
  if (!url) { notice('Вставьте ссылку', 'err'); return null; }
  $('link-hint').textContent = 'Ссылка читается…';
  try {
    const info = await api('/api/media/probe', { method: 'POST', body: { url } });
    V.probed = info;
    const mins = info.duration_s ? Math.round(info.duration_s / 60) : 0;
    const subs = [];
    if ((info.subs_manual || []).length) subs.push('есть готовые субтитры');
    else if ((info.subs_auto || []).length) subs.push('есть автоматические субтитры');
    else subs.push('субтитров нет — потребуется распознавание');
    if (info.proxy) subs.push('открылось через прокси');
    $('link-hint').textContent = `«${info.title}»`
      + (mins ? `, около ${mins} мин` : '') + '. ' + subs.join(', ') + '.';
    return info;
  } catch (e) {
    $('link-hint').textContent = e.message;
    notice(e.message, 'err');
    return null;
  }
}

async function vSubmitLink() {
  const url = ($('link-url').value || '').trim();
  if (!url) { notice('Вставьте ссылку', 'err'); return; }
  let info = V.probed;
  if (!info || info.webpage_url !== url) info = await vProbe();
  try {
    const res = await api('/api/media/link',
      { method: 'POST', body: Object.assign({ url, info }, vOpts()) });
    vLog(`Взято в работу: ${(res.meta || {}).title || url}`, 'ok');
    $('link-url').value = '';
    V.probed = null;
    if (res.rec_id) await window.openRecording(res.rec_id);
  } catch (e) { vLog(e.message, 'err'); notice(e.message, 'err'); }
}

async function vSubmitSource(kind, body) {
  try {
    const res = await api('/api/media/source',
      { method: 'POST', body: Object.assign({ kind }, body, vOpts()) });
    vLog(`Взято в работу: ${(res.meta || {}).title || kind}`, 'ok');
    if (res.rec_id) await window.openRecording(res.rec_id);
  } catch (e) { vLog(e.message, 'err'); notice(e.message, 'err'); }
}

/* ------------------------------------------------ SharePoint и Teams */

const SP_MONTHS = ['январь', 'февраль', 'март', 'апрель', 'май', 'июнь', 'июль',
  'август', 'сентябрь', 'октябрь', 'ноябрь', 'декабрь'];

V.sp = { items: [], sel: new Set(), batch: null, jobs: [], hidden: 0, pendingSearch: false };

function vSpFilters() {
  const now = new Date();
  const opts = SP_MONTHS.map((m, i) => `<option value="${i + 1}">${m}</option>`).join('');
  $('sp-m1').innerHTML = opts;
  $('sp-m2').innerHTML = opts;
  $('sp-year').value = now.getFullYear();
  $('sp-m1').value = '1';
  $('sp-m2').value = String(now.getMonth() + 1);
}

function vSpShowLogin(l) {
  if (!l) { $('sp-login').classList.add('hidden'); return; }
  $('sp-login').classList.remove('hidden');
  const url = l.url || 'https://microsoft.com/devicelogin';
  $('sp-url').href = url;
  $('sp-url').textContent = url;
  $('sp-code').textContent = l.code || '';
  const mins = Math.max(1, Math.round((l.expires_in || 900) / 60));
  $('sp-login-note').textContent =
    `Код действует ${mins} мин. Когда войдёте в браузере, здесь появится «Вход выполнен».`;
}

async function vSpStatus() {
  try {
    const st = await api('/api/sharepoint/status');
    vSpState(st.logged_in);
    vSpShowLogin(st.logged_in ? null : st.pending);
  } catch (e) { /* служба ответит при следующем заходе */ }
}

async function vSpLogin() {
  try {
    const res = await api('/api/sharepoint/login', { method: 'POST' });
    vSpShowLogin(res.login || {});
    vLog('Откройте ссылку и введите код — вход в Microsoft', 'ok');
  } catch (e) { vLog(e.message, 'err'); notice(e.message, 'err'); }
}

/* Событие службы о входе: ждём / вошли / код протух */
function vSpEvent(ev) {
  if (ev.state === 'waiting') { vSpShowLogin(ev.login); return; }
  if (ev.state === 'ok') {
    vSpShowLogin(null); vSpState(true); vLog(ev.text, 'ok');
    // «Найти» нажимали до входа — ищем сами, второй раз жать не нужно
    if (V.sp.pendingSearch) { V.sp.pendingSearch = false; vSpSearch(); }
    return;
  }
  V.sp.pendingSearch = false;
  $('sp-login-note').textContent = ev.text || '';
  $('sp-results').innerHTML = `<div class="tiny">${esc(ev.text || 'Вход не выполнен')} Нажмите «Найти» ещё раз — будет новый код.</div>`;
  vLog(ev.text || 'Вход не выполнен', 'err');
}

function vSpMinutes(s) {
  const n = Math.round((Number(s) || 0) / 60);
  return n ? `${n} мин` : '';
}

function vSpRender() {
  const items = V.sp.items;
  const box = $('sp-results');
  $('sp-head').classList.toggle('hidden', !items.length);
  $('sp-foot').classList.toggle('hidden', !items.length);
  if (!items.length) {
    box.innerHTML = '<div class="tiny muted">Ничего не нашлось.'
      + (V.sp.hidden ? ` Скрыто не встреч: ${V.sp.hidden} — снимите галочку «только записи встреч».` : '')
      + '</div>';
    return;
  }
  box.innerHTML = items.map((it, i) => {
    const marks = (it.has_video ? '🎬' : '') + (it.has_transcript === true ? '📝'
      : (it.has_transcript === null && it.checking ? '<span class="muted">…</span>' : ''));
    const info = [it.date || it.modified, vSpMinutes(it.duration_s),
      it.has_video ? vSize(it.size || (it.sizeMB || 0) * 1048576) : 'без видео'].filter(Boolean).join(' · ');
    const p = it.processed;
    const done = !p ? ''
      : (p.ok === false
        ? `<span class="tiny" title="Прошлая попытка обработать эту запись не удалась — её можно выбрать снова">⚠ не получилось <a href="#" class="js-sp-open" data-rec="${esc(p.rec_id)}">открыть</a></span>`
        : `<span class="tiny">✓ уже есть <a href="#" class="js-sp-open" data-rec="${esc(p.rec_id)}">открыть</a></span>`);
    return `<label class="sp-item${p && p.ok !== false ? ' done' : ''}">
      <input type="checkbox" class="js-sp-pick" data-i="${i}"${V.sp.sel.has(i) ? ' checked' : ''}>
      <span class="sp-marks">${marks || '·'}</span>
      <span class="sp-name" title="${esc(it.name)}">${esc(it.name)}</span>
      <span class="tiny muted">${esc(info)}</span>${done}</label>`;
  }).join('');
  box.querySelectorAll('.js-sp-pick').forEach((c) => {
    c.onchange = () => {
      const i = parseInt(c.dataset.i, 10);
      if (c.checked) V.sp.sel.add(i); else V.sp.sel.delete(i);
      vSpCount();
    };
  });
  box.querySelectorAll('.js-sp-open').forEach((a) => {
    a.onclick = async (e) => { e.preventDefault(); await window.openRecording(a.dataset.rec); };
  });
  vSpCount();
}

function vSpCount() {
  const n = V.sp.items.length;
  const k = V.sp.sel.size;
  const done = V.sp.items.filter((x) => x.processed && x.processed.ok !== false).length;
  $('sp-count').textContent = `Найдено: ${n}` + (done ? `, уже обработано: ${done}` : '')
    + (V.sp.hidden ? `, скрыто не встреч: ${V.sp.hidden}` : '') + ` · выбрано: ${k}`;
  $('btn-sp-process').querySelector('.lbl').textContent = k ? `Обработать выбранные (${k})` : 'Обработать выбранные';
  $('btn-sp-process').disabled = !k || !!(typeof S !== 'undefined' && S.recordingId);
}

async function vSpSearch() {
  // Как в старой версии: «Найти» без входа сама начинает вход по коду, а
  // после подтверждения в браузере поиск запускается без второго нажатия.
  // Раньше здесь писалось «войдите и повторите», и казалось, что вход не
  // запускается вовсе (замечено 13.09).
  let st = null;
  try { st = await api('/api/sharepoint/status'); } catch (e) { st = null; }
  if (st && !st.logged_in) {
    V.sp.pendingSearch = true;
    $('sp-head').classList.add('hidden');
    $('sp-foot').classList.add('hidden');
    $('sp-results').innerHTML = '<div class="tiny">Сначала вход в Microsoft: откройте ссылку выше и введите код. '
      + 'Поиск запустится сам, как только вход подтвердится.</div>';
    if (st.pending && st.pending.code) vSpShowLogin(st.pending);
    else await vSpLogin();
    $('sp-login').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    return;
  }
  const body = {
    query: ($('sp-query').value || '').trim(),
    year: parseInt($('sp-year').value, 10) || null,
    month_from: parseInt($('sp-m1').value, 10) || 1,
    month_to: parseInt($('sp-m2').value, 10) || 12,
    meetings_only: $('sp-meetings').checked,
  };
  $('sp-results').innerHTML = '<div class="tiny muted">Ищу…</div>';
  $('btn-sp-search').disabled = true;
  try {
    const res = await api('/api/sharepoint/search', { method: 'POST', body });
    V.sp.items = res.items || [];
    V.sp.hidden = res.hidden || 0;
    V.sp.sel = new Set();       // ничего не выбрано само: пачка — только по воле человека
    vSpRender();
    vSpCheckTranscripts();
  } catch (e) {
    $('sp-head').classList.add('hidden');
    $('sp-foot').classList.add('hidden');
    if (/Вход в Microsoft/.test(e.message)) {
      // вход истёк между проверкой и поиском — тот же путь, что без входа
      V.sp.pendingSearch = true;
      $('sp-results').innerHTML = `<div class="tiny">${esc(e.message)} Откройте ссылку выше и введите код — поиск запустится сам.</div>`;
      await vSpLogin();
      return;
    }
    $('sp-results').innerHTML = `<div class="tiny">${esc(e.message)}</div>`;
  } finally {
    $('btn-sp-search').disabled = false;
  }
}

/* Расшифровку Teams поиск не видит — спрашиваем про записи порциями */
async function vSpCheckTranscripts() {
  // И записи «без видео» тоже: пустой mp4 Teams создаёт ровно затем, чтобы
  // прицепить к нему расшифровку.
  const todo = V.sp.items.filter((x) => x.kind !== 'transcript' && x.has_transcript !== true);
  const snapshot = V.sp.items;
  for (let i = 0; i < todo.length; i += 10) {
    const part = todo.slice(i, i + 10);
    part.forEach((x) => { x.checking = true; });
    vSpRender();
    try {
      const res = await api('/api/sharepoint/transcripts', {
        method: 'POST',
        body: { items: part.map((x) => ({ id: x.id, driveId: x.driveId, web_url: x.web_url })) },
      });
      if (V.sp.items !== snapshot) return;      // пока проверяли, искали заново
      part.forEach((x) => {
        const flag = (res.transcripts || {})[x.id];
        if (flag === true) x.has_transcript = true;
        else if (flag === false) x.has_transcript = false;
        x.checking = false;
      });
    } catch (e) {
      part.forEach((x) => { x.checking = false; });
      vSpRender();
      return;
    }
    vSpRender();
  }
}

async function vSpProcess() {
  const picked = Array.from(V.sp.sel).sort((a, b) => a - b).map((i) => V.sp.items[i]).filter(Boolean);
  if (!picked.length) return;
  $('btn-sp-process').disabled = true;
  try {
    const res = await api('/api/sharepoint/batch',
      { method: 'POST', body: Object.assign({ items: picked }, vOpts()) });
    V.sp.batch = res.batch;
    V.sp.jobs = (res.started || []).map((s) => s.job_id);
    (res.failed || []).forEach((f) => vLog(f, 'err'));
    vLog(`SharePoint: взято в работу ${V.sp.jobs.length} из ${picked.length}`, 'ok');
    notice(`Взято в работу: ${V.sp.jobs.length}. Идут по одной, ход — в «В работе».`, 'ok');
    (res.started || []).forEach((s) => {
      const it = picked.find((x) => x.name === s.name);
      if (it) it.processed = { rec_id: s.rec_id, title: s.name, ok: true };
    });
    V.sp.sel = new Set();
    vSpRender();
    vSpJobs();
  } catch (e) { vLog(e.message, 'err'); notice(e.message, 'err'); vSpCount(); }
}

async function vSpStop() {
  if (!V.sp.batch) return;
  try {
    const res = await api(`/api/sharepoint/batch/${V.sp.batch}/cancel`, { method: 'POST' });
    notice(`Пачка останавливается: снято задач ${res.stopped}. Начатая прервётся не сразу.`, '');
  } catch (e) { notice(e.message, 'err'); }
}

/* Ход пачки по задачам очереди */
function vSpJobs() {
  if (!V.sp.jobs.length) return;
  const all = (typeof S !== 'undefined' && S.jobs) || [];
  const mine = V.sp.jobs.map((id) => all.find((j) => j.id === id)).filter(Boolean);
  const count = (st) => mine.filter((j) => j.status === st).length;
  const active = count('queued') + count('running');
  const parts = [`готово ${count('done')} из ${V.sp.jobs.length}`];
  if (count('error')) parts.push(`ошибок ${count('error')}`);
  if (count('cancelled')) parts.push(`остановлено ${count('cancelled')}`);
  $('sp-progress').textContent = parts.join(', ');
  $('btn-sp-stop').classList.toggle('hidden', !active);
}

/* Обработчики навешиваются один раз, из app.js, после разбора страницы. */
function videoInit() {
  document.querySelectorAll('.src-btn').forEach((b) => {
    b.onclick = () => vPickSource(b.dataset.src);
  });
  ['v-lang', 'v-only'].forEach((id) => {
    const el = $(id);
    if (el) el.onchange = vAsrNote;
  });
  // Свой выбор типа запоминаем: после него подсказка по источнику молчит.
  $('v-kind').onchange = () => { V.kindTouched = true; vApplyKind(); };
  // «Только скачать» без сохранения видео бессмысленно: скачивать было бы некуда.
  $('v-only').addEventListener('change', () => {
    if ($('v-only').checked) $('v-store').value = 'video';
    vPaintFields();
  });
  // «Что сохранить» решает, нужно ли качество видео.
  $('v-store').addEventListener('change', vPaintFields);
  // Выбранную руками категорию тип записи больше не меняет.
  $('v-category').addEventListener('change', () => { V.catTouched = true; });

  $('btn-link-probe').onclick = vProbe;
  $('btn-link').onclick = vSubmitLink;
  $('link-url').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); vSubmitLink(); }
  });

  $('btn-gcvh').onclick = () => {
    const url = ($('gcvh-url').value || '').trim();
    if (!url) { notice('Вставьте ссылку', 'err'); return; }
    vSubmitSource('gcvh', { url });
  };

  $('btn-sp-login').onclick = vSpLogin;
  $('btn-sp-search').onclick = vSpSearch;
  $('sp-query').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); vSpSearch(); }
  });
  $('btn-sp-copy').onclick = () => {
    navigator.clipboard.writeText($('sp-code').textContent || '')
      .then(() => notice('Код скопирован', 'ok'));
  };
  $('btn-sp-all').onclick = () => {
    V.sp.items.forEach((it, i) => { if (!it.processed || it.processed.ok === false) V.sp.sel.add(i); });
    vSpRender();
  };
  $('btn-sp-none').onclick = () => { V.sp.sel = new Set(); vSpRender(); };
  $('btn-sp-process').onclick = vSpProcess;
  $('btn-sp-stop').onclick = vSpStop;
  vSpFilters();
  vSpStatus();

  $('btn-webauth').onclick = async () => {
    // Браузера может не быть в папке — спросим до того, как человек введёт пароль.
    if (window.ensurePart && !await window.ensurePart('browser')) return;
    const urls = ($('web-urls').value || '').split('\n').map((s) => s.trim()).filter(Boolean);
    if (!urls.length) { notice('Вставьте хотя бы одну ссылку', 'err'); return; }
    const login = $('web-login').value;
    const password = $('web-pass').value;
    if (!password) { notice('Нужен пароль от площадки', 'err'); return; }
    urls.forEach((url) => vSubmitSource('webauth', { url, login, password }));
    $('web-pass').value = '';       // пароль в поле не оставляем
  };

  const dz = $('dropzone');
  if (dz) {
    dz.onclick = () => {
      if (dz.classList.contains('off')) return;
      const inp = document.createElement('input');
      inp.type = 'file';
      inp.multiple = true;
      inp.accept = 'video/*,audio/*,.mkv,.webm,.m4a,.opus,.vtt,.srt';
      inp.onchange = () => vPickFiles(Array.from(inp.files || []));
      inp.click();
    };
  }
  $('btn-process').onclick = vProcess;
  $('btn-files-clear').onclick = () => { V.files = []; vPaintFiles(); };

  vPickSource('files');
  vPaintFiles();
  vLoadOptions();
}

window.videoInit = videoInit;
window.videoPaint = videoPaint;
window.videoSpEvent = vSpEvent;
window.videoJobChanged = vSpJobs;
window.videoPickFiles = vPickFiles;
window.videoLog = vLog;
window.videoReloadOptions = vLoadOptions;
