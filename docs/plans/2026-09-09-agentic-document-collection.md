# Agentic Document Collection and Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. Each task must finish with its own verification checkpoint before the next task starts.

**Goal:** Build a platform-neutral, agent-first document collection and screening system that can be called through CLI and MCP, can later be wrapped as Skills or host-specific Plugins, and can process the two authorized Feishu Drive sources without changing the source files.

**Architecture:** Use a thin, runtime-neutral Agent Harness over typed capability tools. The core Python package owns deterministic operations, durable state, evidence, and verification; the Agent chooses the next capability, handles ambiguity, and drives the loop. MCP is the cross-platform interface; Skills describe agent behavior; Plugins are optional host-specific packaging only.

**Tech Stack:** Python 3.12; standard library `asyncio`, `sqlite3`, `dataclasses`, `argparse`, `pathlib`, `hashlib`, `json`; existing `PyMuPDF`, `pdfplumber`, `python-docx`, `openpyxl`, `pandas`, `litellm`; `python-pptx` for PPTX; official MCP Python SDK v2 in an isolated/optional dependency group; PySide6 only as a later UI adapter; pytest and pytest-asyncio for tests.

**Spec:** Current Feishu collection plan `REDACTED_DOC_039`, revision `198`; primary requirement `REDACTED_DOC_074`, revision `428`; multi-format requirement `OtdnwZwPaiOhHfk43mYcpuAOndg`, revision `1140`. The plan must record these revisions and hashes in every run snapshot; it must not silently follow a newer live document during an active run.

## Global Constraints

- The source folders are `知识库_2026` and `docQA.llm`; source files must remain unchanged.
- The two source trees must be inventoried and deduplicated together, not counted independently as final documents.
- P0 languages are English, Russian, French, Chinese, Arabic, and Spanish; each requires at least 20 final documents under the current plan.
- P1 subjects are Law, Education/Linguistics, Clinical Medicine/Nursing, and Basic Science; each requires at least one final document under the current plan.
- The current plan requires 10 queries per final document; each query requests fewer than 12 questions; query text must target the whole document and must not name a fragment, chapter, page, Sheet, or table.
- The current plan treats file-size limits as hard gates: PDF/DOC/DOCX/PPTX up to 100 MB; XLSX/CSV/TXT/MD up to 20 MB; online resources remain `pending_size_verification` until their exported or authoritative size is known.
- Documents over 50,000 characters are not automatically rejected; they require a full-understanding path and coverage verification.
- Image-understanding is not the batch default; graphic-dominant documents are excluded, while text, tables, and formulas may be retained.
- Hard rules and soft targets must be separate. Format ratio is a soft target and must never force low-quality documents into the final set.
- No LLM output can override a deterministic hard rejection without an explicit human decision recorded in the ledger.
- All external side effects are deny-by-default. The collection system may read/list/download/export and write local artifacts; it must not delete, move, rename, or modify cloud sources.
- All long-running operations must be resumable, idempotent, and auditable.
- Existing crawler/query flows must not be changed in the first implementation slice. New document collection code must live in a separate package and use adapters around existing capabilities where safe.
- The current working tree of `D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集` is dirty. Implementation must begin from an explicit branch/worktree snapshot and must not mix unrelated pre-existing edits into this feature.

---

## 1. Scope, boundaries, and intended product

### 1.1 In scope

- Requirement snapshot and requirement-to-policy compilation.
- Feishu Drive recursive inventory for the two authorized folders.
- Local acquisition/export with retry and checkpointing.
- Format detection and document extraction.
- Page/section/sheet/semantic-unit locators.
- Exact and normalized-text deduplication plus near-duplicate review clusters.
- Deterministic hard filtering and configurable soft ranking.
- Agent-assisted semantic classification and exception handling.
- Whole-document query generation and query-level evidence records.
- MCP Server exposure and one-host Agent smoke tests.
- Generic orchestration loop after individual capabilities pass acceptance.
- Reports, audit ledger, replay fixtures, and operator documentation.

### 1.2 Explicitly out of scope for the first release

- Writing back to the Feishu folders.
- Automatically accepting unverified business-rule changes from a live document.
- Fully autonomous modification of source code, prompts, or production policies.
- A large multi-agent swarm.
- Full-time cloud orchestration, Airflow, Temporal, or Kubernetes deployment.
- A polished PySide6 interface before the CLI/MCP path has passed the pilot.
- Automatic OCR of every scanned PDF.
- Treating an LLM judge as the only acceptance mechanism.

### 1.3 Product layering

```text
Host Agent (OpenClaw / Codex / another MCP client)
          |
          | Skill: instructions, stop rules, tool selection guidance
          v
MCP Server / CLI
          |
          | Typed capability services
          v
Core package
  - source adapters
  - acquisition
  - extractors
  - deduplication
  - policy evaluation
  - query generation
  - verification
          |
          v
SQLite run ledger + immutable local artifacts + reports
```

The Skill is not the data-processing engine. It is a thin behavior layer. The MCP Server and CLI must be able to execute the same capability without a Skill so that the system remains testable and usable by non-agent automation.

---

## 2. Stable contracts to design before implementation

### 2.1 Run context

Create `core/document_collection/contracts.py` with immutable dataclasses or Pydantic models for the following logical records:

```python
@dataclass(frozen=True)
class RunContext:
    run_id: str
    requirement_revision: str
    policy_hash: str
    source_snapshot_id: str
    workspace: str
    dry_run: bool
    approval_mode: str  # "deny_by_default" | "approved_read" | "operator_confirmed"
    max_cost: float | None
    max_steps: int
    random_seed: int
```

The implementation must also define:

```python
@dataclass(frozen=True)
class SourceItem:
    source_name: str
    source_folder_token: str
    source_path: str
    item_token: str
    item_type: str
    name: str
    url: str | None
    mime_type: str | None
    extension: str | None
    remote_size: int | None
    remote_modified_time: str | None
    metadata: dict[str, Any]

@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    path: str
    sha256: str
    media_type: str
    byte_size: int
    created_by: str

@dataclass(frozen=True)
class EvidenceRef:
    artifact_id: str
    locator_type: str  # page, section, sheet, table, char_range, semantic_unit
    locator: dict[str, Any]
    quote_hash: str | None

@dataclass(frozen=True)
class DecisionRecord:
    item_token: str
    decision: str  # accepted, rejected, review, pending
    reason_codes: tuple[str, ...]
    hard_failures: tuple[str, ...]
    soft_scores: dict[str, float]
    evidence: tuple[EvidenceRef, ...]
    policy_hash: str
    reviewer: str  # deterministic, agent, human
```

