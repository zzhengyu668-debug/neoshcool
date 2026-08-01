"""Read-only Phase W0 environment checks for the neoschool project."""

from __future__ import annotations

import importlib
import importlib.metadata
import platform
import shutil
import struct
import sys
from pathlib import Path

import tomllib


REQUIRED_PATH_KEYS = (
    "raw_compressed",
    "raw_uncompressed",
    "interim",
    "processed",
    "reports",
    "documents",
    "figures",
    "tables",
    "models",
)

REQUIRED_PACKAGES = {
    "duckdb": "duckdb",
    "pyarrow": "pyarrow",
    "pandas": "pandas",
    "polars": "polars",
    "scikit-learn": "sklearn",
    "tqdm": "tqdm",
    "orjson": "orjson",
    "matplotlib": "matplotlib",
    "seaborn": "seaborn",
    "jupyterlab": "jupyterlab",
}

COMPRESSED_FILES = (
    ("meta_categories", "meta_Electronics.jsonl.gz"),
    ("meta_categories", "meta_Home_and_Kitchen.jsonl.gz"),
    ("review_categories", "Electronics.jsonl.gz"),
    ("review_categories", "Home_and_Kitchen.jsonl.gz"),
)

UNCOMPRESSED_FILES = (
    ("meta_categories", "meta_Electronics.jsonl"),
    ("meta_categories", "meta_Home_and_Kitchen.jsonl"),
    ("review_categories", "Electronics.jsonl"),
    ("review_categories", "Home_and_Kitchen.jsonl"),
)


def find_project_root() -> Path | None:
    """Locate the project from this script, never from the working directory."""
    script_path = Path(__file__).resolve()
    for candidate in script_path.parents:
        if (
            (candidate / "PROJECT_HANDOFF.md").is_file()
            and (candidate / "config" / "project.toml").is_file()
        ):
            return candidate
    return None


def format_gib(byte_count: int) -> str:
    return f"{byte_count / (1024**3):.2f} GiB"


def main() -> int:
    errors: list[str] = []
    pending: list[str] = []

    print("Phase W0 environment check")
    print(f"[INFO] Python: {platform.python_version()} ({sys.executable})")

    is_64_bit = struct.calcsize("P") * 8 == 64
    if is_64_bit:
        print("[OK] Python architecture: 64-bit")
    else:
        errors.append("Python must be 64-bit.")
        print("[ERROR] Python architecture is not 64-bit")

    if sys.version_info >= (3, 11):
        print(f"[OK] Python version: {platform.python_version()}")
    else:
        errors.append("Python 3.11 or newer is required.")
        print(f"[ERROR] Python version is too old: {platform.python_version()}")

    project_root = find_project_root()
    if project_root is None:
        errors.append("Could not locate PROJECT_HANDOFF.md and config/project.toml.")
        print("[ERROR] Project root could not be resolved from the script path")
        return 1

    print(f"[OK] Project root: {project_root}")
    config_path = project_root / "config" / "project.toml"

    try:
        with config_path.open("rb") as config_file:
            config = tomllib.load(config_file)
        print(f"[OK] Configuration loaded: {config_path}")
    except (OSError, tomllib.TOMLDecodeError) as exc:
        errors.append(f"Could not read project.toml: {exc}")
        print(f"[ERROR] Could not read project.toml: {exc}")
        return 1

    configured_paths = config.get("paths", {})
    resolved_paths: dict[str, Path] = {}
    for key in REQUIRED_PATH_KEYS:
        raw_value = configured_paths.get(key)
        if not isinstance(raw_value, str) or not raw_value.strip():
            errors.append(f"Missing path configuration: paths.{key}")
            print(f"[ERROR] Missing path configuration: paths.{key}")
            continue

        relative_path = Path(raw_value)
        if relative_path.is_absolute():
            errors.append(f"Configured path must be relative: paths.{key}")
            print(f"[ERROR] Absolute configured path: paths.{key}={raw_value}")
            continue

        resolved = (project_root / relative_path).resolve()
        try:
            resolved.relative_to(project_root)
        except ValueError:
            errors.append(f"Configured path escapes the project root: paths.{key}")
            print(f"[ERROR] Path escapes project root: paths.{key}={raw_value}")
            continue

        resolved_paths[key] = resolved
        if resolved.is_dir():
            print(f"[OK] Directory exists: {raw_value}")
        else:
            errors.append(f"Configured directory is missing: {raw_value}")
            print(f"[ERROR] Missing directory: {raw_value}")

    for distribution, module_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module_name)
            version = importlib.metadata.version(distribution)
            print(f"[OK] Import {module_name}: {version}")
        except Exception as exc:
            errors.append(f"Could not import {module_name}: {exc}")
            print(f"[ERROR] Import {module_name} failed: {exc}")

    disk = shutil.disk_usage(project_root)
    print(
        "[INFO] Project disk: "
        f"total={format_gib(disk.total)}, "
        f"free={format_gib(disk.free)}, "
        f"used={format_gib(disk.used)}"
    )

    compressed_root = resolved_paths.get("raw_compressed")
    uncompressed_root = resolved_paths.get("raw_uncompressed")

    if compressed_root is not None:
        for category, filename in COMPRESSED_FILES:
            path = compressed_root / category / filename
            if path.is_file():
                print(f"[INFO] Compressed data present: {path.relative_to(project_root)}")
            else:
                pending.append(str(path.relative_to(project_root)))
                print(f"[PENDING] Compressed data not downloaded: {path.relative_to(project_root)}")

    if uncompressed_root is not None:
        for category, filename in UNCOMPRESSED_FILES:
            path = uncompressed_root / category / filename
            if path.is_file():
                print(f"[INFO] Uncompressed data present: {path.relative_to(project_root)}")
            else:
                pending.append(str(path.relative_to(project_root)))
                print(f"[PENDING] Uncompressed data not available: {path.relative_to(project_root)}")

    print(
        f"[SUMMARY] errors={len(errors)} pending_data_files={len(pending)} "
        "(pending data does not fail W0)"
    )
    if errors:
        for error in errors:
            print(f"[SUMMARY ERROR] {error}")
        return 1

    print(
        "[PASS] Project environment is valid. "
        "Research-phase status is tracked in data/amazon_reviews_2023/reports/."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
