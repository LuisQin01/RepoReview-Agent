"""``tests/test_reviewers.py`` 针对规则审查器 ``src/reviewers.py`` 进行单元测试。

测试体系中的位置
----------------
规则审查器（rule-based reviewer）是 Agent 流水线中的“静态检查”步骤
（``run_static_checks``），与 LLM 审查器互为补充：规则审查器快速、确定性、
不消耗 token，负责命中可枚举的安全/质量模式；LLM 审查器则负责更语义化的判断。

本文件聚焦于验证规则审查器的一个核心能力——**硬编码 secret 检测**：当
新增行里出现形如 ``api_key = "abc123"`` 的赋值时，审查器必须把它标记为
``category == "secret"`` 且 ``source == "rule"``，便于上游在统计/脱敏/溯源时
区分“规则命中”与“LLM 命中”。

测试策略
--------
1. 用 :func:`src.diff_parser.parse_diff` 把内联 diff 文本转成
   :class:`~src.schemas.ChangedFile` 列表，再喂给
   :func:`src.reviewers.review_changed_files`，由此覆盖“解析 → 规则审查”这一
   小型集成链路。
2. 断言集中在 ``category`` 与 ``source`` 两个字段：
   * ``category`` 验证 secret 规则确实被触发；
   * ``source == "rule"`` 验证所有命中项都来源于规则器而非 LLM。
"""
from src.diff_parser import parse_diff
from src.reviewers import review_changed_files


def test_reviewers_find_hardcoded_secret():
    """验证规则审查器能命中硬编码 secret，并打上 ``category=secret``、``source=rule``。

    测试目的
    --------
    确认 ``review_changed_files`` 在新增行出现 ``api_key = "abc123"`` 这类
    硬编码凭证时，会产出至少一个 ``category == "secret"`` 的 issue，且
    所有 issue 的 ``source`` 都等于 ``"rule"``（即来自规则器而非 LLM）。
    这是 P0 安全能力，直接关系到能否在 LLM 未启用/不可用时仍挡住明显泄露。

    测试场景
    --------
    构造一段 diff：在 ``src/auth.py`` 的 ``login`` 函数中新增
    ``    api_key = "abc123"``。这种“变量名是常见凭证名 + 字符串字面量”
    的组合是规则审查器最典型的 secret 模式。

    预期结果
    --------
    * ``"secret"`` 出现在所有 issue 的 ``category`` 集合中；
    * **所有** issue 的 ``source`` 都是 ``"rule"``（用 ``all(...)`` 全量校验，
      而非只检查 secret 那一条，避免漏掉其他规则把来源标错的回归）。

    特殊逻辑解释
    ------------
    测试通过 ``parse_diff`` 而非直接构造 ``ChangedFile``，是为了顺便覆盖
    “解析器输出 → 规则审查器输入”的契约兼容性。``categories`` 用集合存储
    是因为本测试只关心“是否存在 secret”，不关心其顺序或重复次数。
    """
    diff_text = """diff --git a/src/auth.py b/src/auth.py
--- a/src/auth.py
+++ b/src/auth.py
@@ -1,2 +1,3 @@
 def login():
+    api_key = "abc123"
     return True
"""

    changed_files = parse_diff(diff_text)
    issues = review_changed_files(changed_files)

    categories = {issue.category for issue in issues}

    assert "secret" in categories  # 必须命中 secret 类别（P0 安全能力）
    # 全量校验所有 issue 的来源都是 rule，而非仅检查 secret 那一条
    assert all(issue.source == "rule" for issue in issues)
