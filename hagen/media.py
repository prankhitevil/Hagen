# -*- coding: utf-8 -*-
"""Режим «Видео»: готовые файлы, ссылки и записи со сторонних площадок.

Главная мысль: видео — это не второе приложение внутри первого, а ещё один
способ ЗАВЕСТИ ОБЫЧНУЮ ЗАПИСЬ. Файл или ссылка создаёт всё ту же папку
data/<rec_id> с тем же meta.json и transcript.json, поэтому список записей,
очередь задач, редактор говорящих, разметка, база голосов, протокол и выгрузка
в Obsidian работают для видео сами, без единой новой строки.

Отличий от записи с микрофона ровно три:
  1. звук не пишется, а добывается — скачиванием либо извлечением из файла;
  2. текст может прийти готовым (.vtt от Teams, субтитры YouTube) — тогда
     распознавание не запускается вовсе, и это самый дешёвый путь;
  3. тяжёлый файл (видео) живёт НЕ в папке записи, а в отдельном хранилище:
     сейф Obsidian лежит на Яндекс.Диске, и гигабайты роликов уезжали бы в
     облако. Путь к видео пишется в meta, чтобы его было видно в заметке.

Этапы пишутся в meta["stages"]. Это нужно не для красоты: служба закрывается
вместе с окном, и без карты этапов часовое видео пришлось бы качать и
распознавать заново. Повторный запуск пропускает то, что уже сделано.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Callable

from . import audio_io, config, diarize_jobs, jobs, store
from .events import hub

log = logging.getLogger("hagen.media")

#: Порядок этапов обработки. Каждый отмечается в meta["stages"] после успеха.
STAGES = ("media", "text", "diarize", "summary", "note")

# ---------------------------------------------------------------- этапы


def _stages(rec_id: str) -> dict[str, Any]:
    meta = store.get(rec_id) or {}
    raw = meta.get("stages")
    return dict(raw) if isinstance(raw, dict) else {}


def _mark(rec_id: str, stage: str, value: Any = True) -> None:
    done = _stages(rec_id)
    done[stage] = value
    store.update(rec_id, {"stages": done})


def _done(rec_id: str, stage: str) -> bool:
    return bool(_stages(rec_id).get(stage))


# ---------------------------------------------------------------- настройки задания


def _flag(opts: dict[str, Any], key: str) -> bool:
    """Значение галочки: из задания, а если его там нет — из настроек."""
    if key in opts and opts[key] is not None:
        return bool(opts[key])
    return bool(config.get(key))


def _int(opts: dict[str, Any], key: str, default: int) -> int:
    try:
        return int(opts.get(key) if opts.get(key) not in (None, "") else config.get(key))
    except (TypeError, ValueError):
        return default


def _lang(opts: dict[str, Any]) -> str:
    lang = str(opts.get("asr_lang") or config.get("asr_lang") or "ru").strip().lower()
    return lang if lang in ("ru", "en") else "ru"


#: Тип записи. Он решает три вещи разом: размечать ли говорящих, какой
#: получится документ и что хранить на диске. Раньше разметка включалась
#: всегда, и час лекции с одним голосом впустую съедал одиннадцать минут
#: процессора — размечать там было нечего.
KINDS = ("meeting", "lecture", "interview", "transcript")

#: Что тип подставляет по умолчанию. Человек вправе поменять любое поле:
#: селектор именно ПОДСТАВЛЯЕТ значения, а не запирает их.
KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    # встреча: несколько голосов, важно кто что сказал, запись нужна целиком
    "meeting": {"diarize": True, "document": "meeting", "store": "video"},
    # лекция: один голос, размечать нечего, хватит текста и конспекта
    "lecture": {"diarize": False, "document": "lecture", "store": "none"},
    # интервью и подкаст: голоса важны, а картинка обычно нет
    "interview": {"diarize": True, "document": "interview", "store": "audio"},
    # только расшифровка: ни разметки, ни документа
    "transcript": {"diarize": False, "document": "", "store": "none"},
}

#: Что хранить после обработки: ничего, только звук, видео со звуком.
STORE_MODES = ("none", "audio", "video")


def _kind(opts: dict[str, Any]) -> str:
    """Тип записи из задания. Неизвестное значение — как встреча."""
    kind = str(opts.get("video_kind") or "").strip().lower()
    return kind if kind in KINDS else "meeting"


def category_for_kind(kind: str) -> str:
    """Категория заметки по умолчанию для типа записи.

    Встреча ложится туда же, куда записи встреч; лекция, интервью и
    расшифровка — в категорию видео. Раньше окно подставляло всем типам
    первую категорию списка, и лекция уезжала во «Встречи».
    """
    video = str(config.get("video_category") or config.get("default_category") or "")
    if kind == "meeting":
        return str(config.get("default_category") or video)
    return video


def _store_mode(opts: dict[str, Any]) -> str:
    """Что хранить. Если в задании не сказано — берём из типа записи."""
    mode = str(opts.get("store_media") or "").strip().lower()
    if mode in STORE_MODES:
        return mode
    if "keep_video" in opts and opts["keep_video"] is not None:
        return "video" if opts["keep_video"] else "audio"   # старые задания
    return str(KIND_DEFAULTS[_kind(opts)]["store"])


def _keeps_video(opts: dict[str, Any]) -> bool:
    """Оставляем ли сам видеофайл рядом с записью."""
    return _store_mode(opts) == "video" or _flag(opts, "video_only")


def _wants_diarize(opts: dict[str, Any]) -> bool:
    """Нужна ли разметка говорящих.

    Два условия: тип записи, где голоса вообще различают, И общая настройка
    «делать автоматически». Тип отвечает за смысл, настройка — за право
    занимать однопоточную очередь без спроса.
    """
    return bool(KIND_DEFAULTS[_kind(opts)]["diarize"]) and _flag(opts, "diarize_auto")


def asr_where(opts: dict[str, Any]) -> str:
    """Где распознавать: «local» или «cloud». Выбор — в настройках (21.09);
    задание может сказать своё, если его прислал вызывающий."""
    where = str(opts.get("asr_files") or config.get("asr_files") or "local").strip().lower()
    return where if where in ("local", "cloud") else "local"


# ---------------------------------------------------------------- хранилище тяжёлого


def _assets_dir(rec_id: str, meta: dict[str, Any]) -> Path:
    """Папка записи в хранилище тяжёлых файлов. Имя — дата и название.

    Если саму запись уже удалили, папку не заводим и задачу обрываем: иначе
    прерванная обработка складывала бы гигабайты в папку записи, которой нет.
    """
    from . import obsidian

    if not store.rec_dir(rec_id).exists():
        raise RuntimeError("Запись удалена — обработка остановлена.")

    folder = str(meta.get("assets_folder") or "").strip()
    if not folder:
        created = str(meta.get("created_at") or "")[:10]
        title = str(meta.get("title") or rec_id)
        folder = obsidian.safe_name("%s %s" % (created, title), limit=80)
        store.update(rec_id, {"assets_folder": folder})
    path = store.assets_dir(rec_id, folder)
    path.mkdir(parents=True, exist_ok=True)
    return path


def rename_assets_folder(rec_id: str, new_name: str) -> Path | None:
    """Переименовать папку тяжёлых файлов — когда модель придумала имя «по сути»."""
    from . import obsidian

    meta = store.get(rec_id) or {}
    safe = obsidian.safe_name(new_name, limit=80)
    old = str(meta.get("assets_folder") or "")
    if not safe or safe == old:
        return None
    src = store.assets_dir(rec_id, old) if old else None
    dst = store.assets_dir(rec_id, safe)
    try:
        if src is not None and src.exists() and not dst.exists():
            src.rename(dst)
        dst.mkdir(parents=True, exist_ok=True)
    except OSError as err:
        log.info("папку переименовать не вышло (%s), оставляю прежнюю", err)
        return None
    patch: dict[str, Any] = {"assets_folder": safe}
    video = str(meta.get("video_path") or "")
    if video and src is not None and video.startswith(str(src)):
        patch["video_path"] = str(dst / Path(video).name)
    store.update(rec_id, patch)
    return dst


# ---------------------------------------------------------------- шаг «текст»


def _segments_from_subs(rec_id: str, path: Path, handle: Any) -> int:
    """Готовые субтитры вместо распознавания: бесплатно и с именами говорящих."""
    from . import subs

    segs = subs.load_segments(path, handle=handle)
    if not segs:
        raise RuntimeError("В файле субтитров не нашлось ни одной реплики.")
    store.replace_segments(rec_id, segs)
    speakers = subs.speakers_from_segments(segs)
    patch: dict[str, Any] = {
        "transcript_source": "subs",
        "subs_name": path.name,
        "speakers": speakers,
        "status": "recorded",
    }
    # Если имена в субтитрах есть (Teams их даёт), разметка говорящих не нужна:
    # имена уже настоящие, а pyannote дал бы безличных «Спикер 1».
    if speakers:
        patch["diarized"] = True
        patch["diarize_status"] = "subs"
    store.update(rec_id, patch)
    if speakers:
        # Имена — сразу в базу голосов (решение 13.09): связать с
        # человеком по имени или завести. Голоса выучатся позже, по звуку.
        from . import speakers as spk

        try:
            spk.link_names(rec_id)
        except Exception as err:
            log.warning("имена из расшифровки не попали в базу голосов: %s", err)
    store.refresh_participants(rec_id)
    handle.log("взят готовый текст: реплик %d, голосов %d"
         % (len(segs), len(speakers)))
    return len(segs)


def _segments_from_asr(rec_id: str, wav: Path, duration: float,
                       opts: dict[str, Any], handle: Any) -> int:
    """Распознать звук: на этом компьютере или в облаке — по выбору человека."""
    where = asr_where(opts)
    lang = _lang(opts)
    if where == "cloud":
        # Считает чужой сервер — нашему процессору ждать незачем.
        return _segments_from_cloud(rec_id, wav, opts, handle)
    with handle.heavy("жду очереди на распознавание"):
        return _segments_from_local(rec_id, wav, duration, lang, handle)


def _segments_from_local(rec_id: str, wav: Path, duration: float, lang: str,
                         handle: Any) -> int:
    from . import asr, vad

    if lang == "en":
        ok, why = _english_ready()
        if not ok:
            raise RuntimeError(why)
    pcm, _sr = audio_io.read_wav(wav)
    spans = vad.split_for_asr(pcm)
    handle.log("фрагментов речи: %d" % len(spans))
    # Точная модель работает примерно за 0,18 от длительности записи
    try:
        handle.eta(max(5.0, duration * 0.18))
    except Exception:
        pass

    def prog(frac: float) -> None:
        handle.progress(0.45 + 0.40 * float(frac), "распознано %.0f%%" % (frac * 100))

    pieces = _run_local_asr(asr, pcm, spans, lang, prog)
    segs: list[dict[str, Any]] = []
    for p in pieces:
        seg = store.make_segment(store.TRACK_FILE, p["start"], p["end"], p["text"],
                                 speaker="Участник", speaker_key="file")
        seg["words"] = p.get("words") or []
        segs.append(seg)
    store.replace_segments(rec_id, segs)
    store.update(rec_id, {
        "status": "recorded",
        "transcript_source": "local_en" if lang == "en" else "local",
        "asr_model": _local_model_name(lang),
        "asr_lang": lang,
    })
    handle.log("распознано реплик: %d" % len(segs))
    return len(segs)


def _run_local_asr(asr: Any, pcm: Any, spans: Any, lang: str,
                   prog: Callable[[float], None]) -> list[dict[str, Any]]:
    """Вызов распознавания. Английская ветка появилась позже, поэтому мягко.

    Если сборка asr.py ещё не знает про язык, русский путь работает как всегда,
    а на английский честно говорим, что он не готов, вместо тихой подмены языка.
    """
    try:
        return asr.transcribe_spans(pcm, spans, precise=True, words=True,
                                    lang=lang, progress=prog)
    except TypeError:
        if lang != "ru":
            raise RuntimeError(
                "Английское распознавание в этой сборке недоступно. Выберите "
                "русский язык или распознавание в облаке."
            ) from None
        return asr.transcribe_spans(pcm, spans, precise=True, words=True, progress=prog)


def _local_model_name(lang: str) -> str:
    if lang == "en":
        return str(config.get("asr_model_en") or "parakeet-tdt-0.6b-v2")
    return str(config.get("offline_model") or "v3_e2e_rnnt")


def _english_ready() -> tuple[bool, str]:
    from . import asr

    fn = getattr(asr, "english_available", None)
    if fn is None:
        return False, ("Английское распознавание в этой сборке недоступно. "
                       "Выберите русский язык или распознавание в облаке.")
    try:
        return fn()
    except Exception as err:
        return False, "Английская модель не готова: %s" % err


def _segments_from_cloud(rec_id: str, wav: Path, opts: dict[str, Any],
                         handle: Any) -> int:
    try:
        from . import asr_cloud
    except ImportError:
        raise RuntimeError(
            "Распознавание в облаке в этой сборке недоступно — выберите "
            "распознавание на этом компьютере."
        ) from None
    ok, why = asr_cloud.available()
    if not ok:
        raise RuntimeError(why)

    def prog(frac: float, note: str = "") -> None:
        handle.progress(0.45 + 0.40 * float(frac), note or "распознаю в облаке")

    res = asr_cloud.transcribe_file(wav, handle=handle, progress=prog, lang=_lang(opts))
    segs = res.get("segments") or []
    if not segs:
        raise RuntimeError("Сервис распознавания вернул пустой текст.")
    store.replace_segments(rec_id, segs)
    store.update(rec_id, {
        "status": "recorded",
        "transcript_source": "cloud",
        "asr_model": res.get("model") or "",
        "asr_degraded": bool(res.get("degraded")),
        "asr_words": bool(res.get("words")),
        "asr_cost": res.get("cost") or "",
        "asr_lang": _lang(opts),
    })
    tail = "" if res.get("words") else ", отметки только по репликам"
    handle.log("распознано в облаке: реплик %d%s" % (len(segs), tail))
    return len(segs)


# ---------------------------------------------------------------- конвейер


def _extract_audio(rec_id: str, src: Path, handle: Any) -> float:
    """Вынуть звук в 16 кГц моно — в том же виде, в котором его пишет микрофон."""
    handle.log("извлекаю звук")
    dst = store.track_path(rec_id, store.TRACK_FILE)
    duration = audio_io.ffmpeg_to_wav16k(src, dst)
    store.update(rec_id, {"duration_s": round(duration, 2),
                          "status": "processing",
                          "tracks": [store.TRACK_FILE]})
    handle.progress(0.42, "звук извлечён, %.0f c" % duration)
    return duration


def _keep_video(rec_id: str, meta: dict[str, Any], src: Path, handle: Any) -> Path | None:
    """Перенести скачанное видео в хранилище тяжёлых файлов."""
    folder = _assets_dir(rec_id, meta)
    dst = folder / ("video" + (src.suffix or ".mp4"))
    try:
        # Источник мог скачать файл сразу на место (GetCourse кладёт video.mp4
        # в ту же папку): тогда переносить нечего, а «убрать старое» стёрло бы
        # только что скачанное видео.
        same = False
        try:
            same = src.resolve() == dst.resolve()
        except OSError:
            same = str(src) == str(dst)
        if not same:
            if dst.exists():
                dst.unlink()
            shutil.move(str(src), str(dst))
    except OSError as err:
        log.warning("видео не переехало в хранилище (%s), оставляю на месте", err)
        return None
    store.update(rec_id, {"video_path": str(dst),
                          "video_bytes": dst.stat().st_size})
    handle.log("видео сохранено: %s" % dst)
    return dst


def _save_transcript_copy(rec_id: str, meta: dict[str, Any],
                          opts: dict[str, Any] | None = None) -> None:
    """Текстовая копия стенограммы рядом с видео — чтобы читалась без приложения.

    Если хранить просили «ничего», копию не делаем: заводить папку ради одного
    текстового файла незачем, текст и так есть в заметке.
    """
    from . import minutes

    if opts is not None and _store_mode(opts) == "none":
        return
    try:
        folder = _assets_dir(rec_id, store.get(rec_id) or meta)
        text = minutes.build_transcript_text(rec_id)
        (folder / "transcript.txt").write_text(text, encoding="utf-8", newline="\n")
    except Exception as err:
        log.info("копию стенограммы рядом с видео не сохранил: %s", err)


def process(rec_id: str, get_source: Callable[[Any], dict[str, Any]],
            opts: dict[str, Any], handle: Any) -> dict[str, Any]:
    """Общий конвейер: добыть → получить текст → разметить → саммари → заметка.

    get_source(handle) отдаёт словарь по договору источников: media, transcript,
    title, url, source_name, duration_s. Всё, что дальше, для всех источников
    одинаково, поэтому источники ничего про конвейер не знают.
    """
    handle = jobs.as_handle(handle)
    meta = store.get(rec_id) or {}
    video_only = _flag(opts, "video_only")

    # --- этап 1: добыть файл и, если есть, готовый текст
    if not _done(rec_id, "media"):
        handle.check()
        handle.progress(0.02, "получаю файл")
        got = get_source(handle) or {}
        media = str(got.get("media") or "")
        subs_path = str(got.get("transcript") or "")
        patch: dict[str, Any] = {}
        if got.get("title"):
            patch["title"] = str(got["title"])[:200]
        if got.get("url"):
            patch["url"] = str(got["url"])
        if got.get("source_name"):
            patch["source_name"] = str(got["source_name"])
        if got.get("duration_s"):
            patch["duration_s"] = round(float(got["duration_s"]), 2)
        if patch:
            store.update(rec_id, patch)
        meta = store.get(rec_id) or meta

        if subs_path:
            # Текст субтитров кладём в папку записи, чтобы он не зависел от
            # временных файлов. Если исходник исчез — говорим об этом прямо:
            # иначе дальше конвейер уйдёт в распознавание и выдаст невнятное
            # «нет звука», хотя дело совсем не в звуке.
            src_subs = Path(subs_path)
            if not src_subs.exists():
                raise RuntimeError("Файл с текстом не найден: %s" % src_subs.name)
            kept = store.paths(rec_id)["subs"]
            try:
                if src_subs != kept:
                    shutil.copyfile(src_subs, kept)
            except OSError as err:
                raise RuntimeError("Не удалось сохранить текст записи: %s" % err) from None
            store.update(rec_id, {"subs_path": str(kept)})

        if media:
            src = Path(media)
            if not src.exists():
                raise RuntimeError("Файл не найден: %s" % src.name)
            handle.progress(0.35, "файл получен")
            if _keeps_video(opts):
                saved = _keep_video(rec_id, store.get(rec_id) or meta, src, handle)
                src = saved or src
            if not video_only:
                _extract_audio(rec_id, src, handle)
            if not _keeps_video(opts):
                try:
                    src.unlink(missing_ok=True)
                except OSError:
                    pass
        elif not subs_path:
            raise RuntimeError("По этому источнику не нашлось ни файла, ни текста.")
        _mark(rec_id, "media")

    if video_only:
        store.update(rec_id, {"status": "recorded"})
        _mark(rec_id, "text", "skip")
        handle.progress(1.0, "готово")
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        hub.publish({"type": "notice", "level": "ok",
              "text": "Видео скачано: %s" % (store.get(rec_id) or {}).get("title")})
        return {"video_only": True}

    # --- этап 2: текст. Готовые субтитры дешевле распознавания в любом случае
    count = 0
    if not _done(rec_id, "text"):
        handle.check()
        meta = store.get(rec_id) or meta
        kept = store.paths(rec_id)["subs"]
        have_subs = kept.exists() and kept.stat().st_size > 32
        if have_subs and _flag(opts, "prefer_transcript"):
            count = _segments_from_subs(rec_id, kept, handle)
        else:
            wav = store.track_path(rec_id, store.TRACK_FILE)
            if not wav.exists():
                raise RuntimeError("Нет звука для распознавания.")
            duration = float((store.get(rec_id) or {}).get("duration_s") or 0.0)
            count = _segments_from_asr(rec_id, wav, duration, opts, handle)
        _mark(rec_id, "text")
        from . import echo

        echo.mark(rec_id)
        hub.publish({"type": "segments", "rec_id": rec_id,
              "segments": store.sorted_segments(rec_id)})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
    handle.progress(0.86, "текст готов")

    # --- этап 3: говорящие. Если имена пришли из субтитров, разметка не нужна
    meta = store.get(rec_id) or meta
    if not _done(rec_id, "diarize"):
        if str(meta.get("diarize_status")) == "subs":
            # Имена настоящие, переподписывать не нужно. Но если звук есть — по
            # нему можно выучить голоса этих людей для будущих записей.
            has_audio = store.track_path(rec_id, store.TRACK_FILE).exists()
            if (has_audio and KIND_DEFAULTS[_kind(opts)]["diarize"]
                    and _flag(opts, "diarize_auto")):
                try:
                    diarize_jobs.queue_diarize(rec_id)
                    _mark(rec_id, "diarize", "queued")
                    handle.log("имена взяты из расшифровки; голоса этих людей выучу по звуку")
                except Exception as err:
                    log.info("обучение голосам не поставил в очередь: %s", err)
                    _mark(rec_id, "diarize", "subs")
            else:
                _mark(rec_id, "diarize", "subs")
                handle.log("имена взяты из субтитров, разметка голосов не нужна")
        elif not KIND_DEFAULTS[_kind(opts)]["diarize"]:
            _mark(rec_id, "diarize", "skip")
            handle.log("тип записи «%s» — без разметки голосов" % _kind(opts))
        elif _flag(opts, "diarize_auto"):
            try:
                diarize_jobs.queue_diarize(rec_id)
                _mark(rec_id, "diarize", "queued")
                handle.log("разметка голосов поставлена в очередь")
            except Exception as err:
                log.info("разметку не поставил в очередь: %s", err)

    _save_transcript_copy(rec_id, meta, opts)

    # --- этап 4: документ. Отдельная задача, чтобы скачивание не ждало модель.
    # У типа «только расшифровка» документа нет вовсе — за него и не платим.
    document = str(KIND_DEFAULTS[_kind(opts)]["document"])
    if document and _flag(opts, "make_summary") and not _done(rec_id, "summary"):
        handle.log("документ поставлен в очередь")
        try:
            submit_summary(rec_id, document=document)
        except Exception as err:
            log.info("документ не поставил в очередь: %s", err)
    elif not document:
        _mark(rec_id, "summary", "skip")

    # Хранить нечего: звук больше не нужен. Ждём разметку, если она поставлена
    # в очередь, — ей дорожка ещё понадобится; уберёт её служба, когда закончит.
    if _store_mode(opts) == "none":
        if _stages(rec_id).get("diarize") == "queued":
            handle.log("звук будет убран после разметки голосов")
        else:
            freed = store.drop_media(rec_id).get("freed_bytes", 0)
            handle.log("звук убран, освободилось %.1f МБ" % (freed / 1048576.0))

    handle.progress(1.0, "готово")
    hub.publish({"type": "notice", "level": "ok",
          "text": "Готово: %s (%d реплик)" % (meta.get("title"), count)})
    return {"segments": count, "rec_id": rec_id}


# ---------------------------------------------------------------- задания


def _new_record(title: str, opts: dict[str, Any], source: str,
                source_name: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        "doc_kind": "summary",      # в заметке раздел называется «Краткое содержание»
        "stages": {},
        "asr_files": asr_where(opts),
        "asr_lang": _lang(opts),
        # Тип и способ хранения запоминаем в самой записи: обработка идёт
        # этапами и может продолжиться в другом запуске службы, когда форма
        # с настройками давно закрыта.
        "video_kind": _kind(opts),
        "store_media": _store_mode(opts),
    }
    payload.update(extra or {})
    return store.create(
        title=title,
        mode="file",
        source=source,
        source_name=source_name or None,
        category=str(opts.get("category") or category_for_kind(_kind(opts))),
        extra=payload,
    )


def submit_file(tmp: Path, name: str, opts: dict[str, Any],
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Готовый файл: видео, аудио или сами субтитры.

    `extra` — дополнительные поля карточки записи: так входящие с телефона
    помнят, откуда пришли (папка или страница в сети), оставаясь для страницы
    и заметки обычной записью из файла.
    """
    from . import subs

    is_subs = subs.is_transcript_name(name)
    meta = _new_record(Path(name).stem, opts, "file", name, extra)
    rec_id = meta["id"]

    def get_source(handle: Any) -> dict[str, Any]:
        if is_subs:
            return {"transcript": str(tmp), "title": Path(name).stem,
                    "source_name": name}
        return {"media": str(tmp), "title": Path(name).stem, "source_name": name}

    def work(handle: Any) -> dict[str, Any]:
        try:
            return process(rec_id, get_source, opts, handle)
        finally:
            # Загруженный файл убираем всегда: если видео хранится, оно уже
            # переехало в хранилище и по этому пути его нет; если обработка
            # упала или отменена — иначе файл оставался бы в data\_uploads
            # навсегда. Субтитры к этому моменту
            # скопированы в папку записи.
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError:
                pass

    job_id = jobs.submit("media", work, "Обработка файла: %s" % name, rec_id=rec_id)
    hub.publish({"type": "recordings"})
    return {"job_id": job_id, "rec_id": rec_id, "meta": store.get(rec_id)}


