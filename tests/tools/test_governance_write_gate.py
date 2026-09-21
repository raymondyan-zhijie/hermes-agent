"""治理生产区写入闸门（2026-09-21 fork carry, P2-4 收口）回归测试。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agent.file_safety import is_write_approval_required  # noqa: E402


def test_governance_paths_are_gated():
    H = "/home/admin/.hermes"
    gated = [
        f"{H}/SOUL.md", f"{H}/config.yaml",
        f"{H}/profiles/ops/SOUL.md", f"{H}/profiles/design/config.yaml",
        f"{H}/skills/devops/x/references/write-governance.md",
        f"{H}/profiles/research/skills/media/y/references/notes.md",
    ]
    for p in gated:
        assert is_write_approval_required(p) is True, p


def test_non_governance_paths_are_not_gated():
    H = "/home/admin/.hermes"
    free = [f"{H}/work/backlog.md", f"{H}/docs/IT-ASSETS.md",
            f"{H}/skills/x/SKILL.md", "/tmp/notes.txt"]
    for p in free:
        assert is_write_approval_required(p) is False, p
