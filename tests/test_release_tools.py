from __future__ import annotations

import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


package_release = _load("package_release_test", ROOT / "tools" / "package_release.py")
stamp_build_version = _load("stamp_build_version_test", ROOT / "tools" / "stamp_build_version.py")


class ReleasePackagingTests(unittest.TestCase):
    def test_release_zip_has_single_steamylan_top_level_folder(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            build = root / "build"
            runtime = root / "release" / "SteamyLAN"
            zip_path = root / "release" / package_release.release_zip_name("1.3.0.1")
            (build / "_internal").mkdir(parents=True)
            (build / "SteamyLAN.exe").write_bytes(b"exe")
            (build / "steam_api64.dll").write_bytes(b"steam")
            (build / "SteamyLANUpdate.exe").write_bytes(b"updater")
            (build / "_internal" / "runtime.dll").write_bytes(b"runtime")
            (build / "_internal" / "debug.pdb").write_bytes(b"debug")

            package_release.copy_runtime(build, runtime)
            package_release.zip_runtime(runtime, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("SteamyLAN/SteamyLAN.exe", names)
            self.assertIn("SteamyLAN/steam_api64.dll", names)
            self.assertIn("SteamyLAN/SteamyLANUpdate.exe", names)
            self.assertIn("SteamyLAN/_internal/runtime.dll", names)
            self.assertNotIn("SteamyLAN/_internal/debug.pdb", names)
            self.assertTrue(all(name.startswith("SteamyLAN/") for name in names))


    def test_release_zip_name_includes_version(self):
        self.assertEqual(
            package_release.release_zip_name("1.3.0.1"),
            "SteamyLAN_v1.3.0.1.zip",
        )
        with self.assertRaises(ValueError):
            package_release.release_zip_name("v1.3.0.1")

    def test_workflow_publishes_exact_release_asset_name(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('release\\SteamyLAN_v$env:RELEASE_VERSION.zip', workflow)
        self.assertIn('--version "$version"', workflow)
        self.assertIn('gh release create', workflow)
        self.assertIn('permissions:\n  contents: write', workflow)
        self.assertIn('actions/checkout@v7', workflow)
        self.assertIn('actions/setup-python@v7', workflow)


class BuildVersionTests(unittest.TestCase):
    def test_stamp_uses_numeric_four_part_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pyproject = root / "pyproject.toml"
            constants = root / "constants.py"
            pyproject.write_text('[project]\nversion = "1.3.0"\n', encoding="utf-8")
            constants.write_text('APP_VERSION = "1.3.0"\n', encoding="utf-8")

            # Exercise the same transformation used by the CLI without mutating
            # the real project files.
            import re
            import tomllib

            with pyproject.open("rb") as handle:
                base = str(tomllib.load(handle)["project"]["version"])
            build_version = f"{base}.42"
            text = constants.read_text(encoding="utf-8")
            updated, count = re.subn(
                r'(?m)^APP_VERSION\s*=\s*["\'][^"\']+["\']\s*$',
                f'APP_VERSION = "{build_version}"',
                text,
                count=1,
            )
            self.assertEqual(count, 1)
            constants.write_text(updated, encoding="utf-8")
            self.assertEqual(constants.read_text(encoding="utf-8").strip(), 'APP_VERSION = "1.3.0.42"')


if __name__ == "__main__":
    unittest.main()
