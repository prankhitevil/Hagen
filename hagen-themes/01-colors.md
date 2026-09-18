# 1. Цвета по темам и карта замен

Файл токенов: `themes.css`. Тема — атрибут `data-theme` на `<html>`, плотность — `data-density`.
Без атрибутов работают `light` + `normal`.

## 1.1. Смысл цветов (одинаков во всех темах)

| Роль | Токены | Где применяется |
|---|---|---|
| Идёт запись / ошибка / необратимое | `--rec`, `--rec-ink`, `--rec-soft`, `--rec-line`, `--on-rec` | «Старт», «● ИДЁТ ЗАПИСЬ РАЗГОВОРА», пилюля «пишется», `.danger`, `.vlog.err`, `.notice.err` |
| Вопрос / требует внимания | `--warn-ink`, `--warn-soft`, `--warn-soft-hover`, `--warn-line` | «имя?», подсказка «похоже на…», конфликт, предупреждение о петле, черновик |
| Готово | `--ok`, `--ok-ink`, `--ok-soft`, `--ok-line`, `--on-ok` | «✓ говорящие», «авторазметка ✓», `.notice.ok` |
| Главное действие | `--accent` и производные | «Сохранить стенограмму», активная вкладка, активный источник |
| Мой голос / собеседники | `--me`, `--them`, `--speaker-1…6`, `--speaker-neutral` | имена в стенограмме |

Важно: `--rec` — это **фон** (на нём текст `--on-rec`), `--rec-ink` — **красный текст на светлом фоне**.
Разделение нужно для тёмных тем: один и тот же красный не может одновременно быть контрастным
как фон кнопки и как текст на панели. То же у зелёного (`--ok` / `--ok-ink`).

## 1.2. Таблица цветов

### Основа

| Токен | Светлая | Тёмная | Тёплая | Яркая |
|---|---|---|---|---|
| `--bg` | `#f4f6f9` | `#14171c` | `#f6f1e8` | `#0f1324` |
| `--panel` | `#ffffff` | `#1b1f26` | `#fffdf8` | `#181d33` |
| `--panel-2` | `#f8fafc` | `#22272f` | `#f4ede2` | `#1f2540` |
| `--line` | `#dfe3ea` | `#323a46` | `#e0d5c3` | `#2f3760` |
| `--line-soft` | `#eef1f6` | `#262c34` | `#f0e8da` | `#232a49` |
| `--text` | `#14181f` | `#e8ebf0` | `#241c13` | `#eef1ff` |
| `--muted` | `#5c6573` | `#9aa4b2` | `#6b5f50` | `#a4acd6` |
| `--muted-strong` | `#3f4754` | `#c2c9d4` | `#4b4234` | `#c9cfeb` |

### Акцент

| Токен | Светлая | Тёмная | Тёплая | Яркая |
|---|---|---|---|---|
| `--accent` | `#2f6feb` | `#6aa1ff` | `#a84b18` | `#5b8cff` |
| `--accent-hover` | `#1d54c4` | `#8ab6ff` | `#87390f` | `#83a8ff` |
| `--accent-soft` | `#e8f0fe` | `#1d2c48` | `#fbebdd` | `#1e2850` |
| `--accent-line` | `#c3d7fb` | `#31507f` | `#e6c3a2` | `#3c4d91` |
| `--accent-disabled` | `#b9c7e4` | `#3a4459` | `#dcc5ad` | `#3a4260` |
| `--on-accent` | `#ffffff` | `#0d1117` | `#fffdf8` | `#0a0e1c` |
| `--on-accent-disabled` | `#ffffff` | `#c2c9d4` | `#4b4234` | `#c9cfeb` |

### Состояния

| Токен | Светлая | Тёмная | Тёплая | Яркая |
|---|---|---|---|---|
| `--rec` | `#c4271b` | `#c8332b` | `#b32a18` | `#e0342d` |
| `--rec-hover` | `#9f1e14` | `#e04a41` | `#8e2012` | `#f2534b` |
| `--rec-disabled` | `#9e5148` | `#5e2a26` | `#96544a` | `#5c221f` |
| `--rec-ink` | `#b02318` | `#ff8178` | `#a52818` | `#ff7b73` |
| `--rec-soft` | `#fef3f2` | `#2c1715` | `#fdeee9` | `#2c1520` |
| `--rec-soft-hover` | `#fee4e2` | `#3a1d1a` | `#f9ddd3` | `#3a1a26` |
| `--rec-line` | `#f6c9c3` | `#5a2a25` | `#f0c4b6` | `#5b2431` |
| `--on-rec` | `#ffffff` | `#ffffff` | `#fffdf8` | `#ffffff` |
| `--ok` | `#0b7a4b` | `#1f7a52` | `#1a6f46` | `#12a05a` |
| `--ok-ink` | `#0a6b42` | `#4ede9f` | `#16653f` | `#37e08a` |
| `--ok-soft` | `#ecfdf3` | `#12291f` | `#eaf5ec` | `#0e2b1e` |
| `--ok-line` | `#aee3c6` | `#1f4a36` | `#b8ddc2` | `#1d5237` |
| `--on-ok` | `#ffffff` | `#ffffff` | `#fffdf8` | `#04180d` |
| `--warn-ink` | `#8f4708` | `#f0b429` | `#84500a` | `#ffc14d` |
| `--warn-soft` | `#fffaeb` | `#2b2313` | `#fdf3dc` | `#2e2410` |
| `--warn-soft-hover` | `#fef0c7` | `#352b16` | `#f9e7bd` | `#3a2d14` |
| `--warn-line` | `#f0cf8c` | `#4d3d17` | `#e3c893` | `#574020` |