Do not return large document bodies in MCP tool results. Return `ArtifactRef` and `EvidenceRef`; expose large extracted text through local artifact resources or controlled range reads.

### 2.2 Capability contract

Every capability must declare:

```text
name
version
input_schema
output_schema
preconditions
side_effects
retry_policy
idempotency_key
verification_command
```

A capability must return one of:

```text
completed
partial
needs_approval
needs_human_decision
retryable_failure
permanent_failure
```

This gives the Agent a stable surface even when the underlying parser or source changes.

### 2.3 Policy contract

Create `config/collection_policy.example.json` and keep the active local policy outside version control when it contains private source identifiers or model settings.

The policy must separate:

```json
{
  "requirement_snapshot": {
    "document_id": "...",
    "revision_id": 198,
    "content_sha256": "..."
  },
  "sources": [],
  "hard_rules": {},
  "soft_targets": {},
  "language_quota": {},
  "subject_quota": {},
  "query_policy": {},
  "model_policy": {},
  "review_policy": {},
  "retention_policy": {}
}
```

No threshold may be added only because it is convenient to implement. If the requirement does not define a numeric minimum for document length or parser quality, record the diagnostic and send the item to semantic review rather than inventing a hard cutoff.

---

## 3. Repository layout and file ownership

All implementation paths below are relative to:

```text
D:\Users\admin\Desktop\My_Project\Ella知识库AI生题数据集
```

### 3.1 New core package

```text
core/document_collection/
    __init__.py
    contracts.py          # RunContext, SourceItem, ArtifactRef, decisions
    errors.py             # stable error codes and retryability
    policy.py             # load/validate/fingerprint policy snapshots
    requirement_compiler.py
    state_store.py        # SQLite schema and transactions
    event_log.py          # append-only JSONL events
    artifact_store.py     # atomic, content-addressed local artifacts
    scheduler.py          # bounded concurrency and shared rate limiter
    source_base.py        # DocumentSource protocol
    acquisition.py
    extraction.py
    normalization.py
    deduplication.py
    screening.py
    ranking.py
    query_generation.py
    auditing.py
    service.py             # application service facade
    adapters/
        __init__.py
        lark_drive.py
        local_files.py
        online_doc.py
        online_sheet.py
        online_slides.py
    extractors/
        __init__.py
        pdf_extractor.py
        docx_extractor.py
        legacy_office_extractor.py
        pptx_extractor.py
        xlsx_extractor.py
        csv_extractor.py
        text_extractor.py
```

### 3.2 Agent and MCP layer

```text
mcp_server/
    __init__.py
    server.py              # official MCP Python SDK entrypoint
    tool_registry.py
    resource_registry.py
    error_mapping.py

scripts/
    collection_cli.py      # argparse CLI; calls core.service only
    collection_requirements.py
    collection_replay.py

skills/
    document_collection/
        SKILL.md
        examples/
        capability-manifest.json

workers/
    document_collection_worker.py  # only after CLI is stable
```

### 3.3 Tests and fixtures

```text
tests/document_collection/
    conftest.py
    fixtures/
        lark_drive_pages/
        documents/
        extracted/
        policies/
        expected_reports/
    test_contracts.py
    test_policy.py
    test_state_store.py
    test_artifact_store.py
    test_lark_inventory.py
    test_acquisition.py
    test_extractors.py
    test_deduplication.py
    test_screening.py
    test_query_generation.py
    test_auditing.py
    test_mcp_contract.py
    test_skill_scenarios.py
    test_replay.py
```

### 3.4 Existing files that must not be changed initially

Do not modify `core/datasource.py`, `core/smart_crawler.py`, `core/datasource_wikipedia.py`, or existing `core/doc_processor.py` stage dispatch merely to fit the new feature. Existing Stage 1 extraction and query generation can be wrapped or reused behind an adapter after their output semantics are verified, but the new subsystem needs page/section/sheet provenance that the current flat extracted-text path does not guarantee.

The first GUI integration must not change `app/main_window.py` or the existing workflow pages until the CLI/MCP pilot is accepted. When UI work starts, follow the existing `MainWindow -> WorkflowSession -> workflow page -> worker` pattern and keep business logic in `core/document_collection`.

---

---

## 4. Development tasks and implementation details

## Task 0: Freeze requirements, repository baseline, and risk decisions

**Files:**

- Create: `docs/superpowers/specs/2026-09-09-agentic-document-collection-spec.md`
- Create: `docs/superpowers/specs/requirement-snapshots/2026-09-09.json`
- Create: `docs/superpowers/specs/open-decisions.md`
- Modify only after explicit review: none of the existing source files

**Purpose:** Prevent implementation against a moving requirement document or a dirty codebase.

**Steps:**

- [ ] Record the current requirement IDs and revisions: collection plan `198`, primary requirement `428`, multi-format requirement `1140`.
- [ ] Store SHA-256 hashes of the fetched document contents and the exact fetch command output.
- [ ] Write a reconciliation table for known differences:
  - collection plan query policy versus the source document's user range handling;
  - `PPT/PPTX` source statistics versus the current size rule listing `PPTX`;
  - `DOC`/`PPT` legacy formats versus current parser availability;
  - online resource size verification;
  - draft status of the v0.2 multi-format requirement.
- [ ] Capture the existing repository `git status`, current commit, Python version, dependency versions, and `lark-cli` version.
- [ ] Create an implementation branch/worktree from the chosen baseline. Do not include unrelated current working-tree edits unless the owner explicitly chooses them as the base.
- [ ] Obtain explicit decisions for all entries in `open-decisions.md` before implementing their behavior.

**Verification:**

