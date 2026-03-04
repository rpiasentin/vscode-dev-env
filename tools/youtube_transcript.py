#!/usr/bin/env python3
"""Create a transcript from a YouTube URL with strict preflight and clear fallbacks."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from typing import Dict, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

from youtube_transcript_api import YouTubeTranscriptApi
from yt_dlp import YoutubeDL

DEFAULT_MODEL = "gpt-4o-mini-transcribe"
DEFAULT_RESPONSE_FORMAT = "text"
OPENAI_ALLOWED_EXTS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"}


def _die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def _warn(message: str) -> None:
    print(f"Warning: {message}", file=sys.stderr)


def _parse_video_id(url_or_id: str) -> str:
    raw = url_or_id.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw

    parsed = urlparse(raw)
    host = parsed.netloc.lower()

    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/")[0]
        if candidate:
            return candidate

    if "youtube.com" in host:
        if parsed.path == "/watch":
            candidate = parse_qs(parsed.query).get("v", [None])[0]
            if candidate:
                return candidate
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] in {"shorts", "embed"}:
            return parts[1]

    _die("Could not parse a YouTube video id from the provided URL.")
    return ""


def _normalize_entries(items: Sequence[Dict]) -> List[Dict]:
    rows: List[Dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "text": str(item.get("text", "")).strip(),
                "start": float(item.get("start", 0.0)),
                "duration": float(item.get("duration", 0.0)),
            }
        )
    return rows


def _fetch_captions(video_id: str, language_hint: Optional[str]) -> List[Dict]:
    languages = [language_hint] if language_hint else ["en", "en-US", "en-GB"]
    last_exc: Optional[Exception] = None
    for langs in (languages, []):
        try:
            api = YouTubeTranscriptApi()
            if hasattr(api, "fetch"):
                transcript = api.fetch(video_id, languages=langs or ("en",))
                rows = transcript.to_raw_data() if hasattr(transcript, "to_raw_data") else list(transcript)
            else:
                if langs:
                    rows = YouTubeTranscriptApi.get_transcript(video_id, languages=langs)
                else:
                    rows = YouTubeTranscriptApi.get_transcript(video_id)
            return _normalize_entries(rows)
        except Exception as exc:  # noqa: BLE001 - clear user-facing error below
            last_exc = exc
    detail = str(last_exc) if last_exc else "unknown transcript API error"
    raise RuntimeError(detail)


def _write_caption_outputs(
    out_dir: Path,
    video_id: str,
    entries: List[Dict],
) -> Dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    txt_path = out_dir / f"{video_id}.captions.txt"
    json_path = out_dir / f"{video_id}.captions.json"

    text_lines = [entry["text"] for entry in entries if entry.get("text")]
    txt_path.write_text("\n".join(text_lines) + "\n", encoding="utf-8")
    json_path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    return {
        "mode": "captions",
        "txt": str(txt_path),
        "json": str(json_path),
        "segments": str(len(entries)),
    }


def _download_audio(url: str, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    options = {
        "format": "bestaudio/best",
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
    }
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            output = Path(ydl.prepare_filename(info))
    except Exception as exc:  # noqa: BLE001 - provide actionable error text
        _die(f"yt-dlp failed to download audio: {exc}")

    if not output.exists():
        _die(f"Audio download failed; expected file missing: {output}")
    if output.suffix.lower() not in OPENAI_ALLOWED_EXTS:
        _warn(
            "Downloaded audio extension may not be accepted by OpenAI transcription: "
            f"{output.suffix}. If fallback fails, retry with --strategy captions."
        )
    return output


def _run_openai_transcribe(
    audio_path: Path,
    out_dir: Path,
    video_id: str,
    transcribe_cli: Path,
    model: str,
    response_format: str,
    language_hint: Optional[str],
) -> Dict[str, str]:
    if not os.getenv("OPENAI_API_KEY"):
        _die("OPENAI_API_KEY is not set; cannot run OpenAI fallback.")
    if not transcribe_cli.exists():
        _die(f"Transcribe CLI not found: {transcribe_cli}")

    out_dir.mkdir(parents=True, exist_ok=True)
    extension = "txt" if response_format == "text" else "json"
    out_path = out_dir / f"{video_id}.openai.transcript.{extension}"

    cmd = [
        sys.executable,
        str(transcribe_cli),
        str(audio_path),
        "--model",
        model,
        "--response-format",
        response_format,
        "--chunking-strategy",
        "auto",
        "--out",
        str(out_path),
    ]
    if language_hint:
        cmd.extend(["--language", language_hint])

    subprocess.run(cmd, check=True)
    return {
        "mode": "openai",
        "output": str(out_path),
        "audio": str(audio_path),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create transcript artifacts from a YouTube URL."
    )
    parser.add_argument("url", help="YouTube URL (or 11-char video id)")
    parser.add_argument(
        "--out-dir",
        default="output/transcribe/youtube",
        help="Directory for transcript outputs",
    )
    parser.add_argument(
        "--strategy",
        choices=["auto", "captions", "openai"],
        default="auto",
        help="auto: captions first, then OpenAI fallback",
    )
    parser.add_argument(
        "--language",
        default="en",
        help="Language hint (default: en)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model for fallback transcription (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--response-format",
        choices=["text", "json", "diarized_json"],
        default=DEFAULT_RESPONSE_FORMAT,
        help="OpenAI fallback response format",
    )
    parser.add_argument(
        "--transcribe-cli",
        default=os.getenv(
            "TRANSCRIBE_CLI",
            str(Path.home() / ".codex/skills/transcribe/scripts/transcribe_diarize.py"),
        ),
        help="Path to transcribe_diarize.py",
    )
    parser.add_argument(
        "--keep-audio",
        action="store_true",
        help="Retain downloaded audio for inspection",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    out_dir = Path(args.out_dir).resolve()
    video_id = _parse_video_id(args.url)

    metadata_path = out_dir / f"{video_id}.metadata.json"
    metadata: Dict[str, str] = {
        "video_id": video_id,
        "source_url": args.url,
        "strategy": args.strategy,
    }

    if args.strategy in {"auto", "captions"}:
        try:
            entries = _fetch_captions(video_id, args.language)
            result = _write_caption_outputs(out_dir, video_id, entries)
            metadata.update(result)
            metadata_path.parent.mkdir(parents=True, exist_ok=True)
            metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
            print(json.dumps(metadata, indent=2))
            return
        except Exception as exc:  # noqa: BLE001 - explicit strategy handling below
            if args.strategy == "captions":
                _die(f"Could not fetch YouTube captions: {exc}")
            metadata["captions_error"] = str(exc)

    with tempfile.TemporaryDirectory(prefix="youtube_transcribe_") as temp_dir:
        temp_path = Path(temp_dir)
        audio_path = _download_audio(args.url, temp_path)
        openai_result = _run_openai_transcribe(
            audio_path=audio_path,
            out_dir=out_dir,
            video_id=video_id,
            transcribe_cli=Path(args.transcribe_cli),
            model=args.model,
            response_format=args.response_format,
            language_hint=args.language,
        )
        metadata.update(openai_result)

        if args.keep_audio:
            kept_path = out_dir / audio_path.name
            kept_path.write_bytes(audio_path.read_bytes())
            metadata["kept_audio"] = str(kept_path)

    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    main()
