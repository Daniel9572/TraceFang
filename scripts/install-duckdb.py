#!/usr/bin/env python3
"""Install the pinned offline research worker from verified official assets."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

VERSION = "1.5.6"
ASSETS = {
    ("Darwin", "arm64"): (
        "osx-arm64",
        "8e0f6825653f8d057922e6147db920bebf072cb41f4b041fd35521c18d7d126e",
    ),
    ("Darwin", "x86_64"): (
        "osx-amd64",
        "ae74c8cd74304bde1d92d941aca37bc72084ee8daa5660913819d8a9e9d29331",
    ),
    ("Linux", "x86_64"): (
        "linux-amd64",
        "6e89deac1ebbc36eed0291caf8b567b030c7b86ac35998f71854e22b3c5d5e2f",
    ),
    ("Linux", "aarch64"): (
        "linux-arm64",
        "c544e92c9b7c31fc53c2139802cabd8e2d1b2b3e3f933117f31611239c1402db",
    ),
    ("Windows", "AMD64"): (
        "windows-amd64",
        "798eae475d07c645ff3b914f7b0e676d06502b5412dd7552638fafed81f9e916",
    ),
    ("Windows", "ARM64"): (
        "windows-arm64",
        "266052dbf513da86d0d90209db09ec56e79af75b7fbe2d952810526dfc5d2726",
    ),
}
EXECUTABLE_SHA256 = {
    "osx-arm64": "7d15b2aaf6be05212ada5f99fe79e0b83e63b7ed91e7e422c0b925278e9e0c39",
    "osx-amd64": "ad4eda7e81a9f3c2de218c00232dc08a78b819c1c98c0d6d847cf1b42e76c6e8",
    "linux-amd64": "61238cfbe9dfeaad4bcfcd8a48f6f7123dae2e9603aaadb1f77a4217aeb8980c",
    "linux-arm64": "c0ac0b79e243ee312b3af29c34ac0b94e518aa44c4345036d5d96c1c042c9d48",
    "windows-amd64": "2c6a856516a9efb863482a9146242eba5ad919029a082ff773f4770ae0a7816b",
    "windows-arm64": "11cc497c1175f858fbc2bead3ccd4f65ea142c6a369e8587d4858debe057620c",
}


def default_directory() -> Path:
    if platform.system() == "Darwin":
        base = Path.home() / "Library/Application Support/TraceFang"
    elif os.name == "nt":
        base = (
            Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "TraceFang"
        )
    else:
        base = (
            Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "tracefang"
        )
    return base / "runtime/duckdb" / VERSION


def install(destination: Path, archive: Path | None = None) -> Path:
    asset, expected = ASSETS[(platform.system(), platform.machine())]
    url = f"https://github.com/duckdb/duckdb/releases/download/v{VERSION}/duckdb_cli-{asset}.zip"
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".duckdb-install-", dir=destination.parent) as scratch:
        scratch = Path(scratch)
        if archive is None:
            archive = scratch / "download.zip"
            request = urllib.request.Request(
                url, headers={"User-Agent": "TraceFang-runtime-installer"}
            )
            with (
                urllib.request.urlopen(request, timeout=45) as incoming,
                archive.open("wb") as output,
            ):
                count = 0
                while chunk := incoming.read(1024 * 1024):
                    count += len(chunk)
                    if count > 90 * 1024 * 1024:
                        raise RuntimeError("DuckDB archive exceeds its bounded installer budget")
                    output.write(chunk)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Official DuckDB archive SHA256 differs; installation refused")
        filename = "duckdb.exe" if os.name == "nt" else "duckdb"
        with zipfile.ZipFile(archive) as package:
            member = package.getinfo(filename)
            if member.file_size > 180 * 1024 * 1024:
                raise RuntimeError("DuckDB executable exceeds installer budget")
            binary = package.read(member)
        if hashlib.sha256(binary).hexdigest() != EXECUTABLE_SHA256[asset]:
            raise RuntimeError(
                "DuckDB extracted executable SHA256 differs from the pinned official archive"
            )
        candidate = scratch / filename
        candidate.write_bytes(binary)
        candidate.chmod(0o755)
        version = subprocess.check_output(
            [str(candidate), "--version"], text=True, timeout=10
        ).strip()
        if not version.startswith(f"v{VERSION} ") or "069cc9f9b5" not in version:
            raise RuntimeError("DuckDB version/build differs from the pinned release")
        manifest = {
            "version": VERSION,
            "platform": asset,
            "archive_sha256": expected,
            "executable_sha256": hashlib.sha256(binary).hexdigest(),
            "version_output": version,
            "source_url": url,
            "license": "MIT",
            "license_url": "https://github.com/duckdb/duckdb/blob/v1.5.6/LICENSE",
        }
        with candidate.open("rb") as handle:
            os.fsync(handle.fileno())
        receipt = scratch / "manifest.json"
        with receipt.open("w") as handle:
            json.dump(manifest, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(candidate, destination / filename)
        os.replace(receipt, destination / "manifest.json")
        if os.name != "nt":
            descriptor = os.open(destination, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    return destination / filename


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--destination", type=Path, default=default_directory())
    parser.add_argument(
        "--archive",
        type=Path,
        help="Use an already downloaded official zip; SHA is still mandatory",
    )
    arguments = parser.parse_args()
    print(install(arguments.destination, arguments.archive))
