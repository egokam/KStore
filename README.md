# EgoStore

An automated GitHub Releases → SideStore source generator hosted on GitHub Pages.

## What it does

- Reads a curated list of GitHub repositories from `repositories.json`.
- Fetches every **published** GitHub release, including prereleases.
- Finds `.ipa` release assets and keeps their original GitHub download URLs.
- Builds a SideStore-compatible source with all eligible versions, newest first.
- Publishes `source.json` to GitHub Pages every 6 hours, on pushes, or manually.

> GitHub's public Releases API returns published releases. Draft releases are not included.
> Releases without an IPA asset are skipped because SideStore needs a direct installable IPA URL.

## 1. Configure your apps

Edit `repositories.json`. Each entry represents one app listing in SideStore.

```json
{
  "repository": "owner/repository",
  "name": "Example App",
  "bundleIdentifier": "com.example.app",
  "developerName": "Example Developer",
  "iconURL": "https://example.com/icon.png",
  "localizedDescription": "A short description of the app.",
  "assetPattern": ".*\\.ipa$",
  "versionOverrides": {
    "v1.2.0": "1.2.0"
  }
}
```

Required metadata:
- `repository`: GitHub `owner/repo`.
- `name`: display name.
- `bundleIdentifier`: must match the app's `CFBundleIdentifier`.
- `developerName`.
- `iconURL`: direct HTTPS image URL.

Optional:
- `localizedDescription`
- `assetPattern`: regular expression used to select one IPA if a release contains multiple IPA files. Defaults to the first `.ipa` asset.
- `versionOverrides`: map GitHub tag names to the app's actual `CFBundleShortVersionString`.

### Important version compatibility note

SideStore uses the first compatible item in `versions` as the latest version, and the version string should match the app's `CFBundleShortVersionString`. EgoStore sorts by GitHub `published_at` newest-first. Git tags do not always equal the app's internal version, so add `versionOverrides` when needed. The generator deliberately does not guess the IPA's internal version.

Each repository must represent a distinct app / bundle identifier. A source should not contain duplicate `bundleIdentifier` values.

## 2. Publish

1. Create a **public** GitHub repository named `EgoStore` (or any name).
2. Upload the contents of this project to its default branch (`main`).
3. In GitHub, open **Settings → Pages**.
4. Set **Build and deployment → Source** to **GitHub Actions**.
5. Open **Actions**, enable workflows if GitHub asks, then run **Build EgoStore source** once.
6. When the workflow succeeds, GitHub Pages will show the deployed site.

The source URL will be:

`https://YOUR-GITHUB-USERNAME.github.io/YOUR-REPOSITORY/source.json`

Add that URL in SideStore → Sources → `+`.

The landing page at `/` contains a link to the source JSON.

## 3. Automation

- Scheduled run: every 6 hours (GitHub Actions cron is UTC).
- Run on every push to `main`.
- Run manually from Actions → Build EgoStore source → Run workflow.

The workflow uses GitHub's automatically provided `GITHUB_TOKEN`; no personal access token is required for public repositories under normal rate limits.

## 4. Local preview

Requires Python 3.10+.

```bash
python scripts/build_source.py
```

The generated files are written to `dist/`:
- `dist/source.json`
- `dist/index.html`

To use a GitHub token locally (optional, helps with API rate limits):

```bash
GITHUB_TOKEN=github_pat_xxx python scripts/build_source.py
```

## 5. Known limitations

- Only published releases with at least one `.ipa` asset are included.
- If a release contains multiple IPA files, one is selected using `assetPattern` (or the first IPA by asset name).
- GitHub release tag names are used as versions unless overridden. They must match the app's internal short version for SideStore updates to work correctly.
- `size` is the GitHub asset size in bytes.
- This does not verify that an IPA is signed, compatible with your iOS version, or safe to install. Only add repositories and apps you trust.
- GitHub Pages updates after the workflow completes; scheduled jobs may start a little later than their scheduled time.

## Source format

The generated JSON follows SideStore's AltStore-compatible source structure:
- top-level `name`, `identifier`, `apps`
- app metadata including `bundleIdentifier`, `developerName`, `iconURL`
- `versions[]` in reverse chronological order, with `version`, `date`, `downloadURL`, `size`, and release notes.

See the [SideStore app sources guide](https://docs.sidestore.io/docs/advanced/app-sources).