### Голоса, шкалы, поверхности

| Токен | Светлая | Тёмная | Тёплая | Яркая |
|---|---|---|---|---|
| `--me` | `#1b5fd9` | `#7fb0ff` | `#1d6675` | `#59b0ff` |
| `--them` | `#6b32a8` | `#c99bf5` | `#7a3b9c` | `#c78bff` |
| `--speaker-neutral` | `#5c6573` | `#9aa4b2` | `#6b5f50` | `#a4acd6` |
| `--speaker-1` | `#6b32a8` | `#c99bf5` | `#7a3b9c` | `#c78bff` |
| `--speaker-2` | `#0f6f7a` | `#5fd3c6` | `#1d6675` | `#2fd4c4` |
| `--speaker-3` | `#a1452b` | `#ff9e7a` | `#8c4a12` | `#ff9770` |
| `--speaker-4` | `#2c5f9e` | `#8ab6ff` | `#2f5aa0` | `#7aa7ff` |
| `--speaker-5` | `#7a5200` | `#e7c565` | `#6d5300` | `#ffd166` |
| `--speaker-6` | `#8c2f63` | `#f58fc2` | `#8f2f57` | `#ff87c3` |
| `--meter-1 / 2 / 3` | `#12b76a` `#84cc16` `#e08c00` | `#22c55e` `#a3e635` `#f59e0b` | `#2f9e5f` `#a3b716` `#d98218` | `#22d38f` `#a3e635` `#ffb020` |
| `--meter-far-1 / 2` | `#7a3fb8` `#a855f7` | `#a855f7` `#d8b4fe` | `#7a3b9c` `#b070d8` | `#a855f7` `#e0aaff` |
| `--draft-bg` / `--draft-ink` | `#fffdf5` / `#57534e` | `#241f14` / `#cfc6b4` | `#fdf6e4` / `#5d5140` | `#221c3d` / `#cbc6e8` |
| `--notice-bg` / `--notice-ink` | `#101828` / `#ffffff` | `#2b323c` / `#e8ebf0` | `#2a2118` / `#fdf8f0` | `#2a3157` / `#eef1ff` |
| `--overlay` | `rgba(16,24,40,.45)` | `rgba(0,0,0,.62)` | `rgba(42,33,24,.45)` | `rgba(6,9,22,.66)` |

Тени и `--focus-ring` — отдельные токены на тему (`--shadow`, `--shadow-pop`, `--shadow-toast`,
`--shadow-dialog`, `--shadow-notice`), значения в `themes.css`.

## 1.3. Карта замен: что было прописано прямо в app.css

Все 32 значения из старого `app.css` (кроме уже бывших переменными). Новый `app.css` уже
содержит правый столбец — таблица нужна для проверки и для поиска остатков хардкода.

