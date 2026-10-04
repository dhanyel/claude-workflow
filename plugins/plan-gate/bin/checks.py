#!/usr/bin/env python3
"""checks: registrable checks that approve a plan or a spec (spec §4.2).

A plugin ships `plan-gate-check.json` (one object or a list of them):
  {"id": "...", "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/bin/x.py\" --json",
   "required": true, "applies_to": ["plan", "spec"]}
Its SessionStart hook copies each entry to `<state_dir()>/checks.d/<id>.json`, with `${CLAUDE_PLUGIN_ROOT}`
already resolved.

Command contract: `shlex.split(command)`, run WITHOUT a shell, with the checked file's path as the LAST
argument; it prints `{"status": "pass"|"fail", "findings": [...]}` on stdout. Anything else -- stdout that is
not that JSON, an invalid status, a command that cannot start, a timeout -- is `fail` with a `check-error`
group carrying the reason. The exit code is NOT what decides (R17): the preflights exit 1/2 on a fail, and
their findings have to survive.
"""
import json, os, re, shlex, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

import config        # noqa: E402
import i18n          # noqa: E402

# R31: a check that hangs cannot hang the hook -- a hook killed by its own timeout does not block anything.
# hooks/hooks.json gives the `mark` hook a timeout of a few times this.
CHECK_TIMEOUT = 45
# Test-only: shortens the timeout so a test does not wait 45 s. Honored only when SHORTER, so setting it can
# never make a check run longer than CHECK_TIMEOUT.
TEST_TIMEOUT_ENV = "PLAN_GATE_TEST_CHECK_TIMEOUT"

PLACEHOLDER = "${CLAUDE_PLUGIN_ROOT}"
KINDS = ("plan", "spec")
# The id names a file in checks.d: no separators, no leading dot.
RE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ERROR_GROUP = "check-error"


INBOX = "manifests.d"


def checks_dir():
    return os.path.join(config.state_dir(), "checks.d")


def inbox_dir():
    return os.path.join(config.state_dir(), INBOX)


def _inbox_error_file(plugin):
    return os.path.join(checks_dir(), f"inbox-{plugin}.json")


def timeout():
    try:
        test = float(os.environ.get(TEST_TIMEOUT_ENV, ""))
    except ValueError:
        return CHECK_TIMEOUT
    return test if 0 < test < CHECK_TIMEOUT else CHECK_TIMEOUT


def _problem(entry):
    """Why a manifest entry cannot be used, or None."""
    if not isinstance(entry, dict):
        return i18n.t("checks.not_an_object", got=type(entry).__name__)
    cid = entry.get("id")
    if not isinstance(cid, str) or not RE_ID.match(cid):
        return i18n.t("checks.bad_id", got=repr(cid))
    if cid.startswith("inbox-"):
        # reserved for the gate's own inbox error records: a manifest called `inbox-x` could be deleted by the inbox
        # cleanup, or pass for a resolved error (final review M5)
        return i18n.t("checks.reserved_id", got=repr(cid))
    command = entry.get("command")
    if not isinstance(command, str) or not command.strip():
        return i18n.t("checks.bad_command", id=cid)
    return None


def _owner(manifest_path):
    """Who a manifest speaks for -- a FOLDER, never a declared name (R38, R39).

    - Installed plugin, i.e. the folder holding `.claude-plugin/plugin.json` is
      `.../plugins/cache/<marketplace>/<plugin>/<version>` (whole path SEGMENTS, not a substring): the
      realpath of `.../<marketplace>/<plugin>`. A new version is a new folder under that same parent, so
      the upgrade replaces in place.
    - Any other plugin folder (a `--plugin-dir` dev copy): that folder's own realpath. Its parent is a
      shared folder (`plugins/`, `~/projects/`), and sibling plugins sharing an owner replaced each
      other's checks silently (R39).
    - Not inside a plugin: the manifest's own realpath.

    ⚠️ Not the `name` in plugin.json: anybody can declare `"name": "plan-gate"`, and by name a copy
    silently replaced the real failing preflight with a passing one (R38).
    """
    folder = os.path.dirname(os.path.realpath(manifest_path))
    if not os.path.isfile(os.path.join(folder, ".claude-plugin", "plugin.json")):
        return os.path.realpath(manifest_path)
    parts = folder.replace("\\", "/").split("/")
    if len(parts) >= 6 and parts[-5:-3] == ["plugins", "cache"] and all(parts[-3:]):
        return os.path.dirname(folder)
    return folder


