---
name: release
description: Prepare and publish Cadwyn releases. Use for version selection, changelog and package-version updates, release validation, GitHub Release creation, PyPI publication, or release verification in this repository.
---

# Release Cadwyn

Cadwyn publishes to PyPI when a GitHub Release is created. The release tag and title are the bare PEP 440 version, such as `7.1.0` or `6.0.0.rc1`.

One release publishes two packages in the same version: `cadwyn` from the repository root and `cadwyn-core` from `cadwyn-core/`. `cadwyn` pins `cadwyn-core` exactly, and both fail at import if their versions differ.

## Prepare

1. Work from the intended `main` commit and preserve unrelated changes.
2. Choose the version: major for breaking changes, minor for features, patch for fixes, or `.rcN` for a release candidate.
3. Move the top `CHANGELOG.md` `[Unreleased]` entries under a new `[<version>]` heading, leaving an empty `[Unreleased]` heading above it. Keep the existing Keep a Changelog categories.
4. Set the version in three places: `[project].version` in `pyproject.toml`, `[project].version` in `cadwyn-core/pyproject.toml`, and the `cadwyn-core==<version>` pin in the dependencies of `pyproject.toml`. Then run `uv lock` so both entries in `uv.lock` match. `tests/test_packages.py` fails if the versions differ.
5. Run `make check`. Inspect the release diff and ensure the prepared commit is pushed to `main` with CI passing.

Do not publish with local `uv publish`. Do not create a separate tag.

## Publish

Only continue when the user explicitly intends to publish. Confirm the exact version and target commit, then create the GitHub Release from `main` with generated notes:

```bash
gh release create <version> --target main --title <version> --generate-notes
```

Add `--prerelease` for a release candidate. Release creation triggers `.github/workflows/release.yaml`. It builds both packages with `uv build --all-packages` and publishes them through PyPI trusted publishing, `cadwyn-core` first, because `cadwyn` needs it. It skips the files that PyPI already has, so if one upload fails, rerun the failed run with `gh run rerun <run-id> --failed` instead of making a new release. It rebuilds the same files, because hatchling builds are reproducible.

## Verify

1. Confirm the release targets the expected commit with `gh release view <version>`.
2. Find the corresponding release workflow using `gh run list --workflow release.yaml`, then watch it through completion with `gh run watch <run-id> --exit-status`.
3. Confirm the exact version of both `cadwyn` and `cadwyn-core` appears on PyPI. Report the GitHub Release and workflow links.

PyPI files are immutable. If publication succeeded with a mistake, fix it and release a new version; do not try to replace the uploaded version. If old version is unsafe, yank it.
