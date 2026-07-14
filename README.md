# RepoReview Agent
通过参考[The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent)来实现一个Agent项目，用来进行本地代码审查，可以读取git diff和相关上下文来给出结构化的review建议

## Setup

The timeout and retry implementation is verified with Python 3.11.7,
OpenAI SDK 1.109.1, and pytest 7.4.0. Install the declared dependencies:

```powershell
py -m pip install -r requirements.txt
```

Run the test suite without calling the OpenAI API:

```powershell
py -m pytest tests/ -v
```

## Minimal HTTP API

`POST /reviews` runs one stateless, rule-based review. The API accepts an
inline diff only; its repository root is configured by the server process, so
clients cannot request arbitrary local files, enable LLM calls, or publish to
GitHub through this endpoint.

```powershell
py -m uvicorn src.api:app --host 127.0.0.1 --port 8000
```

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/reviews -ContentType 'application/json' -Body (@{ diff = (Get-Content .\examples\simple.diff -Raw) } | ConvertTo-Json)
```

The response contains redacted structured findings, run errors, step timing,
and summary metrics. It does not persist review runs or expose follow-up
query endpoints.

## GitHub PR summary publishing

Summary publishing is opt-in. The default CLI flow only writes the local
report and does not make GitHub requests. To publish the validated summary to
a GitHub pull request, set the token and the bot login in the environment,
then provide both flags:

```powershell
$env:GITHUB_TOKEN = "<token>"
$env:GITHUB_SUMMARY_COMMENT_AUTHOR_LOGIN = "reporeview-bot"
py -m src.cli --diff path\to\changes.diff --repo . --publish-summary-comment --pr-url https://github.com/OWNER/REPO/pull/123
```

The configured login is non-sensitive and is used to identify the bot's own
marked summary comment. The token is read only from `GITHUB_TOKEN` and is not
printed or recorded in the trace. A publish failure stops the command instead
of being reported as a successful local-only run.

## Agent Loop

RepoReview Agent uses a fixed-step loop for now:

```text
1. receive_task
   - read CLI arguments, including diff path, repo path, output format

2. parse_diff
   - parse git diff into changed files, added lines, deleted lines

3. collect_context
   - read related file contents from the repository

4. run_static_checks
   - run rule-based checks for TODO, debug output, secrets, missing tests

5. run_llm_review
   - optionally send diff, context, and rule findings to the LLM reviewer

6. validate_output
   - make sure findings are in a renderable structure

7. render_report
   - render JSON or Markdown report

8. save_trace
   - record execution steps for debugging and future evaluation
'''
