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
        # Prepend ffmpeg directory to PATH so librosa/audioread finds it
        current_path = os.environ.get('PATH', '')
        if ffmpeg_dir not in current_path:
            os.environ['PATH'] = ffmpeg_dir + os.pathsep + current_path
            logger.info("Added bundled ffmpeg to PATH: %s", ffmpeg_dir)
        return True

    return False


_setup_bundled_ffmpeg()

# Module-level cancel event - set from async side, checked from sync side
_cancel_event = threading.Event()


def _analyze_audio_sync(video_path, options=None, progress_state=None):
    """Run audio analysis synchronously (called in executor thread).

    Extracts audio from video, runs beat detection via librosa,
    optional section detection, tempo tracking, and energy analysis.
    """
    options = options or {}

    try:
        import librosa
    except ImportError as e:
        return {"success": False, "error": f"Missing dependency: {e}"}

    _cancel_event.clear()

    try:
        # --- Stage 1: Load audio ---
        if progress_state is not None:
            progress_state["stage"] = "loading"
            progress_state["percent"] = 0

        logger.info("Audio analysis: loading audio from %s", video_path)

        sr = options.get("sampleRate", 22050)
        max_duration = options.get("maxDuration", None)

        y, sr = librosa.load(video_path, sr=sr, mono=True, duration=max_duration)
        audio_duration_ms = len(y) / sr * 1000

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 2: Beat detection ---
        if progress_state is not None:
            progress_state["stage"] = "beats"
            progress_state["percent"] = 15

        logger.info("Audio analysis: detecting beats (%.1fs audio)", len(y) / sr)

        tempo_result = librosa.beat.beat_track(y=y, sr=sr, units="frames")
        tempo = tempo_result[0]
        beat_frames = tempo_result[1]
        beat_times = librosa.frames_to_time(beat_frames, sr=sr)

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 3: Onset strength for beat strength values ---
        if progress_state is not None:
            progress_state["stage"] = "onset_strength"
            progress_state["percent"] = 30

        onset_env = librosa.onset.onset_strength(y=y, sr=sr)

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 4: Multi-band onset detection for beat typing ---
        if progress_state is not None:
            progress_state["stage"] = "beat_types"
            progress_state["percent"] = 45

        S = np.abs(librosa.stft(y))
        freqs = librosa.fft_frequencies(sr=sr)

        # Frequency band masks
        low_mask = (freqs >= 20) & (freqs < 250)
        mid_mask = (freqs >= 250) & (freqs < 4000)
        high_mask = (freqs >= 4000) & (freqs <= 16000)

        # Per-band onset strength
        low_S = S[low_mask, :] if low_mask.any() else S[:1, :]
        mid_S = S[mid_mask, :] if mid_mask.any() else S[:1, :]
        high_S = S[high_mask, :] if high_mask.any() else S[:1, :]

        low_onset = librosa.onset.onset_strength(
            S=librosa.amplitude_to_db(low_S, ref=np.max), sr=sr
        )
        mid_onset = librosa.onset.onset_strength(
            S=librosa.amplitude_to_db(mid_S, ref=np.max), sr=sr
        )
        high_onset = librosa.onset.onset_strength(
            S=librosa.amplitude_to_db(high_S, ref=np.max), sr=sr
        )

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 5: Classify beats ---
        if progress_state is not None:
            progress_state["stage"] = "classify_beats"
            progress_state["percent"] = 55

        beats = []
        max_onset = max(float(onset_env.max()), 0.0001)

        for bt in beat_times:
            frame_idx = librosa.time_to_frames(bt, sr=sr)
            frame_idx = min(frame_idx, len(onset_env) - 1)

            strength = float(onset_env[frame_idx] / max_onset)

            # Determine beat type from band with highest onset energy
            li = min(frame_idx, len(low_onset) - 1)
            mi = min(frame_idx, len(mid_onset) - 1)
            hi = min(frame_idx, len(high_onset) - 1)

            low_val = float(low_onset[li])
            mid_val = float(mid_onset[mi])
            high_val = float(high_onset[hi])

            max_val = max(low_val, mid_val, high_val)
            if max_val == 0:
                beat_type = "kick"
            elif max_val == low_val:
                beat_type = "kick"
            elif max_val == mid_val:
                beat_type = "snare"
            else:
                beat_type = "hihat"

            beats.append({
                "time": int(round(bt * 1000)),
                "strength": round(strength, 3),
                "type": beat_type,
            })

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 6: Downbeats ---
        if progress_state is not None:
            progress_state["stage"] = "downbeats"
            progress_state["percent"] = 65

        # Assume 4/4 time: every 4th beat is a downbeat
        if len(beat_frames) >= 4:
            downbeat_times = librosa.frames_to_time(beat_frames[::4], sr=sr)
        else:
            downbeat_times = beat_times[:1] if len(beat_times) > 0 else []

        downbeats = [int(round(t * 1000)) for t in downbeat_times]

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 7: Section detection (optional, graceful fallback) ---
        if progress_state is not None:
            progress_state["stage"] = "sections"
            progress_state["percent"] = 75

        sections = []
        try:
            from scipy.ndimage import median_filter
            from sklearn.cluster import AgglomerativeClustering

            n_sections = options.get("numSections", 6)

            chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
            mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13)

            features = np.vstack([
                librosa.util.normalize(chroma),
                librosa.util.normalize(mfcc),
            ])

            n_frames = features.shape[1]
            if n_frames > n_sections * 2:
                R = librosa.segment.recurrence_matrix(
                    features, width=3, mode="affinity", sym=True
                )
                R_filtered = median_filter(R, size=(3, 3))

                # Distance matrix from similarity
                dist = 1.0 - librosa.util.normalize(R_filtered, norm=np.inf)
                np.fill_diagonal(dist, 0)

                clustering = AgglomerativeClustering(
                    n_clusters=min(n_sections, n_frames),
                    metric="precomputed",
                    linkage="average",
                )
                labels = clustering.fit_predict(dist)

                frame_times = librosa.frames_to_time(np.arange(n_frames), sr=sr)

                section_labels = ["intro", "verse", "chorus", "bridge", "verse2", "outro"]
                current_label_idx = 0
                seg_start = 0

                for i in range(1, len(labels)):
                    if labels[i] != labels[i - 1]:
                        label = section_labels[current_label_idx % len(section_labels)]
                        sections.append({
                            "start": int(round(frame_times[seg_start] * 1000)),
                            "end": int(round(frame_times[i] * 1000)),
                            "label": label,
                        })
                        seg_start = i
                        current_label_idx += 1

                # Final segment
                if seg_start < len(labels):
                    label = section_labels[current_label_idx % len(section_labels)]
                    sections.append({
                        "start": int(round(frame_times[seg_start] * 1000)),
                        "end": int(round(audio_duration_ms)),
                        "label": label,
                    })

        except ImportError:
            logger.info("Section detection skipped: scipy/sklearn not available")
        except Exception as e:
            logger.warning("Section detection failed (non-fatal): %s", e)

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 8: Energy/loudness curve ---
        if progress_state is not None:
            progress_state["stage"] = "energy"
            progress_state["percent"] = 88

        hop_length = 512
        rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
        rms_times = librosa.frames_to_time(
            np.arange(len(rms)), sr=sr, hop_length=hop_length
        )

        # Downsample to ~500ms intervals
        target_interval = 0.5
        step = max(1, int(target_interval * sr / hop_length))

        max_rms = max(float(rms.max()), 0.0001)
        energy = []
        for i in range(0, len(rms), step):
            energy.append({
                "time": int(round(rms_times[i] * 1000)),
                "loudness": round(float(rms[i] / max_rms), 3),
            })

        if _cancel_event.is_set():
            return _cancelled(progress_state)

        # --- Stage 9: Tempo tracking ---
        if progress_state is not None:
            progress_state["stage"] = "tempo"
            progress_state["percent"] = 95

        tempo_curve = []
        window_length = int(10 * sr / hop_length)  # 10-second windows
        for start_frame in range(0, len(onset_env), window_length):
            end_frame = min(start_frame + window_length, len(onset_env))
            if end_frame - start_frame < 10:
                break
            window_onset = onset_env[start_frame:end_frame]
            local_tempo = librosa.feature.tempo(
                onset_envelope=window_onset, sr=sr, hop_length=hop_length
            )
            t = librosa.frames_to_time(start_frame, sr=sr, hop_length=hop_length)
            tempo_curve.append({
                "time": int(round(t * 1000)),
                "bpm": round(float(local_tempo[0])),
            })

        # Done
        if progress_state is not None:
            progress_state["stage"] = "complete"
            progress_state["percent"] = 100
            progress_state["done"] = True

        global_bpm = round(float(tempo if np.isscalar(tempo) else tempo[0]))

        logger.info(
            "Audio analysis complete: %d beats, %d sections, BPM=%d, duration=%.1fs",
            len(beats), len(sections), global_bpm, audio_duration_ms / 1000,
        )

        return {
            "success": True,
            "beats": beats,
            "downbeats": downbeats,
            "sections": sections,
            "tempo": tempo_curve,
            "energy": energy,
            "bpm": global_bpm,
            "duration": int(round(audio_duration_ms)),
        }

    except Exception as e:
        if progress_state is not None:
            progress_state["done"] = True
        logger.error("Audio analysis failed: %s", e)
        return {"success": False, "error": f"Audio analysis failed: {str(e)}"}


def _cancelled(progress_state):
    if progress_state is not None:
        progress_state["done"] = True
    return {"success": False, "cancelled": True, "error": "Cancelled by user"}


def cancel_audio_analysis():
    """Signal the running analysis to stop."""
    _cancel_event.set()


async def analyze_audio_with_progress(video_path, options=None):
    """Analyze audio with async progress generator.

    Yields progress dicts then final result, exactly like detect_scenes_with_progress.
    """
    if not video_path:
        yield {"type": "result", "success": False, "error": "No video/audio path provided"}
        return

    loop = asyncio.get_event_loop()
    progress_state = {"stage": "init", "percent": 0, "done": False}

    future = loop.run_in_executor(
        EXECUTOR, _analyze_audio_sync, video_path, options, progress_state
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
