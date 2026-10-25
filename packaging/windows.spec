"""Windows/macOS console bundles; historical filename retained for compatibility.

Use BUDGET_BUNDLE_MODE=onedir for no temporary extraction.
"""

from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
mode = os.environ.get("BUDGET_BUNDLE_MODE", "onefile")
if mode not in {"onefile", "onedir"}:
    raise ValueError("BUDGET_BUNDLE_MODE must be onefile or onedir")
folder_bundle = mode == "onedir"
hidden = collect_submodules("beancount_budget_tool")
# Beancount loads its built-in plugins (including balance/pad) by module name.
hidden += collect_submodules("beancount", filter=lambda name: not name.endswith("_test"))
hidden += ["curses", "_curses"]
datas = collect_data_files("beancount", includes=["VERSION"])
for distribution in ("beancount-budget-tool", "beancount", "PyYAML"):
    datas += copy_metadata(distribution)
if sys.platform == "win32":
    datas += copy_metadata("windows-curses")

analysis = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    datas=datas,
    hiddenimports=hidden,
)
archive = PYZ(analysis.pure)
exe = EXE(
    archive, analysis.scripts,
    [] if folder_bundle else analysis.binaries,
    [] if folder_bundle else analysis.datas, [],
    name="beancount-budget-tool", console=True, upx=False,
    exclude_binaries=folder_bundle,
)
if folder_bundle:
    bundle = COLLECT(
        exe, analysis.binaries, analysis.datas,
        name="beancount-budget-tool-portable", upx=False,
    )