- The snapshot can be re-read without network access.
- A reviewer can identify which requirement revision produced any future run.
- No code change is made before the baseline and open decisions are recorded.

**Gate:** Requirements and base branch approved.

## Task 1: Add the independent package skeleton and dependency boundary

**Files:**

- Create: `core/document_collection/__init__.py`
- Create: `core/document_collection/contracts.py`
- Create: `core/document_collection/errors.py`
- Create: `core/document_collection/service.py`
- Create: `scripts/collection_cli.py`
- Create: `requirements-collection.txt`
- Test: `tests/document_collection/test_contracts.py`

**Implementation:**

- [ ] Use standard-library dataclasses and typing for core records; do not make Qt imports available under `core/document_collection`.
- [ ] Add the official MCP Python SDK as an optional collection integration dependency, isolated from the existing GUI dependency set until the first MCP smoke test passes.
- [ ] Add `python-pptx`, `pytest`, and `pytest-asyncio` to the collection dependency file only if the environment does not already provide them.
- [ ] Define stable error codes such as `SOURCE_AUTH`, `SOURCE_PAGE_INCOMPLETE`, `SIZE_EXCEEDED`, `MIME_UNKNOWN`, `EXTRACTION_FAILED`, `LOCATOR_MISSING`, `DUPLICATE_HIGH_CONFIDENCE`, `NEEDS_HUMAN_REVIEW`, and `BUDGET_EXCEEDED`.
- [ ] Make `scripts/collection_cli.py` import only `core.document_collection.service` and expose `--help`, `--version`, and a no-op `validate-policy` command.

**Verification:**

```powershell
.venv\Scripts\python -m pytest tests\document_collection\test_contracts.py -q
.venv\Scripts\python -m compileall -q core\document_collection scripts\collection_cli.py
```

**Gate:** Core imports without Qt and existing application tests remain unchanged.

## Task 2: Implement policy loading, validation, hashing, and requirement snapshots

**Files:**

- Create: `core/document_collection/policy.py`
- Create: `core/document_collection/requirement_compiler.py`
- Create: `config/collection_policy.example.json`
- Create: `scripts/collection_requirements.py`
- Test: `tests/document_collection/test_policy.py`

**Implementation:**

- [ ] Load JSON policy using UTF-8 and reject unknown top-level keys unless explicitly listed under `extensions`.
- [ ] Canonicalize JSON with sorted keys before hashing.
- [ ] Validate all hard rules, quotas, query policy, and side-effect policy before a run can start.
- [ ] Implement `RequirementSnapshot` containing document IDs, revisions, content hashes, fetch timestamps, and reconciliation notes.
- [ ] Implement `compile_requirement_draft(snapshot) -> RequirementSpecDraft`. The LLM may propose semantic fields, but the output must be schema-validated and marked `draft` until a human confirms it.
- [ ] Implement deterministic reconciliation checks for conflicting fields and produce `needs_human_decision` instead of silently picking one.
- [ ] Freeze a confirmed `RequirementSpec` into the run record; later live-document changes must create a new run, not mutate the active one.

**Verification:**

- Same policy content produces the same hash.
- Key order changes do not change the hash.
- A policy missing a hard rule fails before acquisition.
- A conflicting query policy is reported as an open decision.
- A changed requirement revision produces a different snapshot ID.

**Gate:** A run cannot begin without a validated, versioned policy snapshot.

## Task 3: Implement durable state, artifact storage, and event logging

**Files:**

- Create: `core/document_collection/state_store.py`
- Create: `core/document_collection/artifact_store.py`
- Create: `core/document_collection/event_log.py`
- Test: `tests/document_collection/test_state_store.py`
- Test: `tests/document_collection/test_artifact_store.py`

**Implementation:**

- [ ] Use SQLite with explicit migrations stored in `core/document_collection/migrations/`.
- [ ] Create tables for `runs`, `requirement_snapshots`, `source_items`, `acquisition_attempts`, `artifacts`, `extractions`, `dedup_clusters`, `decisions`, `queries`, `evidence`, `approvals`, and `events`.
- [ ] Use one transaction per item state transition; never update an item and its event log in separate uncommitted operations.
- [ ] Store artifacts under `output/document_collection/<run_id>/artifacts/` using content-addressed filenames based on SHA-256.
- [ ] Write files through a temporary path and atomic rename.
- [ ] Define idempotency keys:
  - inventory: `(source_name, folder_token, snapshot_params_hash)`;
  - acquisition: `(item_token, remote_version_or_modified_time, requested_export_format)`;
  - extraction: `(artifact_sha256, extractor_name, extractor_version)`;
  - decision: `(item_token, policy_hash, evidence_hash, reviewer_version)`.
- [ ] Implement `resume_run(run_id)` so already completed idempotent operations are skipped and incomplete operations are re-queued.
- [ ] Store event payloads without full sensitive document text; use artifact/evidence references.

**Verification:**

- Kill and restart a run during each state transition.
- Re-running the same operation does not create duplicate artifacts or decisions.
- SQLite integrity check passes.
- Every state transition has a corresponding event.

**Gate:** A run can resume after process termination without re-downloading or re-deciding completed items.

## Task 4: Implement source adapters and inventory

**Files:**

- Create: `core/document_collection/source_base.py`
- Create: `core/document_collection/adapters/lark_drive.py`
- Create: `core/document_collection/adapters/local_files.py`
- Create: `scripts/collection_cli.py` subcommand `inventory`
- Test: `tests/document_collection/test_lark_inventory.py`
- Fixtures: `tests/document_collection/fixtures/lark_drive_pages/`

**Interfaces:**

```python
class DocumentSource(Protocol):
    async def snapshot(self, ctx: RunContext) -> str: ...
    async def enumerate_items(self, snapshot_id: str) -> AsyncIterator[SourceItem]: ...
    async def acquire(self, item: SourceItem, destination: Path, ctx: RunContext) -> ArtifactRef: ...
```

**Implementation:**