def submit_link(url: str, info: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    """Ссылка на видео: YouTube и всё остальное, что понимает yt-dlp."""
    from . import fetch

    meta = _new_record(str(info.get("title") or "Видео по ссылке"), opts, "link",
                       str(info.get("webpage_url") or url),
                       extra={"url": str(info.get("webpage_url") or url),
                              "uploader": str(info.get("uploader") or ""),
                              "duration_s": round(float(info.get("duration_s") or 0.0), 2)})
    rec_id = meta["id"]
    proxy = str(info.get("proxy") or "") or None
    want_subs = _flag(opts, "prefer_transcript")
    auto_subs = _flag(opts, "yt_auto_subs")
    want_video = _keeps_video(opts)

    def get_source(handle: Any) -> dict[str, Any]:
        folder = _assets_dir(rec_id, store.get(rec_id) or meta)
        subs_path = ""
        if want_subs:
            handle.log("смотрю, есть ли готовые субтитры")
            try:
                got = fetch.download_subs(url, folder, auto_ok=auto_subs, proxy=proxy)
            except Exception as err:
                log.info("субтитры не получились: %s", err)
                got = {}
            if got:
                subs_path = str(got["path"])
                store.update(rec_id, {"subs_kind": got.get("kind") or "",
                                      "subs_lang": got.get("lang") or ""})
        # Если текст уже есть и видео хранить не просили — качать нечего вовсе.
        if subs_path and not want_video:
            handle.log("текст взят субтитрами, видео не качаю")
            return {"transcript": subs_path, "title": info.get("title"),
                    "url": info.get("webpage_url") or url,
                    "source_name": info.get("webpage_url") or url,
                    "duration_s": info.get("duration_s") or 0.0}

        def dl(frac: float, note: str) -> None:
            handle.progress(0.02 + 0.30 * float(frac), note)

        def stop() -> bool:
            try:
                return bool(handle.cancelled)
            except Exception:
                return False

        got = fetch.download_media(
            url, folder,
            mode="video" if want_video else "audio",
            max_height=_int(opts, "max_height", 720),
            proxy=proxy, progress=dl, cancelled=stop,
        )
        return {"media": str(got["path"]), "transcript": subs_path,
                "title": got.get("title"), "url": got.get("webpage_url") or url,
                "source_name": got.get("webpage_url") or url,
                "duration_s": got.get("duration_s") or 0.0}

    def work(handle: Any) -> dict[str, Any]:
        return process(rec_id, get_source, opts, handle)

    job_id = jobs.submit("media", work,
                         "Видео по ссылке: %s" % (info.get("title") or url),
                         rec_id=rec_id)
    hub.publish({"type": "recordings"})
    return {"job_id": job_id, "rec_id": rec_id, "meta": store.get(rec_id), "info": info}


def submit_source(kind: str, payload: dict[str, Any],
                  opts: dict[str, Any], job_extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сторонние площадки: GetCourse, SharePoint, сайт со входом по паролю."""
    title = str(payload.get("title") or payload.get("name") or "Запись")
    if kind == "sharepoint" and payload.get("name"):
        title = Path(str(payload["name"])).stem
    url = str(payload.get("url") or payload.get("webUrl") or payload.get("web_url") or "")
    extra: dict[str, Any] = {"url": url, "site": kind}
    if kind == "sharepoint" and payload.get("id"):
        # Какой именно файл SharePoint: по этим полям поиск помечает записи,
        # которые уже обрабатывались («✓ уже есть»).
        extra.update({"sp_item_id": str(payload.get("id")),
                      "sp_drive_id": str(payload.get("driveId") or ""),
                      "sp_date": str(payload.get("date") or "")})
    meta = _new_record(title, opts, "link", url or kind, extra=extra)
    rec_id = meta["id"]

    def get_source(handle: Any) -> dict[str, Any]:
        folder = _assets_dir(rec_id, store.get(rec_id) or meta)
        if kind == "gcvh":
            from .sources import gcvh

            return gcvh.fetch(url, folder, handle=handle)
        if kind == "sharepoint":
            from .sources import sharepoint

            try:
                return sharepoint.fetch(payload, folder, handle=handle, opts=opts)
            except Exception:
                # Грабли старого проекта: недоступная запись оставляла пустую
                # папку-сироту в хранилище видео.
                try:
                    if folder.exists() and not any(folder.iterdir()):
                        folder.rmdir()
                        store.update(rec_id, {"assets_folder": None})
                except OSError:
                    pass
                raise
        if kind == "webauth":
            from .sources import webauth

            items = webauth.capture([url], str(payload.get("login") or ""),
                                    str(payload.get("password") or ""), folder,
                                    handle=handle)
            if not items:
                raise RuntimeError("С этой страницы не удалось получить поток.")
            return items[0]
        raise RuntimeError("Неизвестный источник: %s" % kind)

    def work(handle: Any) -> dict[str, Any]:
        return process(rec_id, get_source, opts, handle)

    names = {"sharepoint": "SharePoint", "gcvh": "GetCourse", "webauth": "Сайт"}
    job_id = jobs.submit("media", work, "%s: %s" % (names.get(kind, kind), title),
                         rec_id=rec_id, extra=job_extra)
    hub.publish({"type": "recordings"})
    return {"job_id": job_id, "rec_id": rec_id, "meta": store.get(rec_id)}


def submit_summary(rec_id: str, document: str | None = None,
                   engine: str | None = None) -> str:
    """Собрать документ по уже готовой стенограмме отдельной задачей.

    Жанр — из типа записи: у встречи саммари, у лекции конспект, у интервью
    выжимка. Звук и видео для этого не нужны, поэтому кнопка работает и после
    того, как их удалили.
    """
    from . import minutes, obsidian

    meta = store.get(rec_id)
    if meta is None:
        raise ValueError("Запись не найдена")
    document = document or str(meta.get("video_kind") or "meeting")
    if document not in ("meeting", "lecture", "interview"):
        document = "meeting"
    names = {"meeting": "Краткое содержание", "lecture": "Конспект", "interview": "Выжимка"}

    def work(handle: Any) -> dict[str, Any]:
        res = minutes.generate_video_summary(rec_id, handle=handle, document=document,
                                             engine=engine)
        # Папку «по сути» переименовываем только у записей, у которых она есть:
        # у записи с микрофона папки видео нет, и заводить её незачем.
        fresh = store.get(rec_id) or {}
        if (res.get("folder_name") and config.get("smart_folder_name")
                and fresh.get("assets_folder")):
            rename_assets_folder(rec_id, str(res["folder_name"]))
        # Карта этапов — только у видеозаписей. У записи с микрофона её быть не
        # должно: по ней служба при перезапуске считает запись видеозадачей.
        staged = isinstance(fresh.get("stages"), dict)
        if staged:
            _mark(rec_id, "summary")
        try:
            section = minutes.stored_document(rec_id, document) or res["markdown"]
            saved = obsidian.append_minutes(rec_id, section,
                                            heading=minutes.DOC_KINDS[document]["heading"])
            if staged:
                _mark(rec_id, "note")
        except Exception as err:
            log.warning("запись %s: заметка не сохранилась: %s", rec_id, err)
            hub.publish({"type": "notice", "level": "err",
                  "text": "Документ готов, но в Obsidian не записался: %s" % err})
            saved = {}
        # Готовый текст показываем прямо в программе, как это делает протокол:
        # иначе саммари молча уезжало бы в заметку, и человек не видел, что
        # получилось, пока не откроет хранилище.
        hub.publish({"type": "minutes", "rec_id": rec_id,
              "markdown": res["markdown"], "template": document})
        hub.publish({"type": "recording", "meta": store.get(rec_id)})
        done = {"meeting": "Краткое содержание готово", "lecture": "Конспект готов",
                "interview": "Выжимка готова"}
        hub.publish({"type": "notice", "level": "ok", "text": done.get(document, "Документ готов")})
        return {"markdown_bytes": len(res.get("markdown") or ""),
                "saved_to": saved.get("path") or ""}

    return jobs.submit("summary", work,
                       "%s: %s" % (names.get(document, "Документ"), meta.get("title")),
                       rec_id=rec_id,
                       lane=minutes.document_lane(engine),
                       extra={"engine": engine or config.get("minutes_engine"),
                              "retry": {"url": "/api/media/%s/summary" % rec_id,
                                        "body": {"document": document}}})
