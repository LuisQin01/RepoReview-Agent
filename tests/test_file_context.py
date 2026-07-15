"""针对 ``src/file_context.py`` 的单元测试集（RepoReview Agent 的上下文检索核心）。

被测模块职责：
    ``file_context.py`` 负责根据 PR 的 diff 信息回到真实仓库中读取源码，
    按预算截取与变更相关的上下文片段，并包装成 ``FileContext`` 列表，
    供下游 ``prompt_builder`` 拼装进 LLM 输入。它决定了「LLM 能看到什么」，
    同时承担三层敏感防护中的 L1 层（文件名 / 后缀 / 路径黑名单拦截）。

本文件覆盖范围（项目中最全面的单元测试文件）：
    1. 安全防护 L1 层：路径越界拦截、敏感文件跳过（参数化 14 个路径）、
       非敏感文件名含 "key" 不误判、非敏感配置可读、config/ 目录下 yaml 跳过。
    2. provenance（来源信息）保留：敏感文件保留来源但不读取内容。
    3. 截断策略：大文件截断、变更行优先截断、多变更行保留、hunk 内 unchanged
       行保留、多 hunk 分离、超大 hunk 前缀截断、缺失变更行回退到文件起始。
    4. 预算控制：总预算强制约束、额外文件使用剩余预算。
    5. 来源记录：import / call_name 候选的 provenance、重复候选保留首个
       provenance、缺失文件仍保留 provenance。
    6. 输入校验：``ContextBudget`` 非法值拒绝。
    7. 符号定位：``locate_python_symbol`` 对函数 / 方法 / 类 / 不可定位场景的处理。

在整体测试体系中的位置：
    本文件聚焦 ``file_context`` 模块的「单点行为」，是上下文检索层的根基测试。
    与 ``test_sensitive_leak.py``（三层敏感泄露端到端防护）配合，共同保证
    上下文检索的正确性与安全性。两者关系：本文件验证检索 / 截断 / 预算的
    正确语义，``test_sensitive_leak.py`` 验证这些语义在端到端链路上确实
    阻断了敏感内容外泄。
"""

import pytest

from src.file_context import collect_file_contexts, locate_python_symbol, read_file_context
from src.schemas import ChangedFile, ContextBudget, DiffHunk, DiffLine


def test_read_file_context_rejects_outside_repo(tmp_path):
    """验证 L1 层的路径越界拦截：相对路径 ``../`` 逃逸出仓库根目录时必须被拒绝。

    测试场景：
        在 ``tmp_path`` 下创建仓库目录 ``repo``，并在其外部放置一个
        ``secret.txt``；随后以 ``../secret.txt`` 作为 file_path 调用
        ``read_file_context``，模拟攻击者尝试通过相对路径越界读取仓库外文件。

    预期结果：
        - ``context.exists`` 为 ``False``（不读取任何内容）；
        - ``context.error`` 包含 "outside"，表明拦截原因被正确标注。

    设计理由：
        路径越界是读取层最基础的安全边界，必须在 ``read_file_context`` 入口
        处直接拦截，否则后续的敏感文件黑名单可被绕过。
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("do not read me", encoding="utf-8")

    context = read_file_context(
        repo_root=repo,
        file_path="../secret.txt",
        max_chars=100,
    )

    assert context.exists is False  # 越界文件不计入「存在」，防止下游误用
    assert "outside" in context.error  # error 文案需明确标注越界原因，便于排查


@pytest.mark.parametrize(
    "file_path",
    [
        ".env",
        ".env.production",
        ".envrc",
        "keys/id_rsa",
        "certificates/service.pem",
        "certificates/service.key",
        "certificates/service.crt",
        "certificates/service.cer",
        "certificates/service.pfx",
        "config/production.yaml",
        "CONFIG/nested/PROD.JSON",
        "deploy/values.yaml",
        "DEPLOY/nested/VALUES.YML",
        "settings.json",
    ],
)
def test_read_file_context_skips_sensitive_files_without_reading_content(tmp_path, file_path):
    """验证 L1 敏感文件黑名单：14 类典型敏感路径都不读取内容，只标注拒绝原因。

    测试目的：
        覆盖文件名精确匹配（.env/.envrc/id_rsa）、``.env.`` 前缀变体
        （.env.production）、证书后缀匹配（.pem/.key/.crt/.cer/.pfx）、
        config/ 目录下的 yaml、deploy/ 目录下的 yaml/yml、settings.json、
        以及大小写不敏感匹配（CONFIG/nested/PROD.JSON、DEPLOY/.../VALUES.YML）。

    测试场景（参数化设计理由）：
        14 个用例覆盖了 ``_is_sensitive_file_path`` 的所有判定分支——文件名黑名单、
        ``.env.`` 前缀、证书后缀、config/deploy 目录约定、settings.json 特例、
        路径大小写归一化。使用 ``@pytest.mark.parametrize`` 一次性枚举，
        任一用例失败都会独立报告，定位精准。

    预期结果（核心不变量）：
        - ``exists`` 为 ``False``：敏感文件视为「不可读」而非「不存在」，避免
          下游把它当作普通缺失文件处理而尝试其他路径；
        - ``content`` 为空字符串、``chars_read`` 为 0：双重保证字节层面零泄露；
        - ``error`` 包含 "sensitive"：明确归因，便于 L2/L3 层与日志排查。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    target = repo / file_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("API_KEY=secret\n", encoding="utf-8")

    context = read_file_context(repo, file_path, max_chars=100)

    assert context.exists is False  # 敏感文件统一标记为不可读
    assert context.content == ""  # 内容必须为空：L1 层字节级零泄露保证
    assert context.chars_read == 0  # 读取计数为 0，防止通过该字段侧信道泄露长度
    assert "sensitive" in context.error  # 拒绝原因必须可识别，便于下游与日志归类


