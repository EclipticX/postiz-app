import json
import math
import os
import re
import subprocess
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory


WIDTH = 720
HEIGHT = 1280
FPS = 30
DURATION_TOLERANCE = 0.12
SUBTITLE_FONTS = ("DejaVu Sans", "DejaVu Serif", "DejaVu Sans Mono")
# libass force_style uses SSA alignment flags for position overrides.
SUBTITLE_ALIGNMENT = {"top": 6, "middle": 10, "bottom": 2}


class RenderError(RuntimeError):
    pass


def render_video(
    scenes, clip_paths, voice_paths, output_path, *, music_path=None, transcribe=None,
    subtitle_style=None, sfx_path=None,
) -> Path:
    """Render an approved plan and save its MP4 and, when spoken, an SRT sidecar.

    An optional transcriber returns (word, start_seconds, end_seconds) entries
    relative to the supplied narration file. Structured dialogue uses one
    voice path per line in scene order. Background music loops, and the sound
    effect plays once at the beginning.
    """
    total_duration = _total_duration(scenes)
    output_path = Path(output_path).resolve()
    if output_path.suffix.lower() != ".mp4":
        raise RenderError("The render output path must end in .mp4")
    subtitle_style = _subtitle_style(subtitle_style)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    transcribe = transcribe or _transcribe_local

    inputs = []
    filters = []
    concat_inputs = []
    cues = []
    offset = 0
    input_index = 0
    for index, scene in enumerate(scenes):
        scene_id = scene["scene_id"]
        duration = scene["duration"]
        clip = _input_file(clip_paths.get(scene_id), f"Video clip for {scene_id}")
        inputs.extend(["-i", str(clip)])
        filters.append(
            f"[{input_index}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,fps={FPS},format=yuv420p,"
            f"tpad=stop_mode=clone:stop_duration={duration},trim=duration={duration},"
            f"setpts=PTS-STARTPTS[v{index}]"
        )
        input_index += 1

        paths, narration_filters, scene_cues = _narration(
            scene, voice_paths.get(scene_id), index, input_index, offset, transcribe,
        )
        for path in paths:
            inputs.extend(["-i", str(path)])
        input_index += len(paths)
        filters.extend(narration_filters)
        cues.extend(scene_cues)
        concat_inputs.append(f"[v{index}][a{index}]")
        offset += duration

    filters.append(f"{''.join(concat_inputs)}concat=n={len(scenes)}:v=1:a=1[video][narration]")
    tracks = ["[narration]"]
    for path, label, volume, loop in ((music_path, "music", 0.12, True), (sfx_path, "sfx", 0.28, False)):
        if path is None:
            continue
        track = _input_file(path, "Background music" if loop else "Opening sound effect")
        if loop:
            inputs.extend(["-stream_loop", "-1"])
        inputs.extend(["-i", str(track)])
        filters.append(
            f"[{input_index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"volume={volume},atrim=duration={total_duration},asetpts=PTS-STARTPTS[{label}]"
        )
        input_index += 1
        tracks.append(f"[{label}]")
    audio_output = "[narration]"
    if len(tracks) > 1:
        filters.append(
            f"{''.join(tracks)}amix=inputs={len(tracks)}:duration=first:normalize=0,"
            "alimiter=limit=0.95:latency=1[audio]"
        )
        audio_output = "[audio]"

    with TemporaryDirectory(prefix=".render-", dir=output_path.parent) as directory:
        work_dir = Path(directory)
        subtitle_file = work_dir / "approved.srt"
        video_output = "[video]"
        if cues:
            subtitle_file.write_text(_srt(cues), encoding="utf-8")
            filters.append(f"[video]subtitles=approved.srt:force_style='{subtitle_style}'[captioned]")
            video_output = "[captioned]"

        temporary_video = work_dir / "render.mp4"
        _ffmpeg(
            [
                *inputs, "-filter_complex_threads", "2", "-filter_complex", ";".join(filters),
                "-map", video_output, "-map", audio_output, "-t", str(total_duration),
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "19", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary_video),
            ],
            cwd=work_dir,
        )
        _verify_render(temporary_video, total_duration)
        temporary_video.replace(output_path)
        sidecar = output_path.with_suffix(".srt")
        if cues:
            subtitle_file.replace(sidecar)
        else:
            sidecar.unlink(missing_ok=True)

    return output_path


