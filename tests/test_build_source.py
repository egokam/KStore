from __future__ import annotations

import json
import plistlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import build_source


class RepositoryConfigTests(unittest.TestCase):
    def test_repository_url_normalization(self) -> None:
        self.assertEqual(
            build_source.parse_repository_url("https://github.com/Owner/Project.git/"),
            "Owner/Project",
        )

    def test_rejects_non_repository_urls(self) -> None:
        for value in (
            "http://github.com/owner/repo",
            "https://github.com/owner/repo/releases",
            "https://example.com/owner/repo",
            "https://github.com/owner/repo?tab=releases",
        ):
            with self.subTest(value=value), self.assertRaises(build_source.BuildError):
                build_source.parse_repository_url(value)

    def test_config_requires_repository_urls_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config = Path(temp_dir) / "repositories.json"
            config.write_text(json.dumps({"repositories": ["https://github.com/a/b"]}))
            self.assertEqual(build_source.load_config(config), ["a/b"])
            config.write_text(json.dumps({"source": "KStore", "repositories": []}))
            with self.assertRaises(build_source.BuildError):
                build_source.load_config(config)


class IPAInspectionTests(unittest.TestCase):
    def _write_ipa(self, path: Path, *, unsafe_path: bool = False) -> None:
        info = {
            "CFBundleIdentifier": "com.example.music",
            "CFBundleDisplayName": "Music Example",
            "CFBundleName": "MusicExample",
            "CFBundleShortVersionString": "2.5.0",
            "CFBundleVersion": "250",
            "CFBundleIconFiles": ["AppIcon"],
            "UIDeviceFamily": [1, 2],
        }
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Payload/Music.app/Info.plist", plistlib.dumps(info))
            archive.writestr("Payload/Music.app/AppIcon@2x.png", build_source.fallback_icon("M"))
            if unsafe_path:
                archive.writestr("Payload/../../outside.txt", b"unsafe")

    def test_reads_actual_app_metadata_and_declared_icon(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ipa = Path(temp_dir) / "music.ipa"
            self._write_ipa(ipa)
            metadata, icon = build_source.inspect_ipa(ipa, "Repository Name")
        self.assertEqual(metadata["bundle_identifier"], "com.example.music")
        self.assertEqual(metadata["version"], "2.5.0")
        self.assertEqual(metadata["name"], "Music Example")
        self.assertEqual(metadata["device_families"], [1, 2])
        self.assertTrue(icon.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_rejects_zip_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            ipa = Path(temp_dir) / "unsafe.ipa"
            self._write_ipa(ipa, unsafe_path=True)
            with self.assertRaisesRegex(build_source.BuildError, "unsafe ZIP path"):
                build_source.inspect_ipa(ipa, "Repository Name")

    def test_prefers_universal_device_family(self) -> None:
        candidates = [
            {"asset_name": "MusicLocal-iphone.ipa", "bundle_identifier": "com.example.music", "version": "1", "bundle_version": "1", "device_families": [1]},
            {"asset_name": "MusicLocal-universal.ipa", "bundle_identifier": "com.example.music", "version": "1", "bundle_version": "1", "device_families": [1, 2]},
        ]
        selected = build_source.choose_release_asset(candidates, "MusicLocal", "v1")
        self.assertEqual(selected["asset_name"], "MusicLocal-universal.ipa")

    def test_reports_named_build_variants_as_ambiguous(self) -> None:
        candidates = [
            {"asset_name": "Music_Normal2.0.8.ipa", "bundle_identifier": "com.example.music", "version": "2.0.5", "bundle_version": "205", "device_families": [1, 2]},
            {"asset_name": "Music_SmoothPlayer2.0.8.ipa", "bundle_identifier": "com.example.music", "version": "2.0.5", "bundle_version": "205", "device_families": [1, 2]},
        ]
        self.assertIsNone(build_source.choose_release_asset(candidates, "MusicLocal", "v2.0.9"))

    def test_fallback_icon_is_a_png(self) -> None:
        image = build_source.fallback_icon("KStore")
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_repository_icon_finder_prefers_conventional_app_icon(self) -> None:
        tree = [
            {"type": "blob", "path": "assets/favicon.png", "size": 100},
            {"type": "blob", "path": "assets/icons/amber.png", "size": 1000},
            {"type": "blob", "path": "assets/icon.png", "size": 5000},
        ]
        selected = build_source._repository_icon_candidate(tree)
        self.assertEqual(selected["path"], "assets/icon.png")

    def test_developer_display_name_is_discovered_from_github_profile(self) -> None:
        repository = {"owner": {"login": "egokam", "type": "User"}}
        with patch.object(build_source, "request_json", return_value={"name": "Example Developer"}):
            self.assertEqual(build_source.discover_developer_name(repository, None), "Example Developer")


class CacheAndSchemaTests(unittest.TestCase):
    def test_cache_only_hits_exact_asset_identity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = build_source.MetadataCache(Path(temp_dir))
            identity = {"repository_id": 1, "release_id": 2, "asset_id": 3, "size": 42}
            cache.put(identity, {"bundle_identifier": "com.example.app", "version": "1.0"}, build_source.fallback_icon("A"))
            cache.save()
            self.assertIsNotNone(cache.get(identity))
            changed = dict(identity, size=43)
            self.assertIsNone(cache.get(changed))

    def test_generated_shape_matches_official_schema(self) -> None:
        source = {
            "name": "KStore",
            "identifier": "com.egokam.KStore",
            "sourceURL": "https://egokam.github.io/KStore/source.json",
            "apps": [{
                "name": "Example",
                "bundleIdentifier": "com.example.app",
                "developerName": "Developer",
                "localizedDescription": "Example app",
                "iconURL": "https://egokam.github.io/KStore/icons/example.png",
                "versions": [{
                    "version": "1.0.0",
                    "date": "2026-01-01T00:00:00Z",
                    "downloadURL": "https://github.com/example/app/releases/download/v1/app.ipa",
                    "size": 100,
                    "localizedDescription": "First release",
                }],
            }],
        }
        build_source.validate_source(source)
        source["unsupported"] = True
        with self.assertRaisesRegex(build_source.BuildError, "schema validation"):
            build_source.validate_source(source)

    def test_output_generation_publishes_fallback_icon_and_side_store_link(self) -> None:
        source = {
            "name": "KStore",
            "identifier": "com.egokam.KStore",
            "apps": [{
                "name": "Example",
                "bundleIdentifier": "com.example.app",
                "developerName": "Developer",
                "localizedDescription": "Example app",
                "versions": [{
                    "version": "1.0.0",
                    "date": "2026-01-01T00:00:00Z",
                    "downloadURL": "https://github.com/example/app/releases/download/v1/app.ipa",
                    "size": 100,
                }],
            }],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "dist"
            build_source.write_outputs(source, {"com.example.app": None}, "https://example.github.io/KStore", 1, destination)
            deployed = json.loads((destination / "source.json").read_text())
            build_source.validate_source(deployed)
            icon_url = deployed["apps"][0]["iconURL"]
            icon_name = icon_url.rsplit("/", 1)[-1]
            self.assertTrue((destination / "icons" / icon_name).is_file())
            page = (destination / "index.html").read_text()
            self.assertIn("sidestore://source?url=", page)
            self.assertIn("Add to SideStore", page)


if __name__ == "__main__":
    unittest.main()
