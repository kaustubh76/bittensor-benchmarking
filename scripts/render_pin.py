"""Pin a Render image-backed service to the image a deploy just pushed, and
witness that it runs it.

    render_pin.py show   <service-id>               print the image the service runs
    render_pin.py pin    <service-id> <image:tag>   point the service at that image
    render_pin.py assert <service-id> <image:tag>   exit 1 unless it runs that image

WHY THIS EXISTS. The testnet press `acr-api` was PINNED in Render's dashboard to
`acr-api:2026-10-09-1755` while render.yaml said it tracked `:latest`.
`deploy/redeploy-render.sh` pushed `:latest` and triggered deploys, Render
re-pulled the pin, and the script's closing check passed because the old image
serves the same 50 routes: on 2026-10-14 two "successful" deploys shipped
nothing, and the fix they carried was declared live on a lucky run of 200s.
What exposed it was a field the new code adds and the old one lacks. The
service's own `imagePath` is the only evidence of what it runs, so the deploy
now sets it and then reads it back.

Needs RENDER_API_KEY for the workspace that owns the service (see the key note
at the top of deploy/redeploy-render.sh).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request

API = "https://api.render.com/v1"


def _request(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {os.environ['RENDER_API_KEY']}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            # Some fronts here refuse urllib's default agent with an HTML 403.
            "User-Agent": "acr-redeploy/1",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def image_of(service: dict) -> tuple[str | None, str | None]:
    """(imagePath, ownerId) from a service document, wherever the API nests them."""
    found: dict[str, str] = {}

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("imagePath", "ownerId") and isinstance(value, str):
                    found.setdefault(key, value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(service)
    return found.get("imagePath"), found.get("ownerId")


def verdict(running: str | None, wanted: str) -> str | None:
    """None when the service runs `wanted`; otherwise the sentence saying why not."""
    if running == wanted:
        return None
    if running is None:
        return "the service reports no imagePath, so what it runs cannot be shown"
    return f"the service runs {running}, not {wanted}: a deploy would re-pull that and ship nothing new"


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("show", "pin", "assert") or (argv[1] != "show" and len(argv) < 4):
        print(__doc__, file=sys.stderr)
        return 2
    command, service = argv[1], argv[2]
    running, owner = image_of(_request("GET", f"/services/{service}"))
    if command == "show":
        print(running or "")
        return 0 if running else 1

    wanted = argv[3]
    if command == "assert":
        why = verdict(running, wanted)
        if why:
            print(f"  ✗ {why}", file=sys.stderr)
            return 1
        print(f"  ✓ the service runs {wanted}")
        return 0

    # pin
    if running == wanted:
        print(f"  · already pinned to {wanted}")
        return 0
    if owner is None:
        print("  ✗ the service document carries no ownerId to pin under", file=sys.stderr)
        return 1
    _request("PATCH", f"/services/{service}", {"image": {"ownerId": owner, "imagePath": wanted}})
    after, _ = image_of(_request("GET", f"/services/{service}"))
    why = verdict(after, wanted)
    if why:
        print(f"  ✗ the pin did not take: {why}", file=sys.stderr)
        return 1
    print(f"  ✓ pinned {service} to {wanted} (rollback: {running})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
