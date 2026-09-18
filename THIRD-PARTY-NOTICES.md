# Сторонние компоненты и их лицензии

Сам Hagen распространяется под GNU GPL v3.0 (файл `LICENSE`). Здесь перечислено
чужое: модели, библиотеки и внешние программы, которые программа использует,
скачивает по требованию или кладёт в переносимую сборку.

Проверено 18 сентября 2026 года по метаданным установленных пакетов
(149 пакетов в `.venv`) и по карточкам моделей.

## Модели распознавания и разметки

Веса моделей **в этот репозиторий не входят** — программа скачивает их с
Hugging Face при первом запуске. В переносимой сборке (`tools/make_portable.py`)
они лежат внутри архива, поэтому условия ниже касаются и того, кто раздаёт архив.

| Модель | Назначение | Лицензия | Правообладатель |
|---|---|---|---|
| GigaAM v3 (`v3_e2e_ctc`, `v3_e2e_rnnt`) | распознавание русской речи | MIT | SaluteDevices (`ai-sage/GigaAM-v3`) |
| pyannote speaker-diarization-community-1 | разметка говорящих | CC-BY-4.0 | pyannoteAI, CNRS |
| silero-vad | поиск речи в звуке | MIT | Silero Team |
| NVIDIA Parakeet TDT 0.6B v2 | распознавание английской речи (по кнопке) | CC-BY-4.0 | NVIDIA |

**Обязательное указание авторства** (требование CC-BY-4.0):

- разметка говорящих выполняется моделью *pyannote speaker-diarization-community-1*
  (pyannoteAI / CNRS), распространяемой по лицензии CC-BY-4.0;
- распознавание английской речи выполняется моделью *NVIDIA Parakeet TDT 0.6B v2*,
  распространяемой по лицензии CC-BY-4.0.

Доступ к моделям pyannote на Hugging Face выдаётся после принятия условий и
указания контактных данных: пользователь получает их по своему токену сам.
Раздавать веса pyannote CC-BY-4.0 разрешает при сохранении указания авторства.

## Внешние программы

| Программа | Как используется | Лицензия |
|---|---|---|
| FFmpeg | перекодирование звука и видео, вызывается как отдельная программа | GPL-3.0 (сборка gyan.dev «full») / LGPL для отдельных библиотек |
| Python | встроенный интерпретатор в папке `python\` | PSF License 2.0 |
| yt-dlp | загрузка видео по ссылке | Unlicense (общественное достояние) |

**Про FFmpeg.** В репозиторий бинарники не входят (`ffmpeg/` закрыт `.gitignore`),
но в переносимую сборку они попадают. Сборка «full» от gyan.dev собрана с
`--enable-gpl`, то есть распространяется под GPL-3.0. Hagen тоже под GPL-3.0,
несовместимости нет, но у того, кто раздаёт архив, появляется обязанность
сообщить, откуда взять исходный код этой сборки FFmpeg:
<https://www.gyan.dev/ffmpeg/builds/> и <https://git.ffmpeg.org/ffmpeg.git>.

## Прямые зависимости Python

| Пакет | Версия | Лицензия |
|---|---|---|
| gigaam | 0.2.0 | MIT |
| onnxruntime | 1.23.2 | MIT |
| silero-vad | 6.2.1 | MIT |
| onnx-asr | 0.12.0 | MIT |
| pyannote.audio | 4.0.7 | MIT (© 2020 CNRS) |
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
| pywin32 | 312 | PSF |
| psutil | 7.2.2 | BSD-3-Clause |
| windows-toasts | 1.3.1 | Apache-2.0 |
| yt-dlp | 2026.8.19 | Unlicense |
| numpy | 2.5.3 | BSD-3-Clause (и 0BSD, MIT, Zlib для частей) |
| scipy | 1.18.1 | BSD-3-Clause |

Крупные косвенные зависимости: `torch` 2.14.0 и `torchaudio` (BSD-3-Clause),
`lightning` и `torchmetrics` (Apache-2.0), `huggingface_hub` (Apache-2.0),
`onnx` (Apache-2.0), `matplotlib` (PSF-подобная), `transformers`-зависимости
`pyannote.audio` (Apache-2.0).

## Лицензии, требующие внимания

Во всех 149 установленных пакетах копилефт и «особые» лицензии встречаются
только трижды, и все три совместимы с GPL-3.0:

- **soxr** — LGPL-2.1-or-later. Используется как библиотека, подключается
  динамически; исходный код доступен у автора пакета. LGPL допускает работу
  внутри программы под GPL-3.0.
- **certifi** — MPL-2.0. Совместима с GPL благодаря оговорке о вторичной
  лицензии в самой MPL-2.0.
- **tqdm** — MPL-2.0 и MIT.

Остальное — MIT, BSD, Apache-2.0, PSF и Unlicense: разрешительные лицензии,
которые GPL-3.0 не нарушают.

## Как обновить этот список

```
.venv\Scripts\python.exe -c "import importlib.metadata as m; [print(d.metadata['Name'], d.version, d.metadata.get('License-Expression') or d.metadata.get('License')) for d in sorted(m.distributions(), key=lambda d: d.metadata['Name'].lower())]"
```

Лицензии моделей проверять по карточкам на Hugging Face: они меняются
независимо от кода.

---

**Third-party notices.** Hagen itself is licensed under GNU GPL v3.0. Speech
recognition uses GigaAM v3 (MIT, SaluteDevices) and, for English, NVIDIA
Parakeet TDT 0.6B v2 (CC-BY-4.0). Speaker diarization uses pyannote
speaker-diarization-community-1 (CC-BY-4.0, pyannoteAI / CNRS); voice activity
detection uses silero-vad (MIT). Model weights are not included in this
repository; they are downloaded from Hugging Face by the user. FFmpeg is invoked
as an external program and is not redistributed here.
