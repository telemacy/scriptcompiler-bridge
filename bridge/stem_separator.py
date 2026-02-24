import asyncio
import logging
import os
import sys
import threading
import tempfile

import numpy as np

from .config import EXECUTOR

logger = logging.getLogger(__name__)


def _setup_bundled_ffmpeg():
    """Add bundled ffmpeg to PATH if running from PyInstaller bundle."""
    if getattr(sys, 'frozen', False):
        bundle_dir = sys._MEIPASS
    else:
        bundle_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    ffmpeg_dir = os.path.join(bundle_dir, 'ffmpeg')
    ffmpeg_name = 'ffmpeg.exe' if sys.platform == 'win32' else 'ffmpeg'
    ffmpeg_path = os.path.join(ffmpeg_dir, ffmpeg_name)

    if os.path.isfile(ffmpeg_path):
        current_path = os.environ.get('PATH', '')
        if ffmpeg_dir not in current_path:
            os.environ['PATH'] = ffmpeg_dir + os.pathsep + current_path
            logger.info("Added bundled ffmpeg to PATH: %s", ffmpeg_dir)
        return True

    return False


_setup_bundled_ffmpeg()

# Module-level cancel event - set from async side, checked from sync side
_cancel_event = threading.Event()


def cancel_stem_separation():
    """Signal the running stem separation to stop."""
    _cancel_event.set()


def _cancelled(progress_state):
    if progress_state is not None:
        progress_state["done"] = True
    return {"success": False, "cancelled": True, "error": "Cancelled"}


