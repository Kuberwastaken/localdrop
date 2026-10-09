#!/usr/bin/env python3
"""Strip FOSS-marked source, or verify the resolved shared pub workspace."""

import argparse
import json
from pathlib import Path
import re
import sys


SOURCE_FILES = (
    "app/lib/config/init.dart",
    "app/lib/pages/donation/donation_page.dart",
    "app/lib/pages/donation/donation_page_vm.dart",
)
PURCHASE_PROVIDER = "app/lib/provider/purchase_provider.dart"


def remove_blocks(text, filename):
    result = []
    inside = False
    for line in text.splitlines(keepends=True):
        if "// [FOSS_REMOVE_START]" in line:
            if inside:
                raise ValueError(f"Nested FOSS start marker in {filename}")
            inside = True
        elif "// [FOSS_REMOVE_END]" in line:
            if not inside:
                raise ValueError(f"Unmatched FOSS end marker in {filename}")
            inside = False
        elif not inside:
            result.append(line)
    if inside:
        raise ValueError(f"Unmatched FOSS start marker in {filename}")
    return "".join(result)


def strip(repository):
    # Validate every edit before writing, so malformed markers cannot leave a
    # half-stripped app. Repeating this operation is safe.
    manifest = repository / "app/pubspec.yaml"
    edits = {manifest: "".join(line for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True)
                              if "# [FOSS_REMOVE]" not in line)}
    if re.search(r"^\s+in_app_purchase\w*\s*:", edits[manifest], re.MULTILINE):
        raise ValueError("Purchase dependency has no # [FOSS_REMOVE] marker")
    for relative in SOURCE_FILES:
        path = repository / relative
        text = remove_blocks(path.read_text(encoding="utf-8"), relative)
        if relative.endswith("donation_page.dart"):
            text = text.replace("donationPageVmProvider", "donationPageNoopVmProvider")
        elif relative.endswith("donation_page_vm.dart"):
            text = text.replace("import 'package:localsend_app/util/native/platform_check.dart';\n", "")
        if "purchaseProvider" in text or "package:in_app_purchase/" in text:
            raise ValueError(f"Purchase code remains outside FOSS markers in {relative}")
        edits[path] = text
    for path, text in edits.items():
        with path.open("w", encoding="utf-8", newline="\n") as output:
            output.write(text)
    (repository / PURCHASE_PROVIDER).unlink(missing_ok=True)


def check_resolved(repository):
    # Pub workspace resolution lives at the repository root, not app/.dart_tool.
    manifest = (repository / "app/pubspec.yaml").read_text(encoding="utf-8")
    if re.search(r"^\s+in_app_purchase\w*\s*:", manifest, re.MULTILINE):
        raise ValueError("Purchase dependency remains in app/pubspec.yaml")
    lock = (repository / "pubspec.lock").read_text(encoding="utf-8")
    config = json.loads((repository / ".dart_tool/package_config.json").read_text(encoding="utf-8"))
    plugins = json.loads((repository / "app/.flutter-plugins-dependencies").read_text(encoding="utf-8"))
    if re.search(r"^  in_app_purchase\w*:", lock, re.MULTILINE):
        raise ValueError("Purchase packages remain in shared pubspec.lock; rerun flutter pub get after stripping")
    if any(p["name"].startswith("in_app_purchase") for p in config["packages"]):
        raise ValueError("Purchase packages remain in shared package_config.json")
    if "in_app_purchase" in json.dumps(plugins):
        raise ValueError("Purchase plugins remain in .flutter-plugins-dependencies")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-resolved", action="store_true", help="Check after flutter pub get; make no edits")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    try:
        if args.check_resolved:
            check_resolved(repository)
            print("FOSS dependency graph and plugin metadata verified.")
        else:
            strip(repository)
            print("Proprietary dependencies removed. Run flutter pub get before building.")
    except (OSError, ValueError, KeyError) as error:
        print(f"FOSS build preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
