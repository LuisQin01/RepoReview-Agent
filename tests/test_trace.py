"""轨迹记录（src/trace.py + review_service.record_step）单元测试。

本文件覆盖 RepoReview Agent 的执行轨迹记录能力，验证 record_step 的时间
统计语义、save_trace 步骤的持久化完整性，以及 LLM 重试错误中敏感信息的
脱敏处理。

测试策略：
    - 使用 _make_trace_args 辅助函数构造开启 trace 的命令行参数（含临时仓库
      与 diff 文件），通过 run_review_agent 端到端触发轨迹记录与落盘；
    - 对时间统计，使用 monkeypatch 替换 perf_counter 注入可控时间序列，
      验证 record_step 使用传入的 started_at_perf 而非全局状态计算 duration；
    - 对脱敏逻辑，注入含 secret 的 LLMRetryableError 与 retry_errors，验证
      落盘 trace 文件中敏感信息被 [REDACTED] 替换且保留重试元数据。

在整体测试体系中的位置：
    本文件位于「轨迹记录」测试层，确保审查过程的可观测性与安全性——
    既能完整还原 8 步执行轨迹，又不会泄露 LLM 调用中的敏感凭证。
"""

import json
from types import SimpleNamespace

from src.cli import run_review_agent
from src.llm_client import LLMRetryableError
from src import review_service
from src.review_service import record_step


def _make_trace_args(tmp_path):
    """构造开启 trace 的命令行参数对象，供 run_review_agent 使用。

    用途：在临时目录下生成仓库文件（app.py）与 diff 文件（input.diff），
    并返回一个 SimpleNamespace 模拟 CLI 参数，其中 trace=True 且 trace_dir
    指向临时目录下的 traces 子目录，确保轨迹文件可被测试读取与断言。

    参数：
        tmp_path: pytest 提供的临时目录 fixture。

    返回：
        SimpleNamespace 对象，包含 diff、repo、max_context_chars、format、
        output、llm、llm_provider、trace、trace_dir、max_extra_context_files
        等字段，模拟解析后的命令行参数。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    # 写入一个简单的 Python 源文件，供审查流程读取上下文
    (repo / "app.py").write_text("def run():\n    return True\n", encoding="utf-8")
    diff_file = tmp_path / "input.diff"
    diff_file.write_text(
        """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -0,0 +1,2 @@