@pytest.mark.parametrize("sensitive_path", [".env", "config/settings.json"])
def test_read_file_context_rejects_safe_alias_resolved_to_sensitive_target(
    tmp_path, monkeypatch, sensitive_path
):
    """A safe-looking alias must not bypass the sensitive target check."""
    repo = tmp_path / "repo"
    repo.mkdir()
    sensitive_target = repo / sensitive_path
    sensitive_target.parent.mkdir(parents=True, exist_ok=True)
    sensitive_target.write_text("TOKEN=DO_NOT_EXPOSE", encoding="utf-8")
    alias = repo / "safe.txt"

    # Model an in-repository symlink without requiring symlink privileges on Windows CI.
    original_resolve = type(alias).resolve

    def resolve_to_sensitive_target(path, *args, **kwargs):
        if path == alias:
            return sensitive_target
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(type(alias), "resolve", resolve_to_sensitive_target)

    context = read_file_context(repo, "safe.txt", max_chars=100)

    assert context.exists is False
    assert context.content == ""
    assert context.chars_read == 0
    assert "sensitive" in context.error
    assert "DO_NOT_EXPOSE" not in context.error


def test_read_file_context_does_not_reject_non_sensitive_filename_containing_key(tmp_path):
    """验证 L1 黑名单的精确性：文件名仅「包含」敏感子串（如 "key"）不应被误判。

    测试目的：
        防止黑名单实现使用 ``in`` 子串匹配导致误伤。``monkey.py`` 含子串 "key"
        但显然不是密钥文件，必须正常读取。

    测试场景：
        创建 ``monkey.py``，写入普通代码 ``value = 1``，调用读取。

    预期结果：
        - ``exists`` 为 ``True``；
        - ``content`` 完整等于文件内容，未被截断或拒读。

    设计理由：
        这是一个「负向用例 / 反例」，专门守护黑名单的精确匹配语义，
        防止过度拦截导致正常代码上下文丢失、影响 LLM 审查质量。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "monkey.py").write_text("value = 1\n", encoding="utf-8")

    context = read_file_context(repo, "monkey.py", max_chars=100)

    assert context.exists is True  # 普通文件不应被误判为敏感
    assert context.content == "value = 1\n"  # 内容完整无截断


def test_read_file_context_keeps_non_sensitive_configuration_readable(tmp_path):
    """验证非敏感配置文件可读：业务侧的 openapi.yaml 不应被 config 规则误伤。

    测试目的：
        ``config/`` 目录约定与 ``settings.json`` 等规则可能过度拦截，需确保
        仓库根目录下的普通 yaml（如 OpenAPI 描述文件）能正常读取。

    测试场景：
        在仓库根目录（非 config/ 子目录）创建 ``openapi.yaml``，写入
        ``feature_enabled: true``。

    预期结果：
        ``exists`` 为 ``True`` 且内容完整读取。

    设计理由：
        与上一用例共同守护黑名单的「精确性」边界——只拦截 config/ 目录内的
        yaml，不波及根目录的业务配置，避免上下文检索范围被错误收窄。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "openapi.yaml"
    config.write_text("feature_enabled: true\n", encoding="utf-8")

    context = read_file_context(repo, "openapi.yaml", max_chars=100)

    assert context.exists is True  # 根目录 yaml 不属于敏感 config 规则范围
    assert context.content == "feature_enabled: true\n"  # 完整读取


