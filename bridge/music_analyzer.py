import asyncio
import logging
import os
import sys
import threading

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


# Label normalization map
_LABEL_MAP = {
    "intro": "intro",
    "outro": "outro",
    "chorus": "chorus",
    "verse": "verse",
    "bridge": "bridge",
    "break": "bridge",
    "inst": "bridge",
    "instrumental": "bridge",
}

# Labels to skip entirely
_SKIP_LABELS = {"start", "end", "silence"}


def _normalize_label(raw_label):
    """Normalize a segment label to one of our standard labels.

    Returns the normalized label string, or None if the label should be skipped.
    """
    lower = raw_label.strip().lower()

    if lower in _SKIP_LABELS:
        return None

    if lower in _LABEL_MAP:
        return _LABEL_MAP[lower]

    # Check if the label starts with a known prefix (e.g. "verse 2", "chorus_a")
    for prefix, normalized in _LABEL_MAP.items():
        if lower.startswith(prefix):
            return normalized

    # Unknown label -- default to "verse" rather than dropping it
    return "verse"


def cancel_music_analysis():
    """Signal the running music analysis to stop."""
    _cancel_event.set()


def _cancelled(progress_state):
    if progress_state is not None:
        progress_state["done"] = True
    return {"success": False, "cancelled": True, "error": "Cancelled by user"}


