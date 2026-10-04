import json, os, subprocess, tempfile, unittest, unittest.mock

from .helpers import fake_repo, import_bin, write

loop = import_bin("loop")


def review(verdict="APPROVED", findings=(), promises=()):
    return {"verdict": verdict, "summary": "s", "findings": list(findings),
            "spec_promises_without_task": list(promises)}


BLOCKER = {"severity": "BLOCKER", "where": "Task 1", "problem": "X is missing"}


class TestValidate(unittest.TestCase):
    def test_accepts_what_works(self):
        self.assertIsNone(loop.validate(review()))

    def test_rejects_missing_or_strange_verdict(self):
        # Without this, `data.get("verdict")` == None is not "REJECTED" -- and the gate opens.
        for v in (None, "", "MAYBE", "approved"):
            d = review(); d["verdict"] = v
            self.assertIsNotNone(loop.validate(d), repr(v))

    def test_rejects_finding_without_field_and_invalid_severity(self):
        self.assertIsNotNone(loop.validate(review(findings=[{"severity": "BLOCKER", "where": "x"}])))
        self.assertIsNotNone(loop.validate(review(findings=[dict(BLOCKER, severity="SEVERE")])))

    def test_rejects_every_shape_render_md_cannot_print(self):
        """Final review I4: each of these crashed render_md (exit 1) instead of going to repair / NO VERDICT."""
        good_finding = dict(BLOCKER, why_it_matters="w", evidence="e", fix="f")
        bad = {
            "summary missing": {k: v for k, v in review().items() if k != "summary"},
            "summary not text": dict(review(), summary=["s"]),
            "where not text": review(findings=[dict(BLOCKER, where=1)]),
            "problem not text": review(findings=[dict(BLOCKER, problem={"x": 1})]),
            "why not text": review(findings=[dict(good_finding, why_it_matters=3)]),
            "evidence not text": review(findings=[dict(good_finding, evidence=["a"])]),
            "fix not text": review(findings=[dict(good_finding, fix=None)]),
            "checks not a list": dict(review(), mechanical_checks="grep"),
            "check not an object": dict(review(), mechanical_checks=["grep x"]),
            "check field not text": dict(review(), mechanical_checks=[{"command": ["grep"], "result": "PASSED"}]),
            "promises not text": dict(review(), spec_promises_without_task=[{"x": 1}]),
        }
        for name, data in bad.items():
            with self.subTest(name):
                self.assertIsNotNone(loop.validate(data))
        full = dict(review(findings=[good_finding]),
                    mechanical_checks=[{"check": "c", "command": "grep x", "result": "PASSED", "detail": "d"}])
        self.assertIsNone(loop.validate(full))

    def test_rejects_what_is_not_even_an_object(self):
        self.assertIsNotNone(loop.validate([]))
        self.assertIsNotNone(loop.validate("APPROVED"))


class TestTolerantJson(unittest.TestCase):
    """Origin: a 10min40s round with verdict APPROVED and 3 real findings, lost whole to ONE stray backslash."""

    def test_invalid_escape_does_not_lose_the_round(self):
        # `\C` is not in JSON's escape alphabet (unlike `\t`, which json.loads accepts as a tab).
        raw = r'{"verdict": "APPROVED", "findings": [{"text": "the path C:\Path breaks"}]}'
        with self.assertRaises(ValueError):
            json.loads(raw)
        d = loop.tolerant_json(raw)
        self.assertEqual(d["verdict"], "APPROVED")
        self.assertEqual(len(d["findings"]), 1)

    def test_good_json_passes_intact(self):
        d = loop.tolerant_json('{"verdict": "BLOCKED", "findings": []}')
        self.assertEqual(d["verdict"], "BLOCKED")

    def test_garbage_that_is_not_json_still_fails(self):
        # Tolerating an invalid escape must not become "swallow anything": truncated JSON HAS to fail.
        with self.assertRaises(ValueError):
            loop.tolerant_json('{"verdict": "APPRO')

    def test_empty_json_still_fails(self):
        with self.assertRaises(ValueError):
            loop.tolerant_json("")

    def test_truncated_with_invalid_escape_before_the_cut_still_fails(self):
        # Truncation HAS to win even when the invalid escape that motivated the retry is also present.
        with self.assertRaises(ValueError):
            loop.tolerant_json(r'{"verdict": "APPROVED", "findings": [{"text": "C:\Temp and')


