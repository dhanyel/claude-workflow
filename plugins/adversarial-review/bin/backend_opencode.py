#!/usr/bin/env python3
"""Backend `opencode`/`endpoint` (spec §5.2, §5.3): the per-round `opencode.json`, the version floor, model helpers and
the child environment; then the runner -- `opencode run` under a no-byte watchdog and the round's absolute deadline,
one retry on a start-up stall, one repair turn in the same session, telemetry from the event stream, and the z.ai
keep-alive proxy.

⚠️ CAN_REPAIR = True: opencode resumes a session (`--session`), so an invalid JSON gets ONE repair turn in the same
session instead of becoming NO VERDICT. ⚠️ CAN_MODEL = True: the model really goes to `-m`.

⚠️ The endpoint token is NEVER written to the generated file: the file carries `{env:ADVERSARIAL_REVIEW_ENDPOINT_TOKEN}`
and the value travels only in the opencode child's environment (`child_env`). Nothing this module returns (reason,
telemetry) may carry it: both end up in the .md, stderr and `last_failure`.
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import i18n, loop, settings as settings_mod  # noqa: E402

PROMPTS = os.path.join(os.path.dirname(HERE), "prompts")
# ⚠️ The generated `opencode.json` has a version-bound format; the floor was measured against 1.18.x.
OPENCODE_MIN_VERSION = (1, 18, 0)
ENDPOINT_PROVIDER_ID = "adversarial-endpoint"
AGENT_NAME = "plan-reviewer"
STEPS = 25
ZAI_PROXY_PORT = 8788
ZAI_PROVIDER = "zai-coding-plan"
PROXY = os.path.join(HERE, "zai_proxy.py")
PROXY_START_S = 10      # how long ensure_proxy waits for a proxy it just started
POLL_S = 0.2            # watchdog tick
CAN_REPAIR = True
CAN_MODEL = True
_proxy_proc = None      # the proxy this process started: kept referenced so it is never collected while running


def opencode_version(exe="opencode"):
    """(major, minor, patch) of the installation at `exe`, or None when the read fails.

    ⚠️ None (not an exception) on purpose: a failed read (odd binary, no number, a hang until the timeout) must not
    block whoever has the right version; `check_opencode` only complains about a version PROVEN old.
    """
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", r.stdout or r.stderr or "")
    return tuple(int(x) for x in m.groups()) if m else None


def check_opencode(lang="en", exe=None):
    """A localized message that teaches how to fix it, or None when opencode is usable.

    The version is read from the SAME executable that will run (`exe`, else the one PATH resolves now).
    """
    exe = exe or shutil.which("opencode")
    if not exe:
        return i18n.t("opencode.missing", lang)
    v = opencode_version(exe)
    if v and v < OPENCODE_MIN_VERSION:
        return i18n.t("opencode.too_old", lang, found=".".join(map(str, v)),
                      minimum=".".join(map(str, OPENCODE_MIN_VERSION)))
    return None


def model_parts(model):
    """(provider, upstream model): the provider is what comes BEFORE the first slash.

    ⚠️ The client says `zai-coding-plan/glm-5.3`, the wire says `glm-5.3`; mixing them is a 403 on every call. A slash
    with an empty side is a MALFORMED name, not "no provider": letting it through made the provider block and the
    agent's model disagree silently.
    """
    if "/" not in model:
        return "", model
    provider, _, upstream = model.partition("/")
    if not provider or not upstream:
        raise ValueError(f"invalid model name ({model!r}); expected <provider>/<model>")
    return provider, upstream


def run_model(settings):
    """The model string passed to opencode: `endpoint` -> the declared provider; `opencode` -> the user's own."""
    if settings.backend == "endpoint":
        return f"{ENDPOINT_PROVIDER_ID}/{settings.model}"
    return settings.model


