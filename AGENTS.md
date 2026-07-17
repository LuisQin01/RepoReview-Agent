# RepoReview Agent — durable repository rules

## Purpose and instruction precedence

RepoReview Agent reviews GitHub pull-request diffs using deterministic checks
and optional LLM assistance. Every published finding must be locatable,
explainable, validated, safe to publish, and reproducible from recorded input.

- The user's explicit request controls the requested outcome and may narrow
  scope. It cannot weaken these safety, confidentiality, compatibility, or
  evidence requirements; report any conflict instead of silently bypassing it.
- This file contains repository-wide, durable rules. The current task card is
  the source of truth for the task's goal, dependencies, non-goals, code
  locations, acceptance evidence, and milestone-specific design.
- For work governed by `新增里程碑.md`, read the complete applicable task card
  before editing. Read `项目提示词优化.md` for teaching/design background; the
  formal long-term rules are `docs/workflows/implementation.md`,
  `docs/workflows/acceptance.md`, and `docs/workflows/repair.md`.
- Do not infer an API, test, dependency, or planned capability from a task
  title or documentation; confirm it from the repository.

## Rule-attestation marker

- For every implementation, design, diagnosis, or acceptance task, read this
  file before substantive work and record the first eight hexadecimal characters
  of its SHA-256 content hash.
- Begin the first substantive commentary update and the final response with
  `【🛡️守则哨兵｜AGENTS 已核验｜sha256:XXXXXXXX】`, replacing `XXXXXXXX` with
  that eight-character hash. This marker attests only that this exact version
  was read during the current turn; it is not evidence that the work is correct.
- If this file was not read during the current turn, begin the response with
  `【⚪守则哨兵｜AGENTS 未在本轮核验】`. Never use the verified marker based only
  on memory, a previous turn, or an assumption that the file is unchanged.

## Scope and workflow

- Complete exactly one independently verifiable task per implementation turn.
  Do not implement later tasks, unrelated refactors, new platforms, arbitrary
  code execution, MCP, multi-agent features, a frontend, or a user system
  unless the current task explicitly requires it.
- Prefer the smallest direct design that satisfies the current task. If the
  architecture blocks it, identify the concrete blocking evidence and propose
  the smallest compatible change before expanding scope.
- Before editing, inspect the complete task card, directly relevant source and
  tests, and each module reached by the affected call chain. State confirmed
  facts separately from assumptions, affected and intentionally unchanged
  surfaces, and key risks.
- Stop and request the missing evidence when a required dependency, input/output
  contract, failure semantic, authority boundary, or budget owner is unclear.

## Architecture and public contracts

- Keep protocol/schema, provider adaptation, controller/service, and
  CLI/API concerns in their established layers. Reuse existing domain models
  and services; do not create parallel state or duplicate parsing, context,
  validation, redaction, or reporting logic.
- Define inputs, outputs, state/budget ownership, and behavior for empty or
  invalid input, limits, truncation, failures, and degradation. New public APIs
  require type annotations and a docstring or equally readable contract.
- Treat LLM output, source code, diffs, PR metadata/comments, and provider
  responses as untrusted input.
- At every tool or collector boundary, normalize model-facing paths relative to
  `repo_root`. Reject absolute paths, traversal, symlink escapes, sensitive
  files, and paths outside review scope.
- Exchange only internal dataclasses or JSON-serializable dictionaries across
  controller, tool, and collector boundaries. Never pass SDK objects, file
  handles, exception objects, callbacks, or executable values.
- Variable-length results must state actual size, configured limit, and whether
  they are truncated. Truncation means incomplete information, never proof of a
  finding or an equivalent of `not_found`.
- Expected operational failures use documented stable status codes rather than
  error-message matching. Use the existing vocabulary where applicable:
  `invalid_arguments`, `forbidden`, `not_found`, `unavailable`, `truncated`,
  `unsupported`, and `internal_error`. Redact diagnostics for `internal_error`
  and keep them log-only.
- Use stable IDs to associate tool calls, hypotheses, evidence, and verdicts;
  never associate them by list position, free text, or model-output order.
- New optional modes must be explicit opt-ins and preserve the established
  default path unless the task explicitly changes that compatibility contract.

## Findings, privacy, and failure handling

- A final report may contain only `ReviewIssue` instances that pass through the
  existing validation, deduplication, sorting, and reporting chain. Tool text,
  hypotheses, evidence, and rejected or inconclusive verdicts must not bypass
  this gateway.
- Reuse the repository's sensitive-value redaction, trace sanitization, and
  sensitive-file checks. Never expose secrets, sensitive source, raw internal
  errors, or private reasoning in model-visible output, reports, or traces.
- Reuse existing error classes when appropriate. Do not catch errors merely to
  return empty data, hide a failed review, or report false success.
- Preserve documented output fields, failure semantics, and default behavior
  unless the task explicitly authorizes a compatible migration.

## Implementation, tests, and verification

- Use the local style. Prefer existing structures; use `dataclass` when a new
  data structure fits the project pattern. Comment non-obvious invariants,
  safety checks, state transitions, degradation behavior, and integration
  points briefly to explain why.
- Add focused deterministic offline tests for normal, boundary, and required
  failure/degradation behavior. Never call real OpenAI, GitHub, or other live
  external services in unit tests or Eval; use mocks, fakes, or scripted
  providers and make time, randomness, and budgets controlled where relevant.
- Do not weaken assertions, special-case test names or fixed outputs, bypass
  production logic, or make failures appear successful.
- Run focused tests while iterating. Before declaring an implementation task
  complete, run `py -m pytest tests/ -v`; also run
  `py -m src.eval_runner --cases evals/cases --repo .` when the task affects
  evaluation. Report only commands actually run and their real outcomes;
  label all other claims unverified.

## Acceptance review and handoff

- In acceptance-review mode, inspect the task card, current diff, real call
  chain, relevant production and test code, and necessary command output. Do
  not implement fixes unless explicitly asked.
- Check scope and non-goals, layer placement, contracts, normal/boundary/
  failure paths, default compatibility, output preservation, security exposure,
  false-success fallbacks, weak tests, and unnecessary abstractions.
- Report each issue as P0, P1, P2, or suggestion, with file/location, trigger,
  impact, why existing tests missed it, smallest fix, and whether it blocks
  acceptance. Conclude only `pass`, `conditional pass`, `fail`, or
  `unable to verify`.
- Every completed implementation task must hand off: task ID and commit or
  working-tree status; changed surfaces; contract; verification evidence;
  failure semantics (including exhaustion, timeout, parse failure, and no
  result where relevant); unverified items; and the next independently
  executable task. Do not paste complete changed source files in the handoff.