def _write(target, data):
    """Atomic, through a UNIQUE temp file in the same folder (mkstemp: O_EXCL, never a fixed `.tmp` that two writers
    share or that a planted symlink redirects)."""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(target) + ".", suffix=".tmp", dir=os.path.dirname(target))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o644)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def register(manifest_path, plugin_root):
    """Copies the manifest's checks to checks.d with `${CLAUDE_PLUGIN_ROOT}` resolved. Idempotent.

    Validates every entry BEFORE writing any: a half-registered manifest would be a plugin whose checks
    silently went missing. Returns the ids registered (a conflict is not one of them).

    ⚠️ An id is ONE file in checks.d, so two checks with the same id cannot both live there:
      - twice inside one manifest -> the manifest is refused, nothing written;
      - already registered by ANOTHER owner -> the file becomes a conflict entry naming both manifests,
        which counts as a required check that fails (registered()). Never a silent replace: which one
        survived would depend on hook order, and a failing required check could be swapped for a passing
        one. Only the same owner re-registers in place (idempotent), or an owner whose manifest is gone.
    """
    manifest_path = os.path.abspath(manifest_path)
    try:
        with open(manifest_path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:
        raise ValueError(f"{manifest_path}: {e}") from e
    entries = data if isinstance(data, list) else [data]
    seen = set()
    for entry in entries:
        problem = _problem(entry)
        if problem:
            raise ValueError(f"{manifest_path}: {problem}")
        if entry["id"] in seen:
            raise ValueError(f"{manifest_path}: " + i18n.t("checks.duplicate_in_manifest", id=entry["id"]))
        seen.add(entry["id"])
    owner = _owner(manifest_path)
    os.makedirs(checks_dir(), exist_ok=True)
    ids = []
    for entry in entries:
        resolved = dict(entry, command=entry["command"].replace(PLACEHOLDER, plugin_root),
                        source=manifest_path, owner=owner)
        target = os.path.join(checks_dir(), entry["id"] + ".json")
        existing = _read_existing(target)
        if existing is not None and "conflict" not in existing and _problem(existing):
            # corrupted: it already fails as a required check (R32); left for a human to look at
            print(i18n.t("checks.left_corrupted", id=entry["id"], file=target), file=sys.stderr)
            continue
        if existing is not None and not _replaceable(existing, owner):
            sources = existing.get("conflict") if isinstance(existing.get("conflict"), list) \
                else [existing.get("source") or target]
            if manifest_path not in sources:
                sources = sources + [manifest_path]
            _write(target, {"id": entry["id"], "conflict": sources})
            print(i18n.t("checks.conflict", id=entry["id"], sources=", ".join(map(str, sources))),
                  file=sys.stderr)
            continue
        _write(target, resolved)
        ids.append(entry["id"])
    return ids


def _read_existing(target):
    """The entry already at `target`; None when there is none; {} when it cannot be read (corrupted)."""
    if not os.path.lexists(target):
        return None
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _replaceable(existing, owner):
    """May a registration by `owner` overwrite `existing`? Its own entry, always. Another owner's only when
    that plugin is GONE: its manifest AND its command (an orphan, R40) -- an uninstall or an upgrade removes
    the whole folder. A manifest deleted with the (failing) script still in place is not gone: replacing
    it would swap a failing required check for a passing one. A conflict stays a conflict."""
    if "conflict" in existing:
        return False
    if existing.get("owner") == owner:
        return True
    source = existing.get("source")
    return (isinstance(source, str) and not os.path.exists(source)
            and bool(_missing_paths(existing.get("command") or "")))


def _owns_error_file(path):
    """May the gate touch `path`? Only when absent or an inbox error record -- never a real check that happens
    to be called `inbox-<x>`."""
    if not os.path.lexists(path):
        return True
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and "inbox_error" in data


def ingest_inbox():
    """Registers every manifest a plugin announced in manifests.d (adversarial-review spec §4.1.3).

    ⚠️ Why an inbox and not the plugin calling register-check: the order of two plugins' SessionStart hooks is not
    guaranteed, so a plugin installed before the gate wrote anything would silently go unregistered -- a required
    check that is not there approves nothing ... unless the preflight passes, and then it approves WITHOUT it.
    ⚠️ An entry that cannot be registered becomes a failing required check (`inbox-<plugin>`), never a skipped one:
    skipping it is exactly the zero-coverage window this exists to close.
    """
    folder = inbox_dir()
    if not os.path.isdir(folder):
        return []
    done = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        plugin, source = name[:-len(".json")], os.path.join(folder, name)
        error_file = _inbox_error_file(plugin)
        try:
            with open(source, encoding="utf-8") as f:
                entry = json.load(f)
            manifest, root = entry["manifest"], entry["plugin_root"]
            if not all(isinstance(v, str) and os.path.isabs(v) for v in (manifest, root)):
                raise ValueError(i18n.t("checks.inbox_bad_entry"))
            ids = register(manifest, root)
        except Exception as e:  # noqa: BLE001 -- one bad entry must never drop the ones after it
            try:
                if _owns_error_file(error_file):
                    os.makedirs(checks_dir(), exist_ok=True)
                    _write(error_file, {"id": f"inbox-{plugin}", "inbox_error": f"{type(e).__name__}: {e}",
                                        "source": source})
            except Exception:  # noqa: BLE001
                pass
            continue
        try:
            if os.path.lexists(error_file) and _owns_error_file(error_file):
                os.remove(error_file)
        except Exception:  # noqa: BLE001
            pass
        done.extend(ids)
    return done


def registered():
    """Every manifest in checks.d, sorted by file name.

    ⚠️ R32: a file that cannot be read as a check is NOT skipped -- skipping it would turn a corrupted
    required check into an absent one, and an absent check approves nothing ... unless another passes.
    It comes back as {"id": <file stem>, "error": <why>, "file": <path>}: required, applicable to all, failing.
    """
    folder = checks_dir()
    if not os.path.isdir(folder):
        return []
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        stem = name[:-len(".json")]
        try:
            with open(path, encoding="utf-8") as f:
                entry = json.load(f)
        except (OSError, ValueError) as e:
            out.append({"id": stem, "error": f"{type(e).__name__}: {e}", "file": path})
            continue
        if isinstance(entry, dict) and "inbox_error" in entry:
            out.append({"id": stem, "file": path, "inbox": True,
                        "error": i18n.t("checks.inbox_failed", plugin=stem[len("inbox-"):],
                                        error=entry["inbox_error"], source=entry.get("source", "?"))})
            continue
        if isinstance(entry, dict) and isinstance(entry.get("conflict"), list):
            out.append({"id": stem, "file": path, "conflict": entry["conflict"],
                        "error": i18n.t("checks.conflict_entry", id=stem,
                                        sources=", ".join(map(str, entry["conflict"])))})
            continue
        problem = _problem(entry)
        if not problem and entry["id"] != stem:
            problem = i18n.t("checks.id_mismatch", id=entry["id"], file=name)
        if problem:
            out.append({"id": stem, "error": problem, "file": path})
            continue
        out.append(dict(entry, file=path))
    return out


def _applies(check, kind):
    """R32: an unknown or missing `applies_to` counts as applicable (fail closed)."""
    kinds = check.get("applies_to")
    if not isinstance(kinds, list) or not kinds or not all(k in KINDS for k in kinds):
        return True
    return kind in kinds


def applicable(kind, repo):
    """The checks that apply to a `kind` file in `repo`, each with its effective `required`.

    `.claude/plan-gate.json` -> `checks.<id>.required` overrides the manifest. A non-boolean value counts as
    required, and a corrupted manifest is required whatever the repo says (R32).
    """
    overrides = config.load(repo)["checks"]
    out = []
    every = registered()
    for check in every:
        if not _applies(check, kind):
            continue
        required = check.get("required", True)
        override = overrides.get(check["id"], {})
        if "required" in override:
            required = override["required"]
        if "error" in check or not isinstance(required, bool):
            required = True
        out.append(dict(check, required=required))
    return out + _missing_required(overrides, every)


def _missing_required(overrides, every):
    """R35: a check the repo REQUIRES but nobody registered is a required check that fails -- otherwise
    requiring it would be decorative. `required: false` (and nothing else) lets it be absent."""
    known = {c["id"] for c in every}
    return [{"id": cid, "required": True, "missing": True, "error": i18n.t("checks.not_registered", id=cid)}
            for cid, o in sorted(overrides.items()) if cid not in known and o.get("required") is not False]


def missing_required(repo):
    """Ids the repo config requires that are not registered (for check/status)."""
    return [c["id"] for c in _missing_required(config.load(repo)["checks"], registered())]


def _error(reason):
    return {"status": "fail", "findings": [{"group": ERROR_GROUP, "state": "findings", "count": 1,
                                            "items": [reason]}]}


def _argv(command):
    return shlex.split(command)


def _missing_paths(command):
    """The ABSOLUTE argv tokens that do not exist (`python3 "/x/gone.py"` -> ["/x/gone.py"])."""
    try:
        tokens = _argv(command)
    except ValueError:
        return []
    return [t for t in tokens if os.path.isabs(t) and not os.path.exists(t)]


def run(check, path, content_hash=None):
    """Runs one check on `path` -> {"status": "pass"|"fail", "findings": [...]}. Never raises.

    ⚠️ adversarial-review spec §4.1.1: the check gets the content hash the gate will record, in
    PLAN_GATE_CONTENT_HASH -- ONE owner of the hash (two implementations of it is how an approval stops matching
    what the hook checks).
    """
    if check.get("missing") or "conflict" in check or check.get("inbox"):
        return _error(check["error"])
    if "error" in check:
        return _error(i18n.t("checks.corrupted", id=check.get("id"), error=check["error"]))
    command = check.get("command") or ""
    missing = _missing_paths(command)
    if missing:
        gate = 'python3 "' + os.path.join(os.path.dirname(os.path.abspath(__file__)), "plan_gate.py") + '"'
        return _error(i18n.t("checks.orphan", id=check.get("id"), paths=", ".join(missing), gate=gate))
    try:
        argv = _argv(command) + [os.path.abspath(path)]
    except ValueError as e:
        return _error(i18n.t("checks.unparsable", id=check.get("id"), error=e))
    limit = timeout()
    # ⚠️ Built EVERY time, the inherited variable popped: with no hash, a PLAN_GATE_CONTENT_HASH left in the parent's
    # environment would reach the check as if the gate had vouched for it (final review M1).
    env = {k: v for k, v in os.environ.items() if k != "PLAN_GATE_CONTENT_HASH"}
    if content_hash:
        env["PLAN_GATE_CONTENT_HASH"] = content_hash
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=limit, stdin=subprocess.DEVNULL,
                           cwd=os.path.dirname(os.path.abspath(path)), env=env)
    except subprocess.TimeoutExpired:
        return _error(i18n.t("checks.timed_out", id=check.get("id"), timeout=limit))
    except (OSError, ValueError) as e:
        return _error(i18n.t("checks.could_not_start", id=check.get("id"), error=e))
    # R17: stdout decides, whatever the exit code.
    try:
        result = json.loads(p.stdout)
    except ValueError:
        result = None
    if (not isinstance(result, dict) or result.get("status") not in ("pass", "fail")
            or not isinstance(result.get("findings", []), list)):
        return _error(i18n.t("checks.bad_output", id=check.get("id"), rc=p.returncode,
                             stdout=p.stdout.strip()[:200], stderr=p.stderr.strip()[:200]))
    # R34: "pass" with a failing exit is incoherent -- a crash after printing must not read as a pass. (A
    # "fail" keeps its findings whatever the exit, R17: the preflights exit 1/2 on a fail.)
    if result["status"] == "pass" and p.returncode != 0:
        return _error(i18n.t("checks.pass_with_failing_exit", id=check.get("id"), rc=p.returncode))
    return {"status": result["status"], "findings": result.get("findings", [])}


