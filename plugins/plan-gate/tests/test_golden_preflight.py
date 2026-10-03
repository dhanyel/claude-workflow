import inspect, json, os, subprocess, sys, tempfile, unittest
from .helpers import run, GOLDEN, BIN, import_bin

sys.path.insert(0, GOLDEN)
from golden_lib import materialized

STATE = {"OK": "ok", "NAO_APLICAVEL": "not_applicable", "ACHADO": "findings", "NAO_AVALIADO": "not_evaluated"}
# R10: the INTERNAL run_groups is keyed by SHORT keys, not by titles; the golden records those keys.
GROUP = {"aliases": "aliases", "simbolos": "invoked-symbols", "constantes": "decorative-constants",
         "fail_open": "tolerant-defaults", "arquivos": "cited-files", "commits": "created-not-committed",
         "cobertura": "verification-gap"}


class TestPreflightGolden(unittest.TestCase):
    def test_same_decisions_as_the_internal_plugin(self):
        with open(os.path.join(GOLDEN, "decisions.json")) as f:
            golden = json.load(f)["preflight_plan"]
        self.assertTrue(golden, "an empty golden proves nothing")
        for case, expected in golden.items():
            with materialized(case) as (repo, plan):
                rc, _, _ = run("preflight_plan.py", plan, cwd=repo)
                self.assertEqual(rc, expected["rc"], case)
                _, out, _ = run("preflight_plan.py", "--json", plan, cwd=repo)
                got = {g["group"]: (g["state"], g["count"]) for g in json.loads(out)["findings"]
                       if g["group"] != "nothing-to-check"}
                want = {GROUP[title]: (STATE[g["state"]], g["count"]) for title, g in expected["groups"].items()}
                self.assertEqual(got, want, case)          # group by group, not a loose multiset


class TestVacuity(unittest.TestCase):
    def test_prose_only_plan_does_not_pass(self):
        with materialized("prose_only") as (repo, plan):
            _, out, _ = run("preflight_plan.py", "--json", plan, cwd=repo)
            result = json.loads(out)
            self.assertEqual(result["status"], "fail")
            self.assertIn("nothing-to-check", [g["group"] for g in result["findings"]])

    def test_json_lists_all_seven_groups_with_the_stable_shape(self):
        with materialized("prose_only") as (repo, plan):
            _, out, _ = run("preflight_plan.py", "--json", plan, cwd=repo)
            groups = [g for g in json.loads(out)["findings"] if g["group"] != "nothing-to-check"]
            self.assertEqual(sorted(g["group"] for g in groups), sorted(GROUP.values()))
            for g in groups:
                self.assertEqual({"group", "state", "count", "items"} - set(g), set())

    def test_status_is_pass_only_with_no_finding_and_at_least_one_ok(self):
        pf = import_bin("preflight_plan")
        R = pf.Result

        def status(results):
            from unittest import mock
            with mock.patch.object(pf, "read_plan", return_value="x"), \
                 mock.patch.object(pf, "run_groups", return_value=results), \
                 mock.patch.object(pf, "tasks_of_plan", return_value=[]):
                return pf.json_report("/x/plan.md", "/x")["status"]

        na = lambda: R("not_applicable", "nothing")
        keys = [key for key, _ in pf.GROUPS]

        def groups(**over):
            return {key: over.get(key, na()) for key in keys}

        self.assertEqual(status(groups(aliases=R("ok"))), "pass")
        self.assertEqual(status(groups()), "fail")                                           # vacuous
        self.assertEqual(status(groups(aliases=R("ok"), commits=R("findings", findings=["x"]))), "fail")
        self.assertEqual(status(groups(aliases=R("ok"), commits=R("not_evaluated", "could not"))), "fail")

    def test_unreadable_plan_marks_every_group_not_evaluated(self):
        with materialized("prose_only") as (repo, plan):
            with open(plan, "wb") as f:
                f.write(b"# Plan\n\xff\xfe")
            rc, out, _ = run("preflight_plan.py", "--json", plan, cwd=repo)
            result = json.loads(out)
            self.assertEqual(result["status"], "fail")
            self.assertEqual(rc, 2)
            self.assertEqual({g["state"] for g in result["findings"]}, {"not_evaluated"})
            self.assertEqual(len(result["findings"]), 7)


def probe(pf, repo, text, want):
    """Mirror of capture_internal.probe, against the PORTED functions (not an adapter):
    symbols: does the repo DEFINE it / is it merely USED (check_in_batch); cited: does the
    cited-files block mention the backticked string (looks_like_file kept it)."""
    res = {}
    if want.get("symbols"):
        found = pf.check_in_batch(repo, want["symbols"])
        res["symbols"] = {s: {"defined": bool(found[s][0]), "used": found[s][1] >= 1} for s in want["symbols"]}
    if want.get("cited"):
        block, _n = pf.cited_files(text, repo)
        res["cited"] = {c: f"`{c}`" in block for c in want["cited"]}
    return res


def without_rg(env):
    import shutil
    rg = shutil.which("rg", path=env.get("PATH"))
    keep = [d for d in env.get("PATH", "").split(os.pathsep) if not rg or d != os.path.dirname(rg)]
    return dict(env, PATH=os.pathsep.join(keep))


CHILD_MAIN = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import preflight_plan as pf
with open(sys.argv[2]) as f:
    want = json.load(f)
print(json.dumps(probe(pf, os.getcwd(), pf.read_plan(sys.argv[3]), want), sort_keys=True))
"""


def child_source():
    # the child runs the SAME `probe` (its source), so the comparison is not against a second copy
    return inspect.getsource(probe) + CHILD_MAIN


class TestProbes(unittest.TestCase):
    """R11: function-level decisions that group states cannot show -- recorded from the INTERNAL
    `checar_em_lote` / `arquivos_citados`, replayed here against `check_in_batch` / `cited_files`,
    in-process AND in a child whose PATH has no rg (search.HAS_RG is read at import)."""

    def test_probes_match_the_internal_decisions_with_and_without_rg(self):
        with open(os.path.join(GOLDEN, "decisions.json")) as f:
            probes = json.load(f)["probes"]
        self.assertTrue(probes, "an empty probe set proves nothing")
        pf = import_bin("preflight_plan")
        for case, expected in probes.items():
            with materialized(case) as (repo, plan), tempfile.TemporaryDirectory() as home, \
                 tempfile.TemporaryDirectory() as state:
                probe_file = os.path.join(GOLDEN, "cases", case, "probe.json")
                with open(probe_file) as f:
                    want = json.load(f)
                self.assertEqual(probe(pf, repo, pf.read_plan(plan), want), expected, case + " (in-process)")
                env = dict(os.environ, HOME=home, PLAN_GATE_DIR=state)
                env.pop("PLAN_GATE_LANG", None)
                child = subprocess.run(
                    [sys.executable, "-c", child_source(), BIN, probe_file, plan],
                    capture_output=True, text=True, env=without_rg(env), cwd=repo)
                self.assertEqual(child.returncode, 0, child.stderr)
                self.assertEqual(json.loads(child.stdout), expected, case + " (no rg)")
