# 3. Правки разметки, иконки и точки в app.js

Правила: **id не меняются**, подписи не переписываются (предложения по тексту — отдельно,
`06-copy.md`, применять по желанию), функции не убираются. Ниже — только то, что нужно
для тем, иконок и наведённого порядка в кнопках.

## 3.0. Подключение (index.html, `<head>`)

```html
<link rel="stylesheet" href="/static/themes.css">   <!-- ДОБАВИТЬ, строго первым -->
<link rel="stylesheet" href="/static/app.css">
```

И атрибуты на `<html>` — значения ставит переключатель, начальные можно оставить как есть:

```html
<html lang="ru" data-theme="light" data-density="normal">
```

Файлы из этой папки кладутся в `static\`: `themes.css`, `app.css` (заменяет прежний),
`icons.svg`, выбранная иконка приложения.

## 3.1. Иконки: эмодзи → `icons.svg`

Эмодзи в Windows цветные и не подчиняются теме: в тёмной они светятся, в тёплой спорят
с палитрой. Заменяем на спрайт (`use href` — тот же origin, 127.0.0.1, ничего из интернета).

Шаблон вставки:

```html
<svg class="ic" aria-hidden="true"><use href="/static/icons.svg#ic-mic"></use></svg>
```

`.ic` не нужен как класс — размер задают правила `button.action svg` и т.п. в `app.css`.

### index.html

| Было | Стало (внутри той же кнопки, подпись не меняется) |
|---|---|
| `🎙 Запись` (`.mode-btn[data-mode=rec]`) | `<svg aria-hidden="true"><use href="/static/icons.svg#ic-mic"></use></svg> Запись` |
| `🎬 Видео` (`.mode-btn[data-mode=video]`) | `…#ic-video…` + ` Видео` |
| `● Старт` (`#btn-start`) | `…#ic-dot-rec…` + ` Старт` |
| `■ Стоп` (`#btn-stop`) | `…#ic-stop…` + ` Стоп` |
| `📋 Копировать` (`#btn-copy-transcript`) | `…#ic-copy…` + ` Копировать` |
| `Свернуть` (`#btn-fold-transcript`) | `…#ic-fold…` + ` Свернуть` |
| `👥 Разметить говорящих` (`#btn-diarize`) | `…#ic-people…` + ` Разметить говорящих` |
| `📄 Сделать документ` (`#btn-minutes`) | `…#ic-doc…` + ` Сделать документ` |
| `🔍 Перечитать точнее` (`#btn-retry`) | `…#ic-refine…` + ` Перечитать точнее` |
| `Удалить` (`#btn-delete`) | `…#ic-trash…` + ` Удалить` |
| `+ Новая заметка` (`#btn-new`) | `…#ic-plus…` + ` Новая заметка` |
| `+ новая` / `− убрать` (`#btn-new-cat` / `#btn-del-cat`) | `…#ic-plus…` / `…#ic-minus…` + подпись |
| `▶ Обработать` (`#btn-process`, `#btn-sp-process`) | `…#ic-play…` + подпись |
| `■ Остановить` (`#btn-sp-stop`) | `…#ic-stop…` + ` Остановить` |
| `✕` (`.dlg-close`) | `…#ic-x…` (кнопка получает `aria-label="Закрыть"`) |
| `👥 Здесь несколько человек — разделить голоса` (`#btn-speaker-split`) | `…#ic-split…` + подпись |
| `⚠` в `#far-warning` | `…#ic-warn…` (в начале строки) |
| `🎙` в `#empty-state .empty-icon` | `<svg aria-hidden="true"><use href="/static/icons.svg#ic-mic"></use></svg>` |
| `☁` в `#cloud-warning` / кнопке облака | `…#ic-cloud…` |

Эмодзи, которые **остаются**: `🎬 / 📝 / ✓` в легенде результатов SharePoint
(`#src-sharepoint .tiny.muted`) — это подписи к пометкам в списке, а не кнопки;
`⏳` в пилюле «размечаю» можно оставить или заменить на `#ic-clock`.

### app.js (иконки внутри строк, которые собирает скрипт)

| Строка (примерно) | Было | Стало |
|---|---|---|
| ~177 | `<button class="icon-btn job-stop js-stop-job" …>✕</button>` | внутрь `<svg><use href="/static/icons.svg#ic-x"></use></svg>`, добавить `aria-label="Остановить"` |
| ~271 | `<div style="font-size:34px">🎙</div>` | `<div class="empty-icon"><svg aria-hidden="true"><use href="/static/icons.svg#ic-mic"></use></svg></div>` |
| ~464 | `<button class="ghost small js-reveal" …>Открыть папку</button>` | `<button class="ghost icon-only js-reveal" data-key="…" type="button" title="Открыть папку" aria-label="Открыть папку"><svg aria-hidden="true"><use href="/static/icons.svg#ic-folder"></use></svg></button>` |
| ~984 | `👥 Под именем «…»` | `<svg aria-hidden="true"><use href="/static/icons.svg#ic-people"></use></svg> Под именем «…»` |

