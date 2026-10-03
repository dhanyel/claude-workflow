#!/usr/bin/env python3
"""config: per-repository configuration."""
import json, os, re, stat, sys, urllib.parse

# R27: the most a config is read. A real one is a few hundred bytes.
MAX_CONFIG_BYTES = 1 << 20

def state_dir():
    """Return the plan-gate state directory.

    Priority: PLAN_GATE_DIR env > ~/.claude/claude-workflow/plan-gate/
    Reads PLAN_GATE_DIR on every call (never cached).
    """
    env_dir = os.environ.get("PLAN_GATE_DIR")
    if env_dir:
        return env_dir
    return os.path.expanduser("~/.claude/claude-workflow/plan-gate")

def load(repo):
    """Load configuration for a repository.

    Returns a dict with defaults (copied per call):
    - language: None
    - plans_dir: "docs/superpowers/plans"
    - specs_dir: "docs/superpowers/specs"
    - checks: {}
    - mr_template: None
    - platform: None ("github" | "gitlab"; None = from the `origin` remote)
    - api_url: None (http(s) base of the API; None = derived from the remote)

    If repo is None, returns defaults without touching filesystem.
    If config file exists, merges overrides into defaults.
    Unknown keys produce one warning line on stderr and are ignored.
    Broken JSON raises ValueError with the path.
    Non-object top-level JSON raises ValueError.
    """
    defaults = {
        "language": None,
        "plans_dir": "docs/superpowers/plans",
        "specs_dir": "docs/superpowers/specs",
        "checks": {},
        "mr_template": None,
        "platform": None,
        "api_url": None,
    }

    if repo is None:
        return dict(defaults)  # Copy defaults

    config_path = os.path.join(repo, ".claude", "plan-gate.json")
    # `lexists`: a dangling link is NOT "no config" (R30)
    if not os.path.lexists(config_path):
        return dict(defaults)  # Copy defaults
    # ⚠️ R30: a link lets the config "be" any file. The gate lets a broken config be written to fix it,
    # and through a link that write would land on whatever the link names -- a source file included.
    if os.path.islink(config_path):
        raise ValueError(f"Config {config_path} is a symbolic link")

    # Load and validate the JSON file
    try:
        data = json.loads(_read_regular(config_path))
    except json.JSONDecodeError as e:
        raise ValueError(f"Broken JSON in {config_path}: {e}")
    except UnicodeDecodeError as e:
        raise ValueError(f"Config {config_path} is not UTF-8: {e}") from e
    except OSError as e:
        # a directory in place of the file, or a file nobody can read: broken, never silent defaults
        raise ValueError(f"Cannot read {config_path}: {e}") from e

    # Validate that top-level is an object
    if not isinstance(data, dict):
        raise ValueError(f"Top-level JSON in {config_path} must be an object, not {type(data).__name__}")

    # Check for unknown keys and warn
    known_keys = set(defaults.keys())
    for key in data.keys():
        if key not in known_keys:
            print(f"Warning: unknown config key '{key}' in {config_path}", file=sys.stderr)

    # Merge overrides into defaults (copy defaults first to avoid mutation)
    result = dict(defaults)
    for key in known_keys:
        if key in data:
            result[key] = data[key]

    _validate(result, config_path)
    return result


def _read_regular(path):
    """The bytes of `path`, only if it is a regular file of at most MAX_CONFIG_BYTES.

    ⚠️ R27: a FIFO blocks `open`, and a link to /dev/zero never ends a read -- the hook hangs until it
    times out, and a hook timeout does not block the tool. `os.stat` follows the link to its target;
    the open is O_NONBLOCK so a FIFO swapped in after the stat cannot block it either.
    """
    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"Config {path} is not a regular file")
    if st.st_size > MAX_CONFIG_BYTES:
        raise ValueError(f"Config {path} is larger than {MAX_CONFIG_BYTES} bytes")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise ValueError(f"Config {path} is not a regular file")
        raw = f.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ValueError(f"Config {path} is larger than {MAX_CONFIG_BYTES} bytes")
    return raw.decode("utf-8")


def _folder_problem(value):
    """Why a plans/specs folder cannot be used, or None.

    ⚠️ A folder that never matches leaves the gate open, and `.` makes every `.md` a plan: both are a
    broken config, not a setting.
    """
    if not isinstance(value, str):
        return f"must be a string, not {type(value).__name__}"
    # R29: the RAW value -- " docs/x" validated after a strip and then never matched.
    if value != value.strip():
        return "must not start or end with whitespace"
    if value.startswith("~"):
        return "must be relative to the repository (no '~')"
    v = value.replace("\\", "/")
    if not v.strip("/"):
        return "must not be empty or the root"
    if v.startswith("/") or os.path.isabs(v):
        return "must be relative to the repository"
    parts = [p for p in v.split("/") if p]
    if ".." in parts:
        return "must not contain '..'"
    if all(p == "." for p in parts):
        return "must not be the repository itself"
    return None


def _validate(result, config_path):
    """Raises ValueError naming the path for values the gate cannot use (`language` is i18n's job)."""
    for key in ("plans_dir", "specs_dir"):
        problem = _folder_problem(result[key])
        if problem:
            raise ValueError(f"Invalid '{key}' in {config_path}: {problem} (got {result[key]!r})")
    checks = result["checks"]
    if not isinstance(checks, dict) or not all(isinstance(v, dict) for v in checks.values()):
        raise ValueError(f"Invalid 'checks' in {config_path}: must be an object of objects (got {checks!r})")
    if result["mr_template"] is not None and not isinstance(result["mr_template"], str):
        raise ValueError(f"Invalid 'mr_template' in {config_path}: must be a string or null "
                         f"(got {result['mr_template']!r})")
    if result["platform"] not in (None, "github", "gitlab") or isinstance(result["platform"], bool):
        raise ValueError(f"Invalid 'platform' in {config_path}: must be \"github\", \"gitlab\" or null "
                         f"(got {result['platform']!r})")
    problem = _api_url_problem(result["api_url"])
    if problem:
        raise ValueError(f"Invalid 'api_url' in {config_path}: {problem}")


def _api_url_problem(value):
    """Why `api_url` cannot be used as the base of the API requests, or None.

    ⚠️ R57: credentials, a query or a fragment in the base would ride along in every URL -- and a URL ends up
    in error texts. The token travels only in a header.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return f"must be an http(s) URL string or null (got {value!r})"
    if not value or re.search(r"\s", value):
        return "must not be empty or contain whitespace"
    parts = urllib.parse.urlsplit(value)
    if parts.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    if not parts.hostname:
        return "must name a host"
    if "@" in parts.netloc:
        return "must not carry credentials (the token goes in GITHUB_TOKEN / GITLAB_TOKEN)"
    if parts.query or parts.fragment or "?" in value or "#" in value:
        return "must not have a query or a fragment"
    try:
        parts.port
    except ValueError:
        return "has an invalid port"
    return None