class TestTolerantJsonBackslashes(unittest.TestCase):
    """The old regex fixed BACKSLASH BY BACKSLASH, by position, ignoring whether it already formed a pair. The
    rule now treats whole RUNS: backslashes are consumed in pairs (each pair untouched); only a leftover from an
    odd count is judged against the escape alphabet, and doubled only when the next character is not in it."""

    def test_lone_backslash_alone_recovers(self):
        raw = r'{"verdict": "REJECTED", "findings": [{"problem": "C:\Wrong"}]}'
        with self.assertRaises(ValueError):
            json.loads(raw)
        d = loop.tolerant_json(raw)
        self.assertEqual(d["findings"][0]["problem"], r"C:\Wrong")

    def test_valid_pair_alone_passes_intact_without_the_regex_touching_it(self):
        raw = r'{"verdict": "REJECTED", "findings": [{"problem": "C:\\Users"}]}'
        json.loads(raw)  # already valid
        d = loop.tolerant_json(raw)
        self.assertEqual(d["findings"][0]["problem"], r"C:\Users")

    def test_lone_backslash_and_valid_pair_in_the_same_document_both_survive(self):
        raw = (r'{"verdict": "REJECTED", "findings": ['
               r'{"severity": "BLOCKER", "where": "Task 1", '
               r'"problem": "wrong path: C:\Wrong"}, '
               r'{"severity": "NOTE", "where": "Task 2", '
               r'"problem": "right path: C:\\Users"}]}')
        with self.assertRaises(ValueError):
            json.loads(raw)
        d = loop.tolerant_json(raw)
        self.assertEqual(d["findings"][0]["problem"], r"wrong path: C:\Wrong")
        # The valid pair stays ONE literal backslash -- never two, never three.
        self.assertEqual(d["findings"][1]["problem"], r"right path: C:\Users")

    def test_three_backslashes_before_an_invalid_letter_consume_the_pair_and_double_the_leftover(self):
        raw = r'{"verdict": "REJECTED", "findings": [{"problem": "three: \\\d"}]}'
        with self.assertRaises(ValueError):
            json.loads(raw)
        d = loop.tolerant_json(raw)
        self.assertEqual(d["findings"][0]["problem"], "three: " + "\\" * 2 + "d")

    def test_valid_escapes_stay_intact_even_with_recovery_in_the_document(self):
        raw = (r'{"verdict": "REJECTED", "findings": ['
               r'{"problem": "line1\nline2\ttab \u00e7"}, '
               r'{"problem": "C:\Wrong"}]}')
        with self.assertRaises(ValueError):
            json.loads(raw)
        d = loop.tolerant_json(raw)
        self.assertEqual(d["findings"][0]["problem"], "line1\nline2\ttab ç")
        self.assertEqual(d["findings"][1]["problem"], r"C:\Wrong")

    def test_incomplete_u_still_fails_even_with_something_recoverable_elsewhere(self):
        # `\u12` (missing hex digits): the LETTER `u` is in the alphabet, so the regex never touches it;
        # guessing the missing digits would invent content.
        raw = (r'{"verdict": "REJECTED", "findings": ['
               r'{"problem": "C:\Wrong"}, {"problem": "\u12"}]}')
        with self.assertRaises(ValueError):
            loop.tolerant_json(raw)

    def test_backslash_at_the_end_of_the_document_still_fails_by_truncation(self):
        raw = r'{"verdict": "REJECTED", "findings": [{"problem": "wrong path \\'
        with self.assertRaises(ValueError):
            loop.tolerant_json(raw)

    def test_regex_never_produces_valid_json_with_different_content(self):
        raw = (r'{"verdict": "REJECTED", "summary": "C:\Wrong and C:\\Users in the same text", '
               r'"findings": [{"severity": "BLOCKER", "where": "Task 1", '
               r'"problem": "C:\Wrong"}], "spec_promises_without_task": []}')
        with self.assertRaises(ValueError):
            json.loads(raw)
        d = loop.tolerant_json(raw)
        self.assertEqual(d["verdict"], "REJECTED")
        self.assertEqual(d["summary"], r"C:\Wrong and C:\Users in the same text")
        self.assertEqual(len(d["findings"]), 1)
        self.assertEqual(d["findings"][0]["severity"], "BLOCKER")
        self.assertEqual(d["findings"][0]["where"], "Task 1")
        self.assertEqual(d["spec_promises_without_task"], [])

    def test_second_attempt_reports_the_error_of_the_original_text_not_the_modified_one(self):
        original = r'{"a": "C:\W", "b": "APRO'  # original: invalid escape; the retry: unterminated string
        with self.assertRaises(ValueError) as ctx:
            loop.tolerant_json(original)
        with self.assertRaises(ValueError) as expected:
            json.loads(original)
        self.assertIn("escape", str(expected.exception))
        self.assertEqual(str(ctx.exception), str(expected.exception))


