"""发行版一致性测试：skill 包 + slash 命令作为可发行产品的最低质量门。

验证对象是仓库里的 `skills/` 与 `.zcode/commands/`（ZCode 可选增强层）：
1. 每个能力域 skill 的 frontmatter 合法、引用的 CLI 子命令真实存在；
2. 能力域 skill 不复制业务数字（规则唯一事实来源是 config/collection_policy.json）；
3. 全部 runtime.json 同值（5 个 skill 目录指向同一仓库根）；
4. slash 命令文件可解析、frontmatter 引用的 skill 真实存在、无动态 shell 块。

架构口径（2026-09-13 修订）：skill 按**能力域**切，不按流水线步骤切。
官方 skill 指南要求"一个 skill = 一个连贯工作单元"；按步骤切（原 9 个 collect-*）
会让一个任务要加载多个 skill、描述互相重叠，是"scoped too narrowly"。
"""

from __future__ import annotations

import json
import os
import re

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SKILLS_ROOT = os.path.join(REPO_ROOT, "skills")
COMMANDS_ROOT = os.path.join(REPO_ROOT, ".zcode", "commands", "collect")

# 4 个能力域 skill（每个收敛该域全部命令）+ 1 个薄编排
DOMAIN_SKILLS = ("ella-collect", "ella-screen", "ella-generate", "ella-report")
ORCHESTRATOR = "document_collection"

# 编排 SKILL.md 能力对照表里出现的全部 CLI 子命令（诊断含 status）
VALID_CLI_COMMANDS = {
    "inventory", "acquire", "extract", "deduplicate", "screen", "query", "questions",
    "report", "resume", "status", "explain", "audit", "runs", "validate-config",
    "ingest", "reconcile",
}


