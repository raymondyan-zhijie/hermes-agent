"""治理生产区写入闸门 —— 树外插件 `governance-write-gate` 的回归测试（P4 重定位）。

历史：该闸门原为核心补丁（`agent/file_safety.py` +39/−3、`tools/file_tools_write_guards.py` +12/−5），
2026-09-25 出树为用户插件（#14），核心恢复**上游原样**。

因此本测试**不再断言核心单方行为**：上游核心本就不拦 `SOUL.md`
（2026-09-25 实测 `is_write_approval_required(H/SOUL.md) is False` ⇒ 旧测 1 failed）。
改为断言**插件装上后的合成行为**：

  1. 插件自报生效（`PATCH_STATUS`）—— 判据扩展或文案修正未生效即失败（fail loud，不用 skip 掩盖）；
  2. 治理生产区（`SOUL.md` / `config.yaml` / `skills/**/references/**`）被拦；
  3. `work/`、`docs/`、普通 `SKILL.md`、`/tmp` 不被拦。

插件缺失 ⇒ 明确失败（治理能力缺失必须可见）。
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

_H = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
_PLUGIN = os.path.join(_H, "plugins", "governance-write-gate", "__init__.py")
_MODNAME = "governance_write_gate_under_test"


def _load_plugin():
    """加载插件（模块级 install() 完成判据扩展）；重复调用只加载一次。"""
    if _MODNAME in sys.modules:
        return sys.modules[_MODNAME]
    assert os.path.exists(_PLUGIN), f"治理写闸门插件缺失：{_PLUGIN}"
    spec = importlib.util.spec_from_file_location(_MODNAME, _PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_MODNAME] = mod
    spec.loader.exec_module(mod)
    return mod


def test_plugin_reports_itself_installed():
    mod = _load_plugin()
    assert mod.PATCH_STATUS.get("build_write_approval_paths") is True, \
        f"判据扩展未生效（治理写保护缺失）：{mod.PATCH_STATUS}"
    assert mod.PATCH_STATUS.get("write_guard_wording") is True, \
        f"治理措辞修正未生效：{mod.PATCH_STATUS}"


def test_governance_paths_are_gated_after_plugin():
    _load_plugin()
    from agent.file_safety import _hermes_root_path, is_write_approval_required  # noqa: E402

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
    _load_plugin()
    from agent.file_safety import _hermes_root_path, is_write_approval_required  # noqa: E402

    H = str(_hermes_root_path())
    free = [f"{H}/work/backlog.md", f"{H}/docs/IT-ASSETS.md",
            f"{H}/skills/x/SKILL.md", "/tmp/notes.txt"]
    for p in free:
        assert is_write_approval_required(p) is False, p