- [ ] Wrap `lark-cli drive files list` in one adapter. Keep CLI argument construction and JSON response parsing inside `lark_drive.py`.
- [ ] Use explicit `folder_token`, `page_size`, and `page_token` fields; do not rely on client-side guesses about pagination.
- [ ] Recursively enumerate `知识库_2026` and `docQA.llm` and retain the exact source folder name at every row.
- [ ] Preserve raw page responses in a fixture/replay artifact after redacting tokens only where required for test publication.
- [ ] Detect `has_more` and fail closed if a page claims more data but has no next token.
- [ ] Separate folder, file, online Docx, online Sheet, online Slides, shortcut, and unknown resource types.
- [ ] Do not call content download during inventory.
- [ ] Provide `--dry-run` and `--record-response` modes.
- [ ] Add a local filesystem adapter so all downstream stages can be tested without Feishu access.

**Verification:**

- Replay fixtures produce deterministic inventory rows.
- Inventory counts match the current recorded source snapshot, with any source-side changes reported rather than silently accepted.
- No source mutation command exists in the adapter.
- Pagination and recursion tests cover empty folders, one-page folders, multi-page folders, nested folders, duplicate tokens, and malformed pages.

**Gate:** Inventory-only can complete both folders and produce a source snapshot without downloads.

## Task 5: Implement acquisition, export, retry, and rate limiting

**Files:**

- Create: `core/document_collection/acquisition.py`
- Create: `core/document_collection/scheduler.py`
- Modify: `core/document_collection/adapters/lark_drive.py`
- Test: `tests/document_collection/test_acquisition.py`

**Implementation:**

- [ ] Add a shared concurrency limiter, not only per-task sleeps.
- [ ] Honor server-provided retry delays when available; use bounded exponential backoff otherwise.
- [ ] Classify failures as retryable, permanent, or approval-required.
- [ ] Use atomic temporary downloads and verify byte size and SHA-256 before committing the artifact record.
- [ ] Implement source-type-specific acquisition:
  - local files: copy or hard-link only when safe;
  - PDF/DOC/DOCX/PPTX/XLSX/CSV/TXT/MD: download binary content;
  - online Docx/Sheet/Slides: export or fetch through the correct document adapter;
  - unknown/shortcut/JPG/URL: retain inventory metadata but do not force them into the final set.
- [ ] Keep online-resource size as `pending_size_verification` until the exported file or authoritative metadata is available.
- [ ] Implement `--max-concurrency`, `--min-interval`, `--max-retries`, `--max-bytes`, and `--approval-mode`.
- [ ] Do not log access tokens or signed URLs.

**Verification:**

- Simulate 429, 5xx, timeout, truncated response, checksum mismatch, and disk-full errors.
- Confirm that retries do not create duplicate partial files.
- Confirm that a paused run resumes from the last committed item.
- Confirm that delete/move/rename operations are not callable by the adapter.

**Gate:** A read-only pilot can acquire a small approved batch with complete provenance and no source mutation.

## Task 6: Implement format detection and provenance-preserving extraction

**Files:**

- Create: `core/document_collection/extraction.py`
- Create: `core/document_collection/normalization.py`
- Create: `core/document_collection/extractors/*.py`
- Test: `tests/document_collection/test_extractors.py`
- Fixtures: `tests/document_collection/fixtures/documents/`

**Implementation:**

- [ ] Detect real MIME using file signature plus extension; never trust extension alone.
- [ ] Define one normalized extraction result:

```python
@dataclass(frozen=True)
class ExtractionResult:
    artifact_id: str
    extractor: str
    extractor_version: str
    text_units: tuple[TextUnit, ...]
    char_count: int
    word_count: int
    cjk_char_count: int
    page_count: int | None
    extraction_ratio: float | None
    has_tables: bool
    has_formulas: bool
    has_images: bool
    graphic_dominant: bool | None
    locator_quality: str
    diagnostics: tuple[str, ...]
```

- [ ] PDF: PyMuPDF first, pdfplumber fallback; preserve page numbers and text blocks; send scanned or visually complex pages to a bounded OCR/vision fallback.
- [ ] DOCX: python-docx for paragraphs and tables; preserve heading style and table coordinates where available.
- [ ] DOC: use an explicit optional conversion adapter; do not silently treat DOC as DOCX. If no approved converter is available, mark `conversion_required`.
- [ ] PPTX: python-pptx for slide text and speaker notes if present; preserve slide numbers. Treat legacy PPT as a separate unsupported/conversion-required type until verified.
- [ ] XLSX: openpyxl; preserve workbook, worksheet, cell range, merged cells, formulas, and displayed values where available.
- [ ] CSV: csv module with encoding detection and delimiter diagnostics; preserve row and column coordinates.
- [ ] TXT/MD: preserve line ranges and detect whether Markdown structure is real or flattened text.
- [ ] Online Docx/Sheet/Slides: normalize the exported result into the same `ExtractionResult` contract and record the original online token.
- [ ] Arabic and mixed-direction text: record extraction order diagnostics and route suspicious pages to visual review.
- [ ] Do not use the current flat Stage 1 output as the only source of truth because query evidence needs page/section/sheet locators.
- [ ] Mark long documents instead of rejecting them; full-understanding coverage is a later verification step.

**Verification:**

- Every fixture has stable page/section/sheet locators.
- Extraction is deterministic for the same artifact and extractor version.
- A malformed or password-protected file produces a typed failure.
- DOC/PPT legacy behavior is explicit.
- A graphic-dominant fixture is classified separately from text-with-illustrations.

**Gate:** The extraction layer can support internal evidence for every candidate that reaches screening.

## Task 7: Implement exact, normalized, and near-duplicate analysis

**Files:**

- Create: `core/document_collection/deduplication.py`
- Test: `tests/document_collection/test_deduplication.py`

**Implementation:**

- [ ] First compare artifact SHA-256.
- [ ] Then normalize text using a documented, language-aware normalization function that removes extraction-only whitespace noise but does not remove meaningful numbers, formulas, or language characters.
- [ ] Compute normalized text hashes.
- [ ] Use metadata blocking (title, page count, language, approximate character count) before any near-duplicate comparison.
- [ ] Implement a bounded near-duplicate candidate comparison. Do not automatically delete a near-duplicate unless the confidence threshold and evidence policy say it is safe.
- [ ] Produce `dedup_cluster_id`, `duplicate_confidence`, `canonical_candidate`, and `retention_reason`.
- [ ] Prefer the version with stronger education evidence, better locator quality, higher extraction ratio, and fewer parser failures.
- [ ] Keep all source provenance even when only one representative enters the final set.

