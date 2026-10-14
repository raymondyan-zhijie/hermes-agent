"""
Checkpoint Manager — Transparent filesystem snapshots via a single shared
shadow git store.

Creates automatic snapshots of working directories before file-mutating
operations (``write_file``, ``patch``, ``terminal`` with destructive flags),
triggered once per conversation turn.  Provides rollback to any previous
checkpoint.

This is NOT a tool — the LLM never sees it.  It's transparent infrastructure
controlled by the ``checkpoints`` config flag or ``--checkpoints`` CLI flag.

Storage layout (single shared store, git objects deduplicated across projects)
-----------------------------------------------------------------------------

    ~/.hermes/checkpoints/
        store/                          — single bare-ish git repo
            HEAD, config, objects/      — standard git internals (shared)
            refs/hermes/<hash16>        — per-project branch tip
            indexes/<hash16>            — per-project git index
            projects/<hash16>.json      — {workdir, created_at, last_touch}
            snapshot-state/<hash16>.json — last snapshot outcome (ok/code/detail)
            info/exclude                — default excludes (shared)
        .last_prune                     — auto-prune idempotency marker
        legacy-<timestamp>/             — archived pre-v2 per-project shadow
                                          repos (auto-migrated on first init)

Why a single store?
-------------------

The pre-v2 design kept a full shadow repo per working directory.  Each one
re-stored most of the project's files under its own ``objects/`` tree, with
zero sharing across worktrees of the same project.  A single user with a
dozen worktrees of the same repo burned ~40 MB each (~500 MB total) storing
the same blobs over and over.  A single shared store lets git's content-
addressable object DB deduplicate across projects and across turns, so adding
a new worktree costs near-zero.

The shadow store uses ``GIT_DIR`` + ``GIT_WORK_TREE`` + ``GIT_INDEX_FILE``
so no git state leaks into the user's project directory.

Auto-maintenance (``tools.checkpoint_maintenance``)
---------------------------------------------------

Shadow state accumulates over time.  ``prune_checkpoints`` deletes refs whose
recorded working directory no longer exists (orphan) or whose last touch is
older than ``retention_days`` (stale), then runs ``git gc --prune=now`` to
reclaim object storage.  A size-cap pass drops the oldest checkpoints per
project until total store size is under ``max_total_size_mb``.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path, PurePosixPath
from hermes_constants import get_hermes_home
from hermes_cli._subprocess_compat import selected_git_env, windows_hide_flags
from hermes_cli.gitlock import clear_stale_tmp_packs
from typing import Dict, List, Optional, Set, Tuple

from utils import env_int, rmtree_readonly

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHECKPOINT_BASE = get_hermes_home() / "checkpoints"
_CHECKPOINT_BASE_AT_IMPORT = CHECKPOINT_BASE


def _resolve_checkpoint_base() -> Path:
    """Active profile's checkpoint root at call time: the patched ``CHECKPOINT_BASE`` when a test
    changed it, else live profile-scoped HERMES_HOME — under the multiplexed gateway one process
    serves every profile, so the import-time constant would write every profile's code-edit
    checkpoints into the launch profile's store."""
    return CHECKPOINT_BASE if CHECKPOINT_BASE != _CHECKPOINT_BASE_AT_IMPORT else get_hermes_home() / "checkpoints"

# Single shared store directory under CHECKPOINT_BASE.
_STORE_DIRNAME = "store"
_REFS_PREFIX = "refs/hermes"
_INDEXES_DIRNAME = "indexes"
_PROJECTS_DIRNAME = "projects"
_LEDGERS_DIRNAME = "ledgers"
_SNAPSHOT_STATE_DIRNAME = "snapshot-state"
_LEGACY_PREFIX = "legacy-"
_PRUNE_MARKER_NAME = ".last_prune"

# Agent-write ledger cap: newest entries retained per project.
_LEDGER_MAX_ENTRIES = 2000

DEFAULT_EXCLUDES = [
    # Dependency / build output
    "node_modules/",
    "dist/",
    "build/",
    "target/",
    "out/",
    ".next/",
    ".nuxt/",
    # Caches
    "__pycache__/",
    "*.pyc",
    "*.pyo",
    ".cache/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    "coverage/",
    ".coverage",
    # Virtualenvs
    ".venv/",
    "venv/",
    "env/",
    # VCS
    ".git/",
    ".hg/",
    ".svn/",
    # Worktrees (Hermes convention — don't recursively snapshot siblings)
    ".worktrees/",
    # Native / compiled binaries
    "*.so",
    "*.dylib",
    "*.dll",
    "*.o",
    "*.a",
    "*.jar",
    "*.class",
    "*.exe",
    "*.obj",
    # Media / large binaries
    "*.mp4",
    "*.mov",
    "*.mkv",
    "*.webm",
    "*.zip",
    "*.tar",
    "*.tar.gz",
    "*.tgz",
    "*.7z",
    "*.rar",
    "*.iso",
    # Secrets
    ".env",
    ".env.*",
    ".env.local",
    ".env.*.local",
    # OS junk
    ".DS_Store",
    "Thumbs.db",
    # Logs
    "*.log",
]

# Git subprocess timeout (seconds).
_GIT_TIMEOUT: int = max(10, min(60, env_int("HERMES_CHECKPOINT_TIMEOUT", 30)))

# Staging budget (seconds) for ``git add -A`` — the one git step whose cost grows
# with the tree instead of with the plumbing around it.  ``_GIT_TIMEOUT`` stays
# clamped so a cheap call can never hold a turn for minutes, but that same 60 s
# ceiling is structurally too small for the first snapshot of a large project:
# 19,992 files / 3.2 GB measured 30.9 s of pure hashing *before* any write
# amplification, so a cold object DB exceeds it every time rather than
# occasionally.  Applies to the staging call only.
_GIT_STAGE_TIMEOUT: int = max(10, min(600, env_int("HERMES_CHECKPOINT_STAGE_TIMEOUT", 180)))

# A leftover ``<index>.lock`` older than this, with no live holder, is removed
# once and the stage retried.  Git writes ``<GIT_INDEX_FILE>.lock`` and renames it
# into place at the end, so a stage that its own timeout kills leaves the lock
# behind and every later add in that project fails with
# ``fatal: Unable to create '<index>.lock': File exists`` (rc=128) — forever, and
# on the gateway path silently.  ``--ignore-errors`` does not soften it: the lock
# is fatal before any entry is read (measured, git 2.53).
_STALE_LOCK_AGE_S: int = max(0, env_int("HERMES_CHECKPOINT_STALE_LOCK_AGE", 30))

# Upper bound on generated pathspec exclusions for oversize files.  Past it the
# pre-stage filter gives up and the post-stage drop stays the only guard, so a
# pathological project cannot push the argument list at ARG_MAX.
_MAX_PATHSPEC_EXCLUDES = 2000

# Max entries to snapshot — skip huge directories to avoid slowdowns.  The count
# comes from ``_dir_file_count`` (``rglob('*')``), so it includes directories, and
# it is overridable per host because a working tree can legitimately outgrow a
# constant: the skip writes no snapshot at all.
_MAX_FILES: int = max(100, env_int("HERMES_CHECKPOINT_MAX_ENTRIES", 50_000))

# Valid git commit hash pattern: 4–40 hex chars (short or full SHA-1/SHA-256).
_COMMIT_HASH_RE = re.compile(r'^[0-9a-fA-F]{4,64}$')


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------

def _validate_commit_hash(commit_hash: str) -> Optional[str]:
    """Validate a commit hash to prevent git argument injection.

    Returns an error string if invalid, None if valid.
    Values starting with '-' would be interpreted as git flags
    (e.g., '--patch', '-p') instead of revision specifiers.
    """
    if not commit_hash or not commit_hash.strip():
        return "Empty commit hash"
    if commit_hash.startswith("-"):
        return f"Invalid commit hash (must not start with '-'): {commit_hash!r}"
    if not _COMMIT_HASH_RE.match(commit_hash):
        return f"Invalid commit hash (expected 4-64 hex characters): {commit_hash!r}"
    return None


def _validate_file_path(file_path: str, working_dir: str) -> Optional[str]:
    """Validate a file path to prevent path traversal outside the working directory.

    Returns an error string if invalid, None if valid.
    """
    if not file_path or not file_path.strip():
        return "Empty file path"
    if os.path.isabs(file_path):
        return f"File path must be relative, got absolute path: {file_path!r}"
    abs_workdir = _normalize_path(working_dir)
    resolved = (abs_workdir / file_path).resolve()
    try:
        resolved.relative_to(abs_workdir)
    except ValueError:
        return f"File path escapes the working directory via traversal: {file_path!r}"
    return None


# ---------------------------------------------------------------------------
# Path / hash helpers
# ---------------------------------------------------------------------------

def _normalize_path(path_value: str) -> Path:
    """Return a canonical absolute path for checkpoint operations."""
    return Path(path_value).expanduser().resolve()


def _project_hash(working_dir: str) -> str:
    """Deterministic per-project hash: sha256(abs_path)[:16]."""
    abs_path = str(_normalize_path(working_dir))
    return hashlib.sha256(abs_path.encode()).hexdigest()[:16]


def _store_path(base: Optional[Path] = None) -> Path:
    """Return the single shared shadow store path."""
    return (base or _resolve_checkpoint_base()) / _STORE_DIRNAME


def _store_has_head(store: Path) -> bool:
    return (store / "HEAD").exists()


def _index_path(store: Path, dir_hash: str) -> Path:
    return store / _INDEXES_DIRNAME / dir_hash


def _ledger_path(store: Path, dir_hash: str) -> Path:
    return store / _LEDGERS_DIRNAME / f"{dir_hash}.json"


def _snapshot_state_path(store: Path, dir_hash: str) -> Path:
    """Per-project last-snapshot outcome, written for read-only probes."""
    return store / _SNAPSHOT_STATE_DIRNAME / f"{dir_hash}.json"


def _hash_file(path: Path) -> Optional[str]:
    """Streaming sha256 of a file's bytes. None if unreadable/missing."""
    try:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _load_ledger(store: Path, dir_hash: str) -> dict[str, dict]:
    """Load the agent-write ledger: {relpath: {"sha256": ..., "ts": ...}}.

    The ledger records the content hash of every file the last successful
    ``write_file`` / ``patch`` produced, so restores can tell "Hermes wrote
    this" apart from "the user hand-edited this afterwards".
    """
    try:
        raw = _ledger_path(store, dir_hash).read_text(encoding="utf-8-sig")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_ledger(store: Path, dir_hash: str, ledger: dict[str, dict]) -> None:
    """Persist the agent-write ledger, capped to the newest entries."""
    try:
        if len(ledger) > _LEDGER_MAX_ENTRIES:
            newest = sorted(
                ledger.items(),
                key=lambda kv: kv[1].get("ts", 0) if isinstance(kv[1], dict) else 0,
                reverse=True,
            )[:_LEDGER_MAX_ENTRIES]
            ledger = dict(newest)
        path = _ledger_path(store, dir_hash)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(ledger), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        logger.debug("Failed to save agent-write ledger for %s", dir_hash, exc_info=True)


