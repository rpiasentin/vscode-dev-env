#!/usr/bin/env python3
"""List voices in an ElevenLabs account."""

from __future__ import annotations

import argparse
import os
import sys

import requests


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List ElevenLabs voices for the configured account.")
    parser.add_argument("--api-key", default="", help="ElevenLabs API key (or set ELEVENLABS_API_KEY)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = (args.api_key or "").strip() or (os.getenv("ELEVENLABS_API_KEY") or "").strip()
    if not api_key:
        die("ELEVENLABS_API_KEY is not set. Pass --api-key or export ELEVENLABS_API_KEY.")

    resp = requests.get(
        "https://api.elevenlabs.io/v1/voices",
        headers={"xi-api-key": api_key},
        timeout=60,
    )
    if resp.status_code >= 400:
        die(f"ElevenLabs request failed (status={resp.status_code}): {resp.text[:1000]}")

    payload = resp.json()
    voices = payload.get("voices") or []
    if not voices:
        print("No voices found.")
        return

    print("Voices:")
    for v in voices:
        name = v.get("name", "<unnamed>")
        voice_id = v.get("voice_id", "<no-id>")
        category = v.get("category", "unknown")
        print(f"- {name}\tvoice_id={voice_id}\tcategory={category}")


if __name__ == "__main__":
    main()