## 3.2. Старт/Стоп — одно место на экране

`index.html`, внутри `.ctl-row`: обернуть две существующие кнопки, **ничего не удаляя**.

```html
<div class="rec-toggle">
  <button id="btn-start" class="rec-btn" title="Начать запись: микрофон и звук собеседников">
    <svg aria-hidden="true"><use href="/static/icons.svg#ic-dot-rec"></use></svg> Старт</button>
  <button id="btn-stop" class="stop-btn" disabled title="Остановить запись и дописать стенограмму">
    <svg aria-hidden="true"><use href="/static/icons.svg#ic-stop"></use></svg> Стоп</button>
</div>
```

CSS показывает ту кнопку, которая сейчас работает, опираясь на `disabled` — его `app.js`
уже расставляет сам (`paintRecState`). Правок в скрипте не нужно. Пока обе кнопки
заблокированы («включаю устройства…»), видна заблокированная «Старт».

## 3.3. Блок устройств сворачивается после старта

`index.html`, первым потомком `.controls#live-controls` — сводная строка и кнопка:

```html
<div class="ctl-summary">
  <span>Микрофон</span><div class="meter"><div class="meter-fill" style="width:38%"></div></div>
  <span>Собеседники</span><div class="meter"><div class="meter-fill far" style="width:22%"></div></div>
  <span id="ctl-summary-dev" class="tiny muted"></span>
  <button id="btn-devices-fold" class="icon-btn fold-btn" type="button"
          title="Показать выбор устройств" aria-label="Показать выбор устройств">
    <svg aria-hidden="true"><use href="/static/icons.svg#ic-unfold"></use></svg></button>
</div>
```

Кнопка сворачивания в развёрнутом виде — та же, с другой иконкой; проще держать одну и
менять `href` иконки в скрипте. Минимальный патч `app.js`:

```js
// рядом с остальными обработчиками в конце файла
$('btn-devices-fold').onclick = () => {
  const box = $('live-controls');
  box.classList.toggle('folded');
  box.querySelector('#btn-devices-fold use')
     .setAttribute('href', '/static/icons.svg#' + (box.classList.contains('folded') ? 'ic-unfold' : 'ic-fold'));
};
// в paintRecState(), после расстановки disabled:
$('live-controls').classList.toggle('folded', !!S.recordingId);
```

Уровни в свёрнутом виде рисуются теми же `#meter-mic` / `#meter-far`? Нет — они остаются
в развёрнутом блоке. В `.ctl-summary` шкалы декоративные: если хотите живые, дайте им
id `meter-mic-mini` / `meter-far-mini` и обновляйте в том же месте, где обновляются
основные (одна строка на шкалу).

## 3.4. Нижний ряд действий: два блока и разделитель

`index.html`, вторая `.fb-row` в `.footer-bar`. Порядок кнопок и их id не меняются —
добавляются только обёртки `.fb-group` и `.fb-sep`; `#btn-delete` уезжает вправо
правилом CSS (`margin-left:auto`), поэтому в разметке остаётся последним.

```html
<div class="fb-row">
  <div class="fb-group">
    <button id="btn-diarize" class="action">…Разметить говорящих</button>
    <span id="auto-diarize-note" class="tiny muted" title="…"></span>
    <label id="room-field" class="inline tiny hidden" title="…">
      <input type="checkbox" id="chk-room"> со мной в комнате были ещё люди</label>
  </div>
  <span class="fb-sep"></span>
  <div class="fb-group">
    <button id="btn-minutes" class="action" title="…">…Сделать документ</button>
    <button id="btn-retry" class="action">…Перечитать точнее</button>
  </div>
  <button id="btn-delete" class="danger small" title="…">…Удалить</button>
</div>
```

Смысл: слева — «кто говорил», справа — «что сделать с текстом», «Удалить» отдельно.
При узком окне (900 px) ряд переносится по границам блоков, а не по случайной кнопке.

Первая `.fb-row` остаётся как есть: `Категория … + новая … − убрать | Сохранить стенограмму`
— главная кнопка одна, справа через `.fb-spacer` идут «Копировать» и «Свернуть».

## 3.5. Цвета имён: свой цвет только у именованных