def _ref_name(dir_hash: str) -> str:
    return f"{_REFS_PREFIX}/{dir_hash}"


def _gitlink_paths(ls_tree_z: str) -> list[str]:
    """Paths of gitlinks (nested repositories stored as a commit reference, not
    their files) in ``git ls-tree -r -z`` output."""
    return [
        record.split("\t", 1)[1]
        for record in ls_tree_z.split("\x00")
        if record.startswith("160000 commit ") and "\t" in record
    ]


def _uncaptured_note(nested_repos: list[str]) -> str:
    """Checkpoint-message suffix naming nested repositories whose files were
    not captured, so every /rollback listing discloses it."""
    shown = ", ".join(nested_repos[:3])
    more = f" (+{len(nested_repos) - 3})" if len(nested_repos) > 3 else ""
    return f" [nested git repos not captured: {shown}{more}]"


def _project_meta_path(store: Path, dir_hash: str) -> Path:
    return store / _PROJECTS_DIRNAME / f"{dir_hash}.json"


# ---------------------------------------------------------------------------
# Git env
# ---------------------------------------------------------------------------

def _git_env(
    store: Path,
    working_dir: str,
    index_file: Optional[Path] = None,
) -> dict:
    """Build env dict that redirects git to the shared store.

    The shared store is internal Hermes infrastructure — it must NOT inherit
    the user's global or system git config.  User-level settings like
    ``commit.gpgsign = true``, signing hooks, or credential helpers would
    either break background snapshots or, worse, spawn interactive prompts
    (pinentry GUI windows) mid-session every time a file is written.

    Isolation strategy:
    * ``GIT_CONFIG_GLOBAL=<os.devnull>`` — ignore ``~/.gitconfig`` (git 2.32+).
    * ``GIT_CONFIG_SYSTEM=<os.devnull>`` — ignore ``/etc/gitconfig`` (git 2.32+).
    * ``GIT_CONFIG_NOSYSTEM=1`` — legacy belt-and-suspenders for older git.

    ``index_file``, if given, forces git to use a per-project index under
    ``store/indexes/<hash>`` so projects don't race on a shared index.
    """
    normalized_working_dir = _normalize_path(working_dir)
    # git child with hand-isolated config env; exact preservation — a HOME
    # rewrite would change which ~/.gitconfig the isolation vars are hiding.
    from tools.environments.local import build_subprocess_env

    env = selected_git_env(build_subprocess_env(scrub_secrets=False, inherit_profile_home=False))
    env["GIT_DIR"] = str(store)
    env["GIT_WORK_TREE"] = str(normalized_working_dir)
    env.pop("GIT_NAMESPACE", None)
    env.pop("GIT_ALTERNATE_OBJECT_DIRECTORIES", None)
    if index_file is not None:
        env["GIT_INDEX_FILE"] = str(index_file)
    else:
        env.pop("GIT_INDEX_FILE", None)
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _run_git(
    args: list[str],
    store: Path,
    working_dir: str,
    timeout: int = _GIT_TIMEOUT,
    allowed_returncodes: Optional[set[int]] = None,
    index_file: Optional[Path] = None,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[bool, str, str]:
    """Run a git command against the shared store.  Returns (ok, stdout, stderr).

    ``allowed_returncodes`` suppresses error logging for known/expected non-zero
    exits while preserving the normal ``ok = (returncode == 0)`` contract.
    Example: ``git diff --cached --quiet`` returns 1 when changes exist.
    """
    normalized_working_dir = _normalize_path(working_dir)
    if not normalized_working_dir.exists():
        msg = f"working directory not found: {normalized_working_dir}"
        logger.error("Git command skipped: %s (%s)", " ".join(["git"] + list(args)), msg)
        return False, "", msg
    if not normalized_working_dir.is_dir():
        msg = f"working directory is not a directory: {normalized_working_dir}"
        logger.error("Git command skipped: %s (%s)", " ".join(["git"] + list(args)), msg)
        return False, "", msg

    env = _git_env(store, str(normalized_working_dir), index_file=index_file)
    if extra_env:
        env.update(extra_env)
    git = shutil.which("git", path=env.get("PATH", ""))
    if git is None:
        return False, "", "git is not installed or not on PATH"
    cmd = [git, *args]
    allowed_returncodes = allowed_returncodes or set()

    try:
        # NUL-delimited git output contains literal filenames, not text lines.
        text_options = {} if "-z" in args else {"text": True, "encoding": "utf-8", "errors": "replace"}
        result = subprocess.run(
            cmd,
            capture_output=True,
            **text_options,
            timeout=timeout,
            env=env,
            cwd=str(normalized_working_dir),
            stdin=subprocess.DEVNULL,
            # Checkpoints fire several bare git calls per turn from the
            # console-less desktop/gateway backend; suppress the per-call
            # conhost flash on Windows (no-op on POSIX).
            creationflags=windows_hide_flags(),
            check=False,
        )
        ok = result.returncode == 0
        stdout = os.fsdecode(result.stdout) if "-z" in args else result.stdout.strip()
        stderr = result.stderr.decode("utf-8", errors="replace").strip() if "-z" in args else result.stderr.strip()
        if not ok and result.returncode not in allowed_returncodes:
            logger.error(
                "Git command failed: %s (rc=%d) stderr=%s",
                " ".join(cmd), result.returncode, stderr,
            )
        return ok, stdout, stderr
    except subprocess.TimeoutExpired:
        msg = f"git timed out after {timeout}s: {' '.join(cmd)}"
        logger.error(msg, exc_info=True)
        return False, "", msg
    except FileNotFoundError as exc:
        missing_target = getattr(exc, "filename", None)
        if missing_target == "git":
            logger.error("Git executable not found: %s", " ".join(cmd), exc_info=True)
            return False, "", "git not found"
        msg = f"working directory not found: {normalized_working_dir}"
        logger.error("Git command failed before execution: %s (%s)", " ".join(cmd), msg, exc_info=True)
        return False, "", msg
    except Exception as exc:
        logger.error("Unexpected git error running %s: %s", " ".join(cmd), exc, exc_info=True)
        return False, "", str(exc)


def _git_out(args: list[str], store: Path, working_dir: str, rc: Optional[set[int]] = None) -> str:
    """stdout of a successful git call, else ``""``."""
    ok, out, _ = _run_git(args, store, working_dir, allowed_returncodes=rc)
    return out if ok else ""


# ``git add`` complaints that are entry-level (one path) rather than fatal to the
# project.  ``--ignore-errors`` still exits non-zero for them but writes the
# index, so a snapshot with those entries skipped is usable.
_ENTRY_LEVEL_ADD_ERR_RE = re.compile(
    r"^error: (?:open\(|unable to (?:index|stat) |\S+ does not have a commit checked out$)"
)
# 结构性命中判据（v3b-R1 + R1′ 引号段感知）：一条 git 消息通常占一行。若同一行里在
# 消息边界（行首 / ';' / '|'）之后出现第 2 个消息令牌，则该行是拼接产物 ⇒ 整项 fail-closed。
# 令牌表沿用本文件既有口径（与 `_COMPLAINT_LINE_RE` 同族，大小写不敏感）。
# 只按「消息边界 + 令牌」判，不枚举毒词 ⇒ 不漏防表外措辞。
#
# R1′（kimi-Q8 引号盲区修）：令牌只在**引号段之外**计数。git 把路径名放在引号里
# —— `error: open("<path>")`（双引号 C-style）、`error: unable to index file '<path>'`
# 与 `error: '<path>' does not have a commit checked out`（单引号）—— 而路径名是本判据
# 唯一不掌控的输入：文件名合法地含 `; fatal: `（实测 git 2.53：`a; fatal: b.txt` 可
# add 可 commit）。git 打印 `error: open("a; fatal: b.txt"): Permission denied`，段内
# 那个 `; fatal:` 落在引号段内、并不是第二条消息；按原口径却被数成第 2 个令牌 ⇒
# 整项判非条目级 ⇒ 该工作区**新快照静默全停**（fail-closed 向，非误拍）。
# 屏蔽引号段后再计数 ⇒ 该行恢复为条目级。
#
# 方向性（不得引入 fail-open）：
#   ① 与 `open(`/`unable to …` 同型的**拼接**仍须判非条目级 —— 引号按「同类型、就近成对」
#      匹配：`error: open("a"): Permission denied; fatal: …` 中 `; fatal:` 落在成对引号
#      之外 ⇒ 照旧数到 2 个令牌 ⇒ fail-closed（实测 G1/G2/Q1/Q2 全部保持）。
#   ② **未闭合**的引号一律不屏蔽（整行照原样计数）；路径名内插入引号会打断配对 ⇒ 其后
#      内容照常计数。故「引号藏毒」不能借屏蔽蒙混过关。
_MSG_TOKEN_BOUNDARY_RE = re.compile(
    r"(?i)(?:^|;\s*|\|\s*)(?:error|fatal|warning|usage|bug):"
)


def _outside_quotes(line: str) -> str:
    """*line* 里 git 引号包裹的路径段被空格屏蔽掉后的文本（R1′ 用）。

    git 只在少数几种消息里给路径加引号，本判据不去枚举那几种消息（那会把 R1 退回
    「枚举措辞」的旧病），而是按**引号本身的配对**屏蔽：自左向右扫，遇到 `"` 或 `'`
    就与**同类型的下一个**该字符成对，中间内容一律屏蔽。

    「就近成对」而非「贪心到最后一个」是刻意的：路径名内若嵌了同类引号（`q"uote`），
    配对会被打断，其后内容（可能含 `; fatal: `）照常参与计数 ⇒ 拼接产物仍然 fail-closed。
    整行里任一同类型引号找不到配对 ⇒ 原样返回整行（不屏蔽任何内容）—— 未闭合引号绝不
    允许把消息令牌藏起来。
    """
    spans: List[Tuple[int, int]] = []
    i = 0
    while i < len(line):
        ch = line[i]
        if ch != '"' and ch != "'":
            i += 1
            continue
        end = line.find(ch, i + 1)
        if end == -1:
            return line  # 未闭合 ⇒ 不屏蔽（fail-closed）
        spans.append((i, end + 1))
        i = end + 1
    if not spans:
        return line
    out = list(line)
    for start, end in spans:
        for k in range(start, end):
            out[k] = " "
    return "".join(out)


_BENIGN_ADD_ERR = "working directory not found"

# ``git add`` prints this advisory and exits 1 when the pathspec *names* a path a
# .gitignore already ignores — including when the name arrives as an
# ``:(exclude,literal)`` entry, which is exactly how the pre-stage size filter is
# built.  It is not a stage failure: the other entries are staged and the index is
# written (measured, git 2.53).  Unrecognised, it turned one ignored oversize file
# into ``git_add_failed`` for the whole project, whose snapshots then stopped
# entirely while the log said only "ignored by one of your .gitignore files".
_IGNORED_PATHS_ADVICE_RE = re.compile(
    r"^The following paths are ignored by one of your \.gitignore files:"
)
_IGNORED_ADVICE_HINT_RE = re.compile(r"^hint: ")
# A line git prints as a complaint rather than as one of the advisory's path
# lines.  Such a line always ends the advisory's path run — and it must: with
# ``advice.addIgnoredFile`` false git prints the header and the path lines and
# **no** ``hint:`` lines at all, so a run that only ended on ``hint:``/EOF would
# swallow a genuine failure printed after the advisory (a corrupted object DB, a
# full disk) and report the project as partially snapshotted.
# A file whose *name* starts with ``error:``/``fatal:`` is therefore read as a
# complaint instead of as an ignored path: the stage then stays fail-closed, which
# is the safe direction (no wrong snapshot; the pre-stage filter in
# :meth:`_oversize_excludes` is what removes the advisory in the first place).
_COMPLAINT_LINE_RE = re.compile(r"^(?:error|fatal|warning|usage|BUG):")

# （乙取向 · 2026-10-12）git 的**通知行**：``warning:`` 前缀（如嵌套仓
# ``warning: adding embedded git repository: <dir>``）与 ``hint:`` 前缀（其
# 配套建议块）。它们描述的是"git 照常完成了什么"，不是 stage 失败的原因：
# 嵌套仓本身仍被加入 index（gitlink），快照可用。把它们计入 complaints 会让
# ``_stage_partial`` 的行首锚定正则必然不匹配 ⇒ 整项 False ⇒ 快照全停——
# 这是真实缺陷（10:15 对含 gtest1–4 嵌套仓的 take 全停，见母卡 t_9f4b5694）。
# 判据沿用既有口径：**行首前缀**即通知，不看路径内容。行内出现第 2 个消息
# 令牌（``;``/``|`` 拼接）时仍 fail-closed —— 与 ``_MSG_TOKEN_BOUNDARY_RE``
# 同一道护栏，防通知行被嫁接致命消息。
_GIT_NOTICE_LINE_RE = re.compile(r"^(?:warning|hint):")
# 通知行的行内拼接护栏：``;``/``|`` 边界之后出现第 2 个消息令牌 ⇒ 该行是
# 嫁接产物（通知行 + 致命消息拼接），保留为 complaint ⇒ fail-closed。
_MSG_MIDLINE_TOKEN_RE = re.compile(
    r"(?i)(?:;\s*|\|\s*)(?:error|fatal|warning|usage|bug):"
)


def _index_lock_blocked(stderr: str) -> bool:
    """Whether a stage failure is the ``<index>.lock: File exists`` dead end.

    Matched on git's own wording rather than on a filename: the lock lives at
    ``store/indexes/<hash>.lock``, so searching for the literal ``index.lock``
    never matches the real message.
    """
    text = stderr or ""
    return bool(re.search(r"Unable to create .*\.lock", text)) and bool(
        re.search(r"File exists", text)
    )


def _parse_stage_stderr(stderr: str) -> Tuple[List[str], List[str]]:
    """Split failed ``git add`` stderr into (complaints, ignored-path names).

    The ignored-paths advisory is consumed whole — its header, the path lines it
    lists, then git's own ``hint:`` lines — so a genuine error printed next to it
    is still returned as a complaint.  Matching on git's wording rather than on
    any path keeps every other message in the fail-closed set.

    The path-line run ends at the first line that is not a path: a ``hint:`` line,
    a complaint (``error:``/``fatal:``/…), or the end of the message.  Ending it on
    ``hint:`` alone would be a failure-open hole — ``advice.addIgnoredFile=false``
    (settable in the store's own config, which ``_git_env`` does not mask) makes
    git print the header and the paths with **no** hint lines, and everything
    printed after the advisory would then be read as ignored paths.
    """
    # NOTE (measured, git 2.53): the advisory's path lines are git's own
    # short-form pathspec echo — for a path that is ignored, that is the plain
    # relative path (no ``:(exclude,literal)`` prefix, no leading ``./``), which is
    # what :meth:`_oversize_excludes` names in the pathspec.
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    complaints: List[str] = []
    ignored: List[str] = []
    index = 0
    while index < len(lines):
        if _IGNORED_PATHS_ADVICE_RE.match(lines[index]):
            index += 1
            while index < len(lines) and not (
                _IGNORED_ADVICE_HINT_RE.match(lines[index])
                or _COMPLAINT_LINE_RE.match(lines[index])
            ):
                if lines[index] not in ignored:
                    ignored.append(lines[index])  # paths: ignored, not staged
                index += 1
            while index < len(lines) and _IGNORED_ADVICE_HINT_RE.match(lines[index]):
                index += 1  # "Use -f …" / "Disable this message …"
            continue
        # （乙取向）git 通知行（``warning:``/``hint:`` 前缀）不是 complaint：
        # 嵌套仓仍被加入 index，快照可用。行内拼接第 2 个消息令牌
        # （``;``/``|`` 边界后出现 ``error:``/``fatal:`` 等）时仍保留为
        # complaint ⇒ fail-closed（与 `_MSG_TOKEN_BOUNDARY_RE` 同一道护栏）。
        if _GIT_NOTICE_LINE_RE.match(lines[index]):
            if _MSG_MIDLINE_TOKEN_RE.search(lines[index]):
                complaints.append(lines[index])
            index += 1
            continue
        complaints.append(lines[index])
        index += 1
    return complaints, ignored


def _stage_partial(stderr: str) -> bool:
    """Whether a stage skipped only entry-level problems.

    Every complaint has to be entry-level: a broken object DB, a full disk or a
    bad pathspec must keep the stage failing closed.  The ignored-paths advisory
    is not a complaint (see :func:`_parse_stage_stderr`): an ignored path is
    outside the commit by design, and git still stages every other entry when it
    prints that advisory (measured, git 2.53), so the snapshot is partial rather
    than failed.  An advisory alongside a real complaint is not tolerated.
    """
    complaints, ignored = _parse_stage_stderr(stderr)
    if not complaints:
        return bool(ignored)
    return all(
        _ENTRY_LEVEL_ADD_ERR_RE.match(line)
        and len(_MSG_TOKEN_BOUNDARY_RE.findall(_outside_quotes(line))) < 2
        for line in complaints
    )


def _skipped_entries(stderr: str) -> List[str]:
    """The paths a tolerated stage problem left out, for the warning."""
    out: List[str] = []
    for line in (stderr or "").splitlines():
        match = (
            re.search(r"open\(\"(.*?)\"\)", line)
            or re.search(r"unable to stat '(.*?)'", line)
            or re.search(r"error: '(.*)' does not have a commit checked out", line)
        )
        if match and match.group(1) not in out:
            out.append(match.group(1))
    for rel in _parse_stage_stderr(stderr)[1]:
        if rel not in out:
            out.append(rel)
    return out


def _ignored_paths(store: Path, working_dir: str, rel_paths: List[str]) -> Set[str]:
    """The subset of *rel_paths* the project's ignore rules already exclude.

    The pre-stage size filter hands oversize paths to ``git add`` as
    ``:(exclude,literal)`` entries.  Git takes that for a path it would otherwise
    add, but exits **1** printing the ignored-paths advisory when the path is
    already ignored — so such a path must not be named in the pathspec at all.
    It is out of the commit either way, so dropping it from the list leaves the
    snapshot identical and removes the failure.

    ``git check-ignore`` exits 0 when at least one argument matched the ignore
    rules and 1 when none did; any other outcome (git missing, timeout, usage
    error) means the answer is unknown, so nothing is dropped and the caller
    keeps its fail-closed behaviour.  Paths are passed as arguments, not on stdin:
    ``-z`` is stdin-only in git, and ``_run_git`` feeds git an empty stdin.
    """
    if not rel_paths:
        return set()
    ok, out, err = _run_git(
        ["check-ignore", "--", *rel_paths],
        store, working_dir,
        allowed_returncodes={1},
    )
    if not ok:
        if err:
            logger.debug(
                "Checkpoint: check-ignore unavailable for %s, keeping every oversize "
                "path in the pathspec: %s", working_dir, err,
            )
        return set()
    return {line.strip() for line in out.splitlines() if line.strip()}


def _lock_holder_visible(lock_path: Path) -> bool:
    """Whether any process this user can inspect holds *lock_path* open.

    ``/proc/<pid>/fd`` is readable for this user's own processes, and they are the
    only ones that stage into this user's store.  When ``/proc`` is unavailable
    (non-Linux host) or cannot be walked, the answer is "held": an unverifiable
    lock is left alone rather than deleted on a guess.
    """
    target = str(lock_path)
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return True
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return True
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            fds = list((entry / "fd").iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                if os.readlink(fd) == target:
                    return True
            except OSError:
                continue
    return False


def _removable_stale_lock(index_file: Path) -> Optional[Path]:
    """The stale ``<index>.lock`` blocking *index_file*, when it is safe to delete.

    Both conditions must hold.  Age alone would delete a live writer's lock — an
    add over a large tree legitimately holds it for minutes; holder detection
    alone would delete the lock a just-killed stage left a moment ago and race
    the writer that is about to take it.  ``None`` means leave the lock alone.
    """
    lock = Path(str(index_file) + ".lock")
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return None
    if age < _STALE_LOCK_AGE_S:
        return None
    if _lock_holder_visible(lock):
        return None
    return lock


def _ref_tip(store: Path, working_dir: str, ref: str) -> Optional[str]:
    """Commit sha at ``ref``, or None when the ref does not exist yet."""
    return _git_out(["rev-parse", "--verify", ref + "^{commit}"], store, working_dir, {128}) or None


def _list_project_refs(store: Path, working_dir: str) -> list[str]:
    out = _git_out(["for-each-ref", "--format=%(refname)", _REFS_PREFIX], store, working_dir, {128})
    return [r for r in out.splitlines() if r.strip()]


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Store initialisation + legacy migration
# ---------------------------------------------------------------------------

def _migrate_legacy_store(base: Path) -> Optional[Path]:
    """Move pre-v2 per-project shadow repos into a ``legacy-<ts>/`` dir.

    The pre-v2 layout had one shadow git repo per working directory directly
    under ``CHECKPOINT_BASE``.  The v2 layout wants a single ``store/`` dir.
    Rather than delete the old data (users might want to recover), rename
    everything except our own v2 entries into ``legacy-<timestamp>/``.  The
    legacy dir is subject to the same retention sweep and can be manually
    cleared with ``hermes checkpoints clear-legacy``.

    Returns the legacy-archive path, or None if nothing to migrate.
    """
    if not base.exists():
        return None
    store = _store_path(base)
    legacy_root: Optional[Path] = None
    # Reserved top-level entries managed by v2.
    reserved = {_STORE_DIRNAME, _PRUNE_MARKER_NAME}
    for child in list(base.iterdir()):
        name = child.name
        if name in reserved or name.startswith(_LEGACY_PREFIX):
            continue
        # Candidate: pre-v2 shadow repo (has HEAD) OR stray dir.  Either way
        # we archive it so v2 starts clean.
        if legacy_root is None:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            legacy_root = base / f"{_LEGACY_PREFIX}{stamp}"
            try:
                legacy_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("Could not create legacy archive dir: %s", exc)
                return None
        dest = legacy_root / name
        try:
            shutil.move(str(child), str(dest))
        except OSError as exc:
            logger.warning("Could not archive legacy checkpoint %s: %s", child, exc)
    # If the store still hasn't been created, create it here.
    _ = store
    if legacy_root is not None:
        logger.info(
            "Migrated pre-v2 checkpoint repos to %s. "
            "Clear with `hermes checkpoints clear-legacy` when safe.",
            legacy_root,
        )
    return legacy_root


def _init_store(store: Path, working_dir: str) -> Optional[str]:
    """Initialise the shared shadow store if needed.  Returns error or None.

    Also performs one-time migration of pre-v2 per-directory shadow repos
    into ``legacy-<timestamp>/``.
    """
    base = store.parent
    # One-time legacy migration before we create the store.
    if not store.exists():
        try:
            base.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return f"Could not create checkpoint base: {exc}"
        # Only migrate if the base dir has pre-existing content that isn't
        # our own v2 layout.
        _migrate_legacy_store(base)

    if (store / "HEAD").exists():
        return None

    store.mkdir(parents=True, exist_ok=True)
    (store / _INDEXES_DIRNAME).mkdir(exist_ok=True)
    (store / _PROJECTS_DIRNAME).mkdir(exist_ok=True)

    # ``git init --bare`` rejects GIT_WORK_TREE, so we can't use _run_git
    # here (which always sets GIT_DIR + GIT_WORK_TREE).  Use a raw
    # subprocess with just the config-isolation env vars.
    from tools.environments.local import build_subprocess_env

    init_env = selected_git_env(build_subprocess_env(scrub_secrets=False, inherit_profile_home=False))
    init_env["GIT_CONFIG_GLOBAL"] = os.devnull
    init_env["GIT_CONFIG_SYSTEM"] = os.devnull
    init_env["GIT_CONFIG_NOSYSTEM"] = "1"
    # Drop any inherited GIT_* that would interfere.
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_NAMESPACE",
              "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        init_env.pop(k, None)
    try:
        git = shutil.which("git", path=init_env.get("PATH", ""))
        if git is None:
            return "Shadow store init failed: git is not installed or not on PATH"
        result = subprocess.run(
            [git, "init", "--bare", str(store)],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            env=init_env, timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
            creationflags=windows_hide_flags(),
            check=False,
        )
        if result.returncode != 0:
            return f"Shadow store init failed: {result.stderr.strip()}"
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        return f"Shadow store init failed: {exc}"

    # Per-store config (isolated by env vars above, but belt-and-suspenders).
    # Use the base dir as the working_dir for config commands — it always
    # exists since we just created the store inside it.
    cfg_wd = str(base)
    _run_git(["config", "user.email", "hermes@local"], store, cfg_wd)
    _run_git(["config", "user.name", "Hermes Checkpoint"], store, cfg_wd)
    _run_git(["config", "commit.gpgsign", "false"], store, cfg_wd)
    _run_git(["config", "tag.gpgSign", "false"], store, cfg_wd)
    _run_git(["config", "gc.auto", "0"], store, cfg_wd)

    info_dir = store / "info"
    info_dir.mkdir(exist_ok=True)
    (info_dir / "exclude").write_text(
        "\n".join(DEFAULT_EXCLUDES) + "\n", encoding="utf-8"
    )

    logger.debug("Initialised checkpoint store at %s", store)
    return None


def _volume_evidence(workdir: Path) -> dict:
    """Record the identity of ``workdir``'s parent while the project is live.

    ``(st_dev, st_ino)`` of the parent directory, captured at a moment when
    the workdir itself is reachable, identifies the *directory* — not just
    the path.  A mount point resolves to the mounted filesystem's root while
    the volume is attached and to the underlying (underlay) directory after
    unmount: same path, different directory, different ``(st_dev, st_ino)``.
    Orphan pruning uses this to distinguish "the project was deleted out of
    the directory we knew" from "a different directory is now visible at
    that path because the volume is detached".

    Returns ``{}`` when the workdir is not currently reachable, when the
    filesystem does not provide a usable directory identity (a zero
    ``st_dev`` or ``st_ino`` — e.g. Windows filesystems without file IDs and
    some network shares), or when the probe fails — callers treat all of
    these as "no evidence recorded" and orphan pruning stays conservative
    for the project (never classified as orphan; retention still applies).
    """
    try:
        if not workdir.exists():
            return {}
        st = workdir.parent.stat()
        if not st.st_dev or not st.st_ino:
            return {}
        return {
            "workdir_parent_dev": st.st_dev,
            "workdir_parent_ino": st.st_ino,
        }
    except OSError:
        return {}


def _register_project(store: Path, working_dir: str) -> None:
    """Create or update ``projects/<hash>.json`` with workdir + timestamps."""
    dir_hash = _project_hash(working_dir)
    meta_path = _project_meta_path(store, dir_hash)
    now = time.time()
    meta: dict = {"workdir": str(_normalize_path(working_dir)),
                  "created_at": now, "last_touch": now}
    evidence = _volume_evidence(_normalize_path(working_dir))
    if evidence:
        meta.update(evidence)
    if meta_path.exists():
        try:
            existing = json.loads(meta_path.read_text(encoding="utf-8-sig"))
            if isinstance(existing, dict):
                meta["created_at"] = existing.get("created_at", now)
                if not evidence:
                    # Fresh probe failed — keep the previously recorded
                    # parent identity rather than dropping it. Stale evidence
                    # only makes pruning MORE conservative (mismatch => not
                    # an orphan).
                    for key in ("workdir_parent_dev", "workdir_parent_ino"):
                        if key in existing:
                            meta[key] = existing[key]
        except (OSError, ValueError):
            pass
    try:
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not write project metadata %s: %s", meta_path, exc)


def _touch_project(store: Path, working_dir: str) -> None:
    """Update last_touch for a project, preserving created_at."""
    dir_hash = _project_hash(working_dir)
    meta_path = _project_meta_path(store, dir_hash)
    if not meta_path.exists():
        _register_project(store, working_dir)
        return
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta["workdir"] = str(_normalize_path(working_dir))
    meta["last_touch"] = time.time()
    meta.setdefault("created_at", meta["last_touch"])
    # Refresh the parent-directory identity while the project is observably
    # live — a remount can legitimately change it (new device, new inode).
    # On probe failure the previous evidence is kept: stale evidence can only
    # make pruning MORE conservative (mismatch => not an orphan).
    evidence = _volume_evidence(_normalize_path(working_dir))
    if evidence:
        meta.update(evidence)
    try:
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except OSError as exc:
        logger.debug("Could not update project metadata %s: %s", meta_path, exc)


def _list_projects(store: Path) -> list[dict]:
    """Return all registered projects under the store."""
    projects_dir = store / _PROJECTS_DIRNAME
    if not projects_dir.exists():
        return []
    out: list[dict] = []
    for meta_path in projects_dir.glob("*.json"):
        dir_hash = meta_path.stem
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        meta["_hash"] = dir_hash
        out.append(meta)
    return out


def _pre_v2_shadow_repos(base: Path) -> list[dict]:
    """Return pre-v2 per-project shadow repos still directly under ``base``.

    Pre-v2 layout kept one shadow git repo per working directory directly
    under ``CHECKPOINT_BASE`` (identified by a ``HEAD`` file).  This is the
    single source of truth for that scan so a preview built from it (e.g.
    ``store_status``) always matches what ``prune_checkpoints`` deletes.
    """
    out: list[dict] = []
    if not base.exists():
        return out
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if child.name == _STORE_DIRNAME or child.name.startswith(_LEGACY_PREFIX):
            continue
        if not (child / "HEAD").exists():
            continue
        workdir: Optional[str] = None
        marker_unreadable = False
        wd_marker = child / "HERMES_WORKDIR"
        if wd_marker.exists():
            try:
                workdir = wd_marker.read_text(encoding="utf-8-sig").strip()
            except (OSError, UnicodeDecodeError):
                # The marker is there, we just could not read it. That is
                # not evidence the project is gone — never delete on it.
                workdir = None
                marker_unreadable = True
        out.append({
            "path": child,
            "workdir": workdir,
            "exists": bool(workdir) and Path(workdir).exists(),
            "marker_unreadable": marker_unreadable,
        })
    return out


def _dir_file_count(path: str) -> int:
    """Quick entry-count estimate (stops early if over _MAX_FILES).

    Counts every entry ``rglob('*')`` yields — directories included — because
    that is what the guard's ceiling has always been read against.  Returns at
    most ``_MAX_FILES + 1``, so a caller never sees the true count of a tree the
    guard rejects.
    """
    count = 0
    try:
        for _ in Path(path).rglob("*"):
            count += 1
            if count > _MAX_FILES:
                return count
    except (PermissionError, OSError):
        pass
    return count


def _dir_size_bytes(path: Path) -> int:
    """Best-effort recursive size in bytes.  Returns 0 on error."""
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


# ---------------------------------------------------------------------------
# CheckpointManager
# ---------------------------------------------------------------------------

class CheckpointManager:
    """Manages automatic filesystem checkpoints.

    Designed to be owned by AIAgent.  Call ``new_turn()`` at the start of
    each conversation turn and ``ensure_checkpoint(dir, reason)`` before
    any file-mutating tool call.  The manager deduplicates so at most one
    snapshot is taken per directory per turn.

    Parameters
    ----------
    enabled : bool
        Master switch (from config / CLI flag).
    max_snapshots : int
        Keep at most this many checkpoints per directory.
    max_total_size_mb : int
        Hard ceiling on total store size.  Oldest checkpoints per project
        are dropped when the store exceeds this after a commit.
    max_file_size_mb : int
        Skip adding any single file larger than this to a checkpoint.
        (Implemented via ``.gitignore`` excludes + a post-stage size check.)
    """

    def __init__(
        self,
        enabled: bool = False,
        max_snapshots: int = 20,
        max_total_size_mb: int = 500,
        max_file_size_mb: int = 10,
    ):
        self.enabled = enabled
        self.max_snapshots = max(1, int(max_snapshots))
        self.max_total_size_mb = max(0, int(max_total_size_mb))
        self.max_file_size_mb = max(0, int(max_file_size_mb))
        self._checkpointed_dirs: set[str] = set()
        # (project, failure code) pairs already reported at WARNING this session —
        # a permanently failing project must be visible without spamming a line
        # per write.
        self._warned_snapshot_problems: set[tuple[str, str]] = set()
        # One report per process for "the whole store is over its budget": the
        # condition is host-wide, so warning per take would drown the log.
        self._warned_store_over_cap = False

    # ------------------------------------------------------------------
    # Turn lifecycle
    # ------------------------------------------------------------------

    def new_turn(self) -> None:
        """Reset per-turn dedup.  Call at the start of each agent iteration."""
        self._checkpointed_dirs.clear()

    def unsupported_backend_reason(self, task_id: str = "default") -> Optional[str]:
        """Explain why host checkpoints are off limits for a container-backed session.

        Classifies the task's backend at call time (nothing is remembered), so /rollback is
        refused before the first mutation and follows a backend change within the session."""
        from tools.file_tools_paths import container_backend_for_task
        backend = container_backend_for_task(task_id)
        if backend is None:
            return None
        return (
            f"Checkpoints are not taken for terminal.backend={backend}: "
            "file paths belong to the container, not this host."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record_agent_write(self, file_path: str) -> None:
        """Record the content hash of a file Hermes just successfully wrote.

        Feeds the agent-write ledger used by :meth:`restore` in safe mode:
        at restore time, a file whose current content no longer matches the
        recorded hash was hand-edited by the user after Hermes last touched
        it, and is skipped instead of clobbered.

        Never raises — the ledger is best-effort bookkeeping.
        """
        if not self.enabled:
            return
        try:
            from tools.checkpoint_pruning import store_lock

            path = _normalize_path(file_path)
            digest = _hash_file(path)
            if digest is None:
                return
            with store_lock(_resolve_checkpoint_base()):
                store = _store_path()
                dir_hash = self._ledger_key(str(path))
                ledger = _load_ledger(store, dir_hash)
                ledger[str(path)] = {"sha256": digest, "ts": time.time()}
                _save_ledger(store, dir_hash, ledger)
        except Exception as exc:
            logger.debug("record_agent_write failed for %s: %s", file_path, exc)

    def _safe_restore_plan(self, working_dir: str, commit_hash: str) -> dict:
        """Classify files changed since ``commit_hash`` for a safe restore.

        Returns ``{"success", "restore": [rel...], "skipped": [rel...],
        "error"?}`` where ``restore`` lists files whose current content
        still matches what Hermes last wrote (per the agent-write ledger)
        and ``skipped`` lists files the user hand-edited after Hermes'
        last write or that Hermes never wrote at all.
        """
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))
        store = _store_path()
        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        # Stage the current tree so the name-only diff sees new files too.
        _run_git(["add", "-A"], store, abs_dir,
                 timeout=_GIT_STAGE_TIMEOUT, index_file=index_file)
        ok, names_out, err = _run_git(
            ["diff", "--name-only", "-z", commit_hash, "--cached"],
            store, abs_dir, index_file=index_file,
        )
        # Reset the index back to the project ref so it doesn't drift.
        _run_git(["read-tree", _ref_name(dir_hash)], store, abs_dir,
                 index_file=index_file, allowed_returncodes={128})
        if not ok:
            return {"success": False, "error": f"Could not compute changed files: {err}"}

        # Read the same marker-walked project key as record_agent_write.
        ledger = _load_ledger(store, self._ledger_key(abs_dir))
        if not ledger:
            # No agent-write ledger yet (pre-existing store, or Hermes has
            # not written any files here since the ledger was introduced).
            # Signal callers to fall back to a full restore rather than
            # skipping every file.
            return {"success": True, "restore": [], "skipped": [],
                    "ledger_empty": True}
        restore: list[str] = []
        skipped: list[str] = []
        for rel in filter(None, names_out.split("\x00")):
            abs_path = Path(abs_dir) / rel
            entry = ledger.get(str(abs_path))
            recorded = entry.get("sha256") if isinstance(entry, dict) else None
            if recorded is None:
                # Hermes never wrote this file (or the ledger predates it) —
                # do not touch it in safe mode.
                skipped.append(rel)
                continue
            current = _hash_file(abs_path)
            if current is None:
                # File deleted since Hermes wrote it: restoring it back is
                # safe — its last content was Hermes-authored.
                restore.append(rel)
            elif current == recorded:
                restore.append(rel)
            else:
                skipped.append(rel)
        return {"success": True, "restore": restore, "skipped": skipped}

    def ensure_checkpoint(self, working_dir: str, reason: str = "auto") -> bool:
        """Take a checkpoint if enabled and not already done this turn.

        Returns True if a checkpoint was taken, False otherwise.
        Never raises — all errors are silently logged.
        """
        if not self.enabled:
            return False

        abs_dir = str(_normalize_path(working_dir))

        # Skip root, home, and other overly broad directories
        if abs_dir in {"/", str(Path.home())}:
            logger.debug("Checkpoint skipped: directory too broad (%s)", abs_dir)
            return False

        if abs_dir in self._checkpointed_dirs:
            return False

        self._checkpointed_dirs.add(abs_dir)

        try:
            from tools.checkpoint_pruning import store_lock

            with store_lock(_resolve_checkpoint_base()):
                return self._take(abs_dir, reason)
        except Exception as e:
            logger.debug("Checkpoint failed (non-fatal): %s", e)
            return False

    def list_checkpoints(self, working_dir: str) -> list[dict]:
        """List available checkpoints for a directory (most recent first)."""
        abs_dir = str(_normalize_path(working_dir))
        store = _store_path()

        if not (store / "HEAD").exists():
            return []

        ref = _ref_name(_project_hash(abs_dir))
        ok, stdout, _ = _run_git(
            ["log", ref, "--format=%H|%h|%aI|%s", "-n", str(self.max_snapshots)],
            store, abs_dir,
            allowed_returncodes={128, 129},
        )

        if not ok or not stdout:
            return []

        results: list[dict] = []
        for line in stdout.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                entry = {
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "timestamp": parts[2],
                    "reason": parts[3],
                    "files_changed": 0,
                    "insertions": 0,
                    "deletions": 0,
                }
                stat_ok, stat_out, _ = _run_git(
                    ["diff", "--shortstat", f"{parts[0]}~1", parts[0]],
                    store, abs_dir,
                    allowed_returncodes={128, 129},
                )
                if stat_ok and stat_out:
                    self._parse_shortstat(stat_out, entry)
                results.append(entry)
        return results

    def list_all_checkpoints(self) -> list[dict]:
        """List checkpoints across every registered project (most recent first).

        Surgical reapply of PR #10633 by @nightq (#10505) onto the v2
        single-store layout: iterate ``projects/<hash>.json`` metadata via
        ``_list_projects`` instead of the pre-v2 per-shadow-dir scan. Each
        entry carries the extra ``workdir`` key so callers can label which
        project a checkpoint belongs to.
        """
        store = _store_path()
        if not (store / "HEAD").exists():
            return []
        results: list[dict] = []
        for meta in _list_projects(store):
            workdir = meta.get("workdir") or ""
            if not workdir:
                continue
            for entry in self.list_checkpoints(workdir):
                entry["workdir"] = workdir
                results.append(entry)
        results.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return results

    @staticmethod
    def _parse_shortstat(stat_line: str, entry: dict) -> None:
        """Parse git --shortstat output into entry dict."""
        m = re.search(r'(\d+) file', stat_line)
        if m:
            entry["files_changed"] = int(m.group(1))
        m = re.search(r'(\d+) insertion', stat_line)
        if m:
            entry["insertions"] = int(m.group(1))
        m = re.search(r'(\d+) deletion', stat_line)
        if m:
            entry["deletions"] = int(m.group(1))

    def diff(self, working_dir: str, commit_hash: str) -> dict:
        """Show diff between a checkpoint and the current working tree."""
        from tools.checkpoint_pruning import PruneError, store_lock

        try:
            with store_lock(_resolve_checkpoint_base()):
                return self._diff(working_dir, commit_hash)
        except (PruneError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    def _diff(self, working_dir: str, commit_hash: str) -> dict:
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))
        store = _store_path()

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, _err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found"}

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        # Stage current state into the per-project index to compare.
        _run_git(["add", "-A"], store, abs_dir,
                 timeout=_GIT_STAGE_TIMEOUT, index_file=index_file)

        ok_stat, stat_out, _ = _run_git(
            ["diff", "--stat", commit_hash, "--cached"],
            store, abs_dir, index_file=index_file,
        )
        ok_diff, diff_out, _ = _run_git(
            ["diff", commit_hash, "--cached", "--no-color"],
            store, abs_dir, index_file=index_file,
        )

        # Reset staged tree back to the project's last checkpoint so the
        # index doesn't drift out of sync with the ref.
        ref = _ref_name(dir_hash)
        _run_git(["read-tree", ref], store, abs_dir,
                 index_file=index_file,
                 allowed_returncodes={128})

        if not ok_stat and not ok_diff:
            return {"success": False, "error": "Could not generate diff"}

        return {
            "success": True,
            "stat": stat_out if ok_stat else "",
            "diff": diff_out if ok_diff else "",
        }

    def session_diff(self, working_dir: str) -> dict:
        """Show the cumulative diff of everything changed in this directory.

        This powers ``/diff session``.  It answers "what has Hermes changed
        here?" by diffing the *earliest retained checkpoint* — the snapshot
        taken before the first recorded edit — against the current working
        tree.  Because checkpoints are captured just before each file-mutating
        tool call, that baseline is the pre-edit state, so the diff covers the
        first edit and everything after it.

        Note: checkpoints are a persistent per-project ref, so the earliest
        *retained* checkpoint may predate the current session (or, after
        pruning, postdate its true start).  It is an approximation of "what
        Hermes changed", not an exact per-session ledger.

        Returns the same shape as :meth:`diff` (``{"success", "stat",
        "diff"}``).  When no checkpoints exist yet — nothing has been edited —
        the call still *succeeds* with empty output and ``"empty": True`` so
        callers can show a friendly "no changes" message rather than an error.
        """
        checkpoints = self.list_checkpoints(working_dir)
        if not checkpoints:
            return {"success": True, "stat": "", "diff": "", "empty": True}

        baseline = checkpoints[-1].get("hash") or ""
        result = self.diff(working_dir, baseline)
        if result.get("success"):
            result.setdefault("baseline", baseline)
            if not result.get("stat") and not result.get("diff"):
                result["empty"] = True
        return result

    def restore(
        self,
        working_dir: str,
        commit_hash: str,
        file_path: str | None = None,
        safe: bool = False,
    ) -> dict:
        """Restore files to a checkpoint state.

        With ``safe=True`` (full-directory restores only), files the user
        hand-edited after Hermes' last write — per the agent-write ledger —
        are left untouched, and only Hermes-authored changes are reverted.
        The result gains ``skipped_user_edits`` listing the preserved paths,
        ``skipped_oversize`` listing paths kept because the size cap excluded
        them from every checkpoint, and — only when a delete failed —
        ``failed_deletes`` listing paths that could not be removed.
        """
        from tools.checkpoint_pruning import PruneError, store_lock

        try:
            with store_lock(_resolve_checkpoint_base()):
                return self._restore(working_dir, commit_hash, file_path, safe)
        except (PruneError, OSError) as exc:
            return {"success": False, "error": str(exc)}

    def _restore(self, working_dir: str, commit_hash: str, file_path: str | None, safe: bool) -> dict:
        hash_err = _validate_commit_hash(commit_hash)
        if hash_err:
            return {"success": False, "error": hash_err}

        abs_dir = str(_normalize_path(working_dir))

        if file_path:
            path_err = _validate_file_path(file_path, abs_dir)
            if path_err:
                return {"success": False, "error": path_err}

        store = _store_path()

        if not (store / "HEAD").exists():
            return {"success": False, "error": "No checkpoints exist for this directory"}

        ok, _, err = _run_git(
            ["cat-file", "-t", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Checkpoint '{commit_hash}' not found",
                    "debug": err or None}

        ok, tree_out, err = _run_git(
            ["ls-tree", "-r", "-z", commit_hash], store, abs_dir,
        )
        if not ok:
            return {"success": False, "error": f"Could not inspect checkpoint: {err}"}
        nested_repos = _gitlink_paths(tree_out)
        selected_paths: Optional[list[str]] = None
        if nested_repos:
            blocked_repos = nested_repos
            if file_path:
                # Select by the path as recorded in the checkpoint, not by
                # where it points now: a captured file later replaced by a
                # symlink into a nested repo is still recoverable. Resolved
                # confinement is enforced separately by _validate_file_path.
                root_path = Path(abs_dir)
                requested_path = Path(os.path.normpath(root_path / file_path))
                try:
                    requested_rel = requested_path.relative_to(root_path)
                except ValueError:
                    requested_rel = Path(file_path)
                requested_parts = tuple(requested_rel.parts)

                def _scope_intersects_nested_repo(repo_path: str) -> bool:
                    repo_parts = tuple(PurePosixPath(repo_path).parts)
                    return (
                        requested_parts[:len(repo_parts)] == repo_parts
                        or repo_parts[:len(requested_parts)] == requested_parts
                    )

                blocked_repos = [
                    repo for repo in nested_repos
                    if _scope_intersects_nested_repo(repo)
                ]

                # Literal paths below a gitlink have no tree entry to match.
                # Keep the explicit refusal above, but let Git resolve all
                # other selections exactly as checkout does (glob, icase,
                # top, exclude, literal, etc.). ls-tree lacks that pathspec
                # support. A disposable index avoids touching the project's
                # index, refs or files before we know rollback is possible.
                if not blocked_repos:
                    with tempfile.TemporaryDirectory(prefix="restore-inspect-", dir=store) as scratch:
                        inspect_index = Path(scratch) / "index"
                        ok, _, err = _run_git(
                            ["read-tree", commit_hash], store, abs_dir,
                            index_file=inspect_index,
                        )
                        if ok:
                            ok, selected, err = _run_git(
                                ["ls-files", "--stage", "--error-unmatch", "-z", "--", file_path],
                                store, abs_dir, index_file=inspect_index,
                            )
                        if not ok:
                            return {"success": False, "error": f"Could not inspect restore selection: {err}"}
                    blocked_repos = [
                        record.split("\t", 1)[1]
                        for record in selected.split("\x00")
                        if record.startswith("160000 ") and "\t" in record
                    ]
                    selected_paths = [
                        record.split("\t", 1)[1]
                        for record in selected.split("\x00")
                        if "\t" in record
                    ]
                    if not selected_paths:
                        # An exclusion-only spec can match nothing. Checkout
                        # with no paths would switch the store's HEAD instead
                        # of restoring files, so refuse before the snapshot.
                        return {
                            "success": False,
                            "error": f"Restore selection matched no files in checkpoint: {file_path}",
                        }

            if blocked_repos:
                paths = ", ".join(blocked_repos)
                return {
                    "success": False,
                    "error": (
                        "Checkpoint contains nested git repositories that were not captured "
                        f"({paths}); rollback was not performed"
                    ),
                    "nested_repositories": blocked_repos,
                }

        skipped_user_edits: list[str] = []
        kept_oversize: list[str] = []
        failed_deletes: list[str] = []
        restore_paths: Optional[list[str]] = None
        if safe and not file_path:
            plan = self._safe_restore_plan(abs_dir, commit_hash)
            if not plan.get("success"):
                return {"success": False, "error": plan.get("error", "Safe-restore plan failed")}
            if plan.get("ledger_empty"):
                # No agent-write history to compare against — fall back to
                # the classic full restore rather than restoring nothing.
                restore_paths = None
            else:
                restore_paths = plan["restore"]
                skipped_user_edits = plan["skipped"]
                if not restore_paths:
                    return {
                        "success": True,
                        "restored_to": commit_hash[:8],
                        "reason": "nothing to restore (all changed files were user-edited)",
                        "directory": abs_dir,
                        "restored_files": [],
                        "skipped_user_edits": skipped_user_edits,
                        "skipped_oversize": [],
                    }

        # Take a pre-rollback snapshot so you can undo the undo.
        self._take(abs_dir, f"pre-rollback snapshot (restoring to {commit_hash[:8]})", prune=False)

        dir_hash = _project_hash(abs_dir)
        index_file = _index_path(store, dir_hash)

        if restore_paths is not None:
            # Split into files present in the checkpoint (checkout) and
            # Hermes-created files absent from it (delete to restore state).
            checkout_targets: list[str] = []
            delete_targets: list[str] = []
            for rel in restore_paths:
                ok_in_commit, _, _ = _run_git(
                    ["cat-file", "-e", f"{commit_hash}:{rel}"],
                    store, abs_dir, allowed_returncodes={1, 128},
                )
                if ok_in_commit:
                    checkout_targets.append(rel)
                elif self._exceeds_size_cap(Path(abs_dir) / rel):
                    # Absent from the checkpoint because ``max_file_size_mb``
                    # kept it out (_drop_oversize_from_index), not because
                    # Hermes created it. Deleting it would not restore a prior
                    # state — no checkpoint holds one — it would destroy the
                    # only copy. The ledger records a content hash, not whether
                    # a write created or modified the file, so an oversize path
                    # cannot be proven agent-created; leaving it costs a stale
                    # file, deleting it costs the file.
                    kept_oversize.append(rel)
                else:
                    delete_targets.append(rel)
            for rel in delete_targets:
                try:
                    target = Path(abs_dir) / rel
                    if target.is_file() or target.is_symlink():
                        target.unlink()
                except OSError as exc:
                    logger.warning(
                        "Safe restore: could not remove %s: %s", rel, exc,
                    )
                    failed_deletes.append(rel)
            if not checkout_targets:
                ok, _stdout, err = True, "", ""
            else:
                ok, _stdout, err = _run_git(
                    ["checkout", commit_hash, "--", *checkout_targets],
                    store, abs_dir, timeout=_GIT_TIMEOUT * 2,
                    index_file=index_file,
                )
        elif selected_paths is not None:
            # Check out exactly the entries inspected above. Re-evaluating
            # file_path here could select something else: _take() has just
            # restaged the project index, and attribute pathspecs read
            # .gitattributes from it. A NUL-separated literal pathspec file
            # keeps long selections off the command line.
            with tempfile.TemporaryDirectory(prefix="restore-select-", dir=store) as scratch:
                spec_file = Path(scratch) / "pathspec"
                spec_file.write_bytes(b"".join(os.fsencode(p) + b"\0" for p in selected_paths))
                ok, _stdout, err = _run_git(
                    ["checkout", commit_hash, f"--pathspec-from-file={spec_file}",
                     "--pathspec-file-nul"],
                    store, abs_dir, timeout=_GIT_TIMEOUT * 2,
                    index_file=index_file,
                    extra_env={"GIT_LITERAL_PATHSPECS": "1"},
                )
        else:
            ok, _stdout, err = _run_git(
                ["checkout", commit_hash, "--", file_path if file_path else "."],
                store, abs_dir, timeout=_GIT_TIMEOUT * 2,
                index_file=index_file,
            )

        if not ok:
            return {"success": False, "error": f"Restore failed: {err}",
                    "debug": err or None}

        ok2, reason_out, _ = _run_git(
            ["log", "--format=%s", "-1", commit_hash], store, abs_dir,
        )
        reason = reason_out if ok2 else "unknown"

        result = {
            "success": True,
            "restored_to": commit_hash[:8],
            "reason": reason,
            "directory": abs_dir,
        }
        if file_path:
            result["file"] = file_path
        if restore_paths is not None:
            # Only what was actually acted on. A kept oversize path was not
            # restored (and a failed unlink left the file in place), and
            # reporting either as restored is how the data loss above stayed
            # silent: the user was told "Restored" for a file that had just
            # been unlinked.
            not_restored = set(kept_oversize) | set(failed_deletes)
            result["restored_files"] = [
                rel for rel in restore_paths if rel not in not_restored
            ]
            result["skipped_user_edits"] = skipped_user_edits
            result["skipped_oversize"] = kept_oversize
            if failed_deletes:
                result["failed_deletes"] = failed_deletes
        # The selected tree was needed until checkout completed. Only now may
        # the safety snapshot's count/size budget make that tree unreachable.
        self._prune(store, abs_dir, _ref_name(dir_hash))
        return result

    def _ledger_key(self, path: str) -> str:
        """Agent-write ledger key: hash of the marker-walked project dir, for writer and reader alike."""
        return _project_hash(self.get_working_dir_for_path(path))

    def get_working_dir_for_path(self, file_path: str) -> str:
        """Resolve a file path to its working directory for checkpointing."""
        path = _normalize_path(file_path)
        if path.is_dir():
            candidate = path
        else:
            candidate = path.parent

        # An explicitly checkpointed root owns its ledger even when an
        # ancestor carries a project marker. Prefer the nearest owner.
        roots = [Path(meta["workdir"]) for meta in _list_projects(_store_path()) if meta.get("workdir")]
        owners = [root for root in roots if candidate.is_relative_to(root)]
        if owners:
            return str(max(owners, key=lambda root: len(root.parts)))
        markers = {".git", "pyproject.toml", "package.json", "Cargo.toml",
                    "go.mod", "Makefile", "pom.xml", ".hg", "Gemfile"}
        home = Path.home().resolve()
        broad = {home, *home.parents}
        check = candidate
        while check != check.parent and check not in broad:
            if any((check / m).exists() for m in markers):
                return str(check)
            check = check.parent

        return str(candidate)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _oversize_excludes(self, working_dir: str) -> List[str]:
        """Pathspec exclusions for files above ``max_file_size_mb``.

        The cap is enforced after staging, by dropping oversize paths from the
        index — but ``git add`` has already hashed and written those blobs into
        the object DB by then, so a 353 MB bundle costs its full size, and its
        share of the shared store, on every first snapshot.  Excluding them from
        the pathspec keeps the bytes out of the object database; the paths stay
        out of the committed tree exactly as the post-stage drop already made
        them.  Costs one ``stat``-walk of the project; past
        ``_MAX_PATHSPEC_EXCLUDES`` matches it gives up and returns nothing.
        Paths the project already ignores are left out of the returned list —
        naming one makes ``git add`` exit 1 with the ignored-paths advisory, which
        the stage reads as a failure for the whole project (see
        :func:`_ignored_paths`).
        """
        cap = self.max_file_size_mb * 1024 * 1024
        if cap <= 0:
            return []
        root = _normalize_path(working_dir)
        found: List[str] = []
        # 嵌套仓（nested repo / gitlink）：其下任何路径都不能进入 ``:(exclude,literal)``
        # pathspec —— git 2.53 实测：``git add -A -- :(exclude,literal)<sub>/<file>`` 在
        # ``<sub>`` 是嵌套仓（parent 是 160000 gitlink 或 ``<sub>/.git`` 在）时会 ``rc=128``
        # 并打印 ``fatal: Pathspec ':(exclude,literal)<sub>/<file>' is in submodule '<sub>'``，
        # 整项 ``git add`` 失败 ⇒ 该工作区所有新快照静默全停（D3 线 · t_136980fe）。
        # ``git check-ignore`` 对这类路径自身也 ``rc=128 fatal``，无法救场 ⇒ 必须在
        # pathspec 构造点处理。识别口径：从 root 起 os.walk 时遇到含 ``.git`` 的目录，
        # 即视为嵌套仓、**不进该目录、不把其下任何路径加入 pathspec**（嵌套仓本身的
        # 文件不属本工作区快照对象，由嵌套仓自己负责）。
        nested_repo_roots: Set[str] = set()
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                # 检测嵌套仓（**在先**，避免被下方 ``dirnames`` 过滤抹掉）：
                # 等 nested 含 ``.git`` 目录 ⇒ 不进该目录、不把其下任何路径加入 pathspec。
                if ".git" in dirnames:
                    nested_repo_roots.add(os.path.normpath(dirpath))
                    dirnames[:] = []
                    continue
                dirnames[:] = [
                    d for d in dirnames
                    if d not in (".git", ".hg", ".svn", ".worktrees")
                ]
                for name in filenames:
                    candidate = os.path.join(dirpath, name)
                    try:
                        # ``stat`` (not ``lstat``) to agree with
                        # :meth:`_exceeds_size_cap`, which measures the target.
                        if os.stat(candidate).st_size <= cap:
                            continue
                    except OSError:
                        continue
                    rel = os.path.relpath(candidate, root)
                    if rel not in found:
                        found.append(rel)
                    if len(found) > _MAX_PATHSPEC_EXCLUDES:
                        logger.warning(
                            "Checkpoint: more than %d oversize files in %s — skipping the "
                            "pre-stage size filter; the post-stage drop still applies",
                            _MAX_PATHSPEC_EXCLUDES, working_dir,
                        )
                        return []
        except OSError as exc:
            logger.debug("Checkpoint: oversize pre-filter walk failed for %s: %s", working_dir, exc)
            return []
        if nested_repo_roots:
            # rfb-Δ1（复核 rfb2 发现）：第二重剔除原判据用绝对 ``rp`` 匹配相对 ``rel``，恒 False
            # ⇒ 属空保险。改为先取 ``root`` 相对基准 ``rn``（与 ``found`` 收集的 ``rel`` 同基准），
            # ``rel == rn``（仓库根名场景）或 ``rel.startswith(rn + os.sep)``（子路径）。
            _nested_rel = sorted(
                os.path.relpath(rp, root) for rp in nested_repo_roots
            )
            before = len(found)
            found = [
                rel for rel in found
                if not any(
                    rel == rn or rel.startswith(rn + os.sep)
                    for rn in _nested_rel
                )
            ]
            dropped = before - len(found)
            if dropped:
                logger.debug(
                    "Checkpoint: %d oversize path(s) live under nested repo(s) in %s — left "
                    "out of the pathspec (git fatal on submodule pathspec entries): %s",
                    dropped, working_dir,
                    ", ".join(sorted(nested_repo_roots)[:3])
                    + ("…" if len(nested_repo_roots) > 3 else ""),
                )
        if found:
            # Naming an already-ignored path here would make the stage exit 1 with
            # the ignored-paths advisory — those paths stay out of the commit
            # either way, so they must stay out of the pathspec too.
            ignored = _ignored_paths(_store_path(), working_dir, found)
            if ignored:
                logger.debug(
                    "Checkpoint: %d oversize path(s) already ignored in %s — left out of the "
                    "pathspec: %s", len(ignored), working_dir,
                    ", ".join(sorted(ignored)[:5]) + ("…" if len(ignored) > 5 else ""),
                )
                found = [rel for rel in found if rel not in ignored]
        return [":(exclude,literal)" + rel for rel in found]

    def _stage(self, store: Path, working_dir: str, index_file: Path) -> Tuple[bool, str, List[str]]:
        """Stage the working tree into the per-project index.

        Returns ``(ok, stderr, skipped)``.  ``skipped`` lists entries git could not
        read: the index is written without them, so they are absent from this
        snapshot and a rollback leaves them untouched instead of restoring them.
        Retries once after removing a stale ``<index>.lock`` — what a stage killed
        by its own timeout leaves behind, and which nothing else ever removes.
        """
        args = ["add", "-A", "--ignore-errors", "--", "."]
        args += self._oversize_excludes(working_dir)
        skipped: List[str] = []
        err = ""
        for attempt in (0, 1):
            ok, _, err = _run_git(
                args, store, working_dir,
                timeout=_GIT_STAGE_TIMEOUT, index_file=index_file,
            )
            if ok:
                return True, err, skipped
            if _stage_partial(err) and index_file.exists():
                # ``--ignore-errors`` exits non-zero for an unreadable entry but
                # still writes the index: the snapshot is usable and partial.
                skipped = _skipped_entries(err)
                logger.warning(
                    "Checkpoint: %d entr%s skipped while staging %s (unreadable, vanished or "
                    "ignored): %s (absent from this snapshot; a rollback leaves them as they are)",
                    len(skipped), "y" if len(skipped) == 1 else "ies", working_dir,
                    ", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""),
                )
                return True, err, skipped
            if attempt == 0 and _index_lock_blocked(err):
                lock = _removable_stale_lock(index_file)
                if lock is not None:
                    try:
                        lock.unlink()
                    except OSError as exc:
                        logger.warning(
                            "Checkpoint: stale index lock %s could not be removed: %s", lock, exc,
                        )
                        return False, err, skipped
                    logger.warning(
                        "Checkpoint: removed stale index lock %s while staging %s "
                        "(no live holder, age ≥ %ds); retrying once",
                        lock, working_dir, _STALE_LOCK_AGE_S,
                    )
                    continue
            return False, err, skipped
        return False, err, skipped

    def _record_snapshot_state(
        self, store: Path, working_dir: str, *, ok: bool, code: str = "", detail: str = "",
    ) -> None:
        """Record this project's last snapshot outcome where a probe can read it.

        Checkpoint failures are invisible on the gateway path, so the outcome is
        written next to the project rather than only into a log: 435 checkpoint
        errors accumulated with nothing on the host reading ``logs/errors.log``.
        Best effort — never raises.
        """
        path = _snapshot_state_path(store, _project_hash(working_dir))
        state: Dict = {
            "workdir": str(_normalize_path(working_dir)),
            "ok": ok,
            "ts": time.time(),
            "code": code,
            "detail": detail[:400],
        }
        try:
            if ok:
                state["last_ok_ts"] = state["ts"]
            else:
                try:
                    previous = json.loads(path.read_text(encoding="utf-8-sig"))
                except (OSError, ValueError):
                    previous = {}
                if isinstance(previous, dict) and previous.get("last_ok_ts"):
                    state["last_ok_ts"] = previous["last_ok_ts"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state), encoding="utf-8")
        except OSError as exc:
            logger.debug("Could not write snapshot state %s: %s", path, exc)

    def _warn_snapshot_problem(self, store: Path, working_dir: str, code: str, detail: str) -> None:
        """Report a snapshot that will not be taken — once per project per session.

        The failure used to be a single DEBUG line, and while the raw git error
        does reach ``logs/errors.log`` at ERROR nothing reads that file: one
        project went eight days without a snapshot unnoticed.  A stable
        ``Checkpoint FAILED`` prefix plus the ``snapshot-state`` file make the
        condition visible without log scraping.
        """
        key = (str(_normalize_path(working_dir)), code)
        if key not in self._warned_snapshot_problems:
            self._warned_snapshot_problems.add(key)
            logger.warning(
                "Checkpoint FAILED for %s (%s): %s — no snapshot was taken, so rollback for "
                "this project covers nothing after its last successful checkpoint",
                working_dir, code, detail,
            )
        self._record_snapshot_state(store, working_dir, ok=False, code=code, detail=detail)

    def _take(self, working_dir: str, reason: str, *, prune: bool = True) -> bool:
        """Take a snapshot.  Returns True on success."""
        store = _store_path()

        err = _init_store(store, working_dir)
        if err:
            logger.debug("Checkpoint store init failed: %s", err)
            return False

        _touch_project(store, working_dir)

        # Quick size guard — don't try to snapshot enormous directories.  The
        # reading is an entry count (directories included, see _dir_file_count),
        # and the skip is reported: it produces no snapshot at all, so a quiet
        # DEBUG line here is how a working tree silently stops being protected.
        entries = _dir_file_count(working_dir)
        if entries > _MAX_FILES:
            self._warn_snapshot_problem(
                store, working_dir, "too_many_entries",
                f"more than {_MAX_FILES} entries in the working tree "
                f"(raise HERMES_CHECKPOINT_MAX_ENTRIES to snapshot it anyway)",
            )
            return False

        dir_hash = _project_hash(working_dir)
        index_file = _index_path(store, dir_hash)
        ref = _ref_name(dir_hash)

        # Seed the per-project index from the last checkpoint, if any, so the
        # diff/commit machinery sees only changes since then.  On first call,
        # clear the index so ``git add -A`` produces a clean tree.
        if index_file.exists():
            # Reset index to current ref tip to avoid accumulating stale paths.
            ok_ref, ref_commit, _ = _run_git(
                ["rev-parse", "--verify", ref + "^{commit}"],
                store, working_dir,
                allowed_returncodes={128},
            )
            if ok_ref and ref_commit:
                _run_git(
                    ["read-tree", ref_commit],
                    store, working_dir,
                    index_file=index_file,
                    allowed_returncodes={128},
                )
            else:
                try:
                    index_file.unlink()
                except OSError:
                    pass
        else:
            # First snapshot for this project.
            index_file.parent.mkdir(parents=True, exist_ok=True)

        # Stage with per-project index.  Include a per-stage file-size filter
        # via ``core.bigFileThreshold`` is not what we want — instead, the
        # oversize paths are excluded from the pathspec before git hashes them,
        # and the exclude file covers the broad patterns (see :meth:`_stage`).
        ok, err, skipped = self._stage(store, working_dir, index_file)
        if not ok:
            if _BENIGN_ADD_ERR in err:
                # The project directory vanished between the size guard and the
                # stage: routine, and _run_git already logs it.  Keep it quiet
                # so the reportable failures below stay meaningful.
                logger.debug("Checkpoint git-add failed: %s", err)
            else:
                self._warn_snapshot_problem(store, working_dir, "git_add_failed", err)
            return False

        if self.max_file_size_mb > 0:
            self._drop_oversize_from_index(store, working_dir, index_file)

        # Compare against the current ref tip (not HEAD — HEAD points to a
        # branch that doesn't exist on a bare store, so ``diff --cached``
        # against HEAD would always show "new file" for every staged path).
        ok_ref, ref_commit, _ = _run_git(
            ["rev-parse", "--verify", ref + "^{commit}"],
            store, working_dir,
            allowed_returncodes={128},
        )
        has_ref = ok_ref and bool(ref_commit)

        if has_ref:
            ok_diff, _, _ = _run_git(
                ["diff-index", "--cached", "--quiet", ref_commit],
                store, working_dir,
                allowed_returncodes={1},
                index_file=index_file,
            )
            if ok_diff:
                logger.debug("Checkpoint skipped: no changes in %s", working_dir)
                return False
        else:
            # No ref yet — skip only if the index is empty.
            ok_ls, ls_out, _ = _run_git(
                ["ls-files", "--cached"],
                store, working_dir,
                index_file=index_file,
            )
            if ok_ls and not ls_out.strip():
                logger.debug("Checkpoint skipped: empty tree in %s", working_dir)
                return False

        # Write tree from per-project index.
        ok_tree, tree_sha, err = _run_git(
            ["write-tree"], store, working_dir,
            index_file=index_file,
        )
        if not ok_tree or not tree_sha:
            logger.debug("Checkpoint write-tree failed: %s", err)
            return False

        # A nested repository is stored as a gitlink, not its files: say so in
        # the checkpoint itself rather than only when a rollback is refused.
        ok_ls, tree_out, _ = _run_git(["ls-tree", "-r", "-z", tree_sha], store, working_dir)
        nested_repos = _gitlink_paths(tree_out) if ok_ls else []
        if nested_repos:
            logger.info("Checkpoint of %s does not capture nested git repositories: %s",
                        working_dir, ", ".join(nested_repos))
            # Listings read the subject (%s), which ends at the first blank
            # line, so keep a multiline reason (a terminal command) on one
            # line or the note falls outside every listing.
            reason = " ".join(
                line.strip() for line in reason.splitlines() if line.strip()
            ) + _uncaptured_note(nested_repos)

        # Build commit (parent = current ref tip, if any).
        commit_args = ["commit-tree", tree_sha, "-m", reason, "--no-gpg-sign"]
        if has_ref:
            commit_args = ["commit-tree", tree_sha, "-p", ref_commit, "-m", reason, "--no-gpg-sign"]
        ok_commit, new_sha, err = _run_git(
            commit_args, store, working_dir,
            index_file=index_file,
        )
        if not ok_commit or not new_sha:
            logger.debug("Checkpoint commit-tree failed: %s", err)
            return False

        # Update the per-project ref.
        update_args = ["update-ref", ref, new_sha]
        if has_ref:
            update_args = ["update-ref", ref, new_sha, ref_commit]
        ok_update, _, err = _run_git(
            update_args, store, working_dir,
        )
        if not ok_update:
            logger.debug("Checkpoint update-ref failed: %s", err)
            return False

        logger.debug("Checkpoint taken in %s: %s (%s)", working_dir, reason, new_sha[:8])
        self._record_snapshot_state(
            store, working_dir, ok=True,
            code="partial_entries" if skipped else "",
            detail=(", ".join(skipped[:5])) if skipped else "",
        )

        # Count and size budgets share one failure boundary.
        if prune:
            self._prune(store, working_dir, ref)

        return True

    def _exceeds_size_cap(self, path: Path) -> bool:
        """Whether *path* is larger than ``max_file_size_mb``.

        The same test :meth:`_drop_oversize_from_index` applies when building a
        checkpoint, so "excluded from the checkpoint" and "refused deletion at
        restore" agree on one definition. A cap of 0 disables it, and an
        unstattable path is not claimed to be oversize.
        """
        cap = self.max_file_size_mb * 1024 * 1024
        if cap <= 0:
            return False
        try:
            return path.stat().st_size > cap
        except OSError:
            return False

    def _drop_oversize_from_index(
        self, store: Path, working_dir: str, index_file: Path,
    ) -> None:
        """Remove any staged file larger than ``max_file_size_mb`` from the index.

        Lets the agent keep snapshotting source code while refusing to
        swallow generated assets (datasets, model weights, logs, videos).
        """
        if self.max_file_size_mb <= 0:
            return
        ok, stdout, _ = _run_git(
            ["ls-files", "--cached", "-z"],
            store, working_dir, index_file=index_file,
        )
        if not ok or not stdout:
            return
        # NUL separators preserve whitespace within each literal filename.
        paths = [p for p in stdout.split("\x00") if p]
        abs_workdir = _normalize_path(working_dir)
        # Same predicate safe restore consults, called rather than restated:
        # a threshold that drifted between the two would make a file both
        # absent from the checkpoint and not recognised as capped at restore,
        # which is precisely the deletion this change exists to prevent.
        oversize = [
            rel for rel in paths if self._exceeds_size_cap(abs_workdir / rel)
        ]
        if not oversize:
            return
        logger.debug(
            "Checkpoint: dropping %d oversize file(s) (>%d MB) from index",
            len(oversize), self.max_file_size_mb,
        )
        # Use --pathspec-from-file for safety with many paths.
        # Chunk into manageable batches.
        BATCH = 200
        for i in range(0, len(oversize), BATCH):
            chunk = oversize[i:i + BATCH]
            _run_git(
                ["rm", "--cached", "--quiet", "--"] + chunk,
                store, working_dir, index_file=index_file,
                allowed_returncodes={128},
            )

    def _prune(self, store: Path, working_dir: str, ref: str) -> None:
        """Checkpoint-take path: this project's snapshot-count budget plus one size round,
        scoped to this project, with the gc deferred to the periodic prune — a repack here
        held the tool call for the whole gc on a large store.

        The size round used to rewrite the oldest snapshot of *every* project whenever the
        shared store was over ``max_total_size_mb``, so one oversized project discarded
        unrelated projects' oldest rollback anchors on every take (silently, with no way
        back).  Only the project being snapshotted loses a snapshot here; the store-wide
        loop belongs to the maintenance prune.
        """
        from tools.checkpoint_pruning import Pruner, PruneError

        pruner = Pruner(_run_git, store, working_dir, _GIT_TIMEOUT, _dir_size_bytes, _REFS_PREFIX)
        try:
            pruner.trim(ref, self.max_snapshots)
            if pruner.drop_oldest_snapshot(ref, self.max_total_size_mb * 1024 * 1024):
                if not self._warned_store_over_cap:
                    self._warned_store_over_cap = True
                    logger.warning(
                        "Checkpoint store is over its %d MB budget — dropped the oldest snapshot "
                        "of %s; no other project's snapshots were touched. Run the maintenance "
                        "prune (`hermes checkpoints prune`) to reclaim the space.",
                        self.max_total_size_mb, working_dir,
                    )
        except (PruneError, OSError) as exc:
            logger.warning("Checkpoint pruning stopped: %s", exc)


