# SPDX-License-Identifier: Apache-2.0
"""Check the layout and checksums of a built Windows experimental package zip."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import unittest
import zipfile

REPO = Path(__file__).resolve().parents[2]
REQUIRED = (
    "bin/inferssd-server.exe",
    "bin/libgcc_s_seh-1.dll",
    "bin/libstdc++-6.dll",
    "bin/libwinpthread-1.dll",
    "scripts/windows-server.py",
    "scripts/windows-server.ps1",
    "python/python.exe",
    "python/LICENSE.txt",
    "framework/src/inferssd/source/gguf_alias.py",
    "framework/src/inferssd/platform/captured_command.py",
    "engine/models/templates/deepseek-ai-DeepSeek-V4.1.jinja",
    "examples/start-deepseek.ps1",
    "docs/windows-package.md",
    "inferssd-package.json",
    "checksums.txt",
    "README.md",
    "WINDOWS-EXPERIMENTAL-PLAN.md",
    "LICENSE",
    "NOTICE",
)


def package_zip() -> Path:
    configured = os.environ.get("INFERSSD_WINDOWS_PACKAGE")
    if configured:
        return Path(configured)
    candidates = sorted((REPO / "dist").glob("inferssd-windows-x64-cpu-*.zip"))
    if not candidates:
        raise unittest.SkipTest("No dist/inferssd-windows-x64-cpu-*.zip; run package-windows-cpu.ps1 first.")
    return candidates[-1]


class PackageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = package_zip()
        cls.archive = zipfile.ZipFile(cls.path)
        cls.prefix = cls.archive.namelist()[0].split("/", 1)[0] + "/"
        cls.names = {name[len(cls.prefix):] for name in cls.archive.namelist()
                     if name.startswith(cls.prefix) and not name.endswith("/")}

    @classmethod
    def tearDownClass(cls):
        cls.archive.close()

    def read(self, name: str) -> bytes:
        return self.archive.read(self.prefix + name)

    def test_required_files(self):
        for name in REQUIRED:
            self.assertIn(name, self.names)

    def test_manifest(self):
        manifest = json.loads(self.read("inferssd-package.json"))
        self.assertEqual(manifest["schema"], "inferssd-windows-package/1")
        self.assertEqual(manifest["model_scope"],
                         "DeepSeek-V4.1-Flash-1017 Q8_0 (deepseek41, MXFP4 routed experts)")
        for key in ("version", "created_utc", "git_revision", "python_version", "server_version"):
            self.assertTrue(manifest[key], key)
        server = self.read("bin/inferssd-server.exe")
        self.assertEqual(manifest["server_sha256"], hashlib.sha256(server).hexdigest())

    def test_checksums(self):
        lines = self.read("checksums.txt").decode("utf-8").splitlines()
        self.assertEqual(len(lines), len(self.names) - 1)  # checksums.txt itself is not listed.
        seen = set()
        for line in lines:
            digest, name = line.split("  ", 1)
            self.assertEqual(len(digest), 64)
            self.assertIn(name, self.names)
            actual = hashlib.sha256(self.read(name)).hexdigest()
            self.assertEqual(digest, actual, name)
            seen.add(name)
        self.assertEqual(seen, self.names - {"checksums.txt"})

    def test_no_development_artifacts(self):
        for name in self.names:
            self.assertNotIn("site-packages", name)
            self.assertNotIn("__pycache__", name)
            self.assertFalse(name.startswith("build-windows-cpu/"), name)
            self.assertFalse(name.endswith(".log"), name)

    def test_bundled_python_is_pruned(self):
        stdlib = {name for name in self.names if name.startswith("python/Lib/")}
        self.assertTrue(stdlib)
        self.assertFalse(any("/test/" in name for name in stdlib))
        self.assertTrue(any(name.startswith("python/DLLs/_ssl.") for name in self.names))


if __name__ == "__main__":
    unittest.main()
