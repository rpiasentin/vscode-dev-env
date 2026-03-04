#!/usr/bin/env python3
"""Render a narrated presentation video from PPTX slides + per-slide audio files.

This pipeline uses:
1) Keynote automation to export slide images from a PPTX.
2) ffmpeg to create one video segment per slide/audio pair.
3) ffmpeg concat to stitch segments into a single MP4.

Designed to avoid silent failures by enforcing strict preflight checks.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile

SUPPORTED_AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aiff", ".aac", ".m4b"}


def die(message: str, code: int = 1) -> None:
    print(f"Error: {message}", file=sys.stderr)
    raise SystemExit(code)


def info(message: str) -> None:
    print(f"[video] {message}")


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def natural_sort_key(path: Path):
    parts = re.split(r"(\d+)", path.name)
    key = []
    for part in parts:
        if part.isdigit():
            key.append(int(part))
        else:
            key.append(part.lower())
    return key


def run(cmd: list[str], check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    kwargs = {
        "text": True,
    }
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    result = subprocess.run(cmd, **kwargs)
    if check and result.returncode != 0:
        stderr = (result.stderr or "").strip() if capture else ""
        die(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{stderr}")
    return result


def count_slides_in_pptx(pptx_path: Path) -> int:
    if not pptx_path.exists():
        die(f"PPTX not found: {pptx_path}")
    if pptx_path.suffix.lower() != ".pptx":
        die(f"Expected a .pptx file, got: {pptx_path.name}")

    pattern = re.compile(r"ppt/slides/slide\d+\.xml$")
    count = 0
    with zipfile.ZipFile(pptx_path, "r") as zf:
        for name in zf.namelist():
            if pattern.match(name):
                count += 1
    return count


def list_audio_files(audio_dir: Path) -> list[Path]:
    if not audio_dir.exists():
        die(f"Audio directory not found: {audio_dir}")
    if not audio_dir.is_dir():
        die(f"Audio path is not a directory: {audio_dir}")

    files = []
    for p in audio_dir.iterdir():
        if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTS:
            files.append(p)
    files.sort(key=natural_sort_key)
    return files


def ffprobe_duration(audio_path: Path) -> float:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    result = run(cmd, check=True, capture=True)
    raw = (result.stdout or "").strip()
    try:
        duration = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"Could not parse duration for {audio_path}: {raw}") from exc
    if duration <= 0:
        raise RuntimeError(f"Non-positive duration for {audio_path}: {duration}")
    return duration


def export_slides_with_keynote(pptx_path: Path, export_root: Path) -> list[Path]:
    export_root.mkdir(parents=True, exist_ok=True)
    export_target = export_root / "slides"
    if export_target.exists():
        if export_target.is_dir():
            shutil.rmtree(export_target)
        else:
            export_target.unlink()

    script = f'''
tell application "Keynote"
  activate
  set inFile to POSIX file "{pptx_path}"
  set outFile to POSIX file "{export_target}"
  set theDoc to open inFile
  export theDoc to outFile as slide images
  close theDoc saving no
end tell
'''
    result = subprocess.run(
        ["osascript", "-e", script],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode != 0:
        die(
            "Keynote export failed. Make sure Keynote automation permissions are enabled.\n"
            + (result.stderr or "").strip()
        )

    slides = sorted(export_target.glob("*.png"), key=natural_sort_key)
    if not slides:
        die(f"No slide images were exported by Keynote in {export_target}")
    return slides


def _build_atempo_filter(speed: float) -> str:
    # ffmpeg atempo supports 0.5-2.0 per stage. Chain filters when needed.
    if speed <= 0:
        raise ValueError("audio speed must be > 0")
    if abs(speed - 1.0) < 1e-6:
        return "atempo=1.0"

    factors = []
    value = speed
    while value > 2.0:
        factors.append(2.0)
        value /= 2.0
    while value < 0.5:
        factors.append(0.5)
        value /= 0.5
    factors.append(value)
    return ",".join(f"atempo={f:.6f}" for f in factors)


def build_segment(
    slide_path: Path,
    audio_path: Path,
    segment_path: Path,
    fps: int,
    audio_speed: float,
) -> None:
    atempo_filter = _build_atempo_filter(audio_speed)
    cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-loop",
        "1",
        "-framerate",
        str(fps),
        "-i",
        str(slide_path),
        "-i",
        str(audio_path),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-tune",
        "stillimage",
        "-filter:a",
        atempo_filter,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-shortest",
        str(segment_path),
    ]
    run(cmd, check=True, capture=True)


def concat_segments(
    segment_paths: list[Path],
    output_path: Path,
    work_dir: Path,
    concat_mode: str,
) -> None:
    concat_file = work_dir / "concat.txt"
    with concat_file.open("w", encoding="utf-8") as f:
        for seg in segment_paths:
            safe = str(seg).replace("'", "'\\''")
            f.write(f"file '{safe}'\n")

    if concat_mode in {"auto", "copy"}:
        copy_cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(output_path),
        ]
        result = run(copy_cmd, check=False, capture=True)
        if result.returncode == 0:
            return
        if concat_mode == "copy":
            die(f"Concat copy mode failed:\n{(result.stderr or '').strip()}")

    reencode_cmd = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_file),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]
    run(reencode_cmd, check=True, capture=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render narrated MP4 from PPTX + per-slide voice recordings."
    )
    parser.add_argument("--pptx", required=True, help="Path to presentation (.pptx)")
    parser.add_argument(
        "--audio-dir",
        required=True,
        help="Directory with per-slide audio files (sorted naturally)",
    )
    parser.add_argument("--out", required=True, help="Output MP4 path")
    parser.add_argument(
        "--work-dir",
        default="tmp/presentation_video_work",
        help="Temporary working directory",
    )
    parser.add_argument("--fps", type=int, default=30, help="Video frame rate")
    parser.add_argument(
        "--audio-speed",
        type=float,
        default=1.0,
        help="Narration speed multiplier (e.g., 1.1 = 10%% faster)",
    )
    parser.add_argument(
        "--concat-mode",
        choices=["auto", "copy", "reencode"],
        default="auto",
        help="How to concatenate segments into final MP4",
    )
    parser.add_argument(
        "--high-priority",
        action="store_true",
        help="Best-effort attempt to raise process priority",
    )
    parser.add_argument(
        "--keep-work",
        action="store_true",
        help="Keep temporary files for debugging",
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="Run checks only (do not render video)",
    )
    return parser.parse_args()


def preflight(pptx_path: Path, audio_dir: Path) -> tuple[int, list[Path], dict[str, float]]:
    missing = [cmd for cmd in ("ffmpeg", "ffprobe", "osascript") if not command_exists(cmd)]
    if missing:
        die(f"Missing required commands: {', '.join(missing)}")

    if not Path("/Applications/Keynote.app").exists():
        die("Keynote.app is required for slide image export and was not found in /Applications")

    slide_count = count_slides_in_pptx(pptx_path)
    if slide_count < 1:
        die("Presentation has zero slides")

    audio_files = list_audio_files(audio_dir)
    if not audio_files:
        die(
            f"No audio files found in {audio_dir}. "
            "Expected per-slide files like slide01.m4a, slide02.m4a, ..."
        )

    if len(audio_files) != slide_count:
        formatted = "\n".join(f"  - {p.name}" for p in audio_files)
        die(
            f"Slide/audio count mismatch: {slide_count} slides vs {len(audio_files)} audio files.\n"
            f"Audio files discovered:\n{formatted}"
        )

    durations: dict[str, float] = {}
    for audio in audio_files:
        try:
            durations[audio.name] = ffprobe_duration(audio)
        except Exception as exc:  # noqa: BLE001
            die(str(exc))

    return slide_count, audio_files, durations


def main() -> None:
    args = parse_args()
    if args.audio_speed <= 0:
        die("--audio-speed must be > 0")

    if args.high_priority:
        try:
            os.nice(-20)
            info("Process priority raised (best effort).")
        except Exception:
            info("Could not raise nice priority; continuing at default user priority.")

    pptx_path = Path(args.pptx).expanduser().resolve()
    audio_dir = Path(args.audio_dir).expanduser().resolve()
    output_path = Path(args.out).expanduser().resolve()
    work_dir = Path(args.work_dir).expanduser().resolve()
    slides_dir = work_dir / "slides_export"
    segments_dir = work_dir / "segments"
    metadata_path = output_path.with_suffix(".metadata.json")

    slide_count, audio_files, durations = preflight(pptx_path, audio_dir)
    total_audio = sum(durations.values())
    adjusted_total_audio = total_audio / args.audio_speed
    info(f"Preflight passed: {slide_count} slides, {len(audio_files)} audio files")
    info(f"Total narration duration (source): {total_audio:.2f}s")
    info(
        "Estimated narration duration after speed "
        f"{args.audio_speed:.3f}x: {adjusted_total_audio:.2f}s"
    )
    if args.preflight_only:
        return

    if work_dir.exists():
        shutil.rmtree(work_dir)
    slides_dir.mkdir(parents=True, exist_ok=True)
    segments_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    info("Exporting slides via Keynote...")
    slide_images = export_slides_with_keynote(pptx_path, slides_dir)
    if len(slide_images) != slide_count:
        die(
            f"Keynote export count mismatch: expected {slide_count} slides, got {len(slide_images)} images"
        )

    info("Building per-slide video segments...")
    segment_paths = []
    for idx, (slide_img, audio_file) in enumerate(zip(slide_images, audio_files), start=1):
        segment = segments_dir / f"segment_{idx:03d}.mp4"
        build_segment(slide_img, audio_file, segment, args.fps, args.audio_speed)
        segment_paths.append(segment)
        info(
            f"Segment {idx}/{slide_count}: {slide_img.name} + {audio_file.name} "
            f"({durations[audio_file.name]:.2f}s source)"
        )

    info("Concatenating segments into final MP4...")
    concat_segments(segment_paths, output_path, work_dir, args.concat_mode)
    if not output_path.exists() or output_path.stat().st_size == 0:
        die(f"Output video not created: {output_path}")

    payload = {
        "pptx": str(pptx_path),
        "audio_dir": str(audio_dir),
        "output_video": str(output_path),
        "slide_count": slide_count,
        "audio_files": [p.name for p in audio_files],
        "audio_speed_multiplier": args.audio_speed,
        "concat_mode": args.concat_mode,
        "audio_durations_seconds": durations,
        "total_audio_seconds": round(total_audio, 3),
        "estimated_output_audio_seconds": round(adjusted_total_audio, 3),
        "work_dir": str(work_dir),
    }
    metadata_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    info(f"Done: {output_path}")
    info(f"Metadata: {metadata_path}")

    if not args.keep_work:
        shutil.rmtree(work_dir, ignore_errors=True)
        info("Temporary work directory removed")
    else:
        info(f"Temporary work directory kept: {work_dir}")


if __name__ == "__main__":
    main()
