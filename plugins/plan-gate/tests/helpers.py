"""Test helpers: package paths and an environment isolated from the machine.

Running the suite must never read or write the real machine: temp HOME (so ~/.claude/settings.json is
never the real one), temp PLAN_GATE_DIR, and no gate switches inherited from the session.
"""
import contextlib, importlib.util, json, os, subprocess, sys, tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(TESTS)
ROOT = os.path.dirname(os.path.dirname(PLUGIN))
BIN = os.path.join(PLUGIN, "bin")
GOLDEN = os.path.join(TESTS, "golden")
# L249: the tokens and host bindings too -- a test that needs one passes it explicitly
SWITCHES = ("PLAN_GATE", "PORTAO_DE_PLANO", "PLAN_GATE_LANG", "GITLAB_TOKEN", "GITHUB_TOKEN", "GITLAB_HOST", "GH_HOST")
CLEAN_PLAN = "# Example plan\n\n**Goal:** example.\n\n### Task 1: Something\n\n- [ ] **Step 1:** do it\n"


REAL_HOME = os.path.realpath(os.path.expanduser("~"))


def _isolated(env):
    env = {k: v for k, v in env.items() if k not in SWITCHES}
    env.setdefault("PLAN_GATE_DIR", tempfile.mkdtemp(prefix="plan-gate-test-"))   # one per call
    env.setdefault("HOME", tempfile.mkdtemp(prefix="plan-gate-home-"))
    # a future run(env=dict(os.environ, ...)) would otherwise write to the real ~/.claude
    assert os.path.realpath(env["HOME"]) != REAL_HOME, "test subprocess would run with the real HOME"
    return env


def import_bin(name):
    if BIN not in sys.path:
        sys.path.insert(0, BIN)
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(script, *args, env=None, stdin=None, cwd=None):
    path = script if os.path.isabs(script) else os.path.join(BIN, script)
    base = dict(os.environ) if env is None else dict(env)
    if env is None:                      # never inherit the real state dir or home
        base.pop("PLAN_GATE_DIR", None)
        base.pop("HOME", None)
    p = subprocess.run([sys.executable, path, *args], input=stdin, capture_output=True,
                       text=True, env=_isolated(base), cwd=cwd)
    return p.returncode, p.stdout, p.stderr


def denied(rc):
    return rc == 2          # the gate denies by exiting 2 with the reason on stderr


@contextlib.contextmanager
def temp_state():
    with tempfile.TemporaryDirectory(prefix="plan-gate-state-") as state, \
         tempfile.TemporaryDirectory(prefix="plan-gate-home-") as home:
        env = {k: v for k, v in os.environ.items() if k not in SWITCHES}
        env.update(PLAN_GATE_DIR=state, HOME=home)
        yield env


@contextlib.contextmanager
def fake_repo():
    with tempfile.TemporaryDirectory(prefix="plan-gate-repo-") as d:
        repo = os.path.realpath(d)          # macOS: /tmp is a symlink; the gate compares real paths
        def git(*a):
            subprocess.run(["git", "-C", repo, *a], check=True, capture_output=True)
        git("init", "-q", "-b", "main")
        git("config", "user.email", "test@example.com")
        git("config", "user.name", "Test")
        for sub in ("plans", "specs"):
            os.makedirs(os.path.join(repo, "docs", "superpowers", sub))
        git("commit", "-q", "--allow-empty", "-m", "init")
        yield repo


def fake_plan(repo, extra=""):
    path = os.path.join(repo, "docs", "superpowers", "plans", "2026-01-01-example.md")
    with open(path, "w") as f:
        f.write(CLEAN_PLAN + extra)
    return path


def hook_input(tool, tool_input, cwd, event="PreToolUse"):
    return json.dumps({"hook_event_name": event, "session_id": "test", "cwd": cwd,
                       "tool_name": tool, "tool_input": tool_input})


def state_of(path, env):
    gate = import_bin("plan_gate")          # key() comes from Task 7; only Task 8+ calls this
    with open(os.path.join(env["PLAN_GATE_DIR"], gate.key(path) + ".json")) as f:
        return json.load(f)