def _analyze_music_sync(audio_path, options=None, progress_state=None):
    """Run music analysis synchronously (called in executor thread).

    Uses Beat This! for beat/downbeat detection and allin1 for structure
    segmentation. Falls back gracefully if either library is unavailable.
    """
    options = options or {}

    _cancel_event.clear()

    try:
        # --- Stage 1: Loading (0%) ---
        if progress_state is not None:
            progress_state["stage"] = "loading"
            progress_state["percent"] = 0

        if not os.path.isfile(audio_path):
            if progress_state is not None:
                progress_state["done"] = True
            return {"success": False, "error": f"File not found: {audio_path}"}

        logger.info("Music analysis: starting analysis of %s", audio_path)

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 2: Beat detection with Beat This! (5-35%) ---
        if progress_state is not None:
            progress_state["stage"] = "beat_detection"
            progress_state["percent"] = 5

        logger.info("Music analysis: running beat detection with Beat This!")

        beats = []
        bpm = 0

        try:
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
            logger.info("Music analysis: using device %s for beat detection", device)

            if progress_state is not None:
                progress_state["percent"] = 10

            # Try torch.hub first, fall back to direct import
            beat_times_sec = None
            downbeat_times_sec = None

            try:
                model = torch.hub.load('CPJKU/beat_this', 'beat_this', device=device)

                if progress_state is not None:
                    progress_state["percent"] = 15

                if _cancel_event.is_set():
                    return _cancelled(progress_state)

                # torch.hub model returns (beat_times, downbeat_times) as numpy arrays
                import torchaudio
                waveform, sr = torchaudio.load(audio_path)
                # Convert to mono if needed
                if waveform.shape[0] > 1:
                    waveform = waveform.mean(dim=0, keepdim=True)
                waveform = waveform.squeeze(0).numpy()

                if progress_state is not None:
                    progress_state["percent"] = 20

                if _cancel_event.is_set():
                    return _cancelled(progress_state)

                beat_times_sec, downbeat_times_sec = model(waveform, sr)

            except Exception as hub_err:
                logger.info(
                    "Music analysis: torch.hub load failed (%s), trying direct import",
                    hub_err
                )

                try:
                    from beat_this.inference import File2Beats

                    if progress_state is not None:
                        progress_state["percent"] = 15

                    if _cancel_event.is_set():
                        return _cancelled(progress_state)

                    file2beats = File2Beats(device=device)

                    if progress_state is not None:
                        progress_state["percent"] = 20

                    if _cancel_event.is_set():
                        return _cancelled(progress_state)

                    beat_times_sec, downbeat_times_sec = file2beats(audio_path)

                except ImportError as imp_err:
                    raise ImportError(
                        f"Beat This! not available via hub or direct import: {imp_err}"
                    ) from imp_err

            if progress_state is not None:
                progress_state["percent"] = 30

            if _cancel_event.is_set():
                return _cancelled(progress_state)

            # Convert numpy arrays to our format
            beat_times_sec = np.asarray(beat_times_sec, dtype=np.float64)
            downbeat_times_sec = np.asarray(downbeat_times_sec, dtype=np.float64)

            # Build a set of downbeat times in ms for fast lookup
            downbeat_ms_set = set(int(round(t * 1000)) for t in downbeat_times_sec)

            for bt in beat_times_sec:
                beat_ms = int(round(bt * 1000))

                # Check if this beat is within 30ms of any downbeat
                is_downbeat = False
                for db_ms in downbeat_ms_set:
                    if abs(beat_ms - db_ms) <= 30:
                        is_downbeat = True
                        break

                beats.append({
                    "time": beat_ms,
                    "isDownbeat": is_downbeat,
                })

            # Estimate BPM from median inter-beat interval
            if len(beat_times_sec) >= 2:
                intervals = np.diff(beat_times_sec)
                median_interval = float(np.median(intervals))
                if median_interval > 0:
                    bpm = round(60.0 / median_interval)

            if progress_state is not None:
                progress_state["percent"] = 35

            logger.info(
                "Music analysis: detected %d beats (%d downbeats), estimated BPM=%d",
                len(beats),
                int(np.sum([1 for b in beats if b["isDownbeat"]])),
                bpm,
            )

        except ImportError as e:
            if progress_state is not None:
                progress_state["done"] = True
            logger.error("Music analysis: Beat This! not available: %s", e)
            return {"success": False, "error": f"Beat This! not available: {e}"}
        except Exception as e:
            if progress_state is not None:
                progress_state["done"] = True
            logger.error("Music analysis: beat detection failed: %s", e, exc_info=True)
            return {"success": False, "error": f"Beat detection failed: {e}"}

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 3: Structure analysis with allin1 (40-85%) ---
        if progress_state is not None:
            progress_state["stage"] = "structure_analysis"
            progress_state["percent"] = 40

        logger.info("Music analysis: running structure analysis with allin1")

        sections = []
        allin1_bpm = None

        try:
            import allin1

            if progress_state is not None:
                progress_state["percent"] = 50

            if _cancel_event.is_set():
                return _cancelled(progress_state)

            # Use GPU if available (device may already be set from beat detection)
            try:
                import torch
                allin1_device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                allin1_device = "cpu"

            result = allin1.analyze(audio_path, device=allin1_device)

            if progress_state is not None:
                progress_state["percent"] = 75

            if _cancel_event.is_set():
                return _cancelled(progress_state)

            # Extract BPM from allin1 if available
            if hasattr(result, 'bpm') and result.bpm is not None:
                allin1_bpm = round(float(result.bpm))

            # Convert segments to our format
            if hasattr(result, 'segments') and result.segments:
                for seg in result.segments:
                    raw_label = str(seg.label) if hasattr(seg, 'label') else "verse"
                    normalized = _normalize_label(raw_label)

                    if normalized is None:
                        continue

                    start_ms = int(round(float(seg.start) * 1000))
                    end_ms = int(round(float(seg.end) * 1000))

                    sections.append({
                        "start": start_ms,
                        "end": end_ms,
                        "label": normalized,
                    })

            if progress_state is not None:
                progress_state["percent"] = 85

            logger.info(
                "Music analysis: detected %d sections via allin1 (BPM=%s)",
                len(sections),
                allin1_bpm,
            )

        except ImportError:
            logger.warning(
                "Music analysis: allin1 not available, skipping structure analysis"
            )
        except Exception as e:
            logger.warning(
                "Music analysis: structure analysis failed (non-fatal): %s", e
            )

        # Use allin1 BPM if available (generally more accurate than interval estimate)
        if allin1_bpm is not None and allin1_bpm > 0:
            bpm = allin1_bpm

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 4: Finalizing (90%) ---
        if progress_state is not None:
            progress_state["stage"] = "finalizing"
            progress_state["percent"] = 90

        # Get audio duration via librosa (lightweight, reads metadata only)
        try:
            import librosa
            duration_sec = librosa.get_duration(path=audio_path)
            duration_ms = int(round(duration_sec * 1000))
        except Exception as e:
            logger.warning("Music analysis: could not get duration via librosa: %s", e)
            # Fallback: estimate from last beat time
            if beats:
                duration_ms = beats[-1]["time"] + 1000
            elif sections:
                duration_ms = sections[-1]["end"]
            else:
                duration_ms = 0

        # If no sections were detected, create one section spanning the whole track
        if not sections and duration_ms > 0:
            sections.append({
                "start": 0,
                "end": duration_ms,
                "label": "verse",
            })

        # Mark complete
        if progress_state is not None:
            progress_state["stage"] = "complete"
            progress_state["percent"] = 100
            progress_state["done"] = True

        logger.info(
            "Music analysis complete: %d beats, %d sections, BPM=%d, duration=%.1fs",
            len(beats), len(sections), bpm, duration_ms / 1000,
        )

        return {
            "success": True,
            "beats": beats,
            "sections": sections,
            "bpm": bpm,
            "duration": duration_ms,
        }

    except Exception as e:
        if progress_state is not None:
            progress_state["done"] = True
        logger.error("Music analysis failed: %s", e, exc_info=True)
        return {"success": False, "error": f"Music analysis failed: {str(e)}"}


async def analyze_music_with_progress(audio_path, options=None):
    """Analyze music with async progress generator.

    Yields progress dicts then final result, matching the pattern of
    analyze_audio_with_progress and separate_stems_with_progress.
    """
    if not audio_path:
        yield {"type": "result", "success": False, "error": "No audio path provided"}
        return

    loop = asyncio.get_running_loop()
    progress_state = {"stage": "init", "percent": 0, "done": False}

    future = loop.run_in_executor(
        EXECUTOR, _analyze_music_sync, audio_path, options, progress_state
    )

    last_percent = -1

    while not future.done():
        await asyncio.sleep(0.3)

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