def test_read_file_context_skips_all_config_yaml_under_config_dir(tmp_path):
    """验证 config/ 目录约定：该目录下任意 yaml 都视为敏感配置而跳过。

    测试目的：
        config/ 目录通常存放环境相关配置（含密钥），统一拦截避免逐文件判定
        漏判。本用例验证 ``config/development.yaml`` 即使文件名本身不敏感
        也会被跳过。

    测试场景：
        在 ``config/development.yaml`` 写入 ``api_key: secret``。

    预期结果：
        与敏感文件用例一致——``exists`` 为 ``False``、``content`` 为空、
        ``chars_read`` 为 0、``error`` 含 "sensitive"。

    设计理由：
        config/ 目录约定是「目录级」防护，与文件名 / 后缀级防护互补，
        覆盖文件名不含敏感词但内容敏感的场景。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    config = repo / "config" / "development.yaml"
    config.parent.mkdir()
    config.write_text("api_key: secret\n", encoding="utf-8")

    context = read_file_context(repo, "config/development.yaml", max_chars=100)

    assert context.exists is False  # config/ 目录下 yaml 一律视为敏感
    assert context.content == ""  # 内容为空
    assert context.chars_read == 0  # 字节计数为 0
    assert "sensitive" in context.error  # 拒绝原因可识别


def test_collect_file_contexts_preserves_sensitive_file_provenance_without_content(tmp_path):
    """验证 L2 层 provenance 保留：敏感文件作为变更文件时保留来源信息但不读内容。

    测试目的：
        ``collect_file_contexts`` 是 L2 收集层。当 PR 直接修改了敏感文件
        （如 ``.env``）时，不能简单丢弃该条目——否则 LLM 会不知道有此变更；
        但也不能读取内容。正确做法是保留 provenance（来源 / 选择理由），
        同时把内容置空，让 L3 层据此对 diff 做脱敏替换。

    测试场景：
        构造一个 ``.env`` 的 ChangedFile（含 added_lines 与 patch），调用
        ``collect_file_contexts``，预算设为 max_extra_context_files=0
        （不收集额外上下文）。

    预期结果（核心不变量）：
        - 返回列表长度为 1，path 仍为 ``.env``（条目保留）；
        - ``exists`` 为 ``False``、``content`` 为空、``chars_read`` 为 0
          （内容不泄露，与 L1 行为一致）；
        - ``source == "changed_file"``、``selection_reason`` 为
          "file is changed in the pull request"（来源链完整）。

    设计理由：
        provenance 是 L2→L3 的契约——L3 据此识别「这是被变更的敏感文件」
        并替换其 diff。若 provenance 丢失，L3 将无法脱敏，导致泄露。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".env").write_text("API_KEY=secret\n", encoding="utf-8")
    changed_file = ChangedFile(
        path=".env",
        added_lines=[DiffLine(file_path=".env", line_no=1, content="+API_KEY=secret")],
        deleted_lines=[],
        patch="@@ -0,0 +1,1 @@\n+API_KEY=secret\n",
        hunks=[DiffHunk(start_line=1, end_line=1)],
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=100, max_extra_context_files=0),
    )

    assert len(contexts) == 1  # 敏感文件条目保留，未因敏感而丢弃
    context = contexts[0]
    assert context.path == ".env"  # path 字段保留，L3 据此定位 diff
    assert context.exists is False  # 内容不可读
    assert context.content == ""  # 内容为空：L2 层零泄露
    assert context.chars_read == 0  # 字节计数为 0
    assert context.source == "changed_file"  # provenance：来源为「变更文件」
    assert context.selection_reason == "file is changed in the pull request"  # 选择理由完整

