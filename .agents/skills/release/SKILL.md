---
name: release
description: Prepare and publish Cadwyn releases. Use for version selection, changelog and package-version updates, release validation, GitHub Release creation, PyPI publication, or release verification in this repository.
---

# Release Cadwyn

Cadwyn publishes to PyPI when a GitHub Release is created. The release tag and title are the bare PEP 440 version, such as `7.1.0` or `6.0.0.rc1`.

## Prepare

1. Work from the intended `main` commit and preserve unrelated changes.
2. Choose the version: major for breaking changes, minor for features, patch for fixes, or `.rcN` for a release candidate.
3. Move the top `CHANGELOG.md` `[Unreleased]` entries under a new `[<version>]` heading, leaving an empty `[Unreleased]` heading above it. Keep the existing Keep a Changelog categories.
4. Set `[project].version` in `pyproject.toml`, then run `uv lock` so Cadwyn's entry in `uv.lock` matches.
5. Run `make check`. Inspect the release diff and ensure the prepared commit is pushed to `main` with CI passing.

Do not publish with local `uv publish`. Do not create a separate tag.

## Publish

Only continue when the user explicitly intends to publish. Confirm the exact version and target commit, then create the GitHub Release from `main` with generated notes:

```bash
gh release create <version> --target main --title <version> --generate-notes
```

Add `--prerelease` for a release candidate. Release creation triggers `.github/workflows/release.yaml`, which builds with `uv build` and publishes through PyPI trusted publishing.

## Verify

1. Confirm the release targets the expected commit with `gh release view <version>`.
2. Find the corresponding release workflow using `gh run list --workflow release.yaml`, then watch it through completion with `gh run watch <run-id> --exit-status`.
3. Confirm the exact version appears on PyPI. Report the GitHub Release and workflow links.

PyPI files are immutable. If publication succeeded with a mistake, fix it and release a new version; do not try to replace the uploaded version. If old version is unsafe, yank it.
