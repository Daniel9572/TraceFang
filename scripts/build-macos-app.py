"""Build a local native application; no Electron, bundled browser, or new Python dependency."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    app = ROOT / "dist" / "TraceFang.app"
    contents = app / "Contents"
    executable = contents / "MacOS" / "TraceFang"
    resources = contents / "Resources"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "xcrun",
            "swiftc",
            "-O",
            str(ROOT / "scripts/macos/TraceFangApp.swift"),
            "-o",
            str(executable),
        ],
        check=True,
        timeout=180,
    )
    (resources / "project-root.txt").write_text(str(ROOT), encoding="utf-8")
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": "com.tracefang.launcher",
                "CFBundleName": "TraceFang",
                "CFBundleDisplayName": "TraceFang",
                "CFBundleExecutable": "TraceFang",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1.0",
                "NSHighResolutionCapable": True,
                "NSPrincipalClass": "NSApplication",
                "NSDocumentsFolderUsageDescription": (
                    "读取您放在文稿文件夹中的 TraceFang 项目, 以启动和停止本地服务。"
                ),
            },
            handle,
        )
    print(app)


if __name__ == "__main__":
    main()