def test_read_file_context_truncates_large_file(tmp_path):
    """验证基础截断：文件内容超过 max_chars 时按前缀截断并标记 truncated。

    测试目的：
        在未提供变更行 / hunk 信息时，``read_file_context`` 应从文件起始
        读取 max_chars 个字符，超出部分截断。

    测试场景：
        写入 ``abcdef``，max_chars=3。

    预期结果：
        - ``content`` 为 ``abc``（前缀）；
        - ``truncated`` 为 ``True``；
        - ``chars_read`` 为 3（与 max_chars 一致，便于预算核算）。

    设计理由：
        这是最朴素的截断路径，作为后续「变更行优先」用例的对照基线。
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    target = repo / "app.py"
    target.write_text("abcdef", encoding="utf-8")

    context = read_file_context(repo, "app.py", max_chars=3)

    assert context.exists is True  # 文件可读
    assert context.content == "abc"  # 按前缀截断
    assert context.truncated is True  # 截断标记置位
    assert context.chars_read == 3  # 实际读取字符数 = 预算


def test_read_file_context_prioritizes_changed_lines_when_truncated(tmp_path):
    """验证变更行优先截断：截断时优先保留变更行而非文件头部。

    测试目的：
        当指定了 ``changed_line_nos`` 且预算不足以容纳整个文件时，截断策略
        应优先把预算分配给变更行（LLM 最关心的部分），而不是机械地取文件前缀。

    测试场景：
        生成 300 行 ``header_N``，将第 250 行替换为 marker
        ``changed_marker = True``。max_chars 恰好等于 marker 长度，
        changed_line_nos=[250]。

    预期结果：
        - ``content`` 等于 marker（变更行被保留）；
        - ``header_1`` 不在内容中（文件头部被牺牲）；
        - ``truncated`` 为 ``True``、``chars_read`` 等于 marker 长度。

    设计理由：
        这是 file_context 截断策略的核心价值——让有限的预算服务于
        「变更相关上下文」，提升 LLM 审查的针对性。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    marker = "changed_marker = True\n"
    lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
    lines[249] = marker
    (repo / "app.py").write_text("".join(lines), encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len(marker),
        changed_line_nos=[250],
    )

    assert context.content == marker  # 变更行被完整保留
    assert "header_1" not in context.content  # 文件头部被截断牺牲
    assert context.truncated is True  # 标记截断
    assert context.chars_read == len(marker)  # 读取量 = 变更行长度


def test_read_file_context_preserves_multiple_changed_lines_when_truncated(tmp_path):
    """验证多变更行保留：预算恰好容纳多条变更行时全部保留。

    测试目的：
        当存在多条变更行时，截断策略应优先保留所有变更行（按出现顺序），
        而非只保留第一条。本用例验证两条变更行在预算恰好够时都被保留。

    测试场景：
        300 行 header，第 20、250 行分别替换为 first/second marker，
        max_chars 设为两条 marker 长度之和。

    预期结果：
        - ``content`` 为两条 marker 拼接（顺序保留）；
        - ``header_1`` 不在内容中（头部被牺牲）。

    设计理由：
        与单变更行用例对照，验证多变更行的预算分配不偏向第一条，
        保证审查覆盖所有变更点。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    markers = ["first_changed_marker\n", "second_changed_marker\n"]
    lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
    lines[19], lines[249] = markers
    (repo / "app.py").write_text("".join(lines), encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=sum(map(len, markers)),
        changed_line_nos=[20, 250],
    )

    assert context.content == "".join(markers)  # 两条变更行都被保留且顺序正确
    assert "header_1" not in context.content  # 非变更的头部被牺牲


def test_read_file_context_preserves_unchanged_lines_in_changed_hunk(tmp_path):
    """验证 hunk 内 unchanged 行保留：hunk 范围内的非变更行也一并保留。

    测试目的：
        hunk 是一个语义块，变更行周围的 unchanged 行对理解变更至关重要
        （如 if 块的头部与 body）。本用例验证当指定 ``changed_hunks`` 时，
        整个 hunk 范围内的行都被保留，即使其中部分行未在 changed_line_nos 中。

    测试场景：
        第 250 行为 ``if enabled:``（unchanged），第 251 行为
        ``    return value``（变更行）。changed_hunks 覆盖 250-251，
        max_chars 恰好容纳这两行。

    预期结果：
        ``content`` 同时包含 if 头部和 return body，``truncated`` 为 ``True``。

    设计理由：
        单独保留变更行会破坏代码语义结构（如只剩 return 而无 if），
        hunk 级保留保证 LLM 看到完整的逻辑块。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
    lines[249] = "if enabled:\n"
    lines[250] = "    return value\n"
    (repo / "app.py").write_text("".join(lines), encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len("if enabled:\n    return value\n"),
        changed_line_nos=[250],
        changed_hunks=[DiffHunk(start_line=250, end_line=251)],
    )

    assert context.content == "if enabled:\n    return value\n"  # hunk 整块保留
    assert context.truncated is True  # 触发截断但保留了完整 hunk