`app.css` держит классы `.who.c1…c6` и нейтральный цвет по умолчанию.
Патч в `app.js`, в сборке строки стенограммы (~296):

```js
// рядом со speakerClass()
const SPEAKER_SLOTS = 6;
function speakerColor(seg) {
  // имя есть — стабильный цвет по имени; безымянный голос остаётся нейтральным
  const nm = String(seg.speaker || '');
  if (!nm || /^(Говорящий|Участник|Голос|Рядом со мной)/i.test(nm) || nm.includes('~')) return '';
  if (seg.track === 'mic' && !String(seg.speaker_key || '').includes('~')) return '';  // «Я» — свой цвет --me
  let h = 0;
  for (let i = 0; i < nm.length; i++) h = (h * 31 + nm.charCodeAt(i)) % 997;
  return ' c' + (h % SPEAKER_SLOTS + 1);
}
```

и в шаблоне строки:

```js
<span class="who${speakerColor(s)}" data-key="…">…</span>
```

Без этого патча всё работает по-старому, но у всех собеседников будет один нейтральный
цвет: `.line.them .who` больше не красит фиолетовым принудительно.

## 3.6. Настройки → новая вкладка «Вид»

Переключатель темы и плотности. Вкладку добавляем пятой, до «Моделей» — она про глаза,
а не про модели. Разметка (вставить в `#dlg-settings`):

```html
<!-- в .tabs -->
<button class="tab" data-tab="t-view">Вид</button>

<!-- рядом с остальными .tabpane -->
<div id="t-view" class="tabpane hidden">
  <div class="field">Тема</div>
  <div id="theme-grid" class="theme-grid">
    <button class="theme-card sel" data-theme-pick="light" type="button">
      <span class="theme-mini" data-mini="light">
        <span class="tm-bar"><i class="tm-dot"></i><i class="tm-dot"></i></span>
        <span class="tm-body"><i class="tm-line"></i><i class="tm-line short"></i>
          <span class="tm-dots"><i class="tm-dot rec"></i><i class="tm-dot warn"></i><i class="tm-dot ok"></i></span>
        </span>
      </span>
      <span class="theme-name">Светлая <svg class="tick" aria-hidden="true"><use href="/static/icons.svg#ic-check"></use></svg></span>
    </button>
    <!-- те же четыре карточки: data-theme-pick="dark" | "warm" | "bright" -->
  </div>

  <div class="field">Размер интерфейса</div>
  <div id="density-seg" class="density-seg">
    <button data-density-pick="tiny" type="button">Мелко</button>
    <button data-density-pick="compact" type="button">Плотно</button>
    <button data-density-pick="normal" class="sel" type="button">Обычно</button>
    <button data-density-pick="large" type="button">Крупно</button>
  </div>
  <div class="tiny muted" style="margin-top:8px">Меняются шрифты, кнопки и отступы. Цвета и функции — нет.</div>
</div>
```

Четыре значения `data-density`: `tiny` (кнопки 24 px), `compact` (26), `normal` (32, по
умолчанию), `large` (38). «Обычно» на 10% ниже прежнего вида — у кнопок был запас по высоте.

Мини-макет карточки раскрашивается инлайновыми переменными, чтобы карточка показывала
**свою** тему, а не текущую: на каждую `.theme-mini` ставится `data-mini` и цвета
берутся из отдельного набора правил — он уже есть в превью
(`Hagen — темы.dc.html`, блок `.theme-mini[data-mini=…]`), скопируйте его в `app.css`
или оставьте как отдельный `themes-preview.css`.

Проводка (ваша часть; для справки — минимум):

```js
function applyView(v) {
  document.documentElement.dataset.theme = v.theme || 'light';
  document.documentElement.dataset.density = v.density || 'normal';
}
// клики по карточкам → applyView + сохранение в settings.json (ключи theme, density)
```

## 3.7. Мелочи разметки

1. `#rec-duration` и `.line .ts` получили моношрифт правилами CSS — правок разметки нет.
2. `.dlg-close` и `#btn-settings`: добавить `aria-label`, иначе кнопка с одной иконкой
   молчит для экранного диктора.
3. `#call-toast` получает красную кромку слева правилом CSS. Разметка не меняется.
   Всплывает по-прежнему в правом нижнем углу — это и есть место уведомлений Windows.
4. Ряд `.mode-row` и `.src-row`: разметка та же, состояния (`hover`, `active`, фокус)
   теперь описаны в CSS.
5. Минимум 900×600: проверено на 900 px — боковая панель 268–324 px по плотности,
   нижний ряд переносится по блокам. Правило `@media (max-width:880px)` оставлено
   как было (окно можно сузить ниже минимума перетаскиванием).
