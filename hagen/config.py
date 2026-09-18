"""Настройки приложения. Один JSON-файл в корне проекта, всё в UTF-8."""
from __future__ import annotations

import io
import json
import re
import os
import threading
from pathlib import Path
from typing import Any

PROJECT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_DIR / "data"
MODELS_DIR = PROJECT_DIR / "models"
LOGS_DIR = PROJECT_DIR / "logs"
TESTS_DIR = PROJECT_DIR / "tests"
SETTINGS_PATH = PROJECT_DIR / "settings.json"
LEGACY_TOKEN_PATH = PROJECT_DIR / "hf_token.txt"

# Кэш Hugging Face держим внутри проекта: модели разметки говорящих переезжают
# вместе с папкой, а не остаются в профиле пользователя. Раньше это задавалось
# переменной среды — на новой машине её нет, и pyannote молча качал модели
# заново. Ставим здесь, до первого импорта huggingface_hub: библиотека читает
# переменную один раз при загрузке.
os.environ.setdefault("HF_HOME", str(MODELS_DIR / "hf"))


def _use_windows_cert_store() -> None:
    """Доверять тем же сертификатам, что и Windows, а не только своему списку.

    Антивирусы с проверкой шифрованных соединений (у нас Kaspersky) встраиваются
    в HTTPS и подменяют сертификат сайта своим. Браузер такую подмену принимает,
    потому что корневой сертификат антивируса лежит в хранилище Windows. Python
    же по умолчанию смотрит только в свой файл certifi, где этого сертификата
    нет, — соединение отвергается, а антивирус на каждую попытку показывает
    «сайт может отображаться неправильно».

    truststore переключает проверку на хранилище Windows. Это не ослабление
    защиты, а наоборот: антивирус получает возможность проверять трафик
    приложения, как он проверяет трафик браузера.
    """
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        # Без него приложение работает, просто в сети с перехватом HTTPS будут
        # ошибки проверки сертификата. Падать из-за этого незачем.
        pass


def _models_already_downloaded() -> bool:
    hub = MODELS_DIR / "hf" / "hub"
    try:
        return any(p.is_dir() and p.name.startswith("models--") for p in hub.iterdir())
    except OSError:
        return False


def _prefer_local_models() -> None:
    """Не ходить в сеть за моделями, которые уже лежат на диске.

    pyannote при каждом запуске разметки спрашивает у huggingface.co, не
    обновились ли файлы, — и тратит на это несколько секунд, хотя всё уже
    скачано. Заодно каждый такой выход в сеть — повод для окна антивируса.
    Если кэш непустой, работаем с диска. Переменная читается huggingface_hub
    один раз при импорте, поэтому ставим её здесь, до всех остальных модулей.
    """
    if "HF_HUB_OFFLINE" in os.environ:
        return                      # уважаем явно заданное снаружи
    if _models_already_downloaded():
        os.environ["HF_HUB_OFFLINE"] = "1"


def _keep_localhost_direct() -> None:
    """Обращения к самому себе не должны идти через прокси.

    Локальный VPN в режиме системного прокси перехватывает даже 127.0.0.1: окно
    приложения перестаёт достукиваться до собственной службы, а сама служба
    падает при старте. Переменную читают и httpx, и uvicorn, и WebView2, поэтому
    ставим её здесь — раньше, чем они импортируются.
    """
    keep = "127.0.0.1,localhost,::1"
    for name in ("NO_PROXY", "no_proxy"):
        current = os.environ.get(name, "")
        missing = [h for h in keep.split(",") if h not in current]
        if missing:
            os.environ[name] = ",".join(filter(None, [current] + missing))


_use_windows_cert_store()
_prefer_local_models()
_keep_localhost_direct()

# Путь по умолчанию — от домашней папки текущего пользователя, а не от машины,
# на которой приложение собиралось: при переносе на другой компьютер он
# подставится сам. Настоящий путь всё равно задаётся в settings.json — при
# установке его спрашивают, — поэтому здесь нейтральное значение: имя чужой
# папки в коде ничего не даёт, а личные данные в него попадают легко.
DEFAULT_VAULT = str(Path.home() / "Documents" / "Obsidian")