class TestCoerceVerdict(unittest.TestCase):
    """The verdict decision is NOT the model's: it has written APPROVED with an open blocker in its own list."""

    def test_blocker_forces_rejected(self):
        d = review("APPROVED", findings=[BLOCKER])
        self.assertEqual(len(loop.coerce_verdict(d)), 1)
        self.assertEqual(d["verdict"], "REJECTED")

    def test_promise_without_task_forces_rejected_even_without_a_blocker(self):
        # A promise without a task is a requirement nobody will implement, and no task test catches it.
        d = review("APPROVED", promises=["the spec asks for X and no task does it"])
        self.assertEqual(loop.coerce_verdict(d), [])
        self.assertEqual(d["verdict"], "REJECTED")

    def test_clean_approved_stays_approved(self):
        d = review("APPROVED")
        loop.coerce_verdict(d)
        self.assertEqual(d["verdict"], "APPROVED")


class TestPreviousRounds(unittest.TestCase):
    def touch(self, d, *names):
        for name in names:
            with open(os.path.join(d, name), "w") as f:
                f.write("x")

    def test_orders_by_N_and_ignores_the_other_namespace(self):
        d = tempfile.mkdtemp()
        self.touch(d, "p-opencode-round-1.md", "p-opencode-round-2.md", "p-opencode-round-10.md",
                   "p-codex-round-1.md", "p-round-1.md")
        found = [os.path.basename(x) for x in loop.previous_rounds(d, "p", "opencode")]
        self.assertEqual(found, ["p-opencode-round-1.md", "p-opencode-round-2.md", "p-opencode-round-10.md"])

    def test_file_without_namespace_in_the_name_does_not_count(self):
        # `p-round-1.md` belongs to no namespace. If it counted, round 1 of one tool would read another's as its own.
        d = tempfile.mkdtemp()
        self.touch(d, "p-round-1.md")
        self.assertEqual(loop.previous_rounds(d, "p", "codex"), [])

    def test_codex_does_not_get_the_opencode_history_nor_vice_versa(self):
        d = tempfile.mkdtemp()
        self.touch(d, "p-opencode-round-2.md", "p-codex-round-1.md", "p-codex-round-2.md", "p-codex-round-3.md")
        self.assertEqual([os.path.basename(x) for x in loop.previous_rounds(d, "p", "codex")],
                         ["p-codex-round-1.md", "p-codex-round-2.md", "p-codex-round-3.md"])
        self.assertEqual([os.path.basename(x) for x in loop.previous_rounds(d, "p", "opencode")],
                         ["p-opencode-round-2.md"])


