"""Package a native folder build without losing Mach-O symlinks or execute bits."""

import argparse
from pathlib import Path
import re
import shutil
import tarfile


ROOT = Path(__file__).resolve().parents[1]


def prepare(bundle: Path, output: Path, arch: str, revision: str, *, source: Path = ROOT) -> Path:
    if arch not in {"arm64", "x64"}:
        raise ValueError("arch must be arm64 or x64")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("revision must be the complete Git commit")
    if not (bundle / "beancount-budget-tool").is_file() or not (bundle / "_internal").is_dir():
        raise ValueError("bundle must contain the executable and its _internal dependencies")
    name = f"beancount-budget-tool-macos-{arch}"
    package, archive = output / name, output / (name + ".tar.gz")
    if package.exists() or package.is_symlink() or archive.exists() or archive.is_symlink():
        raise FileExistsError("download output already exists; choose a new output directory")
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle, package, symlinks=True)
    for filename in ("LICENSE", "QUICKSTART.md", "LEGACY-WORKFLOWS.md"):
        shutil.copy2(source / filename, package / filename)
    shutil.copy2(source / "packaging/THIRD-PARTY-NOTICES.md", package / "THIRD-PARTY-NOTICES.md")
    (package / "docs").mkdir()
    for filename in ("CONFIGURATION.md", "v6-monthly-report.md"):
        shutil.copy2(source / "docs" / filename, package / "docs" / filename)
    (package / "example").mkdir()
    shutil.copytree(source / "examples/v6/simple", package / "example/simple",
                    ignore=shutil.ignore_patterns(".budget-state", "__pycache__"))
    for filename in ("first-month.bean", "first-month-setup.yaml"):
        shutil.copy2(source / "examples/v6" / filename, package / "example" / filename)
    start = (source / "packaging/MACOS-START.md").read_text(encoding="utf-8")
    for old, new in (("../QUICKSTART.md", "QUICKSTART.md"), ("../docs/", "docs/")):
        start = start.replace(old, new)
    (package / "START-HERE.md").write_text(start, encoding="utf-8")
    (package / "packaging").mkdir()
    shutil.copy2(source / "packaging/MACOS-START.md", package / "packaging/MACOS-START.md")
    legacy_path = package / "LEGACY-WORKFLOWS.md"
    legacy = legacy_path.read_text(encoding="utf-8")
    base = f"https://github.com/TinySnow/beancount-budget-tool/blob/{revision}/"
    for target in ("examples/v6/", "docs/budget-v6-review.md", "docs/v6-implementation-status.md"):
        legacy = legacy.replace("(" + target, "(" + base + target)
    legacy_path.write_text(legacy, encoding="utf-8")
    (package / "BUILD-COMMIT.txt").write_text(revision + "\n", encoding="utf-8")
    # Uploading a folder directly drops Unix permissions in Actions artifacts.
    # tar also keeps the framework/dylib symlinks inside the PyInstaller bundle.
    with tarfile.open(archive, "w:gz", dereference=False) as tar:
        tar.add(package, arcname=name)
    return archive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--arch", required=True, choices=("arm64", "x64"))
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    print(prepare(args.bundle, args.output, args.arch, args.revision))


if __name__ == "__main__":
    main()