def _separate_stems_sync(audio_path, options=None, progress_state=None):
    """Run stem separation synchronously (called in executor thread).

    Uses Demucs v4 to separate audio into stems: vocals, drums, bass, other.
    Optionally analyzes each stem for energy curves and beat detection.
    """
    options = options or {}

    try:
        import torch
        import torchaudio
    except ImportError as e:
        return {"success": False, "error": f"Missing dependency: {e}"}

    try:
        from demucs.pretrained import get_model
        from demucs.apply import apply_model
    except ImportError as e:
        return {"success": False, "error": f"Missing dependency: {e}"}

    _cancel_event.clear()

    try:
        # --- Stage 1: Load audio ---
        if progress_state is not None:
            progress_state["stage"] = "loading"
            progress_state["percent"] = 0

        logger.info("Stem separation: loading audio from %s", audio_path)

        wav, sr = torchaudio.load(audio_path)
        # Demucs expects stereo at 44100 Hz
        if sr != 44100:
            wav = torchaudio.functional.resample(wav, sr, 44100)
            sr = 44100
        if wav.shape[0] == 1:
            wav = wav.repeat(2, 1)
        elif wav.shape[0] > 2:
            wav = wav[:2]

        audio_duration_ms = wav.shape[1] / sr * 1000

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 2: Load model ---
        if progress_state is not None:
            progress_state["stage"] = "loading_model"
            progress_state["percent"] = 10

        model_name = options.get("model", "htdemucs_ft")
        logger.info("Stem separation: loading model %s", model_name)

        device = "cuda" if torch.cuda.is_available() else "cpu"
        model = get_model(model_name)
        model.to(device)

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 3: Separate stems ---
        if progress_state is not None:
            progress_state["stage"] = "separating"
            progress_state["percent"] = 25

        logger.info("Stem separation: running on %s (%.1fs audio)", device, wav.shape[1] / sr)

        ref = wav.mean(0)
        wav_input = (wav - ref.mean()) / ref.std()
        wav_input = wav_input.unsqueeze(0).to(device)

        with torch.no_grad():
            sources = apply_model(model, wav_input, progress=False)

        sources = sources[0]  # remove batch dimension
        # Denormalize
        sources = sources * ref.std() + ref.mean()

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 4: Save stems to temp files ---
        if progress_state is not None:
            progress_state["stage"] = "saving_stems"
            progress_state["percent"] = 60

        stem_names = model.sources  # e.g. ['drums', 'bass', 'other', 'vocals']
        stem_dir = tempfile.mkdtemp(prefix="sc_stems_")
        stem_paths = {}

        for i, name in enumerate(stem_names):
            stem_path = os.path.join(stem_dir, f"{name}.wav")
            torchaudio.save(stem_path, sources[i].cpu(), sr)
            stem_paths[name] = stem_path

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 5: Analyze each stem for energy ---
        if progress_state is not None:
            progress_state["stage"] = "analyzing_stems"
            progress_state["percent"] = 70

        try:
            import librosa
        except ImportError:
            # Return stems without analysis
            if progress_state is not None:
                progress_state["done"] = True
            return {
                "success": True,
                "stems": {name: {"path": stem_paths[name]} for name in stem_names},
                "duration": audio_duration_ms
            }

        stems_result = {}
        stem_count = len(stem_names)

        for idx, name in enumerate(stem_names):
            if _cancel_event.is_set():
                return _cancelled(progress_state)

            percent = 70 + int((idx / stem_count) * 20)
            if progress_state is not None:
                progress_state["percent"] = percent
                progress_state["stage"] = f"analyzing_{name}"

            y_stem, stem_sr = librosa.load(stem_paths[name], sr=22050, mono=True)

            # Compute RMS energy envelope (downsampled to ~500ms intervals)
            hop_length = 512
            rms = librosa.feature.rms(y=y_stem, hop_length=hop_length)[0]
            frame_times = librosa.frames_to_time(np.arange(len(rms)), sr=22050, hop_length=hop_length)

            # Downsample to ~2 per second
            step = max(1, int(0.5 * 22050 / hop_length))
            energy = []
            for j in range(0, len(rms), step):
                energy.append({
                    "time": round(float(frame_times[j]) * 1000),
                    "loudness": round(float(rms[j]), 4)
                })

            stem_data = {
                "path": stem_paths[name],
                "energy": energy
            }

            # Beat detection for drums stem
            if name == "drums":
                tempo_result = librosa.beat.beat_track(y=y_stem, sr=22050, units="frames")
                beat_frames = tempo_result[1]
                beat_times = librosa.frames_to_time(beat_frames, sr=22050)

                onset_env = librosa.onset.onset_strength(y=y_stem, sr=22050)

                # Multi-band for drum type classification
                low_onset = librosa.onset.onset_strength(
                    y=y_stem, sr=22050,
                    fmin=20, fmax=200
                )
                mid_onset = librosa.onset.onset_strength(
                    y=y_stem, sr=22050,
                    fmin=200, fmax=4000
                )
                high_onset = librosa.onset.onset_strength(
                    y=y_stem, sr=22050,
                    fmin=4000, fmax=11025
                )

                beats = []
                for bi, bt in enumerate(beat_times):
                    frame_idx = beat_frames[bi] if bi < len(beat_frames) else 0
                    strength = float(onset_env[frame_idx]) if frame_idx < len(onset_env) else 0.5

                    low_val = float(low_onset[frame_idx]) if frame_idx < len(low_onset) else 0
                    mid_val = float(mid_onset[frame_idx]) if frame_idx < len(mid_onset) else 0
                    high_val = float(high_onset[frame_idx]) if frame_idx < len(high_onset) else 0

                    total = low_val + mid_val + high_val + 1e-8
                    if low_val / total > 0.5:
                        beat_type = "kick"
                    elif mid_val / total > 0.4:
                        beat_type = "snare"
                    else:
                        beat_type = "hihat"

                    beats.append({
                        "time": round(float(bt) * 1000),
                        "strength": round(strength, 3),
                        "type": beat_type
                    })

                stem_data["beats"] = beats

            # Vocal onset detection for vocals stem
            if name == "vocals":
                onset_frames = librosa.onset.onset_detect(y=y_stem, sr=22050, units="frames")
                onset_times = librosa.frames_to_time(onset_frames, sr=22050)
                stem_data["onsets"] = [round(float(t) * 1000) for t in onset_times]

            stems_result[name] = stem_data

        # --- Stage 6: Complete ---
        if progress_state is not None:
            progress_state["stage"] = "complete"
            progress_state["percent"] = 100
            progress_state["done"] = True

        logger.info("Stem separation complete: %d stems", len(stems_result))

        return {
            "success": True,
            "stems": stems_result,
            "duration": audio_duration_ms,
            "model": model_name,
            "device": device
        }

    except Exception as e:
        logger.error("Stem separation failed: %s", e, exc_info=True)
        if progress_state is not None:
            progress_state["done"] = True
        return {"success": False, "error": str(e)}


async def separate_stems_with_progress(audio_path, options=None):
    """Async generator that yields progress updates then final result.

    Runs stem separation in executor thread, polls progress_state dict.
    """
    if not audio_path:
        yield {"type": "result", "success": False, "error": "No audio path provided"}
        return

    loop = asyncio.get_event_loop()
    progress_state = {"stage": "init", "percent": 0, "done": False}

    future = loop.run_in_executor(
        EXECUTOR, _separate_stems_sync, audio_path, options, progress_state
    )

    last_percent = -1

    while not future.done():
        await asyncio.sleep(0.5)

        if not progress_state.get("done"):
            percent = progress_state.get("percent", 0)
            stage = progress_state.get("stage", "unknown")

            if percent != last_percent:
                last_percent = percent
                yield {
                    "type": "progress",
                    "percent": percent,
                    "stage": stage,
                }

    result = await future
    result["type"] = "result"
    yield result