class TestFindSpec(unittest.TestCase):
    """Pointing the reviewer at a file of `specs/` made the by-date fallback match the file ITSELF, and the
    review came out against itself, in silence."""

    PLANS, SPECS = "docs/superpowers/plans", "docs/superpowers/specs"

    def setup_files(self, repo, plans=(), specs=()):
        for name in plans:
            write(os.path.join(repo, self.PLANS, name), "# p\n")
        for name in specs:
            write(os.path.join(repo, self.SPECS, name), "# s\n")
        return os.path.join(repo, self.PLANS), os.path.join(repo, self.SPECS)

    def test_pointing_at_a_spec_does_NOT_return_itself(self):
        with fake_repo() as repo:
            # a unique date on purpose: with two specs on the same date `len(candidates) == 1` would already
            # block it by accident, and the test would prove nothing.
            _, d_specs = self.setup_files(repo, specs=["2026-09-18-gateway-design.md"])
            spec = os.path.join(d_specs, "2026-09-18-gateway-design.md")
            self.assertIsNone(loop.find_spec(spec, repo, self.PLANS, self.SPECS))

    def test_plan_with_a_same_name_spec_still_finds_it(self):
        with fake_repo() as repo:
            d_plans, _ = self.setup_files(repo, plans=["2026-09-18-gateway.md"],
                                          specs=["2026-09-18-gateway-design.md"])
            found = loop.find_spec(os.path.join(d_plans, "2026-09-18-gateway.md"), repo, self.PLANS, self.SPECS)
            self.assertEqual(os.path.basename(found), "2026-09-18-gateway-design.md")

    def test_by_date_fallback_still_holds_for_a_plan(self):
        with fake_repo() as repo:
            d_plans, _ = self.setup_files(repo, plans=["2026-09-18-phase1-client.md"],
                                          specs=["2026-09-18-gateway-design.md"])
            found = loop.find_spec(os.path.join(d_plans, "2026-09-18-phase1-client.md"), repo,
                                   self.PLANS, self.SPECS)
            self.assertEqual(os.path.basename(found), "2026-09-18-gateway-design.md")

    def test_find_spec_uses_the_configured_specs_dir(self):
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "work", "plans", "2026-01-01-x.md"), "# P\n")
            spec = write(os.path.join(repo, "work", "specs", "2026-01-01-x-design.md"), "# S\n")
            self.assertEqual(loop.find_spec(plan, repo, "work/plans", "work/specs"), spec)
            self.assertIsNone(loop.find_spec(plan, repo, "work/plans", "docs/superpowers/specs"))


class TestRenderMd(unittest.TestCase):
    def render(self, namespace, telemetry, lang="en"):
        return loop.render_md(review(), path="/repo/plan.md", spec=None, repo="/repo", namespace=namespace,
                              n=1, when="2026-09-18T00:00:00-03:00", wall_s=5, telemetry=telemetry, lang=lang)

    def test_review_title_names_the_tool_not_an_internal_label(self):
        md = self.render("opencode", {"model_requested": "deepseek/deepseek-v3"})
        first_line = md.splitlines()[0]
        self.assertIn("opencode", first_line)
        self.assertNotIn("glm", first_line)

    def test_render_md_declares_reviewer_model_and_route_for_real(self):
        md = self.render("opencode", {"model_requested": "zai-coding-plan/glm-5.3",
                                      "route": "endpoint reviewer.example.com", "steps": 3})
        self.assertIn("- **Reviewer:** opencode · model requested: zai-coding-plan/glm-5.3", md)
        self.assertIn("- **Route:** endpoint reviewer.example.com", md)
        self.assertNotIn("model that answered", md)

    def test_render_md_without_declaration_is_honest_and_does_not_invent(self):
        md = self.render("codex", None)
        self.assertIn("- **Reviewer:** codex · model requested: (not declared)", md)
        self.assertIn("- **Route:** (not declared)", md)