def format_checkpoint_list(checkpoints: list[dict], directory: str) -> str:
    """Format checkpoint list for display to user."""
    if not checkpoints:
        return f"No checkpoints found for {directory}"

    lines = [f"📸 Checkpoints for {directory}:\n"]
    for i, cp in enumerate(checkpoints, 1):
        ts = cp["timestamp"]
        if "T" in ts:
            ts = ts.split("T")[1].split("+")[0].split("-")[0][:5]
            date = cp["timestamp"].split("T")[0]
            ts = f"{date} {ts}"

        files = cp.get("files_changed", 0)
        ins = cp.get("insertions", 0)
        dele = cp.get("deletions", 0)
        if files:
            stat = f"  ({files} file{'s' if files != 1 else ''}, +{ins}/-{dele})"
        else:
            stat = ""

        # Label per-project entries when showing the cross-project view
        # (workdir key only present on list_all_checkpoints results).
        workdir = cp.get("workdir", "")
        if workdir and directory == "all directories":
            workdir_short = Path(workdir).name or workdir
            lines.append(
                f"  {i}. {cp['short_hash']}  {ts}  [{workdir_short}]  {cp['reason']}{stat}"
            )
        else:
            lines.append(f"  {i}. {cp['short_hash']}  {ts}  {cp['reason']}{stat}")

    lines.append("\n  /rollback <N>             restore to checkpoint N")
    lines.append("  /rollback diff <N>        preview changes since checkpoint N")
    lines.append("  /rollback <N> <file>      restore a single file from checkpoint N")
    return "\n".join(lines)
