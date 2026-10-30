#!/usr/bin/env python3
"""Has the version in `mcp/package.json` already been published?

WHY THIS EXISTS. On 2026-10-12 `mcp/package.json` read `0.3.1` and so did npm's
`latest`. The published tarball and `mcp/src` agreed on every marker, so one
version named one artifact and all was well — and the very next edit to
`mcp/src` would have made one version name TWO. The failure is quiet in both
directions: `npm publish` refuses the duplicate, and anyone running
`npx -y acr-mcp` keeps getting the old build while the repo looks shipped.

`prepublishOnly` runs `npm run build && npm test`, and neither knows what the
registry holds, so nothing in the package could see this. Hence a check that
asks npm.

NOT IN CI, deliberately, for the same reason `anchors --check-live` is not: it
needs the network, and a registry hiccup is not a failing build. Run it before
`npm run release`.

STRICT ON AN UNREADABLE REGISTRY. "I could not ask" fails here rather than
passing — a gate needs the opposite default to a report, and the whole point is
to refuse to guess about what is already public.

    make mcp-version-check
"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "mcp" / "package.json"
REGISTRY = "https://registry.npmjs.org/acr-mcp"


def published_versions() -> list[str]:
    """Every version the registry holds, newest-last.

    Read straight off the registry rather than through `npm view`, so this does
    not depend on a logged-in npm or a configured registry — and a 404 means
    "never published", which is a legitimate answer rather than an error.
    """
    req = urllib.request.Request(REGISTRY, headers={"User-Agent": "acr-version-check/1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 — a fixed https URL
            doc = json.load(r)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return []
        raise
    return list((doc.get("versions") or {}).keys())


def main() -> int:
    version = json.loads(PKG.read_text(encoding="utf-8"))["version"]

    try:
        have = published_versions()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ could not read the npm registry ({exc}), so it cannot be shown")
        print("    that acr-mcp "
              f"{version} is unpublished. Re-run before releasing.")
        return 1

    print(f"  mcp/package.json  {version}")
    print(f"  npm has           {', '.join(have) if have else '(nothing published)'}")

    if version in have:
        print()
        print(f"  ✗ acr-mcp {version} IS ALREADY PUBLISHED.")
        print("    `npm publish` will refuse it, and `npx -y acr-mcp` keeps serving the")
        print("    old build while this repo looks shipped. Bump mcp/package.json (and")
        print("    the server.ts handshake literal that tools.test.ts pins to it).")
        return 1

    # Does the working tree differ from what was last published? If `mcp/src` is
    # dirty or ahead, the bump is required; if it is identical, the bump is
    # merely harmless. Reported rather than enforced, because deciding that a
    # change is publish-worthy is a human call.
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "mcp/"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    ).stdout.strip()
    if dirty:
        print("  note: mcp/ has uncommitted changes, so this version is not yet a commit:")
        for line in dirty.splitlines():
            print(f"    {line}")

    print()
    print(f"  ✓ acr-mcp {version} is not on npm, so a release can take it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