| № | Старое значение | Где стояло | Стало |
|---|---|---|---|
| 1 | `#1d5bd6` | `button.primary:hover` | `var(--accent-hover)` |
| 2 | `#b9c7e4` | `button.primary:disabled` | `var(--accent-disabled)` + подпись `var(--on-accent-disabled)`: в тёмных темах тёмный текст на тёмной кнопке исчезал |
| 3 | `#c3d7fb` | `.doc-tab.active`, `.rec-item.active`, `button.action:hover` | `var(--accent-line)` |
| 4 | `#fecdc9` | `button.danger` (рамка) | `var(--rec-line)` |
| 5 | `#fee4e2` | `button.danger:hover` | `var(--rec-soft-hover)` |
| 6 | `#b42318` | `.rec-btn:hover` | `var(--rec-hover)` |
| 7 | `#f3a29b` | `.rec-btn:disabled` | `var(--rec-disabled)` — теперь бордовый, а не бледно-розовый: «Старт, когда нельзя» отличается оттенком, а не только бледностью |
| 8 | `#12b76a` | `.meter-fill` | `var(--meter-1)` |
| 9 | `#84cc16` | `.meter-fill` | `var(--meter-2)` |
| 10 | `#f59e0b` | `.meter-fill`, `.meter-fill.far` | `var(--meter-3)` |
| 11 | `#7a3fb8` | `.meter-fill.far` | `var(--meter-far-1)` |
| 12 | `#a855f7` | `.meter-fill.far` | `var(--meter-far-2)` |
| 13 | `#fffdf5` | `.draft` | `var(--draft-bg)` |
| 14 | `#fde68a` | `.draft`, `.line .ask`, `.conflict`, `.sugg`, `.cloud-warn`, `.dupes` | `var(--warn-line)` |
| 15 | `#57534e` | `.draft-text` | `var(--draft-ink)` |
| 16 | `#c53030` | `.job-title .icon-btn:hover` | `var(--rec-ink)` |
| 17 | `#4b5563` | `.gear-btn` | `var(--muted-strong)` |
| 18 | `#101828` | `.notice` (фон) | `var(--notice-bg)` |
| 19 | `#fef0c7` | `.sugg:hover` | `var(--warn-soft-hover)` |
| 20 | `#fff` | `button.primary`, `.src-btn.active` | `var(--on-accent)` |
| 21 | `#fff` | `.badge-rec`, `.notice` | `var(--on-rec)` / `var(--notice-ink)` |
| 22 | `rgba(16,24,40,.45)` | `.overlay` | `var(--overlay)` |
| 23 | `rgba(16,24,40,.22)` | `.dialog` | `var(--shadow-dialog)` |
| 24 | `rgba(16,24,40,.16)` | `.ac-list` | `var(--shadow-pop)` |
| 25 | `rgba(16,24,40,.18)` | `.toast` | `var(--shadow-toast)` |
| 26 | `rgba(16,24,40,.24)` | `.notice` | `var(--shadow-notice)` |
| 27 | `rgba(16,24,40,.06)` ×2 | `--shadow` в `:root` | `var(--shadow)` на тему |
| 28 | `"Segoe UI",system-ui,…` | `body` | `var(--font)` |
| 29 | `monospace`, `22px` | `.sp-code` | `var(--font-mono)`, размер `calc(var(--fs-sm) + 2px)` — код входа чуть крупнее подписи соседней кнопки и растёт вместе с плотностью |
| 30 | `var(--warn)` как текст | `.pill.work`, `.warn`, `.meter-hint`, `.ask`, `.sa-row`, `.conflict`, `.cloud-warn`, `.sugg-inline`, `.draft-label`, `.vsample.odd`, `.vbadge.warn` | `var(--warn-ink)` |
| 31 | `var(--rec)` как текст | `.pill.rec`, `button.danger`, `.vlog.err` | `var(--rec-ink)` |
| 32 | `var(--ok)` как текст | `.pill.ok`, `#auto-diarize-note.ok-note` | `var(--ok-ink)` |

Хардкод в других файлах:

| Файл | Место | Что делать |
|---|---|---|
| `app.js`, строка ~272 | `<h3 style="…;color:var(--text)">Готов к записи</h3>` | уже переменная, оставить |
| `app.js`, строка ~271 | `<div style="font-size:34px">🎙</div>` | заменить на иконку `#ic-mic` (см. `03-markup.md`, п. 6) |
| `index.html` | `style="margin-bottom:10px"` в «Обработке» | оставить, цвета нет |

Проверка «не осталось ли хардкода» — в `05-checklist.md`, п. 1.

## 1.4. Контраст

Проверены пары, где текст лежит на цветном фоне:

| Пара | Светлая | Тёмная | Тёплая | Яркая |
|---|---|---|---|---|
| `--on-accent` на `--accent` | 4.7 | 9.4 | 5.4 | 8.7 |
| `--on-accent-disabled` на `--accent-disabled` | 4.6 | 5.6 | 5.9 | 6.0 |
| `--on-rec` на `--rec` | 5.5 | 5.1 | 6.0 | 4.6 |
| `--rec-ink` на `--rec-soft` | 5.6 | 6.1 | 6.2 | 6.4 |
| `--warn-ink` на `--warn-soft` | 5.3 | 8.7 | 5.2 | 9.6 |
| `--ok-ink` на `--ok-soft` | 5.1 | 9.8 | 5.5 | 10.2 |
| `--muted` на `--panel` | 5.9 | 6.4 | 6.0 | 6.5 |
| `--me` / `--them` на `--panel` | 5.6 / 6.9 | 6.0 / 5.4 | 5.9 / 6.6 | 5.2 / 5.0 |

Все ≥ 4.5:1 — обычный текст. Заголовки и жирные подписи проходят с запасом.