**Verification:**

- Exact duplicates cluster with 100% precision on fixtures.
- Cleaned/conversion variants produce a review cluster rather than being silently discarded.
- Different documents with common titles do not merge based on title alone.
- Cross-source duplicate reports show both source folder names.

**Gate:** Deduplication is explainable and reversible.

## Task 8: Implement hard screening, semantic review, and ranking

**Files:**

- Create: `core/document_collection/screening.py`
- Create: `core/document_collection/ranking.py`
- Create: `core/document_collection/requirement_review.py`
- Test: `tests/document_collection/test_screening.py`

**Implementation:**

- [ ] Evaluate deterministic hard rules first:
  - real MIME and supported format;
  - size limit;
  - acquisition success;
  - extraction success;
  - language presence and dominance;
  - locator availability;
  - graphic-dominant exclusion;
  - duplicate policy;
  - document-level evidence of university/higher-education use.
- [ ] Return reason codes rather than free-form rejection text.
- [ ] Use the LLM only for semantic properties that are not reliably deterministic: education attribute, subject, material type, content quality, and whether the content can support 10 distinct queries.
- [ ] Require the LLM to return structured JSON with evidence references and confidence; reject malformed or unsupported outputs.
- [ ] Do not expose raw full documents to the model by default. Provide controlled excerpts or structured units; full-document review is an explicit mode with cost and size limits.
- [ ] Keep the current plan's hard and soft rules separate.
- [ ] Rank within feasible language/subject buckets. Satisfy minimum quotas first, then optimize quality and diversity.
- [ ] Treat format ratio `3:2:2:1:1:1` as a soft objective only.
- [ ] Store both Agent and human decisions; never overwrite a previous decision.

**Verification:**

- Seeded hard failures never pass.
- LLM semantic failure results in `review` or `unknown`, not silent acceptance.
- A document with excellent format but weak education evidence is not accepted solely to satisfy format balance.
- Re-ranking under a changed soft target does not change hard eligibility.

**Gate:** A reviewer can explain every accepted/rejected candidate from stored evidence and reason codes.

## Task 9: Implement the query capability as a standalone Skill/Tool

**Files:**

- Create: `core/document_collection/query_generation.py`
- Create: `skills/document_collection/SKILL.md`
- Create: `skills/document_collection/capability-manifest.json`
- Test: `tests/document_collection/test_query_generation.py`
- Test: `tests/document_collection/test_skill_scenarios.py`

**Implementation:**

- [ ] Use the confirmed `RequirementSpec` and selected document artifact, not a live mutable policy.
- [ ] Generate exactly 10 query records per final document.
- [ ] Keep the query text document-level; do not name chapters, sections, pages, Sheets, or tables in the query text.
- [ ] Store internal coverage targets, evidence locators, and source snippets separately.
- [ ] Keep 8 single-type queries and 2 mixed-type compatibility-only queries under the current plan.
- [ ] Use non-repeating suggested per-request question counts under 12, such as 2, 3, 5, 7, and 9, while treating the exact mapping as configurable.
- [ ] Make query language follow the document language.
- [ ] Validate JSON schema, query count, type count, question count, language, evidence presence, and duplicate query text.
- [ ] On malformed model output, retry with a repair prompt only within the configured retry budget; otherwise mark `query_generation_failed`.
- [ ] Store prompt version, model ID, input artifact hash, output hash, and verifier result.

**Skill acceptance scenario:**

```text
Given one approved English or Chinese document fixture,
when the Agent is asked to create the document's evaluation queries,
then it calls the query capability,
returns exactly 10 structured records,
keeps fragment/chapter references out of query text,
retains locators as internal evidence,
and produces a verifier report.
```

**Gate:** The query Skill is independently callable and verifiable before any collection-wide orchestration is built.

## Task 10: Implement MCP Server and capability resources

**Files:**

- Create: `mcp_server/server.py`
- Create: `mcp_server/tool_registry.py`
- Create: `mcp_server/resource_registry.py`
- Create: `mcp_server/error_mapping.py`
- Test: `tests/document_collection/test_mcp_contract.py`

**Implementation:**

- [ ] Use the official MCP Python SDK v2; pin the tested minor version in the collection dependency lock.
- [ ] Start with local stdio transport for development and single-host testing; add HTTP transport only after local contracts are stable.
- [ ] Expose one MCP tool per stable capability, not one tool for the entire pipeline.
- [ ] Keep tool input and output schemas aligned with `contracts.py`.
- [ ] Expose large reports and extracted-text ranges as resources or artifact references, not oversized tool responses.
- [ ] Map typed core errors to structured MCP errors with retryability and human-action fields.
- [ ] Enforce tool allowlists based on `approval_mode`.
- [ ] Add `health`, `capabilities`, and `resume_run` tools.
- [ ] Make the MCP layer a thin adapter; it must not contain document-selection business rules.

**Verification:**

- A schema client can list tools and validate all tool schemas.
- Each tool can be called against local fixtures without Feishu credentials.
- A read-only policy rejects destructive actions.
- A large artifact is returned by reference and can be read through a controlled resource operation.
- MCP restart does not lose run state because state lives in SQLite/artifacts.

**Gate:** One host can invoke inventory, inspect, screening, query, and audit through MCP with no direct import of core internals.

## Task 11: Build the single-host Agent Skill smoke test

**Files:**

- Modify: `skills/document_collection/SKILL.md`
- Create: `tests/document_collection/agent_eval_cases.jsonl`
- Create: `scripts/collection_agent_smoke.py`
- Test: `tests/document_collection/test_skill_scenarios.py`

**Implementation:**

- [ ] Define the Skill's allowed tools, required evidence, stop conditions, and human-approval boundaries.
- [ ] Add scenarios for inventory only; inspect one candidate; reject an over-size file; route an unknown MIME to review; resume after a failed acquisition; generate queries for one accepted document; and refuse a request to alter the cloud source.
- [ ] Record actual tool calls, parameters, state transitions, and final artifacts.
- [ ] Add a deterministic replay mode where the Agent's tool calls are checked against a fixture server.
- [ ] Run at least one real call through the chosen Agent platform after offline tests pass.