def generate_config(repo, output_json, settings, thinking, cfg_path):
    """Write the round's `opencode.json` and return it. No permission is `ask` and no key value is ever written.

    ⚠️ R18: the reviewer may write ONE file, this round's output JSON (relative and absolute spellings) -- not
    `reviews/*`, which let it rewrite earlier rounds' JSON (feeding "(none)" open blockers) and the -override.md.
    """
    with open(os.path.join(PROMPTS, "agent.md"), encoding="utf-8") as f:
        prompt = f.read()
    rel = os.path.relpath(output_json, repo)
    agent = {
        "description": "Adversarial plan reviewer (read-only, writes only its review)",
        "mode": "primary", "model": run_model(settings), "temperature": 0.1, "steps": STEPS, "prompt": prompt,
        "permission": {
            # ⚠️ Any `ask` is auto-rejected in headless and ENDS the session with no verdict (measured with
            # `external_directory`). Only allow/deny.
            # ⚠️ `"*": "deny"` FIRST (the last matching rule wins): a custom agent starts from `"*": "allow"` and the
            # user's GLOBAL MCP tools (e.g. memory write/delete) enter as their own permissions, so without it the
            # reviewer could persist a prompt injection. Everything it may do is listed after.
            "*": "deny",
            "edit": {"*": "deny", rel: "allow", output_json: "allow"},
            # ⚠️ NO shell at all (agent `tools.bash` is false too). opencode's matcher turns `*` into `.*`, so any bash
            # allowlist such as `git diff*` also matches `git difftool -x <cmd>`: arbitrary exec, and in endpoint mode
            # the process env holds the token. Reading is covered by read/glob/grep/list.
            "bash": "deny",
            "read": "allow", "glob": "allow", "grep": "allow", "list": "allow",
            "webfetch": "deny", "websearch": "deny", "task": "deny", "skill": "deny",
            "external_directory": "deny", "question": "deny", "invalid": "allow", "doom_loop": "allow"},
    }
    agent["tools"] = {"bash": False}
    if not thinking:
        agent["thinking"] = {"type": "disabled"}
    # ⚠️ lsp/formatter OFF: with them on in the user's GLOBAL config, opencode runs the REPO's eslint/prettier (repo
    # code) with the whole environment -- the endpoint token included.
    cfg = {"$schema": "https://opencode.ai/config.json", "lsp": False, "formatter": False,
           "agent": {AGENT_NAME: agent}}
    if settings.backend == "endpoint":
        cfg["provider"] = {ENDPOINT_PROVIDER_ID: {
            "npm": "@ai-sdk/openai-compatible", "name": "adversarial-review endpoint",
            "options": {"baseURL": settings.endpoint_url, "apiKey": "{env:" + settings_mod.ENV_TOKEN + "}"},
            "models": {settings.model: {"name": settings.model}}}}
    elif model_parts(settings.model)[0] == ZAI_PROVIDER:
        # ⚠️ The local proxy exists for ONE measured reason: Bun reuses the connection and z.ai hangs the second call.
        # No key here: opencode resolves it from the user's own config.
        cfg["provider"] = {ZAI_PROVIDER: {"options": {"baseURL": f"http://127.0.0.1:{ZAI_PROXY_PORT}"}}}
    # Any other provider: NO `provider` block -- the user's own opencode config applies. Emitting a key here would
    # send one provider's credential to another provider's server.
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    return cfg


def child_env(settings, cfg_path):
    """The opencode child's environment: the round's config (file and inline), no project config, the token ONLY for `endpoint`.

    ⚠️ A leftover token from the parent must not reach a child that talks to another provider.
    """
    # ⚠️ opencode merges the REPO's opencode.json and .opencode/ (agents, plugins) after OPENCODE_CONFIG: a hostile repo
    # could repoint the endpoint baseURL (token to another host), open bash, or run a plugin in the process that holds
    # the token. Project config is disabled, and OPENCODE_CONFIG_CONTENT (loaded last) restates the round's config; it
    # holds no secret, the apiKey is the `{env:...}` reference.
    with open(cfg_path, encoding="utf-8") as f:
        content = f.read()
    env = dict(os.environ, OPENCODE_CONFIG=cfg_path, OPENCODE_DISABLE_PROJECT_CONFIG="1", OPENCODE_CONFIG_CONTENT=content)
    if settings.backend == "endpoint":
        env[settings_mod.ENV_TOKEN] = settings.token
    else:
        env.pop(settings_mod.ENV_TOKEN, None)
    return env