def test_read_file_context_keeps_multiple_hunks_separate(tmp_path):
    """验证多 hunk 分离：两个 hunk 之间的非相关行被丢弃，hunk 之间不互相污染。

    测试目的：
        当变更跨多个 hunk 时，截断策略应只保留各 hunk 范围内的行，
        跳过 hunk 之间的无关代码，避免上下文被无关内容稀释。

    测试场景：
        文件内容含两个 hunk（行 1-2 与行 5-6），中间夹着
        ``ignored_between_hunks``。max_chars 仅够容纳两个 hunk 的关键行。

    预期结果：
        - ``content`` 包含两个 hunk 的行；
        - ``ignored_between_hunks`` 不出现在内容中。

    设计理由：
        保证多个变更区域被独立、紧凑地呈现给 LLM，而非整段平铺。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    content = "one\nfirst_change\ntwo\nignored_between_hunks\nthree\nsecond_change\nfour\n"
    (repo / "app.py").write_text(content, encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len("one\nfirst_change\nthree\nsecond_change\n"),
        changed_hunks=[DiffHunk(1, 2), DiffHunk(5, 6)],
    )

    assert context.content == "one\nfirst_change\nthree\nsecond_change\n"  # 两个 hunk 内容保留
    assert "ignored_between_hunks" not in context.content  # hunk 间无关行被丢弃


def test_read_file_context_only_partially_copies_an_oversized_hunk(tmp_path):
    """验证超大 hunk 前缀截断：单个 hunk 超出预算时按前缀截断而非整块丢弃。

    测试目的：
        若一个 hunk 自身就超过剩余预算，应在 hunk 内部按前缀截取，而不是
        因为「装不下整块」就完全丢弃——否则 LLM 会完全看不到该变更。

    测试场景：
        hunk 覆盖行 2-3（``if enabled:`` 与 ``    return value``），
        max_chars 仅够容纳 ``if enabled:\\n`` 一行。

    预期结果：
        - ``content`` 为 ``if enabled:\\n``（hunk 的前缀部分）；
        - ``truncated`` 为 ``True``。

    设计理由：
        这是「hunk 优先」与「预算硬约束」的折中——优先整块保留，但单个
        hunk 过大时退化为前缀截断，保证至少有部分上下文可见。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("prefix\nif enabled:\n    return value\n", encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=len("if enabled:\n"),
        changed_hunks=[DiffHunk(2, 3)],
    )

    assert context.content == "if enabled:\n"  # hunk 前缀部分被保留
    assert context.truncated is True  # 截断标记置位


def test_read_file_context_falls_back_to_file_start_for_missing_changed_line(tmp_path):
    """验证缺失变更行回退：变更行号超出文件范围时回退到文件起始读取。

    测试目的：
        diff 解析出的 changed_line_nos 可能因文件已修改而与当前磁盘内容
        不一致（行号越界）。此时不应抛异常，而应回退到文件起始读取，
        保证上下文检索的健壮性。

    测试场景：
        文件仅 2 行，但传入 changed_line_nos=[999]（远超文件长度），
        max_chars=5。

    预期结果：
        - ``content`` 为 ``first``（文件前 5 字符）；
        - ``truncated`` 为 ``True``。

    设计理由：
        diff 与磁盘文件存在「时间差」，行号失配是常态而非异常。回退策略
        保证 Agent 在面对真实 PR（多次 commit 后）仍能产出可用上下文。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("first_line\nsecond_line\n", encoding="utf-8")

    context = read_file_context(
        repo,
        "app.py",
        max_chars=5,
        changed_line_nos=[999],
    )

    assert context.content == "first"  # 回退到文件起始的前 5 字符
    assert context.truncated is True  # 截断标记置位


def test_collect_file_contexts_enforces_total_budget_for_changed_files(tmp_path):
    """验证总预算强制：多个变更文件的总读取量不超过 max_prompt_chars。

    测试目的：
        ``collect_file_contexts`` 必须在多个变更文件之间合理分配预算，
        保证它们的 ``chars_read`` 之和不超过 ``max_prompt_chars``。这是
        prompt 不超长的硬约束。

    测试场景：
        两个变更文件 first.py / second.py，各自第 250 行为 marker，
        预算恰好等于两条 marker 长度之和。

    预期结果：
        - 所有 context 的 chars_read 之和 ≤ max_prompt_chars；
        - 每个文件的 content 恰为各自的 marker（变更行优先，预算够用）。

    设计理由：
        验证预算在「变更文件」维度上的全局约束，而非单文件局部约束。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    markers = {
        "first.py": "first_changed_marker\n",
        "second.py": "second_changed_marker\n",
    }
    changed_files = []

    for path, marker in markers.items():
        lines = [f"header_{line_no}\n" for line_no in range(1, 301)]
        lines[249] = marker
        (repo / path).write_text("".join(lines), encoding="utf-8")
        changed_files.append(
            ChangedFile(
                path=path,
                added_lines=[DiffLine(path, 250, marker.rstrip())],
                deleted_lines=[],
                patch="",
            )
        )

    budget = ContextBudget(
        max_prompt_chars=sum(map(len, markers.values())),
        max_extra_context_files=0,
    )
    contexts = collect_file_contexts(repo, changed_files, context_budget=budget)

    assert sum(context.chars_read for context in contexts) <= budget.max_prompt_chars  # 全局预算硬约束
    assert {context.path: context.content for context in contexts} == markers  # 每文件保留各自变更行