def sample_frames(path, output_dir, scenes) -> list[Path]:
    """Save one midpoint JPEG for each scene, in approved scene order."""
    _total_duration(scenes)
    video = _input_file(path, "Video to sample")
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    offset = 0
    with TemporaryDirectory(prefix=".sample-", dir=output_dir) as directory:
        for index, scene in enumerate(scenes, start=1):
            duration = scene["duration"]
            frame = Path(directory) / f"frame-{index:03}.jpg"
            _ffmpeg(["-ss", str(offset + duration / 2), "-i", str(video), "-frames:v", "1", "-q:v", "3", str(frame)])
            if not frame.is_file() or frame.stat().st_size == 0:
                raise RenderError(f"Could not sample a frame for scene {index}")
            frames.append(frame)
            offset += duration
        return [frame.replace(output_dir / frame.name) for frame in frames]


def _total_duration(scenes) -> int:
    if not scenes:
        raise RenderError("At least one scene is required")
    durations = [scene["duration"] for scene in scenes]
    if any(type(duration) is not int or duration not in (4, 5) for duration in durations):
        raise RenderError("Each scene must be 4 or 5 seconds")
    return sum(durations)


def _input_file(path, label) -> Path:
    if path is None or not Path(path).is_file():
        raise RenderError(f"{label} is missing")
    return Path(path).resolve()


def _narration(scene, voice_paths, scene_index, first_input, offset, transcribe):
    scene_id = scene["scene_id"]
    duration = scene["duration"]
    lines = scene.get("dialogue_lines")
    output = f"[a{scene_index}]"
    if lines:
        if not isinstance(voice_paths, (list, tuple)) or len(voice_paths) != len(lines):
            raise RenderError(f"Narration for {scene_id} needs one audio file per dialogue line")
        texts = [line["text"].strip() for line in lines]
    else:
        dialogue = scene.get("dialogue", "").strip()
        if not dialogue:
            return [], [f"anullsrc=r=48000:cl=stereo,atrim=duration={duration},asetpts=PTS-STARTPTS{output}"], []
        texts = [dialogue]
        voice_paths = [voice_paths]

    paths, filters, cues, tracks = [], [], [], []
    elapsed = 0.0
    for line_index, (text, path) in enumerate(zip(texts, voice_paths)):
        label = f"{scene_id} dialogue line {line_index + 1}" if lines else scene_id
        path = _input_file(path, f"Narration for {label}")
        length = duration
        if lines:
            length = _audio_duration(path)
            remaining = duration - elapsed
            if remaining <= 0 or length > remaining + DURATION_TOLERANCE:
                raise RenderError(f"Narration for {scene_id} exceeds its {duration}-second scene")
            length = min(length, remaining)
        cues.extend(_subtitle_cues(text, transcribe(path), length, offset + elapsed, label))
        paths.append(path)
        track = f"[n{scene_index}_{line_index}]" if lines else output
        tracks.append(track)
        filters.append(
            f"[{first_input + line_index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"apad,atrim=duration={length:.6f},asetpts=PTS-STARTPTS{track}"
        )
        elapsed += length

    if lines:
        narration = tracks[0]
        if len(tracks) > 1:
            narration = f"[narration{scene_index}]"
            filters.append(f"{''.join(tracks)}concat=n={len(tracks)}:v=0:a=1{narration}")
        filters.append(f"{narration}apad,atrim=duration={duration},asetpts=PTS-STARTPTS{output}")
    return paths, filters, cues


def _audio_duration(path):
    raw = _run([
        "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=duration:format=duration",
        "-of", "json", str(path),
    ])
    probe = json.loads(raw)
    tracks = probe.get("streams", [])
    if not tracks:
        raise RenderError("A narration file has no audio track")
    for value in (tracks[0].get("duration"), probe.get("format", {}).get("duration")):
        try:
            duration = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(duration) and duration > 0:
            return duration
    raise RenderError("Could not determine narration audio duration")


def _subtitle_style(style):
    if style is None:
        return "FontSize=12,Outline=1,Shadow=0,Alignment=2,MarginV=25"
    if not isinstance(style, dict):
        raise RenderError("Subtitle style must be an object")
    font = style.get("font_name", "DejaVu Sans")
    size = style.get("font_size", 12)
    position = style.get("position", "bottom")
    if font not in SUBTITLE_FONTS:
        raise RenderError("Choose a supported subtitle font")
    if type(size) is not int or not 8 <= size <= 30:
        raise RenderError("Subtitle font size must be between 8 and 30")
    if not isinstance(position, str) or position not in SUBTITLE_ALIGNMENT:
        raise RenderError("Choose top, middle, or bottom subtitle position")
    text = _subtitle_color(style.get("text_color", "#FFFFFF"))
    outline = _subtitle_color(style.get("outline_color", "#000000"))
    return (
        f"FontName={font},FontSize={size},PrimaryColour={text},OutlineColour={outline},"
        f"Outline=1,Shadow=0,Alignment={SUBTITLE_ALIGNMENT[position]},MarginV=25"
    )