**Acceptance metrics:**

- All tool calls conform to schema.
- No forbidden side effect occurs.
- Every final decision has evidence or an explicit `needs_human_decision` result.
- The same fixture run produces equivalent decisions and artifact hashes, excluding timestamps and run IDs.

**Gate:** The individual Skill is stable enough to be used as a building block.

## Task 12: Implement the generic manager loop

**Files:**

- Create: `core/document_collection/agent_loop.py`
- Create: `core/document_collection/stop_conditions.py`
- Create: `core/document_collection/approvals.py`
- Create: `scripts/collection_orchestrator.py`
- Test: `tests/document_collection/test_replay.py`

**Implementation:**

- [ ] Implement the loop as `observe -> plan -> act -> verify -> checkpoint -> continue/stop`.
- [ ] The manager reads only the current `RunContext`, task state, pending queue, verifier findings, and policy snapshot.
- [ ] The manager calls capability tools, not private functions inside Skills.
- [ ] Add hard stop conditions for `quota_met`, `source_exhausted`, `needs_human_decision`, `budget_exceeded`, `max_steps`, `time_limit`, and `verification_failed`.
- [ ] Add a bounded per-run cost and token ledger.
- [ ] Add approval gates before bulk acquisition, full-document LLM review, OCR/vision fallback, and final acceptance.
- [ ] Add recovery policy:
  - retry transient source failures;
  - switch extractor when the capability manifest permits it;
  - quarantine malformed artifacts;
  - request human review for unsupported formats or conflicting rules.
- [ ] Never let the Agent modify the policy snapshot in place.
- [ ] Persist the manager's plan as an event, but treat tool outputs and verifier results as authoritative.

**Verification:**

- The manager reaches a terminal state on every fixture.
- An injected transient failure causes a retry, not an infinite loop.
- An injected hard-rule failure prevents acceptance.
- A changed soft quota causes re-ranking without changing hard failures.
- A stopped run can resume without repeating completed side effects.

**Gate:** The whole process can be driven by an Agent without a fixed hard-coded stage graph.

## Task 13: Add report generation and human review handoff

**Files:**

- Create: `scripts/collection_report.py`
- Create: `docs/document-collection-operator-guide.md`
- Test: `tests/document_collection/test_auditing.py`

**Required outputs per run:**

```text
run_manifest.json
requirement_snapshot.json
source_inventory.jsonl
source_type_counts.csv
acquisition_report.jsonl
extraction_report.jsonl
dedup_clusters.csv
accepted_documents.csv
rejected_documents.csv
human_review_queue.csv
query_records.jsonl
query_evidence.jsonl
coverage_report.md
cost_and_runtime.json
```

**Implementation:**

- [ ] Include exact source folder names in every report.
- [ ] Separate raw candidate counts from post-dedup and final accepted counts.
- [ ] Include reason-code breakdowns and per-language/per-subject coverage.
- [ ] Include all pending and blocked items; do not hide them as rejected.
- [ ] Provide a review queue that can be edited outside the program and re-imported with reviewer, timestamp, decision, and note.
- [ ] Never replace an Agent decision; append a human decision event that supersedes it for the active policy snapshot.
- [ ] Keep raw document text out of routine summary reports unless explicitly requested.

**Gate:** An external reviewer can reconstruct the final count and acceptance decision from the reports without opening the Agent transcript.

## Task 14: Add optional PySide6 integration after CLI/MCP acceptance

**Files:**

- Create: `workers/document_collection_worker.py`
- Create: `app/widgets/document_collection_page.py`
- Modify only after pilot: `app/widgets/workflow_pages.py`, `app/main_window.py`, `app/workflow_session.py`
- Test: `tests/document_collection/test_gui_adapter.py`

**Implementation:**

- [ ] Keep the worker as a thin QThread wrapper around the core service or CLI.
- [ ] Use the existing `WorkflowSession`/`LogDrawer` pattern rather than introducing a second logging system.
- [ ] Expose start, pause, resume, stop, open output, and review queue actions.
- [ ] Do not put selection or parsing logic in Qt widgets.
- [ ] Preserve existing workflow pages and test compact/native layouts before merging.

**Gate:** GUI is a client of the already-tested core/MCP path, not a second implementation.

## Task 15: Add second-host compatibility and optional Plugin wrappers

**Files:**

- Create: `skills/<confirmed-host>/SKILL.md` only when the target host is confirmed
- Create: `plugins/<host>/README.md`
- Create: `tests/document_collection/test_cross_host_contract.py`

**Implementation:**

- [ ] Use the same MCP Server and capability schemas.
- [ ] Keep host-specific Skill text limited to installation, tool discovery, and platform conventions.
- [ ] Do not duplicate core policy or parsing code inside a Plugin.
- [ ] Add a compatibility matrix documenting host, transport, authentication, resource reads, approvals, and known limitations.
- [ ] Package Plugin wrappers only after the same scenarios pass through MCP without host-specific assumptions.

**Gate:** At least two Agent hosts can run the same inventory and query scenarios against the same MCP Server and produce equivalent structured artifacts.

---

## 5. Schedule and release gates

The following is an initial working-day schedule for one developer plus one part-time domain reviewer. It assumes Feishu authorization is available and does not include time for unresolved business decisions.