def test_collect_file_contexts_applies_remaining_budget_to_extra_files(tmp_path):
    """验证额外文件使用剩余预算：变更文件占用后，剩余预算分配给 import/call 候选。

    测试目的：
        预算优先分配给变更文件，剩余部分才用于额外上下文文件（import /
        call_name 候选）。当额外文件自身超过剩余预算时，应被截断而非丢弃。

    测试场景：
        app.py（变更文件）调用 helper()，helper.py 作为 call_name 候选。
        max_prompt_chars=20（容纳 app.py 后所剩无几），max_extra_context_files=1。

    预期结果：
        - 收集顺序为 [app.py, helper.py]（变更文件在前，候选在后）；
        - 总 chars_read ≤ 20；
        - helper.py 的 truncated 为 ``True``（被剩余预算截断）。

    设计理由：
        验证「变更文件 > 额外候选」的预算优先级，以及额外文件超出预算时
        的截断而非丢弃策略。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("helper()\n", encoding="utf-8")
    (repo / "helper.py").write_text(
        "def helper():\n    return 'long context'\n",
        encoding="utf-8",
    )
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[DiffLine("app.py", 1, "helper()")],
        deleted_lines=[],
        patch="",
    )
    budget = ContextBudget(max_prompt_chars=20, max_extra_context_files=1)

    contexts = collect_file_contexts(repo, [changed_file], context_budget=budget)

    assert [context.path for context in contexts] == ["app.py", "helper.py"]  # 顺序：变更文件 → 候选
    assert sum(context.chars_read for context in contexts) <= budget.max_prompt_chars  # 总预算不超
    assert contexts[1].truncated is True  # helper.py 被剩余预算截断而非丢弃


def test_collect_file_contexts_records_provenance_for_each_selection_path(tmp_path):
    """验证 provenance 记录：三种来源路径各自标注正确的 source / selection_reason。

    测试目的：
        ``collect_file_contexts`` 通过三条路径选文件——变更文件、import 候选、
        call_name 候选，每条路径产出的 FileContext 必须记录对应的 provenance，
        供下游解释「为何选中此文件」。

    测试场景：
        app.py 含 ``from helper import imported_value``（触发 import 候选
        helper.py）和 ``call_target()``（触发 call_name 候选 services.py）。
        max_extra_context_files=2 容许两个候选都被收集。

    预期结果：
        - app.py → ("changed_file", "file is changed in the pull request")；
        - helper.py → ("import_candidate", "imported by selected context app.py")；
        - services.py → ("call_name_candidate", "defines added call name(s): call_target")。

    设计理由：
        provenance 是可解释性的核心——LLM 与人工审查都能据此判断上下文
        选取是否合理，也便于回归测试断言来源链。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "from helper import imported_value\ncall_target()\n",
        encoding="utf-8",
    )
    (repo / "helper.py").write_text("imported_value = 1\n", encoding="utf-8")
    (repo / "services.py").write_text(
        "def call_target():\n    return True\n",
        encoding="utf-8",
    )
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[
            DiffLine("app.py", 1, "from helper import imported_value"),
            DiffLine("app.py", 2, "call_target()"),
        ],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=2),
    )

    provenance = {
        context.path: (context.source, context.selection_reason)
        for context in contexts
    }
    assert provenance == {
        "app.py": ("changed_file", "file is changed in the pull request"),  # 变更文件来源
        "helper.py": ("import_candidate", "imported by selected context app.py"),  # import 候选来源
        "services.py": ("call_name_candidate", "defines added call name(s): call_target"),  # call_name 候选来源
    }