DEFAULTS: dict[str, Any] = {
    "port": 8787,
    "open_browser": True,
    "ui_mode": "app",        # app = своё окно, browser = вкладка браузера
    # --- вид (пакет тем Claude Design, решения 13.09) ---
    "ui_theme": "light",     # light / warm / neutral / dark / bright
    "ui_density": "compact", # tiny / compact / normal / large — плотнее привычнее
    # Звук звонков хранится бессрочно; N > 0 — удалять звук записей с микрофона
    # старше N дней (текст и документы остаются). Решение 13.09.
    "audio_retention_days": 0,
    # --- трей (решения 13.09) ---
    "tray_enabled": True,        # значок у часов; крестик окна прячет в трей
    "autostart_windows": True,   # ярлык в «Автозагрузке», запуск сразу в трей
    "tray_hint_shown": False,    # подсказка «свёрнут в трей» показана один раз
    # Предупреждение «в запись попадёт весь звук компьютера» — один раз, при
    # первом включении «писать собеседников» (17.09). Постоянной
    # строкой на экране записи оно только занимало место.
    "far_hint_shown": False,
    # Соседние реплики одного человека склеиваются в абзац, если пауза между
    # ними меньше этого. Имя показывается только при смене
    # говорящего: раньше одно имя повторялось десятками строк подряд.
    "transcript_merge_gap_s": 2.0,
    # Подпись «Подготовил Hagen, consigliere · дата» под протоколом и саммари
    # Протокол уходит коллегам — подпись это и авторство,
    # и единственная реклама программы. Кому не нужно — выключает.
    "sign_documents": True,
    # --- Obsidian ---
    "vault_path": DEFAULT_VAULT,
    "vault_subfolder": "Meetings",
    "categories": ["Встречи", "Звонки", "Заметки"],
    # Категории, убранные из списка программы. Папка в сейфе при этом остаётся.
    "hidden_categories": [],
    "default_category": "Встречи",
    # Проекты и теги записей (решение 17.09). Свой список, а не папки
    # сейфа и не Todoist: ни от чего не зависит и работает без сети. Пополняется
    # сам, когда в карточке вводят новое имя; правится в «Настройки → Obsidian».
    "projects": [],
    "rec_tags": [],
    # --- обработка (решения 13.09) ---
    # Как называть владельца в стенограмме и документах: «Я» или имя.
    "owner_name": "Я",
    # Своя «задача и структура» документов: {"protocol": "...", ...}. Пусто — исходная.
    "prompt_overrides": {},
    # Язык ИНСТРУКЦИЙ модели: "ru" или "en" (решение 17.09).
    # Ответ всегда по-русски, это задаётся отдельной строкой. По умолчанию "ru":
    # нынешние русские промпты работают, и менять их без замера незачем.
    "prompt_lang": "ru",
    "vault_confirmed": False,
    # --- распознавание ---
    "onnx_threads": 7,
    "live_model": "v3_e2e_ctc",
    "offline_model": "v3_e2e_rnnt",
    # Через сколько минут простоя выгружать точную модель из памяти. Она весит
    # около 1,3 ГБ вместе с torch и нужна не всегда: файлам, «Перечитать точнее»
    # и диктовке. Быстрой модели эфира это не касается — она держится всегда,
    # иначе первая фраза записи ждала бы загрузку. 0 = не выгружать.
    "precise_idle_min": 30,
    # --- правило пауз (раздел 6 ТЗ) ---
    "silence_finalize_ms": 2200,
    "final_repass_seconds": 12.0,
    "max_phrase_seconds": 24.0,
    # --- запись ---
    "mode": "online",
    "record_far": True,
    "far_device_index": None,
    "mic_device_index": None,
    "mic_device_name": None,   # имя устройства для ffmpeg
    # --- снимки экрана во время записи (решение 13.09) ---
    # В сейф рядом с заметкой, в стенограмму по времени. Модели не показываются.
    "screenshots_enabled": True,
    "screenshot_folders": [],      # пусто = папка «Снимки экрана» Windows и Яндекс.Диск
    # Брать ли картинки из буфера обмена. Решение 13.09: нет — любая
    # скопированная картинка попадала бы в заметку; снимок — только файл в папке.
    "screenshots_from_clipboard": False,
    # --- эхо колонок ---
    # Если слушать совещание не в наушниках, микрофон повторяет собеседников.
    # Такие реплики помечаются и не идут ни в стенограмму, ни в протокол.
    "echo_filter": True,
    "echo_match_threshold": 0.6,
    # После разметки эхо ищется ещё и по голосу: реплика микрофона, похожая не
    # на владельца, а на собеседника. Спасает там, где дорожки расслышали
    # по-разному («неделя 2665» против «недели две, 665»).
    "echo_by_voice": True,
    "echo_voice_margin": 0.05,
    # --- диаризация ---
    "hf_token": "",
    "diarize_model": "pyannote/speaker-diarization-community-1",
    "diarize_fallback_model": "pyannote/speaker-diarization-3.1",
    "diarize_auto": True,
    # Замер на этой машине (Ryzen 5 5600, 6 ядер, кусок 102 с, community-1):
    # 1 поток — 140,6 с; 4 — 56,7; 6 — 51,4; 7 — 49,7; 9 — 47,1; 12 — 49,5.
    # Повтор в другом порядке дал те же числа, то есть это не прогрев.
    # Девять — лучшая точка; дальше становится хуже. Ядрами задача всё равно
    # не решается: четверть работы не распараллеливается вовсе.
    "diarize_threads": 9,
    # Быстрый путь построения отпечатков голоса: ствол сети считается один раз
    # на окно вместо трёх. Замер: 91 с против 32 с при том же результате.
    # Выключить, если после обновления pyannote разметка начнёт врать.
    "fast_embeddings": True,
    # Шаг скользящего окна разметки, секунды. 0 — как у модели (1 с при окне
    # 10 с, то есть десятикратный нахлёст). Замер на tests\meeting.wav
    # (51 с, три голоса): шаг 1 с — 8,1 с работы, шаг 2 с — 4,3 с, то есть
    # ускорение в 1,88 раза. Голосов найдено столько же, разметка совпала с
    # прежней на 98,1 % кадров. Откат мгновенный — вернуть 0.
    # Проверено на короткой записи; если на длинной встрече начнут теряться
    # короткие реплики, ставить 1.0 или 0.
    "diarize_window_step_s": 2.0,
    # --- продвинутые настройки разметки (решение 15.09) ---
    # None — как у модели community-1. Применяются при каждой разметке, без
    # перезапуска. Замер 15.09 на звонке 9:30 (пять голосов): при заводских
    # нашлось 3; «шаг 1 с · штраф 0,4 · мин. речь 1 с» — 5, но звонок 10:03 с
    # одним собеседником тот же вариант раздробил на 3. Поэтому заводские
    # значения не трогаем, они подбираются на своих записях.
    # Порог группировки: насколько похожие отпечатки считать одним человеком.
    # Больше — чаще склеивает разных; меньше — чаще дробит одного. У модели 0,6.
    "diarize_cluster_threshold": None,
    # Штраф за лишнего говорящего (Fb в VBx): больше — голоса с малым количеством
    # речи выбрасываются и приклеиваются к похожим; меньше — остаются. У модели 0,8.
    "diarize_cluster_fb": None,
    # Сколько секунд человек должен говорить ОДИН внутри 10-секундного окна,
    # чтобы по этому окну его можно было найти как нового. Короче — ловит тех,
    # кто сказал одну фразу, но отпечаток шумнее. У модели 2 с (20 % окна).
    "diarize_min_voice_s": None,
    # Резать фразу по говорящим, если кусок другого голоса в ней не короче
    # стольких секунд. Прежнее правило «чужой речи больше 35 % фразы» остаётся.
    # Найдено 15.09: 16-секундная фраза, где первый человек говорил 3,2 с, не
    # резалась вовсе (21 %), хотя pyannote слышал смену голоса точно. 0 — только
    # правило 35 %.
    "split_min_piece_s": 2.0,
    "max_speakers": None,
    "min_speakers": None,
    # --- правка стенограммы (решение 16.09) ---
    # Перед каждой ручной правкой откладывается снимок стенограммы, «Отменить»
    # (Ctrl+Z) идёт по ним назад. Снимок — это вся стенограмма записи: у
    # полуторачасовой встречи со временем слов он весит сотни килобайт, поэтому
    # стопка не бесконечная. Глубина — сколько правок помнить; срок — через
    # сколько минут снимок выбрасывается, даже если правок было мало. 0 в любом
    # из них — без предела (тогда снимки чистятся только удалением записи).
    "edit_undo_steps": 20,
    "edit_undo_minutes": 30,
    # --- база голосов ---
    "voice_match_threshold": 0.70,
    "voice_suggest_threshold": 0.45,
    # Имя по голосу встаёт само, только если лучший кандидат обгоняет второго
    # хотя бы на столько (решение 13.09: похожие голоса — вопросом).
    "voice_match_margin": 0.10,
    # --- детект звонка ---
    "call_watch_enabled": True,
    # Решение 13.09: звонок начался — запись идёт сама, без вопроса.
    "call_watch_autostart": True,
    "call_watch_processes": [
        "ms-teams.exe", "Teams.exe", "ms-teamsupdate.exe",
        "Zoom.exe", "CptHost.exe",
        "Webex.exe", "atmgr.exe",
        "chrome.exe", "msedge.exe", "firefox.exe",
        "Telegram.exe", "Discord.exe", "slack.exe",
    ],
    "call_watch_require_mic": True,
    # Признак звонка теперь «программа держит микрофон», он не мигает в паузах
    # речи, поэтому ждать шесть секунд незачем.
    "call_watch_debounce_s": 3,
    # Звонок закончился — столько секунд ждём ответа «Остановить?», потом стоп.
    "call_end_confirm_s": 30,
    # Новый звонок в пределах этого времени после записи — спросить,
    # дописать ли его в прошлую заметку.
    "call_resume_window_s": 600,
    "outlook_enabled": True,
    # За сколько минут до встречи считать начавшийся звонок её началом. Имя
    # записи берётся у идущей встречи, а у будущей — только в пределах этого
    # срока. Найдено 16.09: звонок в 12:21 получил имя планёрки, стоявшей на
    # 12:30, потому что окно было жёстко ±15 минут и смотрело в обе стороны.
    "meeting_lookahead_min": 5,
    # --- слова-паразиты при диктовке (решение 16.09) ---
    # Чистится только надиктованный текст, стенограммы звонков — никогда: там
    # речь остаётся как сказана, а паразитов убирает модель в документах.
    # Тянущиеся звуки («э-э-э», «ммм») убираются правилом, их перечислять не
    # надо. Список — обычные слова, каждое убирается целиком.
    "dictate_strip_fillers": True,
    "dictate_fillers": ["ну", "вот", "типа", "как бы", "короче", "блин", "это самое"],
    # --- режим «Видео»: готовые файлы, ссылки, субтитры ---
    # Тяжёлое (скачанные ролики) НЕ кладём в сейф Obsidian: сейф лежит на
    # Яндекс.Диске, и каждый ролик уезжал бы в облако. Пусто = папка по
    # умолчанию, см. store.assets_root().
    "assets_dir": "",
    "video_category": "Видео",     # категория заметок для видео
    "chunk_min": 15,               # длина куска аудио для облачного распознавания, мин
    "max_height": 720,             # потолок качества при скачивании
    "keep_video": True,            # хранить видеофайл после обработки
    "video_only": False,           # только скачать, ничего не распознавать
    # Сразу собирать документ после обработки видео. Решение 13.09:
    # по умолчанию НЕТ — документ только по кнопке (подписка Claude не бесконечна).
    "make_summary": False,
    "prefer_transcript": True,     # брать готовые .vtt/.srt, если они есть
    "yt_auto_subs": True,          # брать автоматические субтитры YouTube
    "smart_folder_name": True,     # имя папки по сути от модели, а не по заголовку
    "yt_proxy": "",                # пусто = напрямую, "auto" = перебрать порты VPN
    # --- модели и облачные сервисы (общее для протокола, саммари и распознавания) ---
    # Чем делать документы: claude_cli (подписка на этом компьютере) либо api (по ключу).
    "minutes_engine": "claude_cli",
    # Выбранный сервис для документов и добавленные вручную сервисы.
    # Встроенные (anthropic, openai, gigachat, yandexgpt, polza) в списке не лежат,
    # они заданы в providers.BUILTIN — здесь только свои.
    "api_provider": "anthropic",
    "providers": [],
    "api_keys": {},
    # Модель на сервис: {"<id сервиса>": "<имя модели>"}. Пусто = «по умолчанию»,
    # то есть приложение подберёт само (сильная для протокола, быстрая для
    # коротких документов и промежуточных выжимок).
    "api_models": {},
    "claude_cli_model": "",
    "claude_timeout_s": 600,
    "minutes_chunk_chars": 0,      # размер куска стенограммы; 0 = считать по модели
    # --- распознавание файлов (живой микрофон это НЕ затрагивает) ---
    "asr_files": "local",          # local = на этом компьютере, cloud = по API
    "asr_lang": "ru",              # ru = GigaAM, en = Parakeet
    "asr_provider": "polza",       # сервис для облачного распознавания
    "asr_model": "openai/whisper-1",
    "asr_fallback": "openai/whisper-large-v3-turbo",
    # --- диктовка текста (см. dictate.py) ---
    # Горячая клавиша диктует текст в любое окно. Считается на этом компьютере
    # той же точной моделью, что и «Перечитать точнее».
    "dictate_enabled": False,
    "dictate_hotkey": "ctrl+shift+space",
    # smart = коротко нажал: старт-стоп, держу: пока держу;
    # hold = только пока держу; toggle = только старт-стоп.
    "dictate_mode": "smart",
    # Язык задаётся здесь и НЕ угадывается по раскладке Windows: ошибка с языком
    # даёт не отказ, а правдоподобный бред (см. докстринг dictate.py).
    "dictate_lang": "ru",
    "dictate_sound": True,
    "dictate_pill": True,        # показывать капсулу «слушаю»
    # Кружок микрофона поверх всех окон на время записи: красный — включён,
    # серый — выключен, щелчок переключает (решение 17.09).
    "mic_pill": True,
    # Микрофон именно для диктовки. Пусто — тот же, что и для записи совещаний.
    "dictate_device_index": None,
    # Превращать произнесённые «точка», «запятая», «новая строка» в знаки.
    # По умолчанию нет: модель ставит знаки сама, а так пострадало бы слово
    # «точка», сказанное по делу.
    "dictate_marks": False,
    # Свои замены: [{"from": "джира", "to": "Jira"}, ...]. Целым словом, без
    # оглядки на регистр исходного слова.
    # Один словарь замен на программу (решение 18.09): у каждой
    # замены помечено, где она действует — "transcript", "dictation" или "both".
    # Прежние два списка (transcript_replacements и dictate_replacements)
    # переносятся сюда один раз при чтении настроек, см. _merge_replacements.
    "replacements": [],
    "dictate_replacements": [],
    # Замены в стенограммах: копятся из ручных правок.
    # Применяются к СВЕЖЕраспознанному тексту, готовые стенограммы не трогают.
    "transcript_replacements": [],
    "fix_suggest_after": 3,      # сколько раз повторить правку, чтобы предложить замену
    # --- задачи наружу (решение 17.09) ---
    # Личный токен Todoist (Settings -> Integrations -> Developer). Секрет: в
    # интерфейс отдаётся только «задан/не задан» и хвостик, из сборки для чужого
    # человека вычищается.
    # --- возможности (решение 18.09) ------------------------------
    # Отключаемые возможности, а не два режима программы: режим — это два
    # продукта, оба надо проверять, а человек, которому нужна одна «продвинутая»
    # вещь, получает сразу все. Выключенная возможность исчезает из интерфейса
    # целиком — не серая, а её нет. Данные при этом остаются: выключили и
    # включили — значения на месте, в готовых заметках ничего не меняется.
    #
    # Основной путь — записать, распознать, разметить, сделать документ,
    # отправить заметку — от возможностей не зависит НИКОГДА.
    #
    # Один реестр на всю программу: здесь значения по умолчанию, на странице
    # элементы помечаются data-feature, на сервере спрашивают config.feature().
    # Возможности уже перенесены с прежней установки (см. adopt_used_features)
    "features_adopted": False,
    "features": {
        "obsidian_links": False,   # проект, теги, продолжение, связанные заметки
        "todoist": False,          # «Поставить задачи» и токен
        "outlook": False,          # название и участники из встречи Outlook
        "calls": False,            # замечать звонки и предлагать запись
        "screenshots": False,      # снимки экрана во время записи — в заметку
        "micmute": True,           # выключатель микрофона и кружок поверх окон
        "dictation": False,        # диктовка в любое окно по горячей клавише
        "video_getcourse": False,  # источник видео: GetCourse
        "video_sharepoint": False, # источник видео: SharePoint и Teams
        "video_password": False,   # источник видео: сайт по паролю
        "advanced": False,         # раздел «Продвинутые»
    },
    "todoist_token": "",
    # Проект Todoist по умолчанию: {"id": "...", "name": "..."} или пусто.
    "todoist_project": {},
}