+def run():
+    return True
""",
        encoding="utf-8",
    )
    # 默认 llm=False 使用 mock provider，避免真实 LLM 调用；
    # trace=True 开启轨迹记录，trace_dir 指向临时目录便于断言
    return SimpleNamespace(
        diff=str(diff_file),
        repo=str(repo),
        max_context_chars=4000,
        format="json",
        output=None,
        llm=False,
        llm_provider="mock",
        trace=True,
        trace_dir=str(tmp_path / "traces"),
        max_extra_context_files=0,
    )


def test_record_step_uses_its_own_start_time_and_allows_zero_duration(monkeypatch):
    """验证 record_step 使用传入的 started_at_perf 计算时长且允许 0 duration。

    测试目的：
        确认 record_step 的 duration 计算基于调用方传入的 started_at_perf
        （步骤起始时间），而非依赖模块级全局状态；同时验证当结束时间等于
        起始时间时，duration_ms 允许为 0（而非负数或异常）。

    测试场景：
        通过 monkeypatch 将 perf_counter 替换为可控的时间序列迭代器
        [10.0, 10.125]，连续调用两次 record_step，均传入 started_at_perf=10.0。
        第一次调用时 perf_counter 返回 10.0（与起始相同），第二次返回 10.125。

    特殊逻辑：
        使用迭代器注入时间序列，使两次 record_step 调用分别得到不同的「当前时间」，
        从而验证 duration = round((current - started_at) * 1000) 的计算逻辑。
        第一次差值为 0 -> duration_ms=0；第二次差值为 0.125s -> duration_ms=125。

    预期输出：
        两步的 duration_ms 列表为 [0, 125]。
    """
    state = SimpleNamespace(trace_steps=[])
    # 注入可控时间序列：第一次调用 perf_counter 返回 10.0，第二次返回 10.125
    timestamps = iter([10.0, 10.125])
    monkeypatch.setattr(review_service, "perf_counter", lambda: next(timestamps))

    # 第一次：started_at=10.0，当前时间=10.0 -> duration=0ms
    record_step(state, "zero", started_at_perf=10.0)
    # 第二次：started_at=10.0，当前时间=10.125 -> duration=125ms
    record_step(state, "later", started_at_perf=10.0)

    # 不变量：duration 基于传入的 started_at_perf 计算，分别为 0 和 125
    assert [step["duration_ms"] for step in state.trace_steps] == [0, 125]


def test_saved_trace_records_duration_for_every_step_including_final_save(tmp_path):
    """验证保存的 trace 文件包含全部 8 步且 duration 非负且 save_trace 步 enabled=True。

    测试目的：
        确认端到端执行后落盘的 trace JSON 文件完整记录了全部 8 个步骤，
        每步的 duration_ms 为非负整数，且最后的 save_trace 步骤的
        detail.enabled 为 True（表示 trace 功能已启用）。

    测试场景：
        使用 _make_trace_args 构造开启 trace 的参数，调用 run_review_agent
        执行完整审查流程，然后读取 traces 目录下生成的 JSON 文件。

    特殊逻辑：
        同时断言 saved_steps == trace_steps，验证内存中的 trace_steps 与
        落盘文件内容完全一致（无序列化丢失）。

    预期输出：
        saved_steps 的步骤名序列为 8 个标准步骤、与 trace_steps 完全相等、
        所有 duration_ms 为非负整数、最后一步 detail.enabled 为 True。
    """
    _, trace_steps = run_review_agent(_make_trace_args(tmp_path))
    # 从 traces 目录读取唯一生成的 JSON 轨迹文件
    trace_path = next((tmp_path / "traces").glob("*.json"))
    saved_steps = json.loads(trace_path.read_text(encoding="utf-8"))["steps"]

    # 不变量：保存的步骤顺序与标准 8 步一致
    assert [step["step"] for step in saved_steps] == [
        "receive_task",
        "parse_diff",
        "collect_context",
        "run_static_checks",
        "run_llm_review",
        "validate_output",
        "render_report",
        "save_trace",
    ]
    # 不变量：落盘内容与内存中的 trace_steps 完全一致（无序列化丢失）
    assert saved_steps == trace_steps
    # 不变量：所有 duration_ms 必须为整数类型
    assert all(isinstance(step["duration_ms"], int) for step in saved_steps)
    # 不变量：所有 duration_ms 必须非负（允许 0）
    assert all(step["duration_ms"] >= 0 for step in saved_steps)
    # 不变量：save_trace 步骤的 detail.enabled 为 True，表示 trace 功能已启用
    assert saved_steps[-1]["detail"]["enabled"] is True


def test_saved_trace_redacts_retry_errors_and_keeps_retry_metadata(tmp_path, monkeypatch):
    """验证 LLM 重试错误中的敏感信息被脱敏且保留重试元数据。

    测试目的：
        确认当 LLM 调用失败并产生含敏感信息（如 token、api_key、password）的
        retry_errors 时，落盘的 trace 文件中所有敏感信息被 [REDACTED] 替换，
        同时保留 attempts / retries / exhausted 等重试元数据，且每条错误
        被截断到不超过 300 字符（含 [REDACTED] 占位符后上限为 303）。

    测试场景：
        定义一个含 secret 的 failing_call_model，其调用时抛出含 Bearer token
        与 api_key 的 LLMRetryableError，并在 last_retry_info 中附带 4 条
        retry_errors（分别覆盖纯文本、超长文本、token 形式、JSON 形式的敏感信息）。
        通过 monkeypatch 注入该模型，开启 llm=True 执行完整审查流程。

    特殊逻辑：
        retry_errors 涵盖多种敏感信息出现形式（URL 参数、JSON 字段、纯文本），
        验证脱敏逻辑的覆盖面；其中第 2 条为 400 字符的 "x"，验证截断逻辑。
        断言 secret 不在 trace 文件全文中出现，确保脱敏彻底。

    预期输出：
        trace 文件全文不含 secret、attempts=3、retries=2、exhausted=True、
        每条 retry_errors 长度 <= 303、首条错误包含 [REDACTED]。
    """
    secret = "super-secret-token"

    def failing_call_model(_prompt):
        # 模拟 LLM 调用失败，错误消息中含 Bearer token 与 api_key
        raise LLMRetryableError(f"Authorization: Bearer {secret}; api_key={secret}")

    # 附带重试元数据与多条含敏感信息的 retry_errors
    failing_call_model.last_retry_info = {
        "attempts": 3,
        "retries": 2,
        "retry_errors": [
            # 第 1 条：纯文本中含 Bearer token 与 api_key
            f"Authorization: Bearer {secret}; api_key={secret}",
            # 第 2 条：超长文本（400 字符），用于验证截断到 300 字符上限
            "x" * 400,
            # 第 3 条：token= 形式的敏感信息
            f"token={secret}",
            # 第 4 条：JSON 形式中含 api_key / token / password 多个敏感字段
            (
                f'provider response {{"api_key": "{secret}", '
                f'"token": "{secret}", "password": "{secret}"}}'
            ),
        ],
        "exhausted": True,  # 重试已耗尽
    }
    monkeypatch.setattr(
        review_service, "get_call_model", lambda *_args, **_kwargs: failing_call_model
    )
    args = _make_trace_args(tmp_path)
    # 开启真实 LLM 路径（使用注入的 failing_call_model）
    args.llm = True

    _, trace_steps = run_review_agent(args)
    trace_path = next((tmp_path / "traces").glob("*.json"))
    trace_text = trace_path.read_text(encoding="utf-8")
    # 从 trace_steps 中提取 run_llm_review 步骤
    llm_step = next(step for step in trace_steps if step["step"] == "run_llm_review")

    # 不变量：trace 文件全文不得出现原始 secret（脱敏彻底）
    assert secret not in trace_text
    # 不变量：重试次数元数据保留
    assert llm_step["detail"]["attempts"] == 3
    # 不变量：重试次数元数据保留
    assert llm_step["detail"]["retries"] == 2
    # 不变量：重试耗尽标记保留
    assert llm_step["detail"]["exhausted"] is True
    # 不变量：每条 retry_errors 截断后长度不超过 303（300 字符 + [REDACTED] 占位）
    assert all(len(error) <= 303 for error in llm_step["detail"]["retry_errors"])
    # 不变量：首条错误中的敏感信息已被 [REDACTED] 替换
    assert "[REDACTED]" in llm_step["detail"]["retry_errors"][0]
