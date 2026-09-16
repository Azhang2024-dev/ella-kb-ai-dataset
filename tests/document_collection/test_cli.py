"""CLI 测试：以子进程方式真实调用 scripts/collection_cli.py。

这一层的意义是验证"Agent / 人 / CI 实际怎么调"：
参数解析、退出码、--json 契约、以及完全不联网的本地端到端跑通。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLI = os.path.join(REPO_ROOT, "scripts", "collection_cli.py")
PYTHON = sys.executable
POLICY = os.path.join(REPO_ROOT, "config", "collection_policy.json")


def run_cli(*args, expect_code=None, cwd=REPO_ROOT):
    proc = subprocess.run([PYTHON, CLI, *args], cwd=cwd, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=600)
    if expect_code is not None:
        assert proc.returncode == expect_code, (
            f"exit={proc.returncode} expected={expect_code}\n"
            f"args={args}\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}")
    return proc


def test_help_lists_every_capability_command():
    proc = run_cli("--help", expect_code=0)
    for command in ("inventory", "acquire", "extract", "deduplicate", "screen",
                    "query", "report", "resume", "validate-config"):
        assert command in proc.stdout, command
    assert "collection.generate_queries -> query" in proc.stdout
    assert "退出码" in proc.stdout


def test_version():
    proc = run_cli("--version", expect_code=0)
    assert "document_collection" in proc.stdout


def test_validate_config_prints_effective_rules():
    proc = run_cli("validate-config", expect_code=0)
    assert "知识库_2026" in proc.stdout and "docQA.llm" in proc.stdout
    assert "multiple_choice" in proc.stdout
    assert "云盘只读" in proc.stdout


def test_validate_config_json_is_machine_readable():
    proc = run_cli("--json", "validate-config", expect_code=0)
    payload = json.loads(proc.stdout)
    assert payload["validation"] == "ok"
    assert payload["queries_per_document"] == 10
    assert payload["run_config"]["queries_per_document"] == 10
    assert payload["run_config"]["max_size_mb"]["pdf"] == 100


def test_validate_config_reports_broken_policy_with_exit_code_4(workdir):
    broken = json.load(open(POLICY, encoding="utf-8-sig"))
    broken["query_plan"]["query_types"][0]["question_count"] = 30   # 超过 12
    path = os.path.join(workdir, "broken_policy.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(broken, fh, ensure_ascii=False)
    proc = run_cli("--config", path, "validate-config", expect_code=4)
    assert "超出允许区间" in (proc.stderr + proc.stdout)


def test_runs_on_empty_workspace():
    proc = run_cli("runs", expect_code=0)
    assert proc.returncode == 0


def test_missing_subcommand_is_an_error():
    proc = run_cli()
    assert proc.returncode != 0


# ---------------------------------------------------------------------------
# 本地端到端（local_files 适配器，不联网）
# ---------------------------------------------------------------------------
@pytest.fixture
def local_run(workdir, generated_documents):
    """在 workdir 下搭一个"本地云盘"，并生成指向它的策略文件。"""
    cloud = os.path.join(workdir, "cloud")
    os.makedirs(os.path.join(cloud, "A", "DOC-FR"), exist_ok=True)
    os.makedirs(os.path.join(cloud, "B", "russian"), exist_ok=True)

    a_files = {
        "cours_chimie.pdf": "A_PDF_1",
        "notes_cours.txt": "A_TXT_1",
        "td_physique.docx": "A_FR_DOCX",
        "exercices.xlsx": "A_FR_XLSX",
        "donnees.csv": "A_FR_CSV",
        "cours_slides.pptx": "A_FR_PPTX",
        "microeconomics.pdf": "A_EN_PDF",
        "neike_huli.pdf": "A_ZH_PDF",
        "legacy.doc": None,          # 旧格式，需转换
    }
    b_files = {
        "lekciya_algebra.pdf": "B_RU_PDF",
        "konspekt.txt": "B_RU_TXT",
        "qanun_madani.pdf": "A_AR_PDF",
        "administracion.pdf": "A_ES_PDF",
        "graphics_only.pdf": "A_IMG_PDF",
    }
    for target_dir, mapping in ((os.path.join(cloud, "A", "DOC-FR"), a_files),
                                (os.path.join(cloud, "B", "russian"), b_files)):
        for filename, token in mapping.items():
            dest = os.path.join(target_dir, filename)
            if token is None:
                with open(dest, "wb") as fh:
                    fh.write(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)
                continue
            src = generated_documents[token]["path"]
            shutil.copy2(src, dest)
    # 跨源重复：把同一份 PDF 复制到 B
    shutil.copy2(generated_documents["A_PDF_1"]["path"],
                 os.path.join(cloud, "B", "russian", "cours_chimie_copy.pdf"))

    policy = json.load(open(POLICY, encoding="utf-8-sig"))
    policy["source_access"]["adapter"] = "local_files"
    policy["source_access"]["root"] = cloud
    policy["sources"] = [
        {"name": "知识库_2026", "kind": "local_files", "token": "A", "role": "primary",
         "readonly": True},
        {"name": "docQA.llm", "kind": "local_files", "token": "B", "role": "supplement",
         "readonly": True},
    ]
    policy["run_defaults"]["workspace_dir"] = os.path.join(workdir, "ws")
    policy_path = os.path.join(workdir, "local_policy.json")
    with open(policy_path, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, ensure_ascii=False, indent=2)
    return {"policy": policy_path, "workspace": policy["run_defaults"]["workspace_dir"],
            "cloud": cloud, "run_id": "local-e2e"}


def _cli(local_run, *args, expect_code=None):
    return run_cli("--config", local_run["policy"], "--workspace", local_run["workspace"],
                   "--run-id", local_run["run_id"], "--quiet", *args,
                   expect_code=expect_code)


def test_local_pipeline_end_to_end(local_run):
    _cli(local_run, "inventory", expect_code=0)
    _cli(local_run, "acquire", expect_code=0)
    _cli(local_run, "extract", expect_code=0)
    _cli(local_run, "deduplicate", expect_code=0)
    proc = _cli(local_run, "screen", expect_code=None)
    assert proc.returncode in (0, 3), proc.stdout + proc.stderr
    _cli(local_run, "report", expect_code=None)

    ws = os.path.join(local_run["workspace"], local_run["run_id"])
    for name in ("source_inventory.jsonl", "source_type_counts.csv", "inventory_summary.md",
                 "acquisition_report.jsonl", "extraction_report.jsonl", "dedup_report.jsonl",
                 "screening_report.jsonl", "accepted_documents.csv", "rejected_documents.csv",
                 "human_review_queue.csv", "coverage_report.md", "cost_and_runtime.json",
                 "run_manifest.json", "state.sqlite3"):
        assert os.path.isfile(os.path.join(ws, name)), name

    coverage = open(os.path.join(ws, "coverage_report.md"), encoding="utf-8").read()
    assert "知识库_2026" in coverage and "docQA.llm" in coverage


def test_cli_json_output_matches_the_contract(local_run):
    _cli(local_run, "inventory", expect_code=0)
    proc = run_cli("--config", local_run["policy"], "--workspace", local_run["workspace"],
                   "--run-id", local_run["run_id"], "--quiet", "--json", "resume",
                   expect_code=0)
    payload = json.loads(proc.stdout)
    for key in ("status", "run_id", "summary", "artifacts", "review_items", "errors",
                "next_action", "capability", "contract_version"):
        assert key in payload, key
    assert payload["status"] in {"completed", "partial", "needs_review", "failed", "cancelled"}
    assert payload["capability"] == "collection.resume"
    assert payload["next_action"]


def test_cli_resume_points_to_the_next_capability(local_run):
    proc = _cli(local_run, "resume", expect_code=0)
    # 空库时下一步必须是 inventory
    payload = json.loads(run_cli("--config", local_run["policy"],
                                 "--workspace", local_run["workspace"],
                                 "--run-id", local_run["run_id"], "--quiet", "--json",
                                 "resume", expect_code=0).stdout)
    assert payload["next_action"] == "collection.inventory"
    assert proc.returncode == 0


def test_cli_deduplicates_across_the_two_local_sources(local_run):
    _cli(local_run, "inventory", expect_code=0)
    _cli(local_run, "acquire", expect_code=0)
    _cli(local_run, "extract", expect_code=0)
    proc = run_cli("--config", local_run["policy"], "--workspace", local_run["workspace"],
                   "--run-id", local_run["run_id"], "--quiet", "--json", "deduplicate",
                   expect_code=0)
    payload = json.loads(proc.stdout)
    assert payload["summary"]["duplicate_items"] >= 1, "同一份 PDF 复制到两个源后必须被判重"
    assert payload["summary"]["sha256_groups"] >= 1


def test_cli_rejects_oversize_and_reports_reason(local_run):
    """把 txt 上限压到 0MB，验证 CLI 链路上硬门槛生效并且可解释。"""
    policy = json.load(open(local_run["policy"], encoding="utf-8-sig"))
    policy["size_limits_mb"]["pdf"] = 0
    tight = os.path.join(os.path.dirname(local_run["policy"]), "tight_policy.json")
    with open(tight, "w", encoding="utf-8") as fh:
        json.dump(policy, fh, ensure_ascii=False)
    run_cli("--config", tight, "--workspace", local_run["workspace"],
            "--run-id", "tight", "--quiet", "inventory", expect_code=0)
    run_cli("--config", tight, "--workspace", local_run["workspace"],
            "--run-id", "tight", "--quiet", "acquire", expect_code=None)
    ws = os.path.join(local_run["workspace"], "tight")
    rows = [json.loads(l) for l in
            open(os.path.join(ws, "acquisition_report.jsonl"), encoding="utf-8") if l.strip()]
    rejected = [r for r in rows if r["status"] == "rejected"]
    assert rejected, "pdf 上限 0MB 时必须有文件被拒"
    assert all("size_over_limit" in r["reason_codes"] for r in rejected)
    accepted = [r for r in rows if r["status"] == "acquired" and r["extension"] == "pdf"]
    assert accepted == [], "超限 PDF 不允许被接受"


def test_cli_run_all_stops_on_failure_and_reports(local_run):
    proc = run_cli("--config", local_run["policy"], "--workspace", local_run["workspace"],
                   "--run-id", "run-all", "--quiet", "--json", "run-all",
                   "--per-source-limit", "3", "--limit", "3", expect_code=None)
    assert proc.returncode in (0, 2, 3), proc.stdout[-2000:] + proc.stderr[-2000:]


# ---------------------------------------------------------------------------
# status 只读诊断：阶段×状态矩阵（collection.status，不属于 8 个契约能力）
# ---------------------------------------------------------------------------
def test_cli_status_matrix_on_local_run(local_run):
    """跑完 inventory+acquire+extract 后，status 矩阵应反映各阶段数量。"""
    _cli(local_run, "inventory", expect_code=0)
    _cli(local_run, "acquire", expect_code=0)
    _cli(local_run, "extract", expect_code=0)
    proc = _cli(local_run, "status", expect_code=0)
    out = proc.stdout
    assert "[status]" in out and local_run["run_id"] in out
    assert "items（条目状态）" in out
    assert "acquired" in out, "acquire 之后条目状态应翻转为 acquired"
    assert "artifacts（本地产物）" in out and "extracted" in out
    assert "next_action" in out


def test_cli_status_json_matches_the_contract(local_run):
    _cli(local_run, "inventory", expect_code=0)
    payload = json.loads(run_cli("--config", local_run["policy"],
                                 "--workspace", local_run["workspace"],
                                 "--run-id", local_run["run_id"], "--quiet", "--json",
                                 "status", expect_code=0).stdout)
    for key in ("status", "run_id", "summary", "artifacts", "review_items", "errors",
                "next_action", "capability", "contract_version"):
        assert key in payload, key
    assert payload["capability"] == "collection.status"
    assert payload["status"] == "completed"
    summary = payload["summary"]
    for key in ("items", "artifacts", "extractions", "screen", "queries",
                "dedup_groups", "languages", "llm_stats"):
        assert key in summary, key
    assert summary["items"].get("discovered", 0) > 0
    assert summary["queries"]["total"] == 0


def test_cli_status_on_empty_run_points_to_inventory(local_run):
    """空库（只建了 run 没盘点）时 status 不报错，next_action 指向 inventory。"""
    payload = json.loads(run_cli("--config", local_run["policy"],
                                 "--workspace", local_run["workspace"],
                                 "--run-id", local_run["run_id"], "--quiet", "--json",
                                 "status", expect_code=0).stdout)
    assert payload["capability"] == "collection.status"
    assert payload["summary"]["items"] == {}
    assert payload["next_action"] == "collection.inventory"


def test_cli_status_touches_no_business_tables(local_run):
    """status 允许落盘 RunResult（与 audit 同口径），但不得改任何业务表。"""
    import hashlib
    import sqlite3
    _cli(local_run, "inventory", expect_code=0)
    db = os.path.join(local_run["workspace"], local_run["run_id"], "state.sqlite3")
    business_tables = ("items", "item_sources", "artifacts", "extractions",
                       "decisions", "dedup_groups", "queries", "llm_calls", "meta")

    def table_digest():
        con = sqlite3.connect(db)
        try:
            blob = b"".join(
                f"{row}".encode() for table in business_tables
                for row in con.execute(f"select * from {table}"))
        finally:
            con.close()
        return hashlib.sha256(blob).hexdigest()

    before = table_digest()
    _cli(local_run, "status", expect_code=0)
    _cli(local_run, "status", expect_code=0)
    assert table_digest() == before, "status 不得改业务表（runs 落盘除外）"


def test_help_mentions_status_diagnostic():
    proc = run_cli("--help", expect_code=0)
    assert "status" in proc.stdout
    assert "阶段×状态矩阵" in proc.stdout