def _subtitle_color(color):
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise RenderError("Subtitle colors must use #RRGGBB format")
    red, green, blue = color[1:3], color[3:5], color[5:7]
    return f"&H00{blue}{green}{red}"


def _subtitle_cues(dialogue, word_timestamps, duration, offset, scene_id):
    timings = sorted(
        (max(0.0, start), end)
        for _, start, end in word_timestamps
        if math.isfinite(start) and math.isfinite(end) and max(0.0, start) < end
    )
    if not timings:
        raise RenderError(f"Whisper did not return speech timings for {scene_id}")
    speech_start = timings[0][0]
    speech_end = max(end for _, end in timings)
    if speech_start >= duration or speech_end > duration + DURATION_TOLERANCE:
        raise RenderError(f"Narration for {scene_id} exceeds its {duration}-second scene")

    words = dialogue.split()
    cues = []
    for start in range(0, len(words), 6):
        end = min(start + 6, len(words))
        if len(words) == len(timings):
            start_at = timings[start][0]
            end_at = timings[end - 1][1]
            if end < len(timings):
                end_at = min(end_at, timings[end][0])
        else:
            # Whisper and the approved script can tokenize the same speech differently.
            # Spread the approved words over the detected speech interval in that case.
            start_at = speech_start + (speech_end - speech_start) * start / len(words)
            end_at = speech_start + (speech_end - speech_start) * end / len(words)
        end_at = min(end_at, duration)
        if end_at <= start_at:
            raise RenderError(f"Whisper returned overlapping subtitle timings for {scene_id}")
        cues.append((offset + start_at, offset + end_at, " ".join(words[start:end])))
    return cues


def _srt(cues) -> str:
    return "\n\n".join(
        f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}"
        for index, (start, end, text) in enumerate(cues, start=1)
    ) + "\n"


def _srt_time(seconds) -> str:
    seconds, milliseconds = divmod(round(seconds * 1000), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"


@lru_cache(maxsize=1)
def _whisper_model(model_path):
    try:
        from faster_whisper import WhisperModel
    except ImportError as error:
        raise RenderError("Install faster-whisper to generate local narration subtitles") from error
    return WhisperModel(str(model_path), device="cpu", compute_type="int8", local_files_only=True)


def _transcribe_local(path):
    model_path = os.environ.get("STUDIO_WHISPER_MODEL_PATH")
    if not model_path or not Path(model_path).is_dir():
        raise RenderError("Set STUDIO_WHISPER_MODEL_PATH to an existing local faster-whisper model directory")
    try:
        segments, _ = _whisper_model(Path(model_path).resolve()).transcribe(str(path), word_timestamps=True)
        return [(word.word, word.start, word.end) for segment in segments for word in segment.words or ()]
    except RenderError:
        raise
    except Exception as error:
        raise RenderError("Local Whisper could not align the narration. Check the model and audio file") from error


def _verify_render(path, total_duration):
    raw = _run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)])
    probe = json.loads(raw)
    video = next((stream for stream in probe["streams"] if stream["codec_type"] == "video"), {})
    audio = next((stream for stream in probe["streams"] if stream["codec_type"] == "audio"), {})
    if (video.get("width"), video.get("height"), video.get("codec_name")) != (WIDTH, HEIGHT, "h264"):
        raise RenderError(f"The render must contain {WIDTH} × {HEIGHT} H.264 video")
    if audio.get("codec_name") != "aac":
        raise RenderError("The render must contain AAC audio")
    for label, track in (("MP4", probe["format"]), ("video", video), ("audio", audio)):
        duration = float(track.get("duration", "nan"))
        if not math.isfinite(duration) or abs(duration - total_duration) > DURATION_TOLERANCE:
            raise RenderError(f"The render's {label} duration is {duration:.2f} seconds. Expected {total_duration} seconds")


def _ffmpeg(args, *, cwd=None):
    return _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args], cwd=cwd)


def _run(command, *, cwd=None):
    try:
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError as error:
        raise RenderError(f"Install {command[0]} and make it available on PATH") from error
    except subprocess.TimeoutExpired as error:
        raise RenderError(f"{command[0]} timed out processing the video") from error
    if result.returncode:
        detail = "\n".join(result.stderr.strip().splitlines()[-4:])
        raise RenderError(f"{command[0]} failed. {detail}")
    return result.stdout
