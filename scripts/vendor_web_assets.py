#!/usr/bin/env python3
"""Download and verify the web assets committed under static/vendor.

Usage:
    uv run python scripts/vendor_web_assets.py
    uv run python scripts/vendor_web_assets.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENDOR_DIR = ROOT / "src/inboxping/web/static/vendor"


@dataclass(frozen=True)
class Asset:
    path: str
    url: str
    sha256: str


ASSETS = (
    Asset(
        "bootstrap/bootstrap.min.css",
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/dist/css/bootstrap.min.css",
        "d85327d99c7a3ee1f9b5d0500d1370acea3ad2db39c163c2f51f232baedbdede",
    ),
    Asset(
        "bootstrap/LICENSE",
        "https://cdn.jsdelivr.net/npm/bootstrap@5.3.8/LICENSE",
        "4620c84ad5ce8602ff65640ed6b7c8b78ebb9e036584f0ebc1ccc88206a4bb51",
    ),
    Asset(
        "bootstrap-icons/bootstrap-icons.min.css",
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/bootstrap-icons.min.css",
        "a5d6387a32ca3baec4d02336b5b3edab50c9dd518355576a011ea3dd9c1d884e",
    ),
    Asset(
        "bootstrap-icons/fonts/bootstrap-icons.woff2",
        "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.13.1/font/fonts/"
        "bootstrap-icons.woff2",
        "6c75710364a1ca5604267716f6d28997b26319fdb078cf11e0b42ab66ff2ea61",
    ),
    Asset(
        "bootstrap-icons/LICENSE",
        "https://raw.githubusercontent.com/twbs/icons/v1.13.1/LICENSE",
        "0fb3e11bd57e896c5a512afd64864d28a37de45d19835016c87ca1ad19ead969",
    ),
    Asset(
        "alpine/alpine.min.js",
        "https://cdn.jsdelivr.net/npm/alpinejs@3.15.12/dist/cdn.min.js",
        "57b37d7cae9a27d965fdae4adcc844245dfdc407e655aee85dcfff3a08036a3f",
    ),
    Asset(
        "alpine/LICENSE",
        "https://raw.githubusercontent.com/alpinejs/alpine/v3.15.12/LICENSE.md",
        "08b7502da6e7aa1d0bbdc97d220fbf669b9366c61bd0f072238283c89bc4773a",
    ),
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(asset: Asset, data: bytes) -> None:
    actual = digest(data)
    if actual != asset.sha256:
        raise RuntimeError(f"SHA-256 mismatch for {asset.path}: {actual}")


def check_committed_assets() -> None:
    for asset in ASSETS:
        path = VENDOR_DIR / asset.path
        verify(asset, path.read_bytes())
        print(f"ok  {asset.path}")


def download_assets() -> None:
    request_headers = {"User-Agent": "InboxPing vendor asset updater"}
    with tempfile.TemporaryDirectory(prefix=".vendor-download-", dir=ROOT) as temp_dir:
        staging = Path(temp_dir)
        for asset in ASSETS:
            request = urllib.request.Request(asset.url, headers=request_headers)
            with urllib.request.urlopen(request, timeout=30) as response:
                data = response.read()
            verify(asset, data)
            path = staging / asset.path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            print(f"downloaded  {asset.path}")

        for asset in ASSETS:
            source = staging / asset.path
            destination = VENDOR_DIR / asset.path
            destination.parent.mkdir(parents=True, exist_ok=True)
            source.replace(destination)
    print("Vendor assets updated and verified.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed files without downloading anything",
    )
    args = parser.parse_args()
    if args.check:
        check_committed_assets()
    else:
        download_assets()


if __name__ == "__main__":
    main()
