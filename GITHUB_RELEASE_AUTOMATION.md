# Automatic GitHub releases

`.github/workflows/release.yml` builds and publishes a Windows release when a pushed commit on `main` changes the application code or a runtime/build dependency. Changes limited to documentation, tests, screenshots, icons, or other images do not trigger a release. A local commit does not reach GitHub Actions until it is pushed.

Automatic release paths include the Python application under `SteamyLan/`, `run.py`, the updater helper, dependency files, `pyproject.toml`, and the Steam runtime DLL. The workflow can still be started manually from GitHub Actions when a recovery build is needed.

Each successful run:

1. checks out the exact pushed commit;
2. installs Python 3.14.7 and the pinned SteamyLAN build dependencies;
3. runs the full unit/regression test suite;
4. resolves and stamps the next three-part release version;
5. builds the Windows `SteamyLAN.exe` with PyInstaller;
6. trims packaging-only/debug files from the runtime build;
7. creates a folder named exactly `SteamyLAN`;
8. creates a versioned asset such as `SteamyLAN_v1.0.0.zip`, whose single top-level folder is exactly `SteamyLAN/`;
9. creates the matching GitHub Release/tag (for example `v1.0.0`) and uploads the matching versioned ZIP.

Versioning starts again at `1.0.0` and uses one decimal patch digit:

```text
1.0.0 → 1.0.1 → … → 1.0.9 → 1.1.0 → 1.1.1
```

Only three-part versions are published. Legacy releases are ignored until the new `v1.0.0` baseline has been published. After that, the workflow reads the latest release in the new sequence, advances it with the rule above, and stamps the same version into the executable. Rebuilding a commit that already has a three-part release tag reuses that version instead of incrementing it.

## Release ZIP layout

The downloadable asset is always:

```text
SteamyLAN_v1.0.0.zip
└── SteamyLAN/
    ├── SteamyLAN.exe
    ├── steam_api64.dll
    └── _internal/
        └── ...runtime dependencies...
```

Source code, tests, Git metadata, caches, debug symbol files (`.pdb`), import libraries (`.lib`/`.exp`), and other development-only files are not placed in the release ZIP.

## Steamworks DLL: one-time setup if the DLL is not tracked

The Windows build needs the official 64-bit `steam_api64.dll`.

The workflow first uses `steam_api64.dll` if it is already tracked in the repository. The current `.gitignore` ignores new copies of this DLL, so if your repository does not already track it, use a repository secret instead.

Create a GitHub Actions repository secret named:

```text
STEAM_API64_DLL_B64
```

On Windows, copy the base64 value to the clipboard with:

```powershell
[Convert]::ToBase64String([IO.File]::ReadAllBytes("steam_api64.dll")) | Set-Clipboard
```

Then paste that value into **Repository Settings → Secrets and variables → Actions → New repository secret**.

Do not use an unofficial Steam DLL. Use the official 64-bit Steamworks redistributable that you already use to build SteamyLAN.

## GitHub permissions

The workflow contains:

```yaml
permissions:
  contents: write
```

This allows the workflow's `GITHUB_TOKEN` to create release tags/releases and upload the versioned release ZIP. If an organization-level policy blocks write access, that policy must also allow the repository workflow to write repository contents.

## Manual rebuild

The workflow also supports **Run workflow** from the GitHub Actions page. Rebuilding a commit that already has a release reuses its tag and replaces the existing versioned ZIP asset instead of creating a duplicate release.