| 日期 | 阶段 | 主要交付 | 出口条件 |
|-|-|-|-|
| 2026-09-10 - 09-11 | Requirements and baseline | Requirement snapshots, reconciliation table, branch/worktree baseline, open decisions | Revision and scope approved |
| 2026-09-14 - 09-18 | Contracts and state | Core contracts, policy loader, SQLite ledger, artifact store, event log | Contract/state tests pass |
| 2026-09-21 - 09-23 | Inventory | Feishu and local adapters, recursive inventory, replay fixtures | Two folders inventory-only succeeds |
| 2026-09-24 - 09-29 | Acquisition | Download/export, retry, rate limit, checkpointing | Read-only acquisition pilot succeeds |
| 2026-09-30 - 10-06 | Extraction | PDF/DOCX/PPTX/XLSX/CSV/TXT/MD adapters, provenance | Fixture extraction and locator tests pass |
| 2026-10-07 - 10-09 | Dedup and hard screening | Exact/normalized dedup, hard gates, reason codes | Seeded violations never pass |
| 2026-10-12 - 10-14 | Semantic review | Agent-assisted education/subject/material classification | Evidence-backed review outputs |
| 2026-10-15 - 10-19 | Query capability | Standalone query Skill/Tool, schema validation, evidence | One-document query scenario passes |
| 2026-10-20 - 10-22 | MCP integration | MCP Server, resources, errors, health/resume | One host calls all stable capabilities |
| 2026-10-23 - 10-27 | Manager loop | Generic Agent loop, approvals, stop conditions, recovery | Replay and failure-injection tests pass |
| 2026-10-28 - 11-03 | Pilot | Stratified 100-200 resource pilot across both sources | Go/no-go metrics met or documented |
| 2026-11-04 - 11-06 | Stabilization | Fixes, reports, operator guide, cross-host smoke | Pilot audit accepted |
| 2026-11-09 onward | Optional UI/Plugin | PySide6 page and host wrappers | Only after core release is stable |

### 5.1 Release gates

- **Gate A  Contract ready:** all inputs/outputs/errors have schemas and fixtures.
- **Gate B  Inventory ready:** source snapshot is complete and replayable.
- **Gate C  Acquisition ready:** read-only download/export is resumable and rate-limited.
- **Gate D  Screening ready:** hard-rule violations have zero false passes in seeded tests.
- **Gate E  Skill ready:** each capability can be invoked by an Agent and produces structured artifacts.
- **Gate F  Orchestration ready:** manager loop handles retries, approval, stop, and resume.
- **Gate G  Pilot ready:** both sources, all relevant formats, and representative languages are covered.
- **Gate H  Production ready:** audit reports, retention rules, runbook, and rollback path are accepted.

## 6. Verification and evaluation plan

### 6.1 Deterministic unit tests

- Contract serialization and validation.
- Policy hashing and revision handling.
- SQLite transaction and migration tests.
- Atomic artifact writes and checksum verification.
- Pagination, recursion, and malformed-source responses.
- MIME sniffing and size limits.
- Page/section/sheet locator preservation.
- Exact and normalized deduplication.
- Hard-rule evaluator.
- Query count/type/question-count checks.
- Report totals and reason-code aggregation.

### 6.2 Recorded integration tests

Use recorded, redacted Lark responses for:

- first page and continuation page;
- nested folders;
- online Docx/Sheet/Slides;
- missing or unknown fields;
- 429, 5xx, timeout, malformed JSON;
- resource that changes between snapshots.

Recorded tests prove adapter behavior but do not prove current live authorization. Live access must be reported separately.

### 6.3 Agent evaluation tests

Create `agent_eval_cases.jsonl` with fields:

```json
{
  "case_id": "inventory_nested_folder_001",
  "goal": "Inventory both authorized sources without modifying them",
  "allowed_tools": ["collection_inventory"],
  "required_artifacts": ["source_inventory.jsonl"],
  "forbidden_actions": ["drive.delete", "drive.move", "drive.rename"],
  "must_verify": ["pagination_complete", "source_names_exact"],
  "terminal_condition": "completed"
}
```

Evaluate:

- tool-selection correctness;
- schema validity;
- forbidden side-effect avoidance;
- evidence completeness;
- correct escalation of ambiguity;
- recovery after injected failure;
- stable final artifacts across repeated runs.

### 6.4 Pilot validation

The pilot must be stratified by:

- source folder;
- language;
- major file type;
- online versus local resource;
- short versus long document;
- clean text versus table/formula-heavy content;
- suspected duplicate versus unique item.

Collect these metrics:

```text
inventory completeness
acquisition success rate
extraction success by format
locator completeness
unknown MIME rate
legacy DOC/PPT conversion rate
exact duplicate rate
near-duplicate review precision
hard-rule false-pass count
semantic-review agreement sample
query schema pass rate
query evidence coverage
mean time per item
retry rate
LLM token/cost per accepted document
human review minutes per accepted document
resume success rate
```

Do not define business-quality thresholds that are absent from the requirements. Define engineering gates now; set semantic acceptance thresholds after the first labeled review sample.

### 6.5 Regression commands

Run after every feature task:

```powershell
.venv\Scripts\python -m pytest tests\document_collection -q
.venv\Scripts\python -m compileall -q core\document_collection mcp_server scripts tests
```

