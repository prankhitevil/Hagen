# Сторонние компоненты и их лицензии

Сам Hagen распространяется под GNU GPL v3.0 (файл `LICENSE`). Здесь перечислено
чужое: модели, библиотеки и внешние программы, которые программа использует,
скачивает по требованию или кладёт в переносимую сборку.

Проверено 18 сентября 2026 года по метаданным установленных пакетов и по
карточкам моделей; 23 сентября сверено с описью выпуска после ухода torch
(64 пакета в основной описи `lock.json`, ещё 72 — только у релиза с pyannote).

## Модели распознавания и разметки

Веса моделей распознавания **в этот репозиторий не входят** — программа
скачивает их с Hugging Face при первом запуске. Исключения — модель разметки
говорящих: её нейросети, переведённые в ONNX, и файлы PLDA лежат в
`models/diar/` (около 31 МБ), чтобы разметке не нужны были ни токен, ни сеть;
и модель поиска речи `models/vad/silero_vad.onnx` (2,2 МБ, файл из пакета
silero-vad 6.2.1 без изменений) — без неё программа не записала бы встречу.
В переносимой сборке (`tools/make_portable.py`) модели лежат внутри архива,
поэтому условия ниже касаются и того, кто раздаёт репозиторий или архив.

| Модель | Назначение | Лицензия | Правообладатель |
|---|---|---|---|
| GigaAM v3 (`v3_e2e_ctc`, `v3_e2e_rnnt`), в ONNX из `istupakov/gigaam-v3-onnx` | распознавание русской речи | MIT | SaluteDevices (`ai-sage/GigaAM-v3`) |
| pyannote speaker-diarization-community-1 | разметка говорящих | CC-BY-4.0 | pyannoteAI, CNRS |
| Silero VAD (`silero_vad.onnx`, в `models/vad/`) | поиск речи в звуке | MIT, © 2020- Silero Team | Silero Team |
| NVIDIA Parakeet TDT 0.6B v2 | распознавание английской речи (по кнопке) | CC-BY-4.0 | NVIDIA |

**Обязательное указание авторства** (требование CC-BY-4.0):

- разметка говорящих выполняется моделью *pyannote speaker-diarization-community-1*
  (pyannoteAI / CNRS), распространяемой по лицензии CC-BY-4.0;
- распознавание английской речи выполняется моделью *NVIDIA Parakeet TDT 0.6B v2*,
  распространяемой по лицензии CC-BY-4.0.

Доступ к моделям pyannote на Hugging Face выдаётся после принятия условий и
указания контактных данных. Раздавать веса community-1 лицензия CC-BY-4.0
разрешает при сохранении указания авторства, поэтому они лежат в
`models/diar/` в другом формате (ONNX), без изменения самих весов. Релиз с
разметкой через pyannote берёт модель с Hugging Face по токену пользователя.

## Перенесённый чужой код

| Файл | Откуда | Лицензия |
|---|---|---|
| `hagen/diar_onnx.py` | конвейер разметки pyannote.audio 4.0 (нарезка окнами, склейка, подсчёт голосов, отпечатки, распределение по людям, сборка разметки) | MIT, © 2020- CNRS, © 2025- pyannoteAI |
| `hagen/vbx.py` | группировка VBx: BUT Speech@FIT (`BUTSpeechFIT/VBx`), в версии pyannote.audio 4.0 | Apache-2.0 |
| `hagen/diar_onnx.py`, fbank | признаки по образцу `torchaudio.compliance.kaldi.fbank` (написано заново на numpy) | BSD-2-Clause (torchaudio) |
| `hagen/vad_onnx.py` | правило нарезки записи на участки речи из silero-vad 6.2.1 (`utils_vad.get_speech_timestamps`), звук — numpy вместо torch | MIT, © 2020- Silero Team |

Текст лицензии MIT для кода pyannote.audio:

> Copyright (c) 2020- CNRS, Copyright (c) 2025- pyannoteAI
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in all
> copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

Лицензия Apache-2.0 для `hagen/vbx.py` — <http://www.apache.org/licenses/LICENSE-2.0>;
её заголовок и авторство сохранены в самом файле.

## Внешние программы