def test_collect_file_contexts_keeps_the_first_provenance_for_a_duplicate_candidate(tmp_path):
    """验证重复候选保留首个 provenance：同一文件被多条路径选中时只保留首次来源。

    测试目的：
        当一个文件同时满足 import 候选和 call_name 候选条件时，不应重复
        收集，也不应覆盖首次的 provenance。本用例验证去重 + 首来源优先。

    测试场景：
        app.py 既 ``from helper import helper`` 又 ``helper()``，使 helper.py
        同时成为 import 候选与 call_name 候选。max_extra_context_files=1
        只容许收集一个候选。

    预期结果：
        - contexts 仅含 [app.py, helper.py]（helper.py 不重复）；
        - helper.py 的 source 为 "import_candidate"（import 先于 call_name
          被判定，首来源保留）。

    设计理由：
        防止重复上下文稀释预算，同时保留「最先发现」的来源信息以维持
        可解释性稳定。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "from helper import helper\nhelper()\n",
        encoding="utf-8",
    )
    (repo / "helper.py").write_text(
        "def helper():\n    return True\n",
        encoding="utf-8",
    )
    changed_file = ChangedFile(
        path="app.py",
        added_lines=[
            DiffLine("app.py", 1, "from helper import helper"),
            DiffLine("app.py", 2, "helper()"),
        ],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=1),
    )

    assert [context.path for context in contexts] == ["app.py", "helper.py"]  # helper.py 去重，不重复出现
    assert contexts[1].source == "import_candidate"  # 首来源（import）保留，未被 call_name 覆盖
    assert contexts[1].selection_reason == "imported by selected context app.py"  # 首来源理由保留


def test_collect_file_contexts_keeps_provenance_when_selected_file_cannot_be_read(tmp_path):
    """验证缺失文件保留 provenance：变更文件不存在时仍保留来源并标注错误。

    测试目的：
        当 PR 引用了一个仓库中不存在的文件（如新建未提交、或 diff 与仓库
        不同步），``collect_file_contexts`` 不应崩溃，应保留 provenance 并
        在 error 中标注 "does not exist"，供下游与用户排查。

    测试场景：
        构造一个 missing.py 的 ChangedFile（无 added_lines），但仓库中
        不存在该文件。

    预期结果：
        - 返回 1 条 context，exists 为 ``False``；
        - source / selection_reason 与正常变更文件一致；
        - error 含 "does not exist"。

    设计理由：
        缺失文件是真实 PR 的常见情况，provenance 保留保证 Agent 不会
        因单文件缺失而中断整次审查。
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    changed_file = ChangedFile(
        path="missing.py",
        added_lines=[],
        deleted_lines=[],
        patch="",
    )

    contexts = collect_file_contexts(
        repo,
        [changed_file],
        context_budget=ContextBudget(max_prompt_chars=4000, max_extra_context_files=0),
    )

    assert len(contexts) == 1  # 缺失文件仍产出 1 条 context（不崩溃）
    context = contexts[0]
    assert context.exists is False  # 文件不存在
    assert context.source == "changed_file"  # provenance 保留
    assert context.selection_reason == "file is changed in the pull request"  # 选择理由保留
    assert "does not exist" in context.error  # error 明确标注缺失原因


def test_context_budget_rejects_invalid_limits():
    """验证 ContextBudget 输入校验：非法的预算上限必须被拒绝并抛 ValueError。

    测试目的：
        ``ContextBudget`` 在构造时校验 max_prompt_chars 与 max_extra_context_files
        的合法性，防止下游因 0 或负值预算产生空上下文 / 异常行为。

    测试场景：
        - max_prompt_chars=0：应抛 ValueError 且 match "max_prompt_chars"；
        - max_extra_context_files=-1：应抛 ValueError 且 match "max_extra_context_files"。

    预期结果：
        两处构造都抛出 ValueError，且错误信息能定位到对应字段。

    设计理由：
        fail-fast——在配置入口拦截非法值，优于让错误传播到深层逻辑。
    """
    with pytest.raises(ValueError, match="max_prompt_chars"):  # 0 字符预算非法
        ContextBudget(max_prompt_chars=0)

    with pytest.raises(ValueError, match="max_extra_context_files"):  # 负数候选数非法
        ContextBudget(max_extra_context_files=-1)