def _frontmatter(text: str) -> dict:
    if not text.startswith("---\n"):
        return {}
    end = text.index("\n---", 4)
    meta = {}
    for line in text[4:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
    return meta


# ---------------------------------------------------------------------------
# 能力域 skill
# ---------------------------------------------------------------------------
def test_domain_skills_exist_with_valid_frontmatter():
    for name in DOMAIN_SKILLS:
        path = os.path.join(SKILLS_ROOT, name, "SKILL.md")
        assert os.path.isfile(path), path
        meta = _frontmatter(open(path, encoding="utf-8").read())
        assert meta.get("name") == name, name
        assert len(meta.get("description", "")) > 60, name
        # 防误触发：能力域 skill 必须指回编排 skill
        assert "document_collection" in meta["description"], name


def _extract_cli_subcommands(text: str) -> set:
    """从 `$CLI ...` 行里提取真正的子命令 token，跳过全局 flag 及其取值。

    例：`$CLI --run-id batch-1 --concurrency 8 extract --dir x`
        -> 去掉 flag 与其值后，第一个裸 token 是 `extract`。
    """
    # 取值型全局 flag（后面跟一个值）；--json/--quiet 是开关，不取值
    value_flags = {"--config", "--workspace", "--run-id", "--replay-dir",
                   "--record-dir", "--concurrency"}
    switch_flags = {"--json", "--quiet"}
    found: set = set()
    for line in text.splitlines():
        if "$CLI" not in line:
            continue
        tokens = line.split("$CLI", 1)[1].split()
        idx = 0
        subcommand = None
        while idx < len(tokens):
            tok = tokens[idx]
            if tok in switch_flags:
                idx += 1
                continue
            if tok in value_flags or (tok.startswith("--") and "=" not in tok
                                      and tok not in switch_flags):
                idx += 2      # flag + value
                continue
            if tok.startswith("--"):
                idx += 1
                continue
            if tok.startswith("[") or tok.startswith("'"):
                break         # 占位符 / 引号里的路径，不属于命令
            subcommand = re.sub(r"[^a-z-].*$", "", tok.strip("`（）()、,，。"))
            break
        if subcommand:
            found.add(subcommand)
    return found


def test_domain_skills_reference_real_cli_commands():
    for name in DOMAIN_SKILLS:
        text = open(os.path.join(SKILLS_ROOT, name, "SKILL.md"), encoding="utf-8").read()
        referenced = _extract_cli_subcommands(text)
        bad = referenced - VALID_CLI_COMMANDS
        assert not bad, f"{name} 引用了不存在的子命令: {bad}"


def test_domain_skills_do_not_duplicate_policy_numbers():
    """业务数字只在 policy 与编排 SKILL.md §9 速查里，能力域 skill 一律指路不复制。"""
    banned_fragments = (
        "≥ 20", "至少 20", "compatibility_only=true", "题量 5", "3:2:2:1:1:1",
        "100 MB", "20 MB", "sample-seed 20260909",
    )
    for name in DOMAIN_SKILLS:
        text = open(os.path.join(SKILLS_ROOT, name, "SKILL.md"), encoding="utf-8").read()
        for fragment in banned_fragments:
            assert fragment not in text, f"{name} 复制了业务数字: {fragment}"


def test_domain_skills_defer_to_orchestrator_stop_rules():
    for name in DOMAIN_SKILLS:
        text = open(os.path.join(SKILLS_ROOT, name, "SKILL.md"), encoding="utf-8").read()
        assert "停止" in text, name
        assert "编排" in text, name


def test_domain_skills_cover_every_capability_command():
    """四个域合起来必须覆盖所有非诊断 CLI 子命令，不能有命令没有归属。"""
    covered: set[str] = set()
    for name in DOMAIN_SKILLS:
        text = open(os.path.join(SKILLS_ROOT, name, "SKILL.md"), encoding="utf-8").read()
        covered |= set(re.findall(
            r"\$CLI[^\n]*?(ingest|inventory|acquire|extract|deduplicate|screen|query|questions|report)\b",
            text))
    expected = {"ingest", "inventory", "acquire", "extract", "deduplicate", "screen",
                "query", "questions", "report"}
    assert expected <= covered, f"以下命令没有归属任何能力域 skill: {expected - covered}"


# ---------------------------------------------------------------------------
# runtime.json：5 份同值，发行模板同构
# ---------------------------------------------------------------------------
def test_all_runtime_json_files_are_valid_and_identical():
    values = {}
    for name in (ORCHESTRATOR, *DOMAIN_SKILLS):
        path = os.path.join(SKILLS_ROOT, name, "runtime.json")
        if not os.path.isfile(path):
            continue  # 本机未装（runtime.json 不入库）时跳过；模板测试单独覆盖
        values[name] = json.load(open(path, encoding="utf-8"))["repo"]
    assert values, "仓库开发机上应至少存在一份 runtime.json"
    unique = set(values.values())
    assert len(unique) == 1, f"runtime.json 值漂移: {values}"


def test_runtime_example_template_shipped_and_is_placeholder():
    path = os.path.join(SKILLS_ROOT, ORCHESTRATOR, "runtime.example.json")
    assert os.path.isfile(path), path
    data = json.load(open(path, encoding="utf-8"))
    assert "repo" in data
    assert "<" in data["repo"] and ">" in data["repo"], "模板必须是占位符，不能是真实路径"


def test_gitignore_excludes_runtime_json_but_ships_example():
    gitignore = open(os.path.join(REPO_ROOT, ".gitignore"), encoding="utf-8").read()
    assert "runtime.json" in gitignore


def test_real_runtime_json_is_not_tracked_by_git():
    """真实 runtime.json 记录本机绝对路径，绝不能入库（只入 runtime.example.json）。"""
    import subprocess

    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--", "skills/**/runtime.json"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        import pytest
        pytest.skip("git 不可用")
    if tracked.returncode != 0:
        import pytest
        pytest.skip("非 git 工作区（如发行副本）")

    leaked = [p for p in tracked.stdout.splitlines() if p.endswith("runtime.json")]
    assert not leaked, f"真实 runtime.json 被误入库: {leaked}（应用 git rm --cached 移除）"


# ---------------------------------------------------------------------------
# slash 命令（ZCode 可选增强层）
# ---------------------------------------------------------------------------
def _command_files() -> dict:
    files = {}
    if not os.path.isdir(COMMANDS_ROOT):
        return files
    for name in sorted(os.listdir(COMMANDS_ROOT)):
        if name.endswith(".md"):
            files[name[:-3]] = open(os.path.join(COMMANDS_ROOT, name),
                                    encoding="utf-8").read()
    return files


def test_collect_commands_exist_and_parse():
    files = _command_files()
    expected = {"status", "ingest", "inventory", "acquire", "extract", "dedup",
                "screen", "query", "report", "audit", "pipeline"}
    assert expected <= set(files), f"缺命令: {expected - set(files)}"
    for name, text in files.items():
        meta = _frontmatter(text)
        assert meta.get("description"), f"{name} 缺 description"
        assert "$ARGUMENTS" in text, f"{name} 缺 $ARGUMENTS 占位"


def test_collect_commands_reference_real_skills():
    shipped = set(DOMAIN_SKILLS) | {ORCHESTRATOR}
    for name, text in _command_files().items():
        meta = _frontmatter(text)
        ref = meta.get("skills", "")
        assert ref in shipped, f"{name} 引用了不存在的 skill: {ref}"


def test_collect_commands_have_no_dynamic_shell_blocks():
    """ZCode 会拒载含 !`cmd` 动态块的命令文件，这里提前拦住。"""
    for name, text in _command_files().items():
        assert not re.search(r"!\s*`", text), f"{name} 含动态 shell 块"


def test_pipeline_command_uses_semi_auto_mode():
    text = _command_files().get("pipeline", "")
    assert "半自动" in text and "gate" in text.lower()
    assert "document_collection" in _frontmatter(text).get("skills", "")

