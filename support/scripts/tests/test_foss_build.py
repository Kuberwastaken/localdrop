"""Test FOSS preparation against isolated copies of the real app sources."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


REPOSITORY = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("foss_build", REPOSITORY / "support/scripts/remove_proprietary_dependencies.py")
foss = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(foss)


class FossBuildTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repository = Path(temporary.name)
        self.sources = ["app/pubspec.yaml", *foss.SOURCE_FILES, foss.PURCHASE_PROVIDER]
        self.originals = {name: (REPOSITORY / name).read_bytes() for name in self.sources}
        for name, content in self.originals.items():
            path = self.repository / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def test_real_sources_strip_idempotently_without_changing_repository(self):
        foss.strip(self.repository)
        self.assertNotIn("in_app_purchase:", (self.repository / "app/pubspec.yaml").read_text())
        self.assertFalse((self.repository / foss.PURCHASE_PROVIDER).exists())
        for name in foss.SOURCE_FILES:
            self.assertNotIn("purchaseProvider", (self.repository / name).read_text())
        self.assertIn("donationPageNoopVmProvider", (self.repository / foss.SOURCE_FILES[1]).read_text())
        after = {name: (self.repository / name).read_bytes() for name in self.sources[:-1]}
        foss.strip(self.repository)
        self.assertEqual(after, {name: (self.repository / name).read_bytes() for name in self.sources[:-1]})
        self.assertEqual(self.originals, {name: (REPOSITORY / name).read_bytes() for name in self.sources})

    def test_bad_markers_fail_before_writing(self):
        path = self.repository / foss.SOURCE_FILES[0]
        text = path.read_text(encoding="utf-8").replace("// [FOSS_REMOVE_END]", "// missing end", 1)
        path.write_text(text, encoding="utf-8")
        before = {name: (self.repository / name).read_bytes() for name in self.sources}
        with self.assertRaises(ValueError):
            foss.strip(self.repository)
        self.assertEqual(before, {name: (self.repository / name).read_bytes() for name in self.sources})

    def test_shared_workspace_and_plugin_graph_validation(self):
        foss.strip(self.repository)
        (self.repository / ".dart_tool").mkdir()
        lock = self.repository / "pubspec.lock"
        config = self.repository / ".dart_tool/package_config.json"
        plugins = self.repository / "app/.flutter-plugins-dependencies"
        lock.write_text("packages:\n  collection:\n    version: '1.0'\n")
        config.write_text(json.dumps({"packages": [{"name": "collection"}]}))
        plugins.write_text(json.dumps({"plugins": {"macos": []}}))
        foss.check_resolved(self.repository)
        for path, stale in [
            (lock, "packages:\n  in_app_purchase_storekit:\n    version: '0.4'\n"),
            (config, json.dumps({"packages": [{"name": "in_app_purchase_android"}]})),
            (plugins, json.dumps({"plugins": {"macos": [{"name": "in_app_purchase_storekit"}]}})),
        ]:
            original = path.read_text()
            path.write_text(stale)
            with self.assertRaisesRegex(ValueError, "Purchase"):
                foss.check_resolved(self.repository)
            path.write_text(original)


if __name__ == "__main__":
    unittest.main()