def test_locate_python_symbol_returns_containing_function():
    """验证符号定位-函数：行号落在顶层函数内时返回该函数的完整信息。

    测试目的：
        ``locate_python_symbol`` 应返回包含指定行的最内层符号。本用例验证
        行 2 落在顶层函数 ``calculate_total`` 内时，返回函数名、kind、
        qualified_name、class_name、行范围与 source 文本均正确。

    测试场景：
        源码含一个顶层函数 calculate_total（行 1-3）和一个无关类 Ignored。
        查询行 2。

    预期结果：
        - name == "calculate_total"，kind == "function"；
        - qualified_name == "calculate_total"（顶层函数无类前缀）；
        - class_name is None（不在类内）；
        - 行范围 (1, 3)，source 为函数完整文本。

    设计理由：
        作为符号定位的基础用例，验证 AST 解析与最内层选择逻辑。
    """
    source = """def calculate_total(values):
    total = sum(values)
    return total


class Ignored:
    pass
"""

    symbol = locate_python_symbol(source, 2)

    assert symbol is not None  # 行 2 位于函数内，应成功定位
    assert symbol.name == "calculate_total"  # 函数名正确
    assert symbol.kind == "function"  # 类型为 function（非 method）
    assert symbol.qualified_name == "calculate_total"  # 顶层函数全名无类前缀
    assert symbol.class_name is None  # 不属于任何类
    assert (symbol.start_line, symbol.end_line) == (1, 3)  # 行范围覆盖整个函数
    assert symbol.source == "def calculate_total(values):\n    total = sum(values)\n    return total\n"  # source 文本完整


def test_locate_python_symbol_distinguishes_method_and_class():
    """验证符号定位-方法 vs 类：方法行返回 method，类体行返回 class。

    测试目的：
        当行号落在类的方法内时，应返回 method（带 class_name 与
        qualified_name 含类前缀）；当行号落在类的非方法行（如类属性）时，
        应返回 class 本身。本用例同时验证两种情况。

    测试场景：
        类 Processor 含类属性 setting（行 2）与方法 handle（行 4-5）。
        分别查询行 5（方法内）与行 2（类属性行）。

    预期结果：
        - method：name=="handle"、kind=="method"、
          qualified_name=="Processor.handle"、class_name=="Processor"、
          行范围 (4,5)、source 为方法文本；
        - containing_class：name=="Processor"、kind=="class"、行范围 (1,5)。

    设计理由：
        验证「最内层」选择——方法行优先返回方法而非外层类，但类信息通过
        class_name / qualified_name 保留，信息不丢失。
    """
    source = """class Processor:
    setting = "safe"

    def handle(self, value):
        return value + 1
"""

    method = locate_python_symbol(source, 5)
    containing_class = locate_python_symbol(source, 2)

    assert method is not None  # 行 5 在方法内
    assert method.name == "handle"  # 方法名
    assert method.kind == "method"  # 类型为 method
    assert method.qualified_name == "Processor.handle"  # 全名含类前缀
    assert method.class_name == "Processor"  # 所属类名
    assert (method.start_line, method.end_line) == (4, 5)  # 方法行范围
    assert method.source == "    def handle(self, value):\n        return value + 1\n"  # 方法 source（含缩进）

    assert containing_class is not None  # 行 2 不在任何方法内，回退到类
    assert containing_class.name == "Processor"  # 类名
    assert containing_class.kind == "class"  # 类型为 class
    assert (containing_class.start_line, containing_class.end_line) == (1, 5)  # 类行范围


def test_locate_python_symbol_returns_none_for_unlocatable_source_or_line():
    """验证符号定位-不可定位：语法错误 / 模块级语句 / 非法行号均返回 None。

    测试目的：
        ``locate_python_symbol`` 在无法定位时必须返回 ``None`` 而非抛异常，
        保证调用方可以优雅回退到文件级上下文。本用例覆盖三种不可定位场景。

    测试场景：
        - 语法错误的源码（``def broken(:``）；
        - 模块级赋值（``value = 1``，行 1 不在任何符号内）；
        - 合法源码但行号为 0（非法 1-based 行号）。

    预期结果：
        三种情况均返回 ``None``。

    设计理由：
        健壮性兜底——AST 解析失败或行号越界是常态，绝不能让符号定位
        打断整个上下文检索流程。
    """
    assert locate_python_symbol("def broken(:\n", 1) is None  # 语法错误：ast.parse 失败，返回 None
    assert locate_python_symbol("value = 1\n", 1) is None  # 模块级语句：不在任何函数/类内
    assert locate_python_symbol("def valid():\n    return 1\n", 0) is None  # 非法行号 0：直接返回 None
