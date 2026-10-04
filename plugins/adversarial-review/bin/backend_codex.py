#!/usr/bin/env python3
"""Backend `codex`: one `codex exec` pass, run on the user's own Codex login (spec §5.1).

⚠️ CAN_REPAIR = False: `codex exec` cannot resume a session. Without the flag the common loop would call `repair`
and die with AttributeError -- an invalid JSON would become a crash instead of NO VERDICT.

⚠️ CAN_MODEL = False: `codex exec` has no `--model`; `review` never reads `settings.model`. The round declares a
model only for a backend that can take one, so `ADVERSARIAL_REVIEW_MODEL` + codex does not write a FALSE label.
"""
import glob, os, re, shutil, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import gate, loop, settings as settings_mod  # noqa: E402

CAN_REPAIR = False
CAN_MODEL = False


def find_codex():
    """`which` first, nvm second, bare name last.

    ⚠️ A hook runs with a lean PATH that lacks the nvm shim: looking only at PATH made the reviewer "not exist" in a
    hook session; looking only in nvm would break whoever installed codex another way.
    """
    found = shutil.which("codex")
    if found:
        return found
    candidates = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/codex"))
    return max(candidates, key=_nvm_version_key) if candidates else "codex"


def _nvm_version_key(codex_path):
    """Numeric (major, minor, patch) of the `node/<vX.Y.Z>` folder; an unparseable folder sorts lowest.

    ⚠️ Not lexicographic: "v9.11.2" would beat "v18.0.0".
    """
    folder = os.path.basename(os.path.dirname(os.path.dirname(codex_path)))
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", folder)
    return (1, tuple(int(g) for g in m.groups()), codex_path) if m else (0, (), codex_path)


def route(settings):
    return "codex (user account)"


def review(*, path, repo, prompt, output_json, settings, options, deadline):
    """One `codex exec` pass; `deadline` is the loop's ABSOLUTE time.monotonic() limit."""
    codex = find_codex()
    cmd = [codex, "exec", "-C", repo, "-s", "read-only", "--output-schema", loop.SCHEMA, "-o", output_json, prompt]
    started = time.monotonic()
    remaining = max(1, int(deadline - started))
    try:
        # ⚠️ stdin=DEVNULL: otherwise `codex exec` may wait for input inside a hook and the round's ceiling turns
        # into a hung process.
        # ⚠️ env without the endpoint credential: codex is not the endpoint, and a child that holds the token can
        # print it, log it, or hand it to a tool (final review S5).
        done = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, errors="replace",
                              timeout=remaining, env=gate.env_without_secrets())
    except subprocess.TimeoutExpired:
        return loop.Result(False, f"codex ran past the round's time limit ({options.timeout_s}s)")
    except OSError as e:
        return loop.Result(False, f"could not run `{codex}`: {e}")
    if done.returncode != 0:
        stderr = done.stderr
        for secret in (getattr(settings, "token", ""), os.environ.get(settings_mod.ENV_TOKEN, "")):
            stderr = loop.mask_secret(stderr, secret)      # masked BEFORE the cut
        return loop.Result(False, f"codex exited {done.returncode}: {stderr.strip()[:200]}")
    return loop.Result(True, telemetry={"wall_s": int(time.monotonic() - started)})