# ---- the runner ----------------------------------------------------------------------------------------------------

def route(settings):
    """The route line of the review: where the plan went. A host, never the URL (no path, no credential)."""
    if settings.backend == "endpoint":
        return f"endpoint {settings.endpoint_host}"
    return "opencode (user provider)"


def _listening(host, port, timeout=0.5):
    with socket.socket() as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def ensure_proxy(deadline):
    """Start the local z.ai proxy unless something already listens on its port. True when it listens.

    ⚠️ `[sys.executable, PROXY, port]` with `start_new_session=True`, NOT the `setsid` binary (absent on macOS). It
    outlives the round on purpose (the next round reuses it), so it gets an environment without the endpoint token.
    """
    if _listening("127.0.0.1", ZAI_PROXY_PORT):
        return True
    if not os.path.isfile(PROXY):
        return False
    global _proxy_proc
    env = {k: v for k, v in os.environ.items() if k != settings_mod.ENV_TOKEN}
    proc = subprocess.Popen([sys.executable, PROXY, str(ZAI_PROXY_PORT)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    limit = min(deadline, time.monotonic() + PROXY_START_S)
    while time.monotonic() < limit:
        time.sleep(POLL_S)
        if _listening("127.0.0.1", ZAI_PROXY_PORT):
            _proxy_proc = proc
            return True
        if proc.poll() is not None:     # it died (and is reaped): e.g. the port is taken by something that does not answer
            return _listening("127.0.0.1", ZAI_PROXY_PORT)
    # started but never listened: no orphan left behind
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return False


def telemetry_from_stream(path, secret=""):
    """(telemetry, session, last_text) read from opencode's `--format json` event stream.

    ⚠️ `secret` is masked in every decoded event BEFORE any json.dumps or truncation: masking the cut result misses a
    token cut in half (or escaped by json.dumps) -- a provider's auth error may quote the key.

    ⚠️ Measured against a real stream (1.18.x): opencode emits NO `modelID`/`providerID`, so `model` is "" today. It
    stays because it is cheap, and whoever reads it must label it the model that ANSWERED, never the one requested.
    """
    steps, tools, tokens, session, texts = 0, {}, None, None, []
    model_id, provider_id = "", ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return {}, None, ""
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        e = _scrub(e, secret)
        session = session or e.get("sessionID")
        p = e.get("part") if isinstance(e.get("part"), dict) else {}
        model_id = model_id or e.get("modelID") or p.get("modelID") or ""
        provider_id = provider_id or e.get("providerID") or p.get("providerID") or ""
        if p.get("type") == "step-start":
            steps += 1
        elif p.get("type") == "tool":
            tools[p.get("tool")] = tools.get(p.get("tool"), 0) + 1
        elif p.get("type") == "step-finish":
            tokens = p.get("tokens")
        elif p.get("type") == "text":
            texts.append(str(p.get("text", "")))
        elif e.get("type") == "error":
            texts.append("ERROR: " + json.dumps(e.get("error"))[:300])
    model = f"{provider_id}/{model_id}" if provider_id and model_id else model_id
    return ({"steps": steps, "tools": tools, "tokens": tokens, "model": model}, session,
            _scrub(" ".join(texts), secret)[-300:])


def _scrub(value, secret):
    """`value` with every occurrence of `secret` masked, recursively -- raw AND JSON-escaped (a provider's auth error may
    quote the key inside a JSON body). It works per value: a token split across two events is not caught here; the
    round masks the reviewer's output again before writing anything (loop.mask_secret)."""
    return loop.mask_secret(value, secret)


def _symlink_in(rev_dir):
    """The first symlink at or under the reviews folder (relative path), or None.

    ⚠️ The reviewer's edit permission is a PATH glob over this folder: a committed symlink there (to src/, to ~) would
    be written THROUGH. The reviewer has no shell, so it cannot plant one during the round; the repo can.
    """
    if os.path.islink(rev_dir):
        return os.path.basename(rev_dir)
    for root, dirs, files in os.walk(rev_dir):
        for name in dirs + files:
            if os.path.islink(os.path.join(root, name)):
                return os.path.relpath(os.path.join(root, name), rev_dir)
    return None


def _prepare(repo, output_json, settings, options, deadline):
    """-> (exe, tmpdir, env, None) or (None, None, None, reason). Checks, proxy and the round's config, in this order.

    ⚠️ generate_config runs BEFORE child_env: child_env reads the generated file (OPENCODE_CONFIG_CONTENT).
    """
    lang = i18n.resolve_language(None)
    exe = shutil.which("opencode")      # ONE resolution: the version is read from the binary that will run
    if not exe:
        return None, None, None, i18n.t("opencode.missing", lang)
    problem = check_opencode(lang, exe=exe)
    if problem:
        return None, None, None, problem
    provider = ""
    if settings.backend != "endpoint":
        try:
            provider = model_parts(settings.model)[0]
        except ValueError as e:     # a malformed model is a readable refusal, never a traceback
            return None, None, None, str(e)
    rev_dir = os.path.dirname(output_json)
    if any(c in os.path.basename(output_json) for c in "*?["):
        # the edit permission is a glob: a wildcard in the name would allow more than this one file
        return None, None, None, (f"refusing to run: the file name {os.path.basename(output_json)!r} contains a "
                                  f"wildcard character (* ? [); rename the plan and run again.")
    link = _symlink_in(rev_dir)
    if link:
        return None, None, None, (f"refusing to run: the reviews folder contains a symlink ({link}); the reviewer "
                                  f"may write there and would write through it. Remove it and run again.")
    # ⚠️ The repair spends quota like the review: running it with the proxy down brings back the very hang the proxy
    # exists to avoid. Never for `endpoint` -- that URL is the user's, not z.ai's.
    if settings.backend == "opencode" and provider == ZAI_PROVIDER and not ensure_proxy(deadline):
        return None, None, None, f"could not start the local z.ai proxy on port {ZAI_PROXY_PORT}"
    try:
        tmpdir = tempfile.mkdtemp(prefix="ar-opencode-")
    except OSError as e:    # R19: a full or unwritable TMPDIR is a reason, not a traceback
        return None, None, None, f"could not create the round's temporary folder: {e}"
    try:
        cfg_path = os.path.join(tmpdir, "opencode.json")
        generate_config(repo, output_json, settings, options.thinking, cfg_path)
        env = child_env(settings, cfg_path)
    except OSError as e:    # e.g. prompts/agent.md missing from a broken install: a reason, not a traceback
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None, None, None, f"could not write the round's opencode config: {e}"
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise
    return exe, tmpdir, env, None


def _run(exe, repo, env, prompt, model, options, deadline, stream_path, session=None):
    """`opencode run` with the watchdog: killed after `no_byte_s` without a byte or at the ABSOLUTE deadline.

    -> (error or None, stalled_at_start). The event stream goes to `stream_path`.
    """
    cmd = [exe, "run", "--dir", repo, "--agent", AGENT_NAME, "--format", "json", "-m", model]
    if session:
        cmd += ["--session", session]
    cmd += [prompt]
    with open(stream_path, "w", encoding="utf-8") as out:
        try:
            # ⚠️ stderr is discarded, not kept: it is the child's, and the child holds the token in its env.
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.DEVNULL, env=env,
                                    cwd=repo)
        except OSError as e:
            return f"could not run `opencode`: {e.strerror or e}", False
        try:
            last, size = time.monotonic(), 0
            while proc.poll() is None:
                time.sleep(POLL_S)
                now_size = os.path.getsize(stream_path)
                if now_size > size:
                    size, last = now_size, time.monotonic()
                if time.monotonic() - last > options.no_byte_s:
                    return f"opencode hung: {options.no_byte_s}s without a byte", size == 0
                if time.monotonic() > deadline:
                    return f"opencode ran past the round's time limit ({options.timeout_s}s)", False
        finally:
            # every exit -- the watchdog, the deadline, an exception, a Ctrl-C -- leaves no child holding the token
            if proc.poll() is None:
                proc.kill()
            proc.wait()
    return (f"opencode exited {proc.returncode}" if proc.returncode else None), False


def _delivery(output_json):
    """The delivery instruction, WITH the schema embedded.

    ⚠️ `codex exec` gets `--output-schema`; opencode has no equivalent, so the schema only arrives in the prompt.
    Without it the model invented `{"verdict": "BLOCKER"}` and the round spent quota twice for NO VERDICT.
    ⚠️ The fake opencode finds the output path by the text "Write the review JSON to `<path>`": keep them in sync.
    """
    with open(loop.SCHEMA, encoding="utf-8") as f:
        schema = json.load(f)
    return (f"\n\n## Delivery\n\n"
            f"Write the review JSON to `{output_json}` using the file-writing tool, and reply with ONLY that same "
            f"JSON, with no text before or after.\n\n"
            f"It must follow EXACTLY this schema:\n\n"
            f"```json\n{json.dumps(schema, ensure_ascii=False, indent=1)}\n```\n\n"
            f"⚠️ `verdict` is APPROVED or REJECTED -- never a severity. `findings` is a list, even when empty, and "
            f"each finding has `severity` (BLOCKER|RISK|NOTE), `where` and `problem`.")


def _failure(error, text, session, telemetry, settings):
    # masked BEFORE the cut too (text already comes masked from telemetry_from_stream; this is the second net)
    text = _scrub(text, settings.token)
    reason = f"{_scrub(error, settings.token)}. Last text: {text[:160]}" if text else _scrub(error, settings.token)
    return loop.Result(False, reason, session or "", _scrub(telemetry, settings.token))


def review(*, path, repo, prompt, output_json, settings, options, deadline):
    """One `opencode run`; `deadline` is the loop's ABSOLUTE time.monotonic() limit."""
    exe, tmpdir, env, problem = _prepare(repo, output_json, settings, options, deadline)
    if problem:
        return loop.Result(False, _scrub(problem, settings.token))
    try:
        try:
            request = prompt + _delivery(output_json)
        except (OSError, ValueError) as e:      # R19: the schema missing or broken in the install
            return loop.Result(False, f"could not read the review schema: {e}")
        model = run_model(settings)
        stream = os.path.join(tmpdir, "review.jsonl")
        error, stalled = _run(exe, repo, env, request, model, options, deadline, stream)
        tel, session, text = telemetry_from_stream(stream, settings.token)
        # ⚠️ ONE more attempt only when it hung at START-UP (opencode#35870: not a byte, so nothing was spent) and
        # there is time left. A stall after output already spent quota; the loop's repair turn covers that.
        if error and stalled and time.monotonic() < deadline:
            stream = os.path.join(tmpdir, "review-retry.jsonl")
            error, _ = _run(exe, repo, env, request, model, options, deadline, stream)
            tel2, session2, text = telemetry_from_stream(stream, settings.token)
            tel.update(tel2)
            session = session2 or session
        tel["thinking"] = "on" if options.thinking else "off"
        if error and not (error.startswith("opencode exited") and os.path.isfile(output_json)):
            return _failure(error, text, session, tel, settings)
        # a non-zero exit that still wrote the JSON: the loop validates what was written
        return loop.Result(True, "", session or "", _scrub(tel, settings.token))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def repair(*, path, repo, session, reason, output_json, settings, options, deadline):
    """ONE repair turn in the SAME session. Never retried: the loop allows exactly one."""
    exe, tmpdir, env, problem = _prepare(repo, output_json, settings, options, deadline)
    if problem:
        return loop.Result(False, _scrub(problem, settings.token), session)
    try:
        try:
            request = (f"The JSON you wrote to `{output_json}` has a problem: {reason}.\n"
                       f"Rewrite the WHOLE file, valid (quoted keys, no trailing comma)." + _delivery(output_json))
        except (OSError, ValueError) as e:
            return loop.Result(False, f"could not read the review schema: {e}", session)
        stream = os.path.join(tmpdir, "repair.jsonl")
        error, _ = _run(exe, repo, env, request, run_model(settings), options, deadline, stream, session=session)
        tel, _, text = telemetry_from_stream(stream, settings.token)
        if error and not (error.startswith("opencode exited") and os.path.isfile(output_json)):
            return _failure(f"repair failed: {error}", text, session, tel, settings)
        return loop.Result(True, "", session, _scrub(tel, settings.token))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
