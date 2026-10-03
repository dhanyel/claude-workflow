"""Builds the golden cases in a throwaway git repo. Shared by the capture script and the tests."""
import contextlib, os, shutil, subprocess, tempfile

GOLDEN = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(GOLDEN, "cases")
HOOKS = os.path.join(GOLDEN, "hooks")


@contextlib.contextmanager
def materialized(case):
    """Copies cases/<case>/repo into a fresh git repo and the plan/spec under docs/superpowers. Yields (repo, file)."""
    src = os.path.join(CASES, case)
    with tempfile.TemporaryDirectory(prefix="golden-") as d:
        repo = os.path.realpath(d)
        if os.path.isdir(os.path.join(src, "repo")):
            shutil.copytree(os.path.join(src, "repo"), repo, dirs_exist_ok=True)
        is_spec = os.path.exists(os.path.join(src, "spec.md"))
        name = "spec.md" if is_spec else "plan.md"
        target = os.path.join(repo, "docs", "superpowers", "specs" if is_spec else "plans", name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy(os.path.join(src, name), target)
        for args in (["init", "-q", "-b", "main"], ["config", "user.email", "g@x"], ["config", "user.name", "g"],
                     ["add", "-A"], ["commit", "-q", "-m", "case"]):
            subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)
        yield repo, target


def clean_env(state_dir, home):
    env = {k: v for k, v in os.environ.items() if k not in ("PLAN_GATE", "PORTAO_DE_PLANO", "PLAN_GATE_LANG")}
    env.update(PLAN_GATE_DIR=state_dir, HOME=home)
    return env


def hook_payload(name, repo):
    with open(os.path.join(HOOKS, name)) as f:
        return f.read().replace("{REPO}", repo)
