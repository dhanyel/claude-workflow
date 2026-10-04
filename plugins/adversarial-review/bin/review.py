#!/usr/bin/env python3
"""review: one adversarial review round of a plan (or, with --as-spec, of a spec).

Exit: 0 verdict recorded; 2 refused before spending anything; 3 NO VERDICT (verdicts untouched).

⚠️ No flag chooses the backend, the model, the route or the URL: that is the user's ENVIRONMENT (settings.py). A
per-round flag is where the INTERNAL had its false-label and wrong-quota findings (spec §3.2).
"""
import argparse, importlib, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gate, i18n, loop, settings  # noqa: E402

MODULES = {"codex": "backend_codex", "opencode": "backend_opencode", "endpoint": "backend_opencode"}


def load_backend(name):
    module_name = MODULES[name]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as e:
        if e.name != module_name:
            raise  # the backend exists but one of ITS imports is missing: a bug, not a refusal
        raise loop.Refused("review.bad_backend", backend=name, problem=f"module {module_name} is not installed")
    problem = loop.backend_problem(module)
    if problem:
        raise loop.Refused("review.bad_backend", backend=name, problem=problem)
    return module


def positive_seconds(value):
    try:
        seconds = int(value)
    except ValueError:
        seconds = 0
    if seconds <= 0:
        # a 0 or negative deadline would end every round before the backend could answer: refuse it up front
        raise argparse.ArgumentTypeError(i18n.t("review.bad_timeout", i18n.resolve_language(None), value=value))
    return seconds


def main(argv=None):
    p = argparse.ArgumentParser(prog="review.py")
    p.add_argument("path")
    p.add_argument("--spec", default=None)
    p.add_argument("--as-spec", action="store_true")
    p.add_argument("--thinking", action="store_true")
    p.add_argument("--redo", action="store_true")
    p.add_argument("--prompt-only", action="store_true")
    p.add_argument("--timeout", type=positive_seconds, default=loop.ROUND_TIMEOUT_S)
    a = p.parse_args(argv)
    path = os.path.abspath(a.path)
    lang = "en"
    try:
        if not os.path.isfile(path):
            raise loop.Refused("review.no_file", path=path)
        cfg = gate.repo_config(path)  # asked ONCE: the language now, the folders inside run_round
        lang = i18n.resolve_language(cfg.get("language"))
        s = settings.resolve()
        options = loop.Options(thinking=a.thinking or s.thinking, redo=a.redo, prompt_only=a.prompt_only,
                               timeout_s=a.timeout, as_spec=a.as_spec,
                               spec=os.path.abspath(a.spec) if a.spec else None)
        return loop.run_round(path, load_backend(s.backend), s, options, gate_api=gate, cfg=cfg)
    except settings.SettingsError as e:
        print(i18n.t(e.key, lang, **e.kwargs), file=sys.stderr)
    except gate.GateUnavailable as e:
        print(i18n.t("review.gate_unavailable", lang, error=e), file=sys.stderr)
    except loop.Refused as e:
        print(i18n.t(e.key, lang, **e.kwargs), file=sys.stderr)
    except loop.StateFailed as e:
        # nothing was recorded, so the verdicts really are untouched: NO VERDICT, not a refusal
        print(i18n.t("review.state_failed", lang, error=e.error, folder=e.folder), file=sys.stderr)
        return 3
    return 2


if __name__ == "__main__":
    sys.exit(main())
