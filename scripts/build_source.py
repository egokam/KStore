#!/usr/bin/env python3
"""Build a SideStore source from public GitHub repositories and release IPAs."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from jsonschema import Draft7Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "repositories.json"
SCHEMA_PATH = ROOT / "schemas" / "sidestore-source-schema.json"
DIST = ROOT / "dist"
API = "https://api.github.com"
SOURCE_NAME = "KStore"
SOURCE_IDENTIFIER = "com.egokam.KStore"
MAX_IPA_SIZE = 1024 * 1024 * 1024
MAX_PLIST_SIZE = 4 * 1024 * 1024
MAX_ICON_SIZE = 12 * 1024 * 1024
MAX_ZIP_ENTRIES = 200_000
USER_AGENT = "KStore-Release-Builder/2.0"


class BuildError(RuntimeError):
    """An error that should be reported without a Python traceback."""


class GitHubError(BuildError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class GitHubAssetRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Follow GitHub release redirects only to HTTPS GitHub-controlled hosts."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parsed = urllib.parse.urlparse(newurl)
        host = (parsed.hostname or "").casefold()
        trusted_host = (
            host == "github.com"
            or host.endswith(".github.com")
            or host == "githubusercontent.com"
            or host.endswith(".githubusercontent.com")
        )
        if parsed.scheme != "https" or not trusted_host:
            raise BuildError(f"Refusing IPA redirect to an untrusted host: {newurl}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def parse_repository_url(value: str) -> str:
    """Return a canonical owner/repo pair, rejecting ambiguous URL forms."""
    if not isinstance(value, str) or not value.strip():
        raise BuildError("Each repository entry must be a non-empty GitHub URL.")
    parsed = urllib.parse.urlparse(value.strip())
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "github.com"
        or parsed.query
        or parsed.fragment
    ):
        raise BuildError(f"Invalid GitHub repository URL: {value!r}")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2:
        raise BuildError(f"Repository URL must point to github.com/owner/repository: {value!r}")
    owner, repository = parts
    if repository.lower().endswith(".git"):
        repository = repository[:-4]
    allowed = re.compile(r"^[A-Za-z0-9_.-]+$")
    if not allowed.fullmatch(owner) or not allowed.fullmatch(repository):
        raise BuildError(f"Invalid owner or repository name in URL: {value!r}")
    if owner in {".", ".."} or repository in {".", ".."}:
        raise BuildError(f"Invalid owner or repository name in URL: {value!r}")
    return f"{owner}/{repository}"


def load_config(path: Path = CONFIG_PATH) -> list[str]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BuildError("repositories.json is missing.") from exc
    except json.JSONDecodeError as exc:
        raise BuildError(f"repositories.json is invalid JSON: {exc}") from exc
    if not isinstance(config, dict) or set(config) != {"repositories"}:
        raise BuildError("repositories.json must contain only a 'repositories' array.")
    repositories = config["repositories"]
    if not isinstance(repositories, list) or not repositories:
        raise BuildError("Add at least one public GitHub repository URL to repositories.json.")
    normalized: list[str] = []
    seen: set[str] = set()
    for value in repositories:
        repository = parse_repository_url(value)
        key = repository.casefold()
        if key in seen:
            raise BuildError(f"Duplicate repository URL: {value}")
        normalized.append(repository)
        seen.add(key)
    return normalized


def _retry_delay(headers: Any, attempt: int) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.25), 30.0)
        except ValueError:
            pass
    reset = headers.get("X-RateLimit-Reset") if headers else None
    remaining = headers.get("X-RateLimit-Remaining") if headers else None
    if reset and remaining == "0":
        try:
            return min(max(float(reset) - time.time() + 1, 0.25), 30.0)
        except ValueError:
            pass
    return min(0.5 * (2**attempt), 8.0)


def request_json(url: str, token: str | None, *, attempts: int = 5) -> Any:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": USER_AGENT,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                try:
                    return json.loads(response.read().decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BuildError(f"GitHub returned invalid JSON for {url}.") from exc
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            retryable = exc.code in {429, 500, 502, 503, 504}
            if exc.code == 403:
                retryable = (
                    exc.headers.get("X-RateLimit-Remaining") == "0"
                    or "secondary rate limit" in detail.lower()
                    or "abuse detection" in detail.lower()
                )
            message = f"GitHub API returned HTTP {exc.code} for {url}: {detail}"
            if not retryable or attempt + 1 >= attempts:
                raise GitHubError(exc.code, message) from exc
            time.sleep(_retry_delay(exc.headers, attempt))
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt + 1 >= attempts:
                raise BuildError(f"Network error requesting {url}: {exc}") from exc
            time.sleep(min(0.5 * (2**attempt), 8.0))
    raise BuildError(f"GitHub API request failed after {attempts} attempts: {url}")


def fetch_all_releases(repository: str, token: str | None) -> list[dict[str, Any]]:
    releases: list[dict[str, Any]] = []
    page = 1
    encoded = urllib.parse.quote(repository, safe="/")
    while True:
        batch = request_json(
            f"{API}/repos/{encoded}/releases?per_page=100&page={page}", token
        )
        if not isinstance(batch, list):
            raise BuildError(f"Unexpected releases response for {repository}.")
        releases.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            return releases
        page += 1
        time.sleep(0.15)


def cache_identity(repository: dict[str, Any], release: dict[str, Any], asset: dict[str, Any]) -> dict[str, Any]:
    return {
        "repository_id": repository.get("id"),
        "repository": repository.get("full_name"),
        "release_id": release.get("id"),
        "asset_id": asset.get("id"),
        "name": asset.get("name"),
        "size": asset.get("size"),
        "updated_at": asset.get("updated_at"),
        "download_url": asset.get("browser_download_url"),
    }


class MetadataCache:
    """Caches extracted plist fields and icon PNGs without retaining IPA files."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.icon_root = root / "icons"
        self.icon_root.mkdir(parents=True, exist_ok=True)
        self.index_path = root / "metadata.json"
        try:
            parsed = json.loads(self.index_path.read_text(encoding="utf-8"))
            self.entries = parsed if isinstance(parsed, dict) else {}
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self.entries: dict[str, Any] = {}

    @staticmethod
    def key(identity: dict[str, Any]) -> str:
        stable = json.dumps(identity, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(stable.encode("utf-8")).hexdigest()

    def get(self, identity: dict[str, Any]) -> dict[str, Any] | None:
        key = self.key(identity)
        entry = self.entries.get(key)
        if not isinstance(entry, dict) or entry.get("identity") != identity:
            return None
        icon_name = entry.get("icon")
        if icon_name is not None:
            if not isinstance(icon_name, str) or Path(icon_name).name != icon_name:
                return None
            if not (self.icon_root / icon_name).is_file():
                return None
        metadata = entry.get("metadata")
        if not isinstance(metadata, dict):
            return None
        result = dict(metadata)
        result["cached_icon"] = str(self.icon_root / icon_name) if icon_name else None
        return result

    def put(self, identity: dict[str, Any], metadata: dict[str, Any], icon: bytes | None) -> dict[str, Any]:
        key = self.key(identity)
        icon_name: str | None = None
        icon_path: Path | None = None
        if icon:
            icon_name = f"{key}.png"
            icon_path = self.icon_root / icon_name
            temp_icon = icon_path.with_suffix(".tmp")
            temp_icon.write_bytes(icon)
            temp_icon.replace(icon_path)
        saved = dict(metadata)
        saved["cached_icon"] = None
        self.entries[key] = {"identity": identity, "metadata": saved, "icon": icon_name}
        self.save()
        result = dict(saved)
        result["cached_icon"] = str(icon_path) if icon_path else None
        return result

    def save(self) -> None:
        temp_path = self.index_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(self.entries, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        temp_path.replace(self.index_path)


def download_asset(url: str, expected_size: int, destination: Path) -> None:
    parsed_url = urllib.parse.urlparse(url)
    if parsed_url.scheme != "https" or (parsed_url.hostname or "").casefold() != "github.com":
        raise BuildError("IPA download URL must be a direct HTTPS github.com release URL.")
    if expected_size <= 0:
        raise BuildError("GitHub reports an invalid or missing IPA size.")
    if expected_size > MAX_IPA_SIZE:
        raise BuildError(f"IPA exceeds the {MAX_IPA_SIZE // (1024 * 1024)} MiB inspection limit.")

    # Deliberately do not attach the GitHub API token here. GitHub may redirect this
    # download to an asset host; no authorization header is sent to that host.
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    opener = urllib.request.build_opener(GitHubAssetRedirectHandler)
    downloaded = 0
    try:
        with opener.open(request, timeout=90) as response, destination.open("wb") as output:
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_IPA_SIZE:
                raise BuildError("IPA response exceeds the inspection size limit.")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
                if downloaded > MAX_IPA_SIZE:
                    raise BuildError("IPA response exceeded the inspection size limit.")
                output.write(chunk)
    except urllib.error.HTTPError as exc:
        raise BuildError(f"IPA download returned HTTP {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        if isinstance(exc, BuildError):
            raise
        raise BuildError(f"IPA download failed: {exc}") from exc
    if downloaded != expected_size:
        raise BuildError(f"IPA size mismatch: GitHub reports {expected_size}, downloaded {downloaded} bytes.")


def _icon_names(info: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for key in ("CFBundleIcons", "CFBundleIcons~ipad"):
        icons = info.get(key)
        if not isinstance(icons, dict):
            continue
        primary = icons.get("CFBundlePrimaryIcon")
        if isinstance(primary, dict):
            files = primary.get("CFBundleIconFiles", [])
            if isinstance(files, list):
                names.extend(value for value in files if isinstance(value, str))
    legacy = info.get("CFBundleIconFiles", [])
    if isinstance(legacy, list):
        names.extend(value for value in legacy if isinstance(value, str))
    return list(dict.fromkeys(names))


def _read_app_icon(archive: zipfile.ZipFile, members: list[zipfile.ZipInfo], app_dir: str, info: dict[str, Any]) -> bytes | None:
    names = _icon_names(info)
    if not names:
        return None
    candidates: list[tuple[int, int, zipfile.ZipInfo]] = []
    for member in members:
        if member.is_dir() or member.file_size > MAX_ICON_SIZE or not member.filename.lower().endswith(".png"):
            continue
        path = PurePosixPath(member.filename)
        if "/".join(path.parts[:-1]) != app_dir:
            continue
        stem = path.stem.casefold()
        for declared in names:
            declared_stem = PurePosixPath(declared).stem.casefold()
            if stem == declared_stem or re.fullmatch(re.escape(declared_stem) + r"@(?:2|3)x", stem):
                try:
                    content = archive.read(member)
                except (OSError, RuntimeError, zipfile.BadZipFile):
                    continue
                if len(content) < 24 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
                    continue
                width, height = struct.unpack(">II", content[16:24])
                if not width or not height or width > 8192 or height > 8192:
                    continue
                candidates.append((width * height, len(content), member))
                break
    if not candidates:
        return None
    _, _, selected = max(candidates, key=lambda candidate: (candidate[0], candidate[1], candidate[2].filename))
    return archive.read(selected)


def inspect_ipa(path: Path, repository_name: str) -> tuple[dict[str, Any], bytes | None]:
    try:
        if path.stat().st_size > MAX_IPA_SIZE:
            raise BuildError("IPA exceeds the inspection size limit.")
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ZIP_ENTRIES:
                raise BuildError(f"IPA has too many ZIP entries ({len(members)}).")
            for member in members:
                raw_path = member.filename.replace("\\", "/")
                parsed_path = PurePosixPath(raw_path)
                if not parsed_path.parts or parsed_path.is_absolute() or ".." in parsed_path.parts or ":" in parsed_path.parts[0]:
                    raise BuildError(f"IPA contains an unsafe ZIP path: {member.filename!r}")

            app_plists = [
                member for member in members
                if len(PurePosixPath(member.filename).parts) == 3
                and PurePosixPath(member.filename).parts[0] == "Payload"
                and PurePosixPath(member.filename).parts[1].endswith(".app")
                and PurePosixPath(member.filename).parts[2] == "Info.plist"
            ]
            if len(app_plists) != 1:
                raise BuildError(f"IPA must contain one top-level Payload/*.app/Info.plist; found {len(app_plists)}.")
            info_member = app_plists[0]
            if info_member.flag_bits & 0x1:
                raise BuildError("IPA contains an encrypted Info.plist.")
            if info_member.file_size > MAX_PLIST_SIZE:
                raise BuildError("IPA Info.plist exceeds the metadata size limit.")
            with archive.open(info_member) as plist_file:
                plist_data = plist_file.read(MAX_PLIST_SIZE + 1)
            if len(plist_data) > MAX_PLIST_SIZE:
                raise BuildError("IPA Info.plist exceeds the metadata size limit.")
            import plistlib

            try:
                info = plistlib.loads(plist_data)
            except Exception as exc:
                raise BuildError(f"IPA Info.plist could not be parsed: {exc}") from exc
            if not isinstance(info, dict):
                raise BuildError("IPA Info.plist did not contain a dictionary.")
            bundle_id = info.get("CFBundleIdentifier")
            version = info.get("CFBundleShortVersionString")
            if not isinstance(bundle_id, str) or not bundle_id.strip():
                raise BuildError("IPA Info.plist has no CFBundleIdentifier; refusing to guess one.")
            if isinstance(version, bool) or not isinstance(version, (str, int, float)) or not str(version).strip():
                raise BuildError("IPA Info.plist has no CFBundleShortVersionString.")
            display_name = info.get("CFBundleDisplayName")
            bundle_name = info.get("CFBundleName")
            app_name = next(
                (value.strip() for value in (display_name, bundle_name, repository_name) if isinstance(value, str) and value.strip()),
                repository_name,
            )
            families = info.get("UIDeviceFamily", [])
            if not isinstance(families, list):
                families = []
            families = sorted({int(value) for value in families if isinstance(value, (int, float))})
            app_dir = "/".join(PurePosixPath(info_member.filename).parts[:2])
            icon = _read_app_icon(archive, members, app_dir, info)
            metadata = {
                "bundle_identifier": bundle_id.strip(),
                "name": app_name,
                "version": str(version).strip(),
                "bundle_version": str(info.get("CFBundleVersion", "")).strip(),
                "device_families": families,
            }
            return metadata, icon
    except BuildError:
        raise
    except (OSError, zipfile.BadZipFile, KeyError, ValueError, TypeError) as exc:
        raise BuildError(f"IPA is invalid or could not be inspected: {exc}") from exc


def inspect_asset(repository: dict[str, Any], release: dict[str, Any], asset: dict[str, Any], cache: MetadataCache) -> dict[str, Any]:
    identity = cache_identity(repository, release, asset)
    cached = cache.get(identity)
    if cached is not None:
        print(f"    cache hit: {asset.get('name', 'IPA')}")
        return cached
    size = asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        raise BuildError("GitHub reports an invalid IPA size.")
    with tempfile.NamedTemporaryFile(prefix="kstore-", suffix=".ipa", delete=False) as temp_file:
        temp_path = Path(temp_file.name)
    try:
        download_asset(str(asset.get("browser_download_url") or ""), size, temp_path)
        metadata, icon = inspect_ipa(temp_path, str(repository.get("name") or "App"))
        return cache.put(identity, metadata, icon)
    finally:
        temp_path.unlink(missing_ok=True)


def selection_score(filename: str, metadata: dict[str, Any]) -> int:
    families = set(metadata.get("device_families", []))
    if {1, 2}.issubset(families):
        score = 200
    elif not families:
        score = 50
    else:
        score = 10
    lower = filename.casefold()
    if re.search(r"(?:universal|all[-_ ]?(?:devices|platforms)|combined)", lower):
        score += 20
    if re.search(r"(?:iphone[-_ ]?only|ipad[-_ ]?only|iphone|ipad|watch|vision)", lower):
        score -= 30
    if "arm64e" in lower:
        score -= 5
    return score


def choose_release_asset(candidates: list[dict[str, Any]], repository_name: str, release_tag: str) -> dict[str, Any] | None:
    if not candidates:
        return None
    ranked = sorted(
        candidates,
        key=lambda item: (selection_score(item["asset_name"], item), item["asset_name"].casefold()),
        reverse=True,
    )
    best_score = selection_score(ranked[0]["asset_name"], ranked[0])
    best = [item for item in ranked if selection_score(item["asset_name"], item) == best_score]
    if len(best) == 1:
        return best[0]
    identities = {
        (
            item.get("bundle_identifier"),
            item.get("version"),
            item.get("bundle_version"),
            tuple(item.get("device_families", [])),
        )
        for item in best
    }
    variant_labels = {
        frozenset(re.findall(r"normal|smooth(?:player)?|classic|lite|full|debug|release|beta|stable|modded|plus", item["asset_name"].casefold()))
        for item in best
    }
    if len(identities) == 1 and len(variant_labels) == 1:
        selected = min(best, key=lambda item: item["asset_name"].casefold())
        print(f"    duplicate equivalent IPA assets in {release_tag}; selected {selected['asset_name']}.")
        return selected
    names = ", ".join(item["asset_name"] for item in best)
    print(f"    ambiguous IPA variants for {repository_name} {release_tag}; skipped release: {names}")
    return None


def iso_date(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    except ValueError:
        return None


def build_app(repository_name: str, repository: dict[str, Any], releases: list[dict[str, Any]], cache: MetadataCache) -> tuple[dict[str, Any] | None, int, list[str]]:
    successful: list[dict[str, Any]] = []
    skipped_versions = 0
    warnings: list[str] = []
    seen_asset_ids: set[int] = set()
    releases.sort(key=lambda release: iso_date(release.get("published_at")) or "", reverse=True)

    for release in releases:
        if release.get("draft") is True:
            continue
        tag = str(release.get("tag_name") or "(untagged)")
        published = iso_date(release.get("published_at")) or iso_date(release.get("created_at"))
        if not published:
            skipped_versions += 1
            warnings.append(f"{tag}: missing a usable release date")
            continue
        assets = release.get("assets", [])
        if not isinstance(assets, list):
            assets = []
        ipa_assets = [
            asset for asset in assets
            if isinstance(asset, dict)
            and isinstance(asset.get("name"), str)
            and asset["name"].casefold().endswith(".ipa")
            and isinstance(asset.get("browser_download_url"), str)
        ]
        if not ipa_assets:
            skipped_versions += 1
            continue

        candidates: list[dict[str, Any]] = []
        for asset in ipa_assets:
            asset_id = asset.get("id")
            if isinstance(asset_id, int) and asset_id in seen_asset_ids:
                print(f"    duplicate asset id {asset_id}; skipped {asset['name']}.")
                continue
            if isinstance(asset_id, int):
                seen_asset_ids.add(asset_id)
            try:
                metadata = inspect_asset(repository, release, asset, cache)
            except BuildError as exc:
                message = f"{tag}/{asset['name']}: {exc}"
                warnings.append(message)
                print(f"    skipped IPA: {message}")
                continue
            metadata = dict(metadata)
            metadata.update({"asset_name": asset["name"], "asset": asset, "release": release, "date": published})
            candidates.append(metadata)

        selected = choose_release_asset(candidates, repository_name, tag)
        if selected is None:
            skipped_versions += 1
            if candidates:
                warnings.append(f"{tag}: ambiguous IPA variants could not be selected safely")
            if not ipa_assets:
                warnings.append(f"{tag}: no IPA assets")
            continue
        print(f"    selected {selected['asset_name']} ({selected['version']}, {selected['bundle_identifier']})")
        successful.append(selected)

    if not successful:
        return None, skipped_versions, warnings
    successful.sort(key=lambda item: item["date"], reverse=True)
    app_identity = successful[0]["bundle_identifier"]
    versions: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    seen_versions: set[str] = set()
    for item in successful:
        if item["bundle_identifier"] != app_identity:
            warning = (
                f"{item['release'].get('tag_name', 'release')}: bundle identifier "
                f"{item['bundle_identifier']} differs from latest app {app_identity}; skipped"
            )
            warnings.append(warning)
            print(f"    {warning}")
            skipped_versions += 1
            continue
        asset = item["asset"]
        url = asset["browser_download_url"]
        version = item["version"]
        if url in seen_urls:
            skipped_versions += 1
            print(f"    duplicate IPA URL for {version}; skipped.")
            continue
        if version in seen_versions:
            skipped_versions += 1
            warning = f"duplicate IPA version {version}; kept the newest published release"
            warnings.append(warning)
            print(f"    {warning}.")
            continue
        notes = item["release"].get("body") or item["release"].get("name") or ""
        if not isinstance(notes, str):
            notes = ""
        versions.append({
            "version": version,
            "date": item["date"],
            "downloadURL": url,
            "size": int(asset["size"]),
            "localizedDescription": notes.strip()[:12_000],
        })
        seen_urls.add(url)
        seen_versions.add(version)
    if not versions:
        return None, skipped_versions, warnings

    latest = successful[0]
    app_name = latest["name"].strip() or repository_name
    owner = repository.get("owner") or {}
    developer_name = owner.get("name") or owner.get("login") or "Unknown developer"
    app = {
        "name": app_name,
        "bundleIdentifier": app_identity,
        "developerName": str(developer_name),
        "localizedDescription": str(repository.get("description") or ""),
        "versions": versions,
    }
    if latest["release"].get("prerelease") is True:
        app["beta"] = True
    return app, skipped_versions, warnings


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    body = chunk_type + data
    return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)


def fallback_icon(label: str) -> bytes:
    """Create a small deterministic PNG fallback using only Python's stdlib."""
    digest = hashlib.sha256(label.encode("utf-8", errors="replace")).digest()
    background = (38 + digest[0] % 60, 58 + digest[1] % 65, 92 + digest[2] % 90)
    pixels = bytearray()
    character = (label.strip()[:1] or "?").upper()
    glyphs = {
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
        "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
        "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
        "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
        "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
        "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
        "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
        "I": ("11111", "00100", "00100", "00100", "00100", "00100", "11111"),
        "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
        "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
        "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
        "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
        "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
        "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
        "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
        "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
        "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
        "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
        "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
        "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
        "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
        "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
        "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
        "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
        "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
        "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
        "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
        "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
        "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
        "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
        "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    }
    glyph = glyphs.get(character, glyphs["K"])
    scale = 12
    x_start, y_start = 34, 22
    for y in range(128):
        pixels.append(0)
        for x in range(128):
            on = False
            gx = (x - x_start) // scale
            gy = (y - y_start) // scale
            if 0 <= gx < 5 and 0 <= gy < 7 and x >= x_start and y >= y_start:
                on = glyph[gy][gx] == "1"
            pixels.extend((245, 248, 255) if on else background)
    header = struct.pack(">IIBBBBB", 128, 128, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(bytes(pixels), 9)) + _png_chunk(b"IEND", b"")


def validate_source(source: dict[str, Any], schema_path: Path = SCHEMA_PATH) -> None:
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"Could not load vendored SideStore source schema: {exc}") from exc
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema, format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(source), key=lambda error: list(error.absolute_path))
    if errors:
        details = "; ".join(
            f"{'.'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message}"
            for error in errors[:8]
        )
        raise BuildError(f"Generated source failed SideStore schema validation: {details}")
    if not source.get("apps"):
        raise BuildError("Refusing to publish a source with zero discovered apps.")
    bundle_ids: set[str] = set()
    for app in source["apps"]:
        bundle_id = app["bundleIdentifier"]
        if bundle_id in bundle_ids:
            raise BuildError(f"Duplicate app bundle identifier in source: {bundle_id}")
        bundle_ids.add(bundle_id)
        if not app["versions"]:
            raise BuildError(f"Refusing to publish {bundle_id} without an IPA version.")
        for version in app["versions"]:
            if not version["downloadURL"].startswith("https://"):
                raise BuildError(f"Version URL for {bundle_id} must use HTTPS.")
            if not iso_date(version["date"]):
                raise BuildError(f"Version date for {bundle_id} is not a valid ISO date.")
    for field in ("sourceURL",):
        value = source.get(field)
        if value is not None and (not isinstance(value, str) or not value.startswith("https://")):
            raise BuildError(f"{field} must be an HTTPS URL.")
    for app in source["apps"]:
        if not app["iconURL"].startswith("https://"):
            raise BuildError(f"Icon URL for {app['bundleIdentifier']} must use HTTPS.")


def _html_page(source: dict[str, Any], source_url: str, version_count: int) -> str:
    safe_name = html.escape(source["name"])
    add_url = "sidestore://source?url=" + urllib.parse.quote(source_url, safe="")
    rows = "".join(
        "<li><img src=\"{}\" alt=\"\" width=\"48\" height=\"48\"><span><strong>{}</strong><small>{} · {} version(s)</small></span></li>".format(
            html.escape(app["iconURL"], quote=True),
            html.escape(app["name"]),
            html.escape(app["developerName"]),
            len(app["versions"]),
        )
        for app in source["apps"]
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="theme-color" content="#111318">
  <title>{safe_name} · SideStore source</title>
  <style>
    :root {{ color-scheme: dark; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
    body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: radial-gradient(circle at 50% 0%, #27334a, #111318 55%); color: #f5f7fb; }}
    main {{ width: min(680px, calc(100% - 40px)); padding: 42px 0; }}
    .card {{ border: 1px solid #343b49; border-radius: 24px; padding: 30px; background: #171b24ed; box-shadow: 0 24px 90px #0008; }}
    .eyebrow {{ color: #8db6ff; letter-spacing: .16em; text-transform: uppercase; font-size: 12px; font-weight: 700; }}
    h1 {{ font-size: clamp(38px, 8vw, 64px); letter-spacing: -.06em; margin: 12px 0; }}
    p {{ color: #aeb8c8; line-height: 1.65; }}
    a {{ display: inline-block; margin: 12px 10px 0 0; padding: 12px 16px; border-radius: 12px; background: #dce8ff; color: #101522; text-decoration: none; font-weight: 700; }}
    a.secondary {{ background: #293244; color: #eaf0ff; }}
    ul {{ display: grid; gap: 12px; margin: 26px 0 0; padding: 0; list-style: none; }}
    li {{ display: flex; align-items: center; gap: 14px; padding: 12px; border: 1px solid #343b49; border-radius: 14px; }}
    img {{ border-radius: 11px; object-fit: cover; }}
    small {{ display: block; color: #aeb8c8; margin-top: 4px; }}
    footer {{ margin-top: 22px; color: #778398; font-size: 12px; }}
  </style>
</head>
<body>
  <main><section class="card">
    <div class="eyebrow">Automated SideStore source</div>
    <h1>{safe_name}</h1>
    <p>{len(source['apps'])} app(s) · {version_count} IPA version(s) · Refreshed by GitHub Actions every six hours</p>
    <a href="{html.escape(add_url, quote=True)}">Add to SideStore</a>
    <a class="secondary" href="./source.json">View source.json</a>
    <ul>{rows}</ul>
    <footer>App metadata and IPA downloads remain hosted by their respective GitHub projects.</footer>
  </section></main>
</body>
</html>
"""


def _atomic_publish(staging: Path, destination: Path) -> None:
    backup = destination.with_name(destination.name + ".previous")
    if backup.exists() and destination.exists():
        shutil.rmtree(backup)
    elif backup.exists() and not destination.exists():
        backup.replace(destination)
    moved_previous = False
    try:
        if destination.exists():
            destination.replace(backup)
            moved_previous = True
        staging.replace(destination)
    except OSError:
        if moved_previous and backup.exists() and not destination.exists():
            backup.replace(destination)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def write_outputs(
    source: dict[str, Any],
    icon_sources: dict[str, Path | None],
    base_url: str,
    version_count: int,
    destination: Path = DIST,
) -> None:
    normalized_base = base_url.rstrip("/")
    source_url = f"{normalized_base}/source.json"
    source["sourceURL"] = source_url

    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        icon_directory = staging / "icons"
        icon_directory.mkdir()
        for app in source["apps"]:
            bundle_id = app["bundleIdentifier"]
            icon_source = icon_sources.get(bundle_id)
            if icon_source and icon_source.is_file():
                icon_bytes = icon_source.read_bytes()
            else:
                icon_bytes = fallback_icon(app["name"])
            digest = hashlib.sha256(icon_bytes).hexdigest()
            icon_name = f"{digest}.png"
            (icon_directory / icon_name).write_bytes(icon_bytes)
            app["iconURL"] = f"{normalized_base}/icons/{icon_name}"
        validate_source(source)
        source_path = staging / "source.json"
        source_path.write_text(json.dumps(source, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        # Validate the exact bytes that will be deployed, not only the in-memory object.
        validate_source(json.loads(source_path.read_text(encoding="utf-8")))
        (staging / "index.html").write_text(_html_page(source, source_url, version_count), encoding="utf-8")
        _atomic_publish(staging, destination)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


def _write_summary(stats: dict[str, Any]) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    skipped = stats["skipped_repositories"]
    lines = [
        "# KStore source build",
        "",
        f"- Configured repositories: {stats['configured_repositories']}",
        f"- Discovered apps: {stats['discovered_apps']}",
        f"- Indexed IPA versions: {stats['indexed_versions']}",
        f"- Skipped releases or assets: {stats['skipped_releases']}",
        f"- Skipped repositories: {len(skipped)}",
    ]
    if skipped:
        lines.extend(["", "## Skipped repositories", ""])
        lines.extend(f"- `{name.replace('`', '')}`: {reason.replace('`', '')}" for name, reason in skipped)
    release_warnings = stats.get("release_warnings", [])
    if release_warnings:
        lines.extend(["", "## Skipped releases and assets", ""])
        lines.extend(
            f"- `{name.replace('`', '')}`: {reason.replace('`', '')}"
            for name, reason in release_warnings
        )
    with open(summary_path, "a", encoding="utf-8") as output:
        output.write("\n".join(lines) + "\n")


def build_source(repositories: list[str], token: str | None, base_url: str, cache_root: Path) -> dict[str, Any]:
    if not base_url.startswith("https://"):
        raise BuildError("KSTORE_BASE_URL must be the verified HTTPS GitHub Pages URL.")
    cache = MetadataCache(cache_root)
    apps: list[dict[str, Any]] = []
    icon_sources: dict[str, Path | None] = {}
    skipped_repositories: list[tuple[str, str]] = []
    indexed_versions = 0
    skipped_releases = 0
    release_warnings: list[tuple[str, str]] = []
    seen_bundle_ids: set[str] = set()

    for repository_url in repositories:
        short_name = repository_url.rsplit("/", 1)[-1]
        print(f"Fetching {repository_url} ...")
        try:
            encoded = urllib.parse.quote(repository_url, safe="/")
            repository = request_json(f"{API}/repos/{encoded}", token)
            if not isinstance(repository, dict):
                raise BuildError("Unexpected repository response.")
            if repository.get("private") is True:
                raise BuildError("Repository is private; KStore only publishes public releases.")
            releases = fetch_all_releases(repository_url, token)
            app, skipped_versions, warnings = build_app(
                str(repository.get("name") or short_name), repository, releases, cache
            )
            skipped_releases += skipped_versions
            release_warnings.extend((repository_url, warning) for warning in warnings)
            for warning in warnings:
                print(f"    warning: {warning}")
            if app is None:
                raise BuildError("No published release had an inspectable IPA asset.")
            bundle_id = app["bundleIdentifier"]
            if bundle_id in seen_bundle_ids:
                raise BuildError(f"Duplicate bundle identifier {bundle_id}; earlier repository kept.")
            latest_release = next(
                item for item in releases
                if not item.get("draft")
                and any(version["downloadURL"] == asset.get("browser_download_url") for version in app["versions"] for asset in (item.get("assets") or []))
            )
            latest_version_url = app["versions"][0]["downloadURL"]
            latest_asset = next(
                asset for asset in latest_release.get("assets", [])
                if asset.get("browser_download_url") == latest_version_url
            )
            latest_identity = cache_identity(repository, latest_release, latest_asset)
            cached_latest = cache.get(latest_identity)
            icon_sources[bundle_id] = Path(cached_latest["cached_icon"]) if cached_latest and cached_latest.get("cached_icon") else None
            if icon_sources[bundle_id] is None:
                print(f"    no usable Info.plist app icon; publishing generated fallback icon.")
            seen_bundle_ids.add(bundle_id)
            apps.append(app)
            indexed_versions += len(app["versions"])
            print(f"  included {app['name']}: {len(app['versions'])} IPA version(s); {skipped_versions} skipped release(s).")
        except (BuildError, StopIteration, KeyError, TypeError, ValueError) as exc:
            reason = str(exc)
            skipped_repositories.append((repository_url, reason))
            print(f"  skipped repository: {reason}")

    cache.save()
    if not apps:
        stats = {
            "configured_repositories": len(repositories),
            "discovered_apps": 0,
            "indexed_versions": 0,
            "skipped_releases": skipped_releases,
            "skipped_repositories": skipped_repositories,
            "release_warnings": release_warnings,
        }
        _write_summary(stats)
        details = "; ".join(f"{name}: {reason}" for name, reason in skipped_repositories)
        raise BuildError(f"No apps were discovered; refusing to publish an empty source. {details}")

    source = {
        "name": SOURCE_NAME,
        "identifier": SOURCE_IDENTIFIER,
        "apps": apps,
    }
    stats = {
        "configured_repositories": len(repositories),
        "discovered_apps": len(apps),
        "indexed_versions": indexed_versions,
        "skipped_releases": skipped_releases,
        "skipped_repositories": skipped_repositories,
        "release_warnings": release_warnings,
    }
    try:
        write_outputs(source, icon_sources, base_url, indexed_versions)
    except BuildError:
        _write_summary(stats)
        raise
    _write_summary(stats)
    return stats


def main() -> int:
    try:
        repositories = load_config()
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        base_url = os.environ.get("KSTORE_BASE_URL", "")
        cache_root = Path(os.environ.get("KSTORE_CACHE_DIR", str(ROOT / ".cache" / "kstore")))
        stats = build_source(repositories, token, base_url, cache_root)
        print(
            f"Built {DIST / 'source.json'} with {stats['discovered_apps']} app(s) "
            f"and {stats['indexed_versions']} IPA version(s)."
        )
        return 0
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