def orphans():
    """Ids of the registered checks with an absolute argv token that no longer exists."""
    return [c["id"] for c in registered() if "error" not in c and _missing_paths(c["command"])]


def corrupted():
    return [c for c in registered() if "error" in c]


def cmd_list():
    found = registered()
    if not found:
        print(i18n.t("checks.none"))
        return 0
    orphan_ids = set(orphans())
    for c in found:
        if "error" in c:
            print(i18n.t("checks.list_corrupted", id=c["id"], error=c["error"]))
            continue
        mark = i18n.t("checks.list_orphan_mark") if c["id"] in orphan_ids else ""
        kinds = c.get("applies_to")
        print(i18n.t("checks.list_line", id=c["id"], mark=mark,
                     required=i18n.t("checks.optional" if c.get("required", True) is False else "checks.required"),
                     kinds=",".join(kinds) if isinstance(kinds, list) else "*", command=c["command"]))
    return 0


def _prune_inbox():
    """Drops the inbox entries whose manifest is gone (or unreadable), with their error record, and the error
    records whose inbox entry no longer exists (they would fail required forever)."""
    gone = []
    folder = inbox_dir()
    names = sorted(n for n in os.listdir(folder) if n.endswith(".json")) if os.path.isdir(folder) else []
    for name in names:
        plugin, source = name[:-len(".json")], os.path.join(folder, name)
        try:
            with open(source, encoding="utf-8") as f:
                manifest = json.load(f)["manifest"]
            alive = isinstance(manifest, str) and os.path.exists(manifest)
        except (OSError, ValueError, KeyError, TypeError):
            alive = False
        if alive:
            continue
        os.remove(source)
        if os.path.lexists(_inbox_error_file(plugin)) and _owns_error_file(_inbox_error_file(plugin)):
            os.remove(_inbox_error_file(plugin))
        gone.append(f"inbox-{plugin}")
    if os.path.isdir(checks_dir()):
        for name in sorted(os.listdir(checks_dir())):
            path = os.path.join(checks_dir(), name)
            if (name.startswith("inbox-") and name.endswith(".json") and _owns_error_file(path)
                    and not os.path.lexists(os.path.join(folder, name[len("inbox-"):]))):
                os.remove(path)
                gone.append(name[:-len(".json")])
    return gone


def cmd_prune():
    """Removes ORPHANS only. A corrupted manifest is kept and named (R32): its command cannot be read, so
    nobody can tell whether it is an orphan -- deleting it would be deleting a required check by guess."""
    removed = []
    for c in registered():
        if "error" not in c and _missing_paths(c["command"]):
            os.remove(c["file"])
            removed.append(c["id"])
    removed.extend(_prune_inbox())
    print(i18n.t("checks.pruned", ids=", ".join(removed)) if removed else i18n.t("checks.nothing_to_prune"))
    bad = corrupted()
    if bad:
        print(i18n.t("checks.corrupted_kept", ids=", ".join(c["id"] for c in bad),
                     folder=checks_dir()))
    return 0
