"""One console executable. Build on Windows x64 with Python 3.12."""

from pathlib import Path
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
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
    archive, analysis.scripts, analysis.binaries, analysis.datas, [],
    name="beancount-budget-tool", console=True, upx=False,
)
