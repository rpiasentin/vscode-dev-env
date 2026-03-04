#!/usr/bin/env python3
"""Generate per-slide narration audio from text files using ElevenLabs TTS."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import sys

import requests


DEFAULT_MODEL_ID = "eleven_multilingual_v2"
DEFAULT_FORMAT = "mp3_44100_128"


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def info(message: str) -> None:
    print(f"[elevenlabs] {message}")


def natural_sort_key(path: Path):
    parts = re.split(r"(\d+)", path.name)
    key = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part.lower())
    return key


def list_script_files(script_dir: Path) -> list[Path]:
    if not script_dir.exists():
        die(f"Script directory not found: {script_dir}")
    if not script_dir.is_dir():
        die(f"Script path is not a directory: {script_dir}")

    files = [p for p in script_dir.iterdir() if p.is_file() and p.suffix.lower() == ".txt"]
    files = [p for p in files if re.match(r"slide\d+\.txt$", p.name, flags=re.IGNORECASE)]
    files.sort(key=natural_sort_key)
    if not files:
        die(
            f"No slide script files found in {script_dir}. "
            "Expected names like slide01.txt, slide02.txt, ..."
        )
    return files


def _trim_and_validate_text(raw: str, file_path: Path) -> str:
    text = raw.strip()
    if not text:
        die(f"Script file is empty: {file_path}")
    # Keep generous limit; ElevenLabs usually allows significantly more than this.
    if len(text) > 15000:
        die(
            f"Script file is too long for a single request ({len(text)} chars): {file_path}. "
            "Split the text into shorter chunks."
        )
    return text


def generate_audio(
    *,
    api_key: str,
    voice_id: str,
    model_id: str,
    output_format: str,
    text: str,
    stability: float,
    similarity_boost: float,
    style: float,
    use_speaker_boost: bool,
    timeout_seconds: int,
) -> bytes:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": api_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {
        "text": text,
        "model_id": model_id,
        "voice_settings": {
            "stability": stability,
            "similarity_boost": similarity_boost,
            "style": style,
            "use_speaker_boost": use_speaker_boost,
        },
    }
    params = {"output_format": output_format}
    response = requests.post(
        url,
        headers=headers,
        params=params,
        json=payload,
        timeout=timeout_seconds,
    )
    if response.status_code >= 400:
        detail = response.text.strip()
        die(
            "ElevenLabs request failed "
            f"(status={response.status_code}): {detail[:1200]}"
        )
    if not response.content:
        die("ElevenLabs returned an empty audio response")
    return response.content


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate per-slide MP3 narration from slideXX.txt scripts."
    )
    parser.add_argument("--script-dir", required=True, help="Directory containing slideXX.txt files")
    parser.add_argument("--out-dir", required=True, help="Directory for output audio files")
    parser.add_argument(
        "--api-key",
        default="",
        help="ElevenLabs API key (or set ELEVENLABS_API_KEY)",
    )
    parser.add_argument(
        "--voice-id",
        default="",
        help="ElevenLabs voice id (or set ELEVENLABS_VOICE_ID)",
    )
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="ElevenLabs model id")
    parser.add_argument("--output-format", default=DEFAULT_FORMAT, help="ElevenLabs output format")
    parser.add_argument("--stability", type=float, default=0.45, help="Voice stability (0-1)")
    parser.add_argument("--similarity-boost", type=float, default=0.80, help="Similarity boost (0-1)")
    parser.add_argument("--style", type=float, default=0.10, help="Style exaggeration (0-1)")
    parser.add_argument(
        "--use-speaker-boost",
        action="store_true",
        help="Enable speaker boost",
    )
    parser.add_argument("--timeout-seconds", type=int, default=180, help="Request timeout in seconds")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip files that already exist in the output directory",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate inputs and print planned output files only",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    script_dir = Path(args.script_dir).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()

    api_key = (args.api_key or "").strip() or (os.getenv("ELEVENLABS_API_KEY") or "").strip()

    voice_id = (args.voice_id or "").strip()
    if not voice_id:
        voice_id = (os.getenv("ELEVENLABS_VOICE_ID") or "").strip()

    if not api_key:
        die("ELEVENLABS_API_KEY is not set. Pass --api-key or export ELEVENLABS_API_KEY.")
    if not voice_id:
        die("ELEVENLABS_VOICE_ID is not set. Pass --voice-id or export ELEVENLABS_VOICE_ID.")

    if not (0 <= args.stability <= 1):
        die("--stability must be between 0 and 1")
    if not (0 <= args.similarity_boost <= 1):
        die("--similarity-boost must be between 0 and 1")
    if not (0 <= args.style <= 1):
        die("--style must be between 0 and 1")

    script_files = list_script_files(script_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    info(f"Discovered {len(script_files)} script files")

    for i, script_path in enumerate(script_files, start=1):
        text = _trim_and_validate_text(script_path.read_text(encoding="utf-8"), script_path)
        out_name = script_path.stem + ".mp3"
        out_path = out_dir / out_name

        if args.skip_existing and out_path.exists() and out_path.stat().st_size > 0:
            info(f"[{i}/{len(script_files)}] Skipping existing {out_path.name}")
            continue

        info(f"[{i}/{len(script_files)}] Generating {out_path.name} from {script_path.name}")
        if args.dry_run:
            continue

        audio_bytes = generate_audio(
            api_key=api_key,
            voice_id=voice_id,
            model_id=args.model_id,
            output_format=args.output_format,
            text=text,
            stability=args.stability,
            similarity_boost=args.similarity_boost,
            style=args.style,
            use_speaker_boost=args.use_speaker_boost,
            timeout_seconds=args.timeout_seconds,
        )
        out_path.write_bytes(audio_bytes)
        info(f"Saved {out_path}")

    info("Done.")


if __name__ == "__main__":
    main()
