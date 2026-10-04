#!/usr/bin/env python3
"""i18n for adversarial-review. The language is resolved by the caller (the plan-gate's resolved config
"language", else PLAN_GATE_LANG, else en) and passed in -- this module never reads a config."""
import json, os, sys

SUPPORTED = ("en", "pt-BR")
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCALES = {}


def _load():
    if not _LOCALES:
        for lang in SUPPORTED:
            with open(os.path.join(_ROOT, "locales", f"{lang}.json"), encoding="utf-8") as f:
                _LOCALES[lang] = json.load(f)
    return _LOCALES


def resolve_language(configured=None):
    if configured in SUPPORTED:
        return configured
    env = os.environ.get("PLAN_GATE_LANG")
    if env in SUPPORTED:
        return env
    if configured is not None or env:
        print(f"Warning: unsupported language {configured or env!r}, using 'en'", file=sys.stderr)
    return "en"


def t(key, lang="en", **kwargs):
    locales = _load()
    for candidate in (lang, "en"):
        if key in locales.get(candidate, {}):
            value = locales[candidate][key]
            return value.format(**kwargs) if kwargs else value
    raise KeyError(key)
