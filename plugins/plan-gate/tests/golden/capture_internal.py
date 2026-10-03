#!/usr/bin/env python3
"""Captures the INTERNAL plugin's DECISIONS on the golden corpus. Run once, before porting."""
import glob, importlib.util, json, os, subprocess, sys, tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from golden_lib import CASES, HOOKS, GOLDEN, materialized, clean_env, hook_payload

INTERNAL = os.environ.get("INTERNAL_PLUGIN_DIR") or sys.exit("set INTERNAL_PLUGIN_DIR to the internal plugin")
BIN = os.path.join(INTERNAL, "bin")


def load(name, file):
    if BIN not in sys.path:
        sys.path.insert(0, BIN)
    spec = importlib.util.spec_from_file_location(name, os.path.join(BIN, file))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(script, *args, env, stdin=None, cwd=None):
    p = subprocess.run([sys.executable, os.path.join(BIN, script), *args], input=stdin,
                       capture_output=True, text=True, env=env, cwd=cwd)
    return p.returncode


def without_rg(env):
    import shutil
    rg = shutil.which("rg", path=env.get("PATH"))
    keep = [d for d in env.get("PATH", "").split(os.pathsep) if not rg or d != os.path.dirname(rg)]
    return dict(env, PATH=os.pathsep.join(keep))


def probe(pf, repo, text, want):
    """symbols: does the repo DEFINE it / is it merely USED (checar_em_lote); cited: does the
    cited-files block mention the backticked string (parece_arquivo kept it)."""
    res = {}
    if want.get("symbols"):
        found = pf.checar_em_lote(repo, want["symbols"])
        res["symbols"] = {s: {"defined": bool(found[s][0]), "used": found[s][1] >= 1} for s in want["symbols"]}
    if want.get("cited"):
        block, _n = pf.arquivos_citados(text, repo)
        res["cited"] = {c: f"`{c}`" in block for c in want["cited"]}
    return res


def main():
    pf = load("preflight_plano", "preflight_plano.py")
    sha = subprocess.run(["git", "-C", INTERNAL, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    out = {"internal_sha": sha, "preflight_plan": {}, "preflight_spec": {}, "probes": {}, "check": {}}
    for case in sorted(os.listdir(CASES)):
        with materialized(case) as (repo, path), tempfile.TemporaryDirectory() as state, \
             tempfile.TemporaryDirectory() as home:
            env = clean_env(state, home)
            if path.endswith("spec.md"):
                out["preflight_spec"][case] = {"rc": run("preflight_spec.py", path, env=env)}
                continue
            groups = {}
            try:
                text = pf.ler_plano(path)          # same reader as the preflight (bytes, strict UTF-8)
                # same call as the preflight's main; returns {group key: Resultado}
                for key, result in pf.executar_grupos(text, repo, pf.tasks_do_plano(text)).items():
                    groups[key] = {"state": result.estado, "count": len(result.achados)}
            except pf.NaoDeuParaChecar:
                groups = {key: {"state": "NAO_AVALIADO", "count": 0} for _title, key in pf.GRUPOS}
            rc_rg = run("preflight_plano.py", path, env=env)
            rc_git = run("preflight_plano.py", path, env=without_rg(env))
            if rc_rg != rc_git:                      # CI runs without ripgrep: the golden must not depend on it
                sys.exit(f"{case}: rc {rc_rg} with rg, {rc_git} without - fix the case before capturing")
            out["preflight_plan"][case] = {"rc": rc_rg, "groups": groups}
            probe_file = os.path.join(CASES, case, "probe.json")
            if os.path.exists(probe_file):               # unit-level decisions the group states cannot show
                with open(probe_file) as f:
                    want = json.load(f)
                with_rg = probe(pf, repo, text, want)
                child = subprocess.run([sys.executable, os.path.abspath(__file__), "--probe", path, probe_file],
                                       capture_output=True, text=True, env=without_rg(env), cwd=repo)
                without = json.loads(child.stdout)       # fresh process: busca.TEM_RG is read at import
                if with_rg != without:
                    sys.exit(f"{case}: probe differs with/without rg - fix the case before capturing")
                out["probes"][case] = with_rg
    for hook in sorted(glob.glob(os.path.join(HOOKS, "*.json"))):
        with materialized("prose_only") as (repo, plan), tempfile.TemporaryDirectory() as state, \
             tempfile.TemporaryDirectory() as home:
            env = clean_env(state, home)
            mark = json.dumps({"hook_event_name": "PostToolUse", "cwd": repo, "tool_name": "Write",
                               "tool_input": {"file_path": plan}})
            rc_mark = run("plan-gate.py", "marcar", env=env, stdin=mark)   # one pending plan in this repo
            rc = run("plan-gate.py", "checar", env=env, stdin=hook_payload(os.path.basename(hook), repo),
                     cwd=repo)
            out["check"][os.path.basename(hook)] = {"rc": rc, "mark_rc": rc_mark}
    with open(os.path.join(GOLDEN, "decisions.json"), "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)


def probe_only(plan, probe_file):
    """Child mode: print the probe for `plan` (cwd = its repo), run with a PATH that has no rg."""
    pf = load("preflight_plano", "preflight_plano.py")
    with open(probe_file) as f:
        want = json.load(f)
    print(json.dumps(probe(pf, os.getcwd(), pf.ler_plano(plan), want), sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--probe":
        probe_only(sys.argv[2], sys.argv[3])
    else:
        main()
