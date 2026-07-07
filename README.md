# RepoReview Agent
通过参考[The-PR-Agent/pr-agent](https://github.com/The-PR-Agent/pr-agent)来实现一个Agent项目，用来进行本地代码审查，可以读取git diff和相关上下文来给出结构化的review建议

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
