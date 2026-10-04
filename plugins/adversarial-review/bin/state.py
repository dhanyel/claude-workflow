#!/usr/bin/env python3
"""state: the review verdicts, per plan and per CONTENT HASH (adversarial-review spec §3.4).

⚠️ Its own folder, never the plan-gate's: two writers with different fields in the same file corrupt both
("two owners", plan-gate spec §4.1).
"""
import contextlib, datetime, fcntl, hashlib, json, os, time


def state_dir():
    return os.environ.get("ADVERSARIAL_REVIEW_DIR") or os.path.expanduser(
        "~/.claude/claude-workflow/adversarial-review")


def key(path):
    return hashlib.sha256(os.path.abspath(path).encode()).hexdigest()[:16]


def state_path(path):
    return os.path.join(state_dir(), key(path) + ".json")


def snapshots_dir(path):
    return os.path.join(state_dir(), "snapshots", key(path))


class StateUnreadable(OSError):
    """The state file exists but cannot be read (permissions, I/O): NOT corrupt, never set aside."""


def load(path):
    """-> (state, None) | (None, None) when there is none | (None, error) when it is malformed.

    Raises StateUnreadable on an OSError; `verdict_for` turns that into an error string, writers let it propagate.

    ⚠️ Unreadable is NOT absent: the check fails closed on it."""
    target = state_path(path)
    if not os.path.lexists(target):
        return None, None
    try:
        with open(target, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError as e:  # UnicodeDecodeError is a ValueError too
        return None, f"{type(e).__name__}: {e}"
    except OSError as e:
        raise StateUnreadable(f"{type(e).__name__}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("reviews", {}), dict):
        return None, "not a review state"
    return data, None


def _write(path, data):
    os.makedirs(state_dir(), exist_ok=True)
    target = state_path(path)
    tmp = f"{target}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


@contextlib.contextmanager
def _locked(path):
    """Exclusive per-plan lock: two concurrent rounds cannot lose each other's entries."""
    os.makedirs(state_dir(), exist_ok=True)
    with open(os.path.join(state_dir(), key(path) + ".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _current(path):
    data, error = load(path)
    if error:
        # a corrupt state is set aside (kept for a human), and the new truth starts clean
        stamp = f"{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}-{time.time_ns()}-{os.getpid()}"
        os.replace(state_path(path), f"{state_path(path)}.corrupt-{stamp}")
        data = None
    return data or {"plan": os.path.abspath(path), "reviews": {}}


def record_verdict(path, content_hash, entry):
    with _locked(path):
        data = _current(path)
        data.setdefault("reviews", {})[content_hash] = entry
        data.pop("last_failure", None)
        _write(path, data)


def record_failure(path, failure):
    """NO VERDICT: only `last_failure` changes -- a failure never approves, not even by omission."""
    with _locked(path):
        data = _current(path)
        data["last_failure"] = failure
        _write(path, data)


def verdict_for(path, content_hash):
    try:
        data, error = load(path)
    except StateUnreadable as e:
        return None, str(e)
    if error:
        return None, error
    entry = (data or {}).get("reviews", {}).get(content_hash)
    if entry is not None and not isinstance(entry, dict):
        return None, f"entry for {content_hash} is not an object"
    return entry, None
