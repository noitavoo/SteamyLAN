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
            zip_path = root / "release" / package_release.release_zip_name("1.0.0")
            (build / "_internal").mkdir(parents=True)
            (build / "SteamyLAN.exe").write_bytes(b"exe")
            (build / "steam_api64.dll").write_bytes(b"steam")
            (build / "SteamyLANUpdate.exe").write_bytes(b"updater")
            (build / "WinDivert.dll").write_bytes(b"windivert")
            (build / "WinDivert64.sys").write_bytes(b"driver")
            (build / "_internal" / "runtime.dll").write_bytes(b"runtime")
            (build / "_internal" / "debug.pdb").write_bytes(b"debug")

            package_release.copy_runtime(build, runtime)
            package_release.zip_runtime(runtime, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())

            self.assertIn("SteamyLAN/SteamyLAN.exe", names)
            self.assertIn("SteamyLAN/steam_api64.dll", names)
            self.assertIn("SteamyLAN/SteamyLANUpdate.exe", names)
            self.assertIn("SteamyLAN/WinDivert.dll", names)
            self.assertIn("SteamyLAN/WinDivert64.sys", names)
            self.assertIn("SteamyLAN/_internal/runtime.dll", names)
            self.assertNotIn("SteamyLAN/_internal/debug.pdb", names)
            self.assertTrue(all(name.startswith("SteamyLAN/") for name in names))


    def test_release_zip_name_includes_version(self):
        self.assertEqual(
            package_release.release_zip_name("1.0.0"),
            "SteamyLAN_v1.0.0.zip",
        )
        with self.assertRaises(ValueError):
            package_release.release_zip_name("v1.0.0")
        with self.assertRaises(ValueError):
            package_release.release_zip_name("1.0.0.1")

    def test_workflow_publishes_exact_release_asset_name(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn('release\\SteamyLAN_v$env:RELEASE_VERSION.zip', workflow)
        self.assertIn('--version "$version"', workflow)
        self.assertIn('gh release create', workflow)
        self.assertIn('permissions:\n  contents: write', workflow)
        self.assertIn('actions/checkout@v7', workflow)
        self.assertIn('actions/setup-python@v7', workflow)
        self.assertIn("- 'SteamyLan/**/*.py'", workflow)
        self.assertIn("- 'run.py'", workflow)
        self.assertIn("- 'third_party/windivert/**'", workflow)
        self.assertNotIn("- 'README.md'", workflow)
        self.assertNotIn("- 'SteamyLan/steamylan.png'", workflow)
        self.assertIn("gh release view 'v1.0.0'", workflow)
        self.assertIn('--previous-version "$previousTag"', workflow)
        self.assertIn('--reuse-version "$currentTag"', workflow)


class BuildVersionTests(unittest.TestCase):
    def test_project_version_is_reset_to_one_zero_zero(self):
        import tomllib

        with (ROOT / "pyproject.toml").open("rb") as handle:
            project_version = str(tomllib.load(handle)["project"]["version"])
        constants = (ROOT / "SteamyLan" / "constants.py").read_text(encoding="utf-8")
        self.assertEqual(project_version, "1.0.0")
        self.assertIn('APP_VERSION = "1.0.0"', constants)

    def test_release_version_uses_single_decimal_patch_and_carries_to_minor(self):
        self.assertEqual(stamp_build_version.next_release_version("1.0.0"), "1.0.1")
        self.assertEqual(stamp_build_version.next_release_version("1.0.8"), "1.0.9")
        self.assertEqual(stamp_build_version.next_release_version("1.0.9"), "1.1.0")
        self.assertEqual(stamp_build_version.next_release_version("1.9.9"), "1.10.0")

    def test_first_release_uses_project_version_and_rebuild_reuses_version(self):
        self.assertEqual(stamp_build_version.resolve_release_version("1.0.0"), "1.0.0")
        self.assertEqual(
            stamp_build_version.resolve_release_version("1.0.0", previous_version="v1.0.0"),
            "1.0.1",
        )
        self.assertEqual(
            stamp_build_version.resolve_release_version("1.0.0", reuse_version="v1.0.7"),
            "1.0.7",
        )

    def test_stamp_writes_numeric_three_part_version(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pyproject = root / "pyproject.toml"
            constants = root / "constants.py"
            pyproject.write_text('[project]\nversion = "1.0.0"\n', encoding="utf-8")
            constants.write_text('APP_VERSION = "9.9.9.9"\n', encoding="utf-8")

            version = stamp_build_version.resolve_release_version(
                "1.0.0", previous_version="1.0.9"
            )
            stamp_build_version.stamp_version(constants, version)
            self.assertEqual(version, "1.1.0")
            self.assertEqual(constants.read_text(encoding="utf-8").strip(), 'APP_VERSION = "1.1.0"')


if __name__ == "__main__":
    unittest.main()
