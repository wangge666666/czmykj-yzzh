"""Check that the current client sources match each installed plugin copy.

This is a read-only pre-commit check. A matching display version is not enough.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

if __package__:
    from .build_hybrid_plugin import entries
else:
    from build_hybrid_plugin import entries


def source_inventory():
    return {name: hashlib.sha256(source.read_bytes()).hexdigest() for source, name in entries()}


def check_directory(label, root, expected):
    issues = []
    manifest = root / "SHA256SUMS.json"
    try:
        actual = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        return [f"{label}: cannot read release inventory ({error})"]
    if actual != expected:
        issues.append(f"{label}: release inventory differs from current sources")
    for name, digest in expected.items():
        path = root / name
        try:
            if not path.is_file() or path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                issues.append(f"{label}: stale or missing {name}")
        except OSError:
            issues.append(f"{label}: unreadable {name}")
    return issues


def check_archive(path, expected):
    issues = []
    try:
        with zipfile.ZipFile(path) as archive:
            prefix = "czmiyou-yzzh/"
            names = set(archive.namelist())
            wanted = {prefix + name for name in expected} | {prefix + "SHA256SUMS.json"}
            if names != wanted:
                issues.append("archive: file list differs from current sources")
            manifest = json.loads(archive.read(prefix + "SHA256SUMS.json"))
            if manifest != expected:
                issues.append("archive: release inventory differs from current sources")
            for name, digest in expected.items():
                try:
                    if hashlib.sha256(archive.read(prefix + name)).hexdigest() != digest:
                        issues.append(f"archive: stale {name}")
                except KeyError:
                    issues.append(f"archive: missing {name}")
    except (OSError, ValueError, zipfile.BadZipFile, KeyError) as error:
        issues.append(f"archive: cannot verify ({error})")
    return issues


def check_codex_registration(version):
    try:
        result = subprocess.run(["codex", "plugin", "list"], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return ["Codex: cannot read installed plugin list"]
    for line in result.stdout.splitlines():
        columns = line.split()
        if columns and columns[0] == "czmiyou-yzzh@personal":
            if len(columns) >= 4 and columns[1:3] == ["installed,", "enabled"] and columns[3] == version:
                return []
            return ["Codex: plugin is not installed, enabled, or at the source version"]
    return ["Codex: czmiyou-yzzh@personal is not registered"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="also verify a built customer ZIP")
    parser.add_argument("--installation", type=Path, default=Path.home() / "Applications/czmiyou-yzzh")
    parser.add_argument("--marketplace", type=Path, default=Path.home() / "plugins/czmiyou-yzzh")
    parser.add_argument("--cache", type=Path, help="override the registered Codex cache path")
    args = parser.parse_args()

    expected = source_inventory()
    manifest = json.loads((Path(__file__).resolve().parents[1] / "plugins/czmiyou-yzzh/.codex-plugin/plugin.json").read_text())
    version = manifest["version"]
    cache = args.cache or Path.home() / ".codex/plugins/cache/personal/czmiyou-yzzh" / version
    issues = []
    for label, root in (("fixed installation", args.installation), ("personal marketplace", args.marketplace), ("Codex cache", cache)):
        issues.extend(check_directory(label, root, expected))
    if args.archive:
        issues.extend(check_archive(args.archive, expected))
    issues.extend(check_codex_registration(version))
    if issues:
        print(f"Plugin sync FAILED: {len(issues)} difference(s).")
        for issue in issues[:20]:
            print("-", issue)
        if len(issues) > 20:
            print(f"- ... and {len(issues) - 20} more")
        return 1
    print(f"Plugin sync passed: {len(expected)} source files match the fixed installation, marketplace, and Codex cache; Codex {version} is enabled.")
    print("Current process loading, cloud deployment, and real generation are separate checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