_SECRET_KEYS = {"hf_token", "api_keys", "todoist_token"}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None


_HF_RE = re.compile(r"(hf_[A-Za-z0-9_\-]{20,})")


def _extract_hf_token_from_file(path: Path) -> str:
    """Достаёт токен из файла в любом виде: голая строка, KEY = "hf_...", JSON.

    Пользователю не нужно угадывать синтаксис: берём первое похожее на токен слово.
    """
    try:
        with io.open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            raw = fh.read()
    except OSError:
        return ""
    m = _HF_RE.search(raw)
    if m:
        return m.group(1)
    # токен без префикса hf_: единственная непустая строка без пробелов
    lines = [ln.strip().strip('"').strip("'") for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    if len(lines) == 1 and " " not in lines[0] and len(lines[0]) >= 20:
        return lines[0]
    return ""


def _read_raw() -> dict[str, Any]:
    if not SETTINGS_PATH.exists():
        return {}
    try:
        with io.open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def load(force: bool = False) -> dict[str, Any]:
    global _cache
    with _lock:
        if _cache is None or force:
            merged = dict(DEFAULTS)
            merged.update(_read_raw())
            # разовый перенос токена из hf_token.txt, который просили создать вручную
            if not merged.get("hf_token") and LEGACY_TOKEN_PATH.exists():
                tok = _extract_hf_token_from_file(LEGACY_TOKEN_PATH)
                if tok:
                    merged["hf_token"] = tok
            _cache = _merge_replacements(merged)
        return dict(_cache)



def _merge_replacements(data: dict[str, Any]) -> dict[str, Any]:
    """Свести два прежних словаря замен в один (решение 18.09).

    Делается один раз: как только «replacements» появился, старые списки больше
    не читаются. Сами они остаются в файле нетронутыми — если человек вернётся
    на прежнюю сборку, его замены будут на месте.
    """
    if data.get("replacements"):
        return data
    merged: list[dict[str, str]] = []
    index: dict[str, int] = {}

    def add(row: Any, where: str) -> None:
        if not isinstance(row, dict):
            return
        was = str(row.get("from") or "").strip()
        became = str(row.get("to") or "").strip()
        if not was or not became or was == became:
            return
        key = was.lower()
        if key in index:
            # Одна и та же пара была в обоих списках — значит, действует всюду.
            if merged[index[key]].get("where") != where:
                merged[index[key]]["where"] = "both"
            return
        index[key] = len(merged)
        merged.append({"from": was, "to": became, "where": where})

    for row in data.get("transcript_replacements") or []:
        add(row, "transcript")
    for row in data.get("dictate_replacements") or []:
        add(row, "dictation")
    if merged:
        data["replacements"] = merged
    return data

def get(key: str, default: Any = None) -> Any:
    return load().get(key, DEFAULTS.get(key, default))


def atomic_json(path: Path, payload: Any, indent: int = 2) -> None:
    """Записать JSON так, чтобы на диске всегда был целый файл.

    Пишем во временный файл рядом, сбрасываем его на диск (`fsync`) и только
    потом подменяем им старый одним `os.replace`. Без `fsync` подмена имени
    попадает на диск раньше содержимого, и при потере питания на месте
    настроек или базы голосов остаётся пустой файл.
    """
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with io.open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=indent)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def save(patch: dict[str, Any]) -> dict[str, Any]:
    global _cache
    with _lock:
        current = load()
        for k, v in patch.items():
            # Словари «по сервису» сливаются по одному ключу, а не заменяются
            # целиком: интерфейс присылает только тот сервис, который правили, и
            # полная замена стёрла бы ключи и выбранные модели остальных.
            # Пустая строка в значении — это «убрать запись».
            if k in ("api_keys", "api_models") and isinstance(v, dict):
                merged = dict(current.get(k) or {})
                for provider, val in v.items():
                    if val == "":
                        merged.pop(provider, None)
                    elif val is not None:
                        merged[provider] = val
                current[k] = merged
            elif k == "call_watch_processes" and isinstance(v, list):
                # Одна и та же программа в списке дважды ничего не добавляет, а
                # в настройках выглядит ошибкой: так однажды оказался
                # «Zoom.exe». Регистр имени файла
                # в Windows не важен, поэтому и сверяем без него.
                seen: set[str] = set()
                clean: list[str] = []
                for name in v:
                    text = str(name or "").strip()
                    if not text or text.lower() in seen:
                        continue
                    seen.add(text.lower())
                    clean.append(text)
                current[k] = clean
            else:
                current[k] = v
        SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_json(SETTINGS_PATH, current)
        _restrict_permissions(SETTINGS_PATH)
        _cache = current
        return dict(current)