class TestRenderMdLabels(unittest.TestCase):
    def render(self, telemetry=None, as_spec=False, lang="en"):
        return loop.render_md(review(), path="/repo/x.md", spec=None, repo="/repo", namespace="opencode", n=2,
                              when="w", wall_s=5, telemetry=telemetry, lang=lang, as_spec=as_spec)

    def test_the_streams_model_is_labelled_and_an_empty_one_is_dropped(self):          # M8
        for lang, label in (("en", "model reported by the stream: zai/glm"),
                            ("pt-BR", "modelo informado pelo stream: zai/glm")):
            with self.subTest(lang=lang):
                md = self.render({"model": "zai/glm", "steps": 3}, lang=lang)
                self.assertIn(label, md)
                self.assertNotIn("model=", md)
        md = self.render({"model": "", "steps": 3})
        self.assertNotIn("model", md.split("Telemetry:**", 1)[1].splitlines()[0])
        self.assertIn("steps=3", md)

    def test_a_spec_review_has_a_spec_title(self):                                    # M9
        self.assertEqual(self.render(as_spec=True).splitlines()[0], "# Spec review — round 2 — opencode")
        self.assertEqual(self.render(as_spec=True, lang="pt-BR").splitlines()[0],
                         "# Revisão da spec — rodada 2 — opencode")
        self.assertEqual(self.render().splitlines()[0], "# Plan review — round 2 — opencode")

    def test_a_spec_prompt_has_no_preflight_line(self):                               # M9
        with fake_repo() as repo:
            spec = write(os.path.join(repo, "docs", "superpowers", "specs", "2026-01-01-x-design.md"), "# S\n")
            text = loop.build_prompt(spec, None, repo, 1, [], "/nonexistent", None, "en", as_spec=True)
            self.assertNotIn("preflight did NOT run", text)
            self.assertNotIn("## Preflight", text)
            plan = write(os.path.join(repo, "docs", "superpowers", "plans", "2026-01-01-x.md"), "# P\n")
            self.assertIn("preflight did NOT run", loop.build_prompt(plan, None, repo, 1, [], "/x", None, "en"))


class TestHonestDiff(unittest.TestCase):
    def files(self, a, b):
        d = tempfile.mkdtemp()
        return (write(os.path.join(d, "a.md"), a), write(os.path.join(d, "b.md"), b))

    @staticmethod
    def spread(old, new):
        """Two texts differing in 3 far-apart places (3 hunks)."""
        base = [f"line{i}\n" for i in range(40)]
        a = "".join(base)
        for i in (2, 20, 38):
            base[i] = f"CHANGED{i}\n"
        return a, "".join(base)

    def test_unchanged_plan_says_so(self):
        a, b = self.files("x\n", "x\n")
        self.assertIn("did NOT change", loop.honest_diff(a, b))

    def test_small_diff_under_the_limit_is_whole_without_warning(self):
        a, b = self.files(*self.spread(0, 0))
        out = loop.honest_diff(a, b)
        self.assertEqual(out.count("@@ -"), 3)
        self.assertNotIn("INCOMPLETE", out)

    def test_over_the_limit_cuts_at_a_hunk_boundary_and_names_the_ranges(self):
        a, b = self.files(*self.spread(0, 0))
        out = loop.honest_diff(a, b, limit=len(loop.honest_diff(a, b)) - 20)  # only the last hunk overflows
        self.assertIn("INCOMPLETE DIFF — 2 of 3 hunks", out)
        self.assertIn("The 1 omitted hunks", out)
        self.assertIn("CHANGED20", out)
        self.assertNotIn("CHANGED38", out.split("INCOMPLETE")[0])
        self.assertIn("36-40", out)  # the omitted hunk's range in the CURRENT plan

    def test_first_hunk_bigger_than_the_limit_is_still_included_whole(self):
        a, b = self.files(*self.spread(0, 0))
        out = loop.honest_diff(a, b, limit=1)
        self.assertIn("INCOMPLETE DIFF — 1 of 3 hunks", out)
        self.assertIn("CHANGED2", out)
        self.assertEqual(out.split("INCOMPLETE")[0].count("@@ -"), 1)

    def test_unreadable_snapshot_is_not_reported_as_unchanged(self):
        _, b = self.files("x\n", "x\n")
        out = loop.honest_diff("/nonexistent/snapshot.md", b)
        self.assertNotIn("did NOT change", out)
        self.assertIn("could not diff", out)

    def test_pure_deletion_hunk_gets_a_sane_range(self):
        # `diff -u` only emits `+N,0` for real when the file ends up empty, so feed a synthetic diff.
        fake = ("--- a\n+++ b\n@@ -1,2 +1,2 @@\n-x\n+y\n z\n k\n"
                "@@ -10,2 +9,0 @@\n-gone\n-gone2\n")
        done = subprocess.CompletedProcess([], 1, stdout=fake, stderr="")
        with unittest.mock.patch.object(loop.subprocess, "run", return_value=done):
            out = loop.honest_diff("a", "b", limit=1)
        self.assertIn("deleted before line 10", out)
        self.assertNotIn("9-8", out)


