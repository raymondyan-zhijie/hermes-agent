"""治理生产区写入闸门（2026-09-21 fork carry, P2-4 收口）回归测试。

2026-09-22 修正：原版把 /home/admin/.hermes 写死，在测试隔离（HERMES_HOME → 临时目录）下必然失败。
改为从 `_hermes_root_path()` 取根（同时遵守上游测试规则：测试不得硬编码 ~/.hermes）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.file_safety import _hermes_root_path, is_write_approval_required  # noqa: E402


def test_governance_paths_are_gated():
    H = str(_hermes_root_path())
    gated = [
        f"{H}/SOUL.md", f"{H}/config.yaml",
        f"{H}/profiles/ops/SOUL.md", f"{H}/profiles/design/config.yaml",
        f"{H}/skills/devops/x/references/write-governance.md",
        f"{H}/profiles/research/skills/media/y/references/notes.md",
    ]
    for p in gated:
        assert is_write_approval_required(p) is True, p


def test_non_governance_paths_are_not_gated():
    H = str(_hermes_root_path())
    free = [f"{H}/work/backlog.md", f"{H}/docs/IT-ASSETS.md",
            f"{H}/skills/x/SKILL.md", "/tmp/notes.txt"]
    for p in free:
        assert is_write_approval_required(p) is False, p