| Программа | Как используется | Лицензия |
|---|---|---|
| FFmpeg | перекодирование звука и видео, вызывается как отдельная программа | GPL-3.0 (сборка gyan.dev «essentials») / LGPL для отдельных библиотек |
| Python | встроенный интерпретатор в папке `python\` | PSF License 2.0 |
| yt-dlp | загрузка видео по ссылке | Unlicense (общественное достояние) |

**Про FFmpeg.** В репозиторий и в архив выпуска бинарники не входят: установщик
качает сборку gyan.dev «essentials» той версии, что записана в описи
`lock.json`, с GitHub-зеркала <https://github.com/GyanD/codexffmpeg/releases>.
В полную сборку (всё уже поставлено, собирается по запросу) они попадают. Сборка
собрана с `--enable-gpl`, то есть распространяется под GPL-3.0. Hagen тоже под
GPL-3.0, несовместимости нет, но у того, кто раздаёт полную сборку, появляется
обязанность сообщить, откуда взять исходный код этой сборки FFmpeg:
<https://www.gyan.dev/ffmpeg/builds/> и <https://git.ffmpeg.org/ffmpeg.git>.

## Готовые сборки пакетов в `vendor/`

У пакета, у которого в открытых каталогах нет готовой сборки под Windows —
только исходники, — сборка из исходников автора лежит в `vendor/` и едет в
выпуске без изменений, чтобы установщику не нужен был компилятор:

| Пакет | Версия | Откуда собран | Лицензия |
|---|---|---|---|
| proxy_tools | 0.1.0 | PyPI, исходный архив | MIT (© Jonathan Tushman) |

## Прямые зависимости Python

| Пакет | Версия | Лицензия |
|---|---|---|
| onnxruntime | 1.23.2 | MIT |
| onnx-asr | 0.12.0 | MIT |
| huggingface_hub | 1.31.0 | Apache-2.0 |
| pyannote.audio (только релиз с pyannote) | 4.0.7 | MIT (© 2020 CNRS) |
| PyAudioWPatch | 0.2.12.8 | Apache-2.0 |
| soundfile | 0.14.0 | BSD-3-Clause |
| soxr | 1.1.0 | **LGPL-2.1-or-later** |
| pycaw | 20251023 | MIT |
| comtypes | 1.4.16 | MIT |
| truststore | 0.10.4 | MIT |
| fastapi | 0.141.1 | MIT |
| uvicorn | 0.52.4 | BSD-3-Clause |
| websockets | 17.1 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pywebview | 6.2.1 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| segno | 1.6.6 | BSD-3-Clause |
| pywin32 | 312 | PSF |
| psutil | 7.2.2 | BSD-3-Clause |
| windows-toasts | 1.3.1 | Apache-2.0 |
| pillow | 12.3.0 | MIT-CMU |
| yt-dlp | 2026.8.19 | Unlicense |
| numpy | 2.5.3 | BSD-3-Clause (и 0BSD, MIT, Zlib для частей) |
| scipy | 1.18.1 | BSD-3-Clause |

Только в релизе с pyannote: `torch` 2.14.0 и `torchaudio` (BSD-3-Clause),
`lightning` и `torchmetrics` (Apache-2.0), `matplotlib` (PSF-подобная),
`pandas` и `scikit-learn` (BSD-3-Clause) и прочие зависимости
`pyannote.audio`. В основном релизе torch нет: поиск речи, распознавание и
разметка идут на onnxruntime.

Инструменты мастерской, которые в выпуски не входят (ставятся разработчику
руками для разового экспорта моделей, `tools/export_asr_onnx.py`): `gigaam`
0.2.0 (MIT, © SaluteDevices), `silero-vad` 6.2.1 (MIT), `torch`, `onnx`
(Apache-2.0), `sentencepiece` (Apache-2.0).

## Лицензии, требующие внимания

Во всех установленных пакетах (основная опись и добавка pyannote вместе)
копилефт и «особые» лицензии встречаются только трижды, и все три совместимы
с GPL-3.0:

- **soxr** — LGPL-2.1-or-later. Используется как библиотека, подключается
  динамически; исходный код доступен у автора пакета. LGPL допускает работу
  внутри программы под GPL-3.0.
- **certifi** — MPL-2.0. Совместима с GPL благодаря оговорке о вторичной
  лицензии в самой MPL-2.0.
- **tqdm** — MPL-2.0 и MIT.

Остальное — MIT, BSD, Apache-2.0, PSF и Unlicense: разрешительные лицензии,
которые GPL-3.0 не нарушают.

## Звуковые образцы для проверок

Записи в `tests/` (`*.wav`, `meeting_video.mp4`) — синтетические: собраны для
этого проекта синтезом речи и из него же, голосов живых людей в них нет. Чужого
материала там нет, они распространяются под той же лицензией, что и Hagen.
Как собраны «совещание» и файл для правила пауз — видно в
`tests/make_meeting.py` и `tests/make_mic_test.py`.

## Как обновить этот список

Точные версии всего окружения — в описи выпуска `lock.json`. Лицензии
установленных пакетов:

```
.venv\Scripts\python.exe -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, d.metadata.get('License-Expression') or d.metadata.get('License')) for d in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower())]"
```

Лицензии моделей проверять по карточкам на Hugging Face: они меняются
независимо от кода.

---

**Third-party notices.** Hagen itself is licensed under GNU GPL v3.0. Speech
recognition uses GigaAM v3 (MIT, SaluteDevices; ONNX export from
`istupakov/gigaam-v3-onnx`) and, for English, NVIDIA Parakeet TDT 0.6B v2
(CC-BY-4.0), both run with onnx-asr (MIT). Speaker diarization uses pyannote
speaker-diarization-community-1 (CC-BY-4.0, pyannoteAI / CNRS); its networks,
converted to ONNX, and PLDA files are included in `models/diar/`, and its
pipeline is ported from pyannote.audio (MIT, CNRS / pyannoteAI) with VBx
clustering (Apache-2.0, BUT Speech@FIT). Voice activity detection uses the
Silero VAD model (MIT, Silero Team), included unchanged in `models/vad/`, with
its segmentation routine ported to numpy in `hagen/vad_onnx.py`. Speech
recognition weights are not included in this repository; they are downloaded
from Hugging Face by the user. FFmpeg is invoked as an external program and is
not redistributed here.
