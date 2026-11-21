# KStore

KStore builds and publishes a SideStore source from public GitHub repositories. The only per-app configuration is the repository URL in [`repositories.json`](repositories.json).

## What KStore discovers

For each repository, KStore uses GitHub's Releases API, with full pagination, to find every published release. It includes prereleases, ignores drafts, and inspects IPA assets directly without executing them. From `Payload/*.app/Info.plist`, it reads the actual bundle identifier, display name, short version, build version, device families, and declared app icon files. If the IPA icon is unavailable, it looks for a conventional PNG app icon in the repository. It never infers a bundle identifier from a repository name or release tag.

The source contains historical IPA versions ordered by release date, with direct GitHub asset URLs, GitHub asset sizes, and release notes. Repository description and owner provide the app description and developer label when those are available.

## Configure repositories

Add or remove public GitHub repository URLs in this format:

```json
{
  "repositories": [
    "https://github.com/owner/repository",
    "https://github.com/another-owner/app"
  ]
}
```

Only direct URLs in the form `https://github.com/owner/repository` are accepted. Private repositories, malformed URLs, repositories with no inspectable published IPA releases, and duplicate app bundle identifiers are reported in the workflow summary. A broken repository does not prevent other configured repositories from being indexed.

When a release contains multiple IPAs, KStore inspects each one and prefers a build whose `UIDeviceFamily` includes both iPhone and iPad. If equally suitable files contain conflicting app metadata, that release is reported as ambiguous and skipped rather than selecting an arbitrary variant. IPA downloads over 1 GiB, malformed archives, missing version metadata, and unsafe ZIP paths are rejected.

KStore caches extracted metadata and icons by repository, release, and asset identity. The cache never stores IPA binaries. A changed asset ID, URL, size, or update timestamp produces a cache miss.

## Build locally

Python 3.10 or newer is required.

```bash
python -m pip install -r requirements.txt
```

The builder needs the actual HTTPS base URL returned by GitHub Pages. Get it from the repository's Pages API or set it for local runs:

```bash
KSTORE_BASE_URL=https://egokam.github.io/KStore python scripts/build_source.py
```

An optional `GITHUB_TOKEN` or `GH_TOKEN` can raise GitHub API limits. It is sent only to `api.github.com`; IPA downloads are unauthenticated, including redirects to GitHub's asset hosts.

Generated files are written to `dist/`:

- `dist/source.json`
- `dist/index.html`
- `dist/icons/*.png`

The generator validates `source.json` against the vendored official SideStore JSON Schema before atomically replacing `dist/`. If every repository fails or yields no app, the build fails and leaves the previous output intact.

Run the offline tests with:

```bash
python -m unittest discover -s tests -v
```

## GitHub Pages automation

The `Build KStore source` workflow runs every six hours, when the catalog, builder, schema, requirements, or tests change on `main`, and when manually dispatched. It restores metadata from GitHub Actions cache, validates the output, uploads the Pages artifact, then deploys it. A failed build never reaches the deploy job, so it cannot replace the last successful Pages deployment.

GitHub Pages must use **GitHub Actions** as its publishing source. The workflow reads the configured Pages URL from GitHub's API and uses that URL for the source, icons, and landing page. The landing page includes an **Add to SideStore** link.

## Format and known limitations

The generated source follows the current official SideStore source schema. SideStore's schema has an app-level `beta` flag but no per-version prerelease field. KStore sets that flag when the newest indexed release is a prerelease; older prereleases remain in the version history without an unsupported field.

Modern icons stored only in Apple's compiled `Assets.car` cannot be extracted with the standard library. If no declared IPA icon or conventional repository PNG can be read, KStore publishes a generated fallback PNG. The source does not claim IPA signing, device compatibility beyond the app's declared device families, or safety; users should only add trusted repositories.
