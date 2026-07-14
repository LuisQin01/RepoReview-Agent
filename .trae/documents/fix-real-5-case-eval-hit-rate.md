# Fix P1: Real 5-case eval not reaching 1.00 hit rate

## Context

Running `py -3.11 -m src.eval_runner --cases evals/cases --repo .` produces `category_hit_rate: 0.20` — only `clean_change_no_issue` passes. The user's report hypothesised the cause was missing fixture source files (`src/config.py`, `src/parser.py`, etc.), but runtime evidence disproves this: every `should_find=true` case has `findings_count > 0`, meaning reviewers DO find issues from the diff alone (they never read source files for rule checks).

The actual root cause has two layers:

1. **Reporter drops `category` from JSON output** (primary blocker). [src/reporter.py:29-40](file:///d:/zhuomian/RepoReview%20Agent/src/reporter.py#L29-L40)'s `issue_to_finding` serialises `severity`, `file`, `line`, `issue`, `reason`, `suggested_fix`, `confidence`, `evidence`, `source` — but NOT `category`. The markdown renderer already shows `Category` (column 4), so JSON is inconsistent with markdown. As a result `extract_categories` ([src/eval_runner.py:166-172](file:///d:/zhuomian/RepoReview%20Agent/src/eval_runner.py#L166-L172)) finds no `category` key and falls back to `reason`, which is always `""` for rule findings. That's why `actual_categories` is `[]` in every failing case.

2. **`run_one_case` still uses strict category-set equality** ([src/eval_runner.py:214-228](file:///d:/zhuomian/RepoReview%20Agent/src/eval_runner.py#L214-L228)). The schema migration already added `expected_findings` / `must_not_findings` to `GroundTruth` ([src/schemas.py:149-162](file:///d:/zhuomian/RepoReview%20Agent/src/schemas.py#L149-L162)) and all 5 fixtures populate them. But `run_one_case` ignores them and requires `actual_categories == expected_categories` for `should_find=true` cases. Even after fix #1, this strict check would still fail `hardcoded_secret`, `deleted_exception_handling`, and `sensitive_log` because the reviewer naturally emits multiple categories per change (e.g. `test_gap` plus `secret`), and the fixtures' `expected_categories` only lists the primary one. The structured `expected_findings` (category + file + line range + severity) is the right granularity.

Existing tests didn't catch this because [tests/test_eval_runner.py:215-257](file:///d:/zhuomian/RepoReview%20Agent/tests/test_eval_runner.py#L215-L257) mocks `run_one_case`, and the other `run_one_case` tests mock `run_review_agent` with synthetic outputs — none run the real 5 fixtures end-to-end.

## Approach

Two minimal, targeted fixes plus a real-fixture regression test. No fixture source files need to be created; the rule reviewers operate on diff content only.

### 1. Add `category` to JSON output — [src/reporter.py](file:///d:/zhuomian/RepoReview%20Agent/src/reporter.py)

Insert `"category": issue.category,` into the dict returned by `issue_to_finding` (line 30). Place it right after `severity` so the field order matches the markdown column order (`Severity | File | Line | Category | ...`). This makes JSON consistent with markdown and lets `extract_categories` find the primary `category` key.

No existing test asserts the absence of `category`, so this is additive.

### 2. Migrate `run_one_case` to structured finding matching — [src/eval_runner.py](file:///d:/zhuomian/RepoReview%20Agent/src/eval_runner.py)

Add three private helpers near `extract_categories` (around line 173):

- `_actual_finding_matches_expected(actual, expected) -> bool` — matches one actual finding dict against one `GroundTruthFinding`. Rules:
  - `actual.category == expected.category` OR `actual.category in expected.acceptable_alternatives`
  - `actual.severity == expected.severity` (note: reporter already maps `error→high`, `warning→medium`, `info→low`, so JSON severities align with `expected_findings` severities)
  - Location:
    - If `expected.file is None` (repo-level): accept `actual.file` in `(None, "", "(repository)")` — handles the `missing_test` case where the rule emits `(repository)` and the fixture says `file: null`.
    - Else: `actual.file == expected.file` AND `expected.start_line <= actual.line <= expected.end_line` (inclusive). Reject non-int / bool `line`.

- `_all_expected_findings_present(actual_findings, expected_findings) -> bool` — for each expected finding, find a distinct matching actual finding (consume matches so one actual finding can't satisfy two expected ones). Returns False if any expected finding has no match.

- `_has_prohibited_finding(actual_findings, must_not_findings) -> bool` — True if any actual finding matches a `must_not_finding`.

Then rewrite the body of `run_one_case` (lines 214-240) to dispatch on whether the case has migrated to structured findings:

```python
has_structured_findings = bool(expected.expected_findings or expected.must_not_findings)

if has_structured_findings:
    prohibited = _has_prohibited_finding(findings, expected.must_not_findings)
    if expected.should_find:
        expected_present = _all_expected_findings_present(findings, expected.expected_findings)
        passed = json_valid and expected_present and not prohibited
        false_positive = prohibited
    else:
        false_positive = len(findings) > 0
        passed = json_valid and not false_positive
else:
    # Legacy category-set path (kept for cases without expected_findings)
    actual_categories = extract_categories(findings)
    expected_categories = set(expected.expected_categories)
    if expected.should_find:
        unexpected = actual_categories - expected_categories
        false_positive = bool(unexpected)
        passed = json_valid and expected_categories.issubset(actual_categories) and not false_positive
    else:
        false_positive = len(findings) > 0
        passed = json_valid and not false_positive
```

The result dict still reports `actual_categories` and `expected_categories` for visibility (computed via `extract_categories` regardless of branch), so the existing CLI output format is unchanged.

This preserves the legacy path used by [tests/test_eval_runner.py:144-179](file:///d:/zhuomian/RepoReview%20Agent/tests/test_eval_runner.py#L144-L179), which constructs cases via `make_case` with only `expected_categories` / `should_find` (no `expected_findings`) and asserts strict-category behaviour. Those tests must continue to pass unchanged.

### 3. Add real-fixture regression tests — [tests/test_eval_runner.py](file:///d:/zhuomian/RepoReview%20Agent/tests/test_eval_runner.py)

Add two tests at the end of the file, no mocking:

- `test_real_eval_reaches_full_hit_rate_on_migrated_fixtures` — calls `eval_runner.run_eval(cases_dir=<project>/evals/cases, repo_root=<project>)` and asserts `cases == 5`, `category_hit_rate == 1.0`, `false_positive_count == 0`, `json_valid_rate == 1.0`. This is the regression test the user asked for.

- `test_real_case_passes_individual_fixture` (parametrised over the 5 case_ids) — calls `eval_runner.run_one_case(case_dir=<project>/evals/cases/<id>, repo_root=<project>)` and asserts `passed is True`, `json_valid is True`, `error == ""`. Pinpoints which case broke if regression appears.

Use `Path(__file__).resolve().parents[1]` for the project root (same pattern as `test_load_ground_truth_validates_all_migrated_cases` at line 50). These tests run the rule reviewer end-to-end (no LLM) and execute in well under a second.

## Files to Modify

- [src/reporter.py](file:///d:/zhuomian/RepoReview%20Agent/src/reporter.py) — one-line addition to `issue_to_finding`.
- [src/eval_runner.py](file:///d:/zhuomian/RepoReview%20Agent/src/eval_runner.py) — three private helpers + rewrite of `run_one_case` body (lines 214-240).
- [tests/test_eval_runner.py](file:///d:/zhuomian/RepoReview%20Agent/tests/test_eval_runner.py) — two new real-fixture tests appended.

No new files. No new dependencies. The five `evals/cases/*` fixtures stay unchanged. The four non-existent source files (`src/config.py`, etc.) do NOT need to be created — the rule reviewers run on diff content only.

## Verification

1. `py -3.11 -m src.eval_runner --cases evals/cases --repo .` — expect `cases: 5`, `category_hit_rate: 1.00`, `false_positive_count: 0`, `json_valid_rate: 1.00`. All 5 result entries show `"passed": true`.
2. `py -3.11 -m pytest tests/test_eval_runner.py -v` — all existing tests pass plus the 2 new ones (6 new parametrised + 1 aggregate = 7 new test cases).
3. `py -3.11 -m pytest tests/test_validation.py tests/test_cli_smoke.py tests/test_diff_parser.py -v` — confirms adding `category` to JSON output doesn't break any reporter / CLI / validation assertions.
4. `py -3.11 -m pytest` — full suite green; the project memory constraint that `src/` files change only when implementation defects are exposed is satisfied (this fix is exactly exposing such a defect: the reporter drops `category`).

## Out of Scope

- Providing the missing fixture source files (`src/config.py`, `src/parser.py`, `src/auth.py`, `src/payment.py`, `docs/usage.md`) is unnecessary — confirmed by `findings_count > 0` in the current eval output.
- Adding `--llm` eval support is not needed; the user's report runs without `--llm`.
- The duplicate `"except" in lowered` check at [src/reviewers.py:241-245](file:///d:/zhuomian/RepoReview%20Agent/src/reviewers.py#L241-L245) is a pre-existing minor defect unrelated to this P1; leave it alone.