class TestBuildPrompt(unittest.TestCase):
    def prompt_for(self, repo, previous=(), as_spec=False, preflight="PF", n=1, snap="/nonexistent"):
        plan = write(os.path.join(repo, "docs", "superpowers", "plans", "2026-01-01-x.md"), "# P\n")
        return plan, lambda: loop.build_prompt(plan, None, repo, n, list(previous), snap, preflight, "en",
                                               as_spec=as_spec)

    def round_files(self, repo, json_text):
        rev = os.path.join(repo, "docs", "superpowers", "plans", "reviews")
        md = write(os.path.join(rev, "2026-01-01-x-opencode-round-1.md"), "md")
        write(md[:-3] + ".json", json_text)
        return md

    def test_round_one_uses_the_full_contract(self):
        with fake_repo() as repo:
            _, build = self.prompt_for(repo)
            text = build()
            self.assertIn("A new path does NOT inherit the unsafe default", text)

    def test_later_round_uses_the_incremental_contract(self):
        with fake_repo() as repo:
            md = self.round_files(repo, json.dumps(review("REJECTED", [BLOCKER])))
            _, build = self.prompt_for(repo, [md], n=2)
            text = build()
            self.assertIn("in round 2", text)
            self.assertIn("X is missing", text)
            self.assertNotIn("A new path does NOT inherit the unsafe default", text)

    def test_as_spec_uses_the_spec_contract(self):
        with fake_repo() as repo:
            _, build = self.prompt_for(repo, as_spec=True)
            text = build()
            self.assertIn("reviewer of a SPEC", text)
            self.assertNotIn("A new path does NOT inherit the unsafe default", text)

    def test_preflight_with_braces_is_appended_verbatim(self):
        with fake_repo() as repo:
            _, build = self.prompt_for(repo, preflight="table {x} {0} {}")
            self.assertIn("table {x} {0} {}", build())

    def test_previous_json_with_the_wrong_shape_refuses_the_round(self):
        for bad in ("[]", json.dumps({"findings": [{"severity": "BLOCKER", "problem": "no where"}]}),
                    json.dumps({"findings": ["BLOCKER"]})):
            with fake_repo() as repo:
                md = self.round_files(repo, bad)
                _, build = self.prompt_for(repo, [md], n=2)
                with self.assertRaises(loop.PreviousBlockersUnreadable):
                    build()

    def test_prompt_states_the_language_and_the_missing_preflight(self):
        with fake_repo() as repo:
            plan = write(os.path.join(repo, "docs", "superpowers", "plans", "2026-01-01-x.md"), "# P\n")
            text = loop.build_prompt(plan, None, repo, 1, [], "/nonexistent", None, "pt-BR")
            self.assertIn("Brazilian Portuguese", text)
            self.assertIn("preflight did NOT run", text)


if __name__ == "__main__":
    unittest.main()