Run before pilot:

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python scripts\collection_cli.py validate-policy --policy config\collection_policy.example.json
.venv\Scripts\python scripts\collection_replay.py --fixture tests\document_collection\fixtures
git diff --check
```

If a real external service was not exercised, label the result as structural/replay verification rather than live integration verification.

---

## 7. Security, privacy, and operational controls

- Use explicit user identity and existing authorized CLI path; do not copy credentials into policy files.
- Redact access tokens, signed URLs, and personal identifiers from event logs and fixtures.
- Keep source artifacts and extracted text in a configured local directory with documented retention.
- Add a `--no-llm` mode for inventory, acquisition, extraction, and deterministic dedup.
- Add a `--no-network` replay mode for tests.
- Make full-document LLM review, OCR/vision, and bulk acquisition approval-gated.
- Add per-run cost, byte, time, and step budgets.
- Add a kill switch that stops new side effects but lets current atomic writes finish.
- Store model name, prompt version, and policy hash with every LLM-assisted decision.
- Do not include raw document content in normal Agent conversation history when an artifact reference is sufficient.
- Define how long downloaded files and extracted text remain before deletion; this is a business/security decision, not an implementation default.

---

## 8. Open decisions and missing requirements

These items are currently missing or insufficiently precise and must be resolved before the affected task starts:

1. **Requirement authority:** The multi-format document is still marked draft v0.2. Confirm whether revision 1140 is binding for this collection run.
2. **Legacy Office support:** Decide whether `.doc` and `.ppt` must be supported now, and whether LibreOffice, Microsoft Word COM, or another converter is approved.
3. **Online export contract:** Define how online Docx, Sheet, and Slides are exported and what counts as authoritative file size.
4. **MIME and format policy:** Resolve `PPT/PPTX` source statistics versus the current `.pptx` size rule.
5. **Full-understanding backend:** Confirm which service/model provides full-document understanding and how long-document cost is budgeted.
6. **Image-understanding quota:** Confirm whether the stated three-per-account-per-week limit is still current and how quota exhaustion is represented.
7. **Model/provider policy:** Select allowed providers, fallback order, maximum cost, timeout, concurrency, and whether raw prompts/responses may be retained.
8. **Semantic acceptance thresholds:** Define the minimum evidence needed to classify a document as university-level, subject-valid, and sufficient for 10 distinct queries.
9. **Human review ownership:** Name the reviewer, review interface, response-time target, and escalation path for ambiguous language, Arabic layout, and duplicate clusters.
10. **Data retention and copyright:** Define retention period, local storage location, access controls, and whether extracted text may be sent to external LLM providers.
11. **Cross-platform matrix:** Confirm the first two Agent hosts that must call the MCP Server; do not build unused Plugin wrappers.
12. **Deployment target:** Confirm local-only, shared Windows host, or a remote service. This determines whether HTTP transport and service accounts are required.
13. **Source monitoring:** Decide whether the system should detect new/modified cloud items on a schedule or only process an explicit snapshot.
14. **Query evidence format:** Confirm whether exact quotes, hashes plus locators, or redacted excerpts are required in the final delivery.
15. **Quota feasibility:** Confirm whether P0 quotas are hard deliverables even if a language does not yield 20 high-quality documents. The system must report infeasibility rather than fill with weak items.
16. **Failure policy:** Decide whether unresolved candidates are held for a later run or permanently excluded from the current release.
17. **Artifact format:** Confirm whether the canonical delivery is JSONL/CSV plus Excel, or whether a Feishu document/Sheet must also be produced.
18. **Operator authority:** Define which actions require user approval: bulk acquisition, OCR/vision, full-document LLM, final acceptance, and external sharing.

---

## 9. Risks and mitigations

| Risk | Impact | Mitigation |
|-|-|-|
| Live requirements change during a run | Inconsistent output | Freeze requirement snapshot and policy hash per run |
| Agent makes a plausible but unsupported decision | Bad data enters final set | Hard deterministic gates, evidence requirements, human review state |
| MCP session is treated as durable state | Lost or duplicated work | SQLite/artifact ledger owns state; MCP only transports calls |
| OpenClaw or another host changes Skill/Plugin behavior | Integration break | MCP contract tests and host-specific thin wrappers |
| `.doc`/`.ppt` cannot be parsed | Candidate loss or false acceptance | Explicit conversion adapter and `conversion_required` state |
| PDF text extraction loses page structure | Query evidence cannot be audited | Provenance-preserving extractor contract and locator tests |
| LLM provider rate limits or costs spike | Run stalls or becomes expensive | Shared limiter, budgets, retry classification, deterministic fallback |
| Near-duplicate algorithm over-merges | Loss of unique documents | Review clusters and reversible canonical selection |
| GUI and CLI diverge | Two incompatible implementations | GUI calls core service only after CLI/MCP acceptance |
| Dirty existing working tree is mixed into feature | Hard-to-review regression | Explicit baseline/worktree and per-task commits |
| Sensitive source text leaks into logs | Security/compliance issue | Artifact references, redaction, no raw content in routine logs |

---

## 10. Definition of done

The first release is complete only when all of the following are true:

- A versioned requirement snapshot produces a validated policy.
- Both authorized folders can be inventoried recursively in read-only mode.
- The source inventory is replayable and auditable.
- Acquisition and processing are resumable and idempotent.
- Major supported formats produce locator-preserving extraction results.
- Hard gates, deduplication, semantic review, and ranking produce reasoned decisions.
- Each capability can be invoked independently through CLI and MCP.
- At least one Agent host can call every released capability and produce structured artifacts.
- The manager loop can recover from transient failures and stop for human decisions.
- Query generation satisfies the current document-level query policy and retains internal evidence separately.
- Reports distinguish raw candidates, post-dedup candidates, accepted documents, rejected documents, and pending review.
- Security, retention, cost, and approval policies are documented.
- The 100200 item pilot passes engineering gates and produces a documented list of business-quality follow-ups.
- Optional GUI/Plugin integration is not considered part of core completion; it may be added after the core release.

---

## 11. Official technical references to consult during implementation

- Model Context Protocol architecture and lifecycle: use it for the cross-platform Tool/Resource/Prompt boundary, not for durable state.
- Official MCP Python SDK: use the supported Python server APIs and transport guidance rather than inventing an MCP adapter.
- OpenClaw Skills documentation: use it to keep Skill files instruction-focused and platform-aware.
- OpenClaw Plugin SDK documentation: use it only for host packaging; do not put the collection core inside the plugin.
- OpenClaw MCP documentation: use it for host integration and tool-policy expectations.
- OpenAI Harness Engineering: use it for repository/system-of-record, legibility, feedback loops, and environment design.
- Anthropic long-running Agent Harness and Loop Engineering: use it for context handoff, bounded loops, stop conditions, and evaluator-driven work.
- LangGraph durable execution documentation: evaluate only if the generic loop needs a third-party checkpoint/runtime implementation after the first pilot.
- Existing repository `AGENTS.md`, `README.md`, and current `core/`/`workers/` boundaries: use them to avoid breaking the current GUI and crawler/query flows.

---

## 12. Execution order summary

```text
Freeze requirements and baseline
  -> define contracts and state
  -> inventory capability
  -> acquisition capability
  -> extraction capability
  -> dedup and hard screening
  -> semantic review capability
  -> query capability
  -> MCP Server
  -> single-host Skill smoke test
  -> generic manager loop
  -> pilot and audit
  -> optional GUI and Plugin wrappers
```

The first implementation target is **inventory-only**, not the full pipeline and not the GUI. The first irreversible design decision is the capability contract and run ledger; those must be reviewed before adding more Skills.
