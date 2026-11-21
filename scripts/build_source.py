#!/usr/bin/env python3
"""Build a SideStore-compatible source from configured GitHub repositories."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "repositories.json"
DIST = ROOT / "dist"
API = "https://api.github.com"


class BuildError(RuntimeError):
    pass


def request_json(url: str, token: str | None) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "EgoStore-Release-Builder/1.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=45) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise BuildError(f"GitHub API returned HTTP {exc.code} for {url}: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise BuildError(f"Network error requesting {url}: {exc}") from exc


def load_config() -> dict[str, Any]:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildError("repositories.json is missing.") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"repositories.json is invalid JSON: {exc}") from exc

    source = config.get("source", {})
    for key in ("name", "identifier"):
        if not isinstance(source.get(key), str) or not source[key].strip():
            raise BuildError(f"source.{key} must be a non-empty string.")

    apps = config.get("apps")
    if not isinstance(apps, list):
        raise BuildError("'apps' must be a JSON array.")

    seen_bundle_ids: set[str] = set()
    seen_repos: set[str] = set()
    for index, app in enumerate(apps):
        if not isinstance(app, dict):
            raise BuildError(f"apps[{index}] must be an object.")
        for key in ("repository", "name", "bundleIdentifier", "developerName", "iconURL"):
            if not isinstance(app.get(key), str) or not app[key].strip():
                raise BuildError(f"apps[{index}].{key} must be a non-empty string.")
        repo = app["repository"].strip()
        if not re.fullmatch(r"[^/\s]+/[^/\s]+", repo):
            raise BuildError(f"Invalid repository '{repo}'. Use owner/repository.")
        bundle_id = app["bundleIdentifier"].strip()
        if bundle_id in seen_bundle_ids:
            raise BuildError(f"Duplicate bundleIdentifier: {bundle_id}")
        if repo.lower() in seen_repos:
            raise BuildError(f"Duplicate repository: {repo}")
        seen_bundle_ids.add(bundle_id)
        seen_repos.add(repo.lower())
        if not app["iconURL"].startswith("https://"):
            raise BuildError(f"apps[{index}].iconURL must use HTTPS.")
    return config


def fetch_all_releases(repository: str, token: str | None) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    encoded_repo = quote(repository, safe="/")
    while True:
        url = f"{API}/repos/{encoded_repo}/releases?per_page=100&page={page}"
        batch = request_json(url, token)
        if not isinstance(batch, list):
            raise BuildError(f"Unexpected releases response for {repository}.")
        releases.extend(batch)
        if len(batch) < 100:
            break
        page += 1
        # Avoid hammering the API when a repository has a very large history.
        time.sleep(0.15)
    return releases


def choose_ipa(assets: list[dict[str, Any]], pattern: str | None, repository: str, tag: str) -> dict[str, Any] | None:
    ipa_assets = [
        asset for asset in assets
        if isinstance(asset.get("name"), str)
        and asset["name"].lower().endswith(".ipa")
        and isinstance(asset.get("browser_download_url"), str)
    ]
    if not ipa_assets:
        return None
    ipa_assets.sort(key=lambda item: item["name"].lower())
    if pattern:
        try:
            matcher = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            raise BuildError(f"Invalid assetPattern for {repository} ({tag}): {exc}") from exc
        matches = [asset for asset in ipa_assets if matcher.search(asset["name"])]
        return matches[0] if matches else None
    return ipa_assets[0]


def iso_date(value: str | None) -> str:
    if not value:
        return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return "1970-01-01T00:00:00Z"


def normalize_tag(tag: str) -> str:
    # Common GitHub tag convention. Keep unusual suffixes such as -beta.1.
    return tag[1:] if tag.lower().startswith("v") and len(tag) > 1 else tag


def build_app(app: dict[str, Any], token: str | None) -> dict[str, Any]:
    repository = app["repository"].strip()
    print(f"Fetching {repository} ...")
    releases = fetch_all_releases(repository, token)
    overrides = app.get("versionOverrides", {})
    if not isinstance(overrides, dict):
        raise BuildError(f"versionOverrides for {repository} must be an object.")

    versions: list[dict[str, Any]] = []
    skipped = 0
    for release in releases:
        # The Releases API lists published releases; drafts are excluded by GitHub.
        if release.get("draft", False):
            continue
        tag = str(release.get("tag_name") or "").strip()
        if not tag:
            skipped += 1
            continue
        asset = choose_ipa(
            release.get("assets", []) or [],
            app.get("assetPattern"),
            repository,
            tag,
        )
        if not asset:
            skipped += 1
            continue

        version = str(overrides.get(tag, normalize_tag(tag))).strip()
        if not version:
            skipped += 1
            continue

        notes = (release.get("body") or "").strip()
        if not notes:
            notes = f"GitHub release {tag}."
        # Keep source descriptions compact and avoid unbounded release-note payloads.
        notes = notes[:12000]

        versions.append({
            "version": version,
            "date": iso_date(release.get("published_at")),
            "downloadURL": asset["browser_download_url"],
            "size": int(asset.get("size") or 0),
            "localizedDescription": notes,
        })

    versions.sort(key=lambda item: item["date"], reverse=True)

    # Do not emit duplicate download URLs if a release was re-published under multiple tags.
    unique_versions: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for version in versions:
        if version["downloadURL"] not in seen_urls:
            unique_versions.append(version)
            seen_urls.add(version["downloadURL"])

    print(f"  {len(unique_versions)} IPA release(s) included; {skipped} release(s) skipped (no matching IPA or tag).")
    result = {
        "name": app["name"],
        "bundleIdentifier": app["bundleIdentifier"],
        "developerName": app["developerName"],
        "iconURL": app["iconURL"],
        "localizedDescription": app.get("localizedDescription", ""),
        "versions": unique_versions,
    }
    if app.get("beta") is True:
        result["beta"] = True
    return result


def main() -> int:
    try:
        config = load_config()
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        apps = [build_app(app, token) for app in config["apps"]]

        source = {
            "name": config["source"]["name"],
            "identifier": config["source"]["identifier"],
            "apps": apps,
        }
        if config["source"].get("subtitle"):
            source["subtitle"] = config["source"]["subtitle"]

        DIST.mkdir(parents=True, exist_ok=True)
        source_path = DIST / "source.json"
        source_path.write_text(
            json.dumps(source, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#111318">
  <title>{source['name']}</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: radial-gradient(circle at 50% 0%, #27334a, #111318 55%); color: #f5f7fb; }}
    main {{ width: min(680px, calc(100% - 40px)); padding: 42px 0; }}
    .card {{ border: 1px solid #343b49; border-radius: 24px; padding: 30px; background: #171b24dd; box-shadow: 0 24px 90px #0008; }}
    .eyebrow {{ color: #8db6ff; letter-spacing: .16em; text-transform: uppercase; font-size: 12px; font-weight: 700; }}
    h1 {{ font-size: clamp(38px, 8vw, 64px); letter-spacing: -.06em; margin: 12px 0; }}
    p {{ color: #aeb8c8; line-height: 1.65; }}
    a {{ display: inline-block; margin: 12px 10px 0 0; padding: 12px 16px; border-radius: 12px; background: #dce8ff; color: #101522; text-decoration: none; font-weight: 700; }}
    a.secondary {{ background: #293244; color: #eaf0ff; }}
    footer {{ margin-top: 22px; color: #778398; font-size: 12px; }}
  </style>
</head>
<body>
  <main><section class="card">
    <div class="eyebrow">Automated SideStore source</div>
    <h1>{source['name']}</h1>
    <p>{source.get('subtitle', 'Apps automatically collected from GitHub Releases.')}</p>
    <p>{len(apps)} app listing(s) · All published releases with matching IPA assets · Refreshed by GitHub Actions</p>
    <a href="./source.json">View source.json</a>
    <footer>Generated automatically. App metadata and binaries remain hosted by their respective GitHub projects.</footer>
  </section></main>
</body>
</html>
"""
        (DIST / "index.html").write_text(page, encoding="utf-8")
        print(f"Built {source_path} with {len(apps)} app listing(s).")
        return 0
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
