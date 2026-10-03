#!/usr/bin/env python3
"""i18n: language and translation support."""
import json, os

_LOCALES = {}
_LOCALE_DIR = None

def _init():
    """Load locale files once."""
    global _LOCALES, _LOCALE_DIR
    if _LOCALE_DIR is not None:
        return
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # bin -> plan-gate
    loaded = {}
    for lang in ("en", "pt-BR"):
        path = os.path.join(root, "locales", f"{lang}.json")
        with open(path) as f:
            loaded[lang] = json.load(f)
    # R72: published only when complete -- `phase start` may translate on the main thread while an abandoned
    # lookup thread is still inside this function
    _LOCALES = loaded
    _LOCALE_DIR = root

def language(repo=None):
    """Return the language setting for a repository or system default.

    Priority: repo config (if supported) > PLAN_GATE_LANG env (if supported) > 'en'
    Supported values: exactly ("en", "pt-BR").
    Unsupported values: print warning on stderr and fall through to next priority.
    Returns: exactly "en" or "pt-BR".
    """
    import config, sys

    SUPPORTED = ("en", "pt-BR")

    # Get language from config (which handles repo config reading)
    cfg_lang = config.load(repo).get("language")
    if cfg_lang is not None:
        if isinstance(cfg_lang, str) and cfg_lang in SUPPORTED:
            return cfg_lang
        if cfg_lang is not None:  # Print warning only if value exists but is unsupported
            print(f"Warning: unsupported language '{cfg_lang}' in repo config, falling back", file=sys.stderr)

    # Check environment variable
    env_lang = os.environ.get("PLAN_GATE_LANG")
    if env_lang:
        if env_lang in SUPPORTED:
            return env_lang
        # Print warning for unsupported env value
        print(f"Warning: unsupported PLAN_GATE_LANG='{env_lang}', falling back to 'en'", file=sys.stderr)

    # Default to English
    return "en"

def t(key, repo=None, **kwargs):
    """Translate a key, optionally formatting with kwargs.

    Falls back: pt-BR missing -> en -> KeyError
    """
    return translate(key, language(repo), **kwargs)

def translate(key, lang, **kwargs):
    """`t` in a language already resolved: reads no config and no environment, so it never raises on a broken
    config and is safe from a worker thread (R72). Same fallback as `t`."""
    _init()

    # Try the requested language first
    if lang in _LOCALES and key in _LOCALES[lang]:
        value = _LOCALES[lang][key]
        return value.format(**kwargs) if kwargs else value

    # Fall back to English if pt-BR is missing
    if lang != "en" and key in _LOCALES["en"]:
        value = _LOCALES["en"][key]
        return value.format(**kwargs) if kwargs else value

    # Not found in either
    raise KeyError(key)
