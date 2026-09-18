# -*- coding: utf-8 -*-
"""Проверка 7: все функции, которые вызывает сервер, существуют с нужными именами."""
import inspect
import io
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))

LINES = []


def say(msg):
    LINES.append(str(msg))
    try:
        print(str(msg), flush=True)
    except Exception:
        # Консоль не знает этих букв (бывает cp1251) — печатаем без них.
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"), flush=True)
        except Exception:
            pass


NEED = {
    "diarize": [
        "available", "load_pipeline", "diarize_pcm", "estimate_seconds",
        "relabel", "save_result", "load_result",
    ],
    "voices": [
        "list_people", "get_person", "add_sample", "rename_person", "delete_person",
        "cosine", "match", "match_all", "apply_to_meta", "stats",
    ],
    "obsidian": [
        "ensure_vault", "list_categories", "add_category", "note_path",
        "render_markdown", "save_note", "append_minutes", "vault_status",
    ],
    "minutes": [
        "TEMPLATES", "build_transcript_text", "resolve_claude_cli", "run_claude_cli",
        "available_engines", "chunk_transcript", "generate", "cloud_warning",
    ],
    "platform.windows.desktop": [
        "list_sessions", "list_capture_sessions", "detect_call", "call_output_device",
        "watch_calls", "outlook_kind", "current_meeting", "suggest_title",
        "attendee_names", "suggest_max_speakers",
    ],
}

bad = 0
for mod_name, names in NEED.items():
    say("")
    say("=== hagen.%s ===" % mod_name)
    try:
        mod = __import__("hagen." + mod_name, fromlist=["*"])
    except Exception as err:
        say("   МОДУЛЬ НЕ ИМПОРТИРУЕТСЯ: %r" % (err,))
        bad += len(names)
        continue
    for n in names:
        obj = getattr(mod, n, None)
        if obj is None:
            say("   НЕТ      %s" % n)
            bad += 1
            continue
        if callable(obj):
            try:
                sig = str(inspect.signature(obj))
            except (TypeError, ValueError):
                sig = "(?)"
            say("   есть     %s%s" % (n, sig))
        else:
            kind = type(obj).__name__
            extra = ""
            if isinstance(obj, dict):
                extra = " ключи: %s" % sorted(obj.keys())
            say("   есть     %s : %s%s" % (n, kind, extra))

# проверка сторожа звонков
try:
    from hagen.platform.windows import desktop

    cw = desktop.CallWatcher
    for m in ("start", "stop", "snooze"):
        say("   CallWatcher.%s: %s" % (m, "есть" if hasattr(cw, m) else "НЕТ"))
    say("   CallWatcher.__init__%s" % str(inspect.signature(cw.__init__)))
    say("   CallWatcher.state: %s" % ("есть" if hasattr(cw, "state") else "НЕТ"))
except Exception as err:
    say("   CallWatcher: %r" % (err,))

say("")
say("ИТОГО пропущено обязательных имён: %d" % bad)
io.open(PROJECT / "tests" / "t7_result.txt", "w", encoding="utf-8").write("\n".join(LINES))
sys.exit(0 if bad == 0 else 1)