def _restrict_permissions(path: Path) -> None:
    """Файл с токенами читает только владелец."""
    try:
        if os.name == "nt":
            from . import platform

            platform.system().restrict_to_owner(path)
        else:
            os.chmod(path, 0o600)
    except Exception:
        pass



# ====================================================================== возможности


def features() -> dict[str, bool]:
    """Что включено. Ключи всегда все: чего нет в файле — берётся из DEFAULTS."""
    out = dict(DEFAULTS["features"])
    saved = load().get("features") or {}
    for name, val in saved.items():
        if name in out:
            out[name] = bool(val)
    return out


def feature(name: str) -> bool:
    """Включена ли возможность. Незнакомое имя считаем выключенным."""
    return bool(features().get(name, False))


def set_feature(name: str, on: bool) -> dict[str, bool]:
    """Включить или выключить одну возможность, не трогая остальные."""
    if name not in DEFAULTS["features"]:
        raise KeyError("нет такой возможности: %s" % name)
    cur = features()
    cur[name] = bool(on)
    save({"features": cur})
    return cur


def adopt_used_features() -> dict[str, bool]:
    """Разовый перенос: включить то, чем уже пользуются.

    На новой машине набор по умолчанию скромный. Но у того, кто обновляется,
    функции уже настроены и работают — и молча пропасть они не должны. Поэтому
    при первом запуске после обновления включается всё, чему нашлись следы:
    задан токен, включено замечание звонков, настроена диктовка и так далее.
    Делается один раз: отметка `features_adopted` в настройках.
    """
    data = load()
    if data.get("features_adopted"):
        return features()
    cur = features()
    if data.get("todoist_token"):
        cur["todoist"] = True
    if data.get("call_watch_enabled"):
        cur["calls"] = True
    if data.get("outlook_enabled"):
        cur["outlook"] = True
    if data.get("screenshots_enabled"):
        cur["screenshots"] = True
    if data.get("dictate_enabled"):
        cur["dictation"] = True
    if data.get("mic_pill"):
        cur["micmute"] = True
    if data.get("projects") or data.get("rec_tags"):
        cur["obsidian_links"] = True
    # Продвинутые: если тонкие настройки трогали, значит, они нужны.
    if any(data.get(k) not in (None, "") for k in
           ("diarize_step_s", "diarize_threshold", "undo_keep")):
        cur["advanced"] = True
    save({"features": cur, "features_adopted": True})
    return cur

def public() -> dict[str, Any]:
    """Настройки для интерфейса: секреты заменены признаком «задан/не задан»."""
    data = load()
    out = {k: v for k, v in data.items() if k not in _SECRET_KEYS}
    tok = data.get("hf_token") or ""
    out["hf_token_set"] = bool(tok)
    out["hf_token_hint"] = _mask(tok)
    keys = data.get("api_keys") or {}
    out["api_keys_set"] = {p: bool(v) for p, v in keys.items() if v}
    out["api_keys_hint"] = {p: _mask(v) for p, v in keys.items() if v}
    return out


def _mask(secret: str | None) -> str:
    if not secret:
        return ""
    s = str(secret)
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * 6}{s[-2:]}"


def vault_root() -> Path:
    root = Path(get("vault_path"))
    sub = (get("vault_subfolder") or "").strip().strip("/\\")
    return root / sub if sub else root


def ensure_dirs() -> None:
    for d in (DATA_DIR, MODELS_DIR, LOGS_DIR, TESTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
