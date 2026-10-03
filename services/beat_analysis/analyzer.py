from __future__ import annotations

import math
import shutil
import struct
import subprocess
import wave
import json
from pathlib import Path


def convert_to_analysis_wav(source: Path, target: Path, timeout: int = 90) -> None:
    executable = shutil.which("ffmpeg")
    probe = shutil.which("ffprobe")
    if not executable:
        raise RuntimeError("FFmpeg is required to analyze this audio")
    if not probe:
        raise RuntimeError("FFprobe is required to validate audio duration")
    metadata = subprocess.run([probe, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(source)], check=True, timeout=15, capture_output=True, text=True)
    duration = float(json.loads(metadata.stdout)["format"]["duration"])
    from config.settings import settings
    if duration <= 0 or duration > settings.beat_analysis_max_duration_seconds:
        raise ValueError("Audio duration exceeds the configured maximum")
    subprocess.run(
        [executable, "-nostdin", "-v", "error", "-y", "-i", str(source), "-t", str(settings.beat_analysis_max_duration_seconds), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(target)],
        check=True, timeout=timeout, capture_output=True,
    )


def analyze_beat(original: Path, wav_path: Path) -> dict:
    convert_to_analysis_wav(original, wav_path)
    with wave.open(str(wav_path), "rb") as audio:
        channels, rate, frames = audio.getnchannels(), audio.getframerate(), audio.getnframes()
        duration = frames / rate
        total_sq = 0.0
        peak_sample = clipped = count = 0
        envelopes = []
        sum_l = sum_r = sum_ll = sum_rr = sum_lr = mid_sq = side_sq = 0.0
        while raw := audio.readframes(rate):
            samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)
            count += len(samples)
            total_sq += sum(x * x for x in samples)
            peak_sample = max(peak_sample, max((abs(x) for x in samples), default=0))
            clipped += sum(1 for x in samples if abs(x) >= 32760)
            envelopes.append(sum(x*x for x in samples) / max(1, len(samples)))
            if channels >= 2:
                left, right = samples[0::channels], samples[1::channels]
                sum_l += sum(left); sum_r += sum(right)
                sum_ll += sum(x*x for x in left); sum_rr += sum(x*x for x in right)
                sum_lr += sum(x*y for x, y in zip(left, right))
                mid_sq += sum((x+y)*(x+y) for x, y in zip(left, right))
                side_sq += sum((x-y)*(x-y) for x, y in zip(left, right))
    peak = peak_sample / 32768
    rms = math.sqrt(total_sq / max(1, count)) / 32768
    bands = {name: None for name in ("sub", "bass", "low_mid", "mid", "high_mid", "high")}
    # Band-level DSP is intentionally omitted without a vetted FFT dependency.
    width = correlation = None
    if channels >= 2:
        pairs = max(1, count // channels)
        covariance = sum_lr - (sum_l * sum_r / pairs)
        variance_l = sum_ll - sum_l * sum_l / pairs
        variance_r = sum_rr - sum_r * sum_r / pairs
        correlation = max(-1.0, min(1.0, covariance / math.sqrt(max(1.0, variance_l * variance_r))))
        width = side_sq / max(1.0, mid_sq + side_sq)
    preview_len = min(25.0, duration)
    # Pick the loudest window from the one-second RMS envelope, excluding the ending.
    window_seconds = max(1, int(preview_len))
    last_start = max(0, len(envelopes) - window_seconds - 1)
    best_start = max(range(last_start + 1), key=lambda index: sum(envelopes[index:index + window_seconds])) if envelopes else 0
    start_seconds = float(best_start)
    sorted_energy = sorted(envelopes)
    low_energy = sorted_energy[int((len(sorted_energy)-1)*.10)] if sorted_energy else 0
    high_energy = sorted_energy[int((len(sorted_energy)-1)*.95)] if sorted_energy else 0
    dynamic_range = 10 * math.log10(max(high_energy, 1) / max(low_energy, 1)) if high_energy else None
    silence_threshold = max((max(envelopes, default=0) * .001), 1)
    leading_silence = next((i for i, value in enumerate(envelopes) if value > silence_threshold), len(envelopes))
    insights = []
    if clipped:
        insights.append({"severity": "warning", "code": "CLIPPING_DETECTED", "title": "Clipping detected", "message": "Some samples reach the digital ceiling.", "suggestion": "Consider checking the output level before publishing."})
    if leading_silence >= 3:
        insights.append({"severity": "info", "code": "LONG_INITIAL_SILENCE", "title": "Initial silence detected", "message": f"The opening {leading_silence} seconds are very quiet.", "suggestion": "You may want to trim the opening before publishing."})
    if start_seconds >= 15:
        insights.append({"severity": "info", "code": "LATE_PREVIEW", "title": "Preview starts later", "message": "The recommended preview is located well after the beginning.", "suggestion": "Consider whether this section represents the beat well in the feed."})
    mono_risk = "low" if channels == 1 or (correlation is not None and correlation > .2) else "high" if correlation is not None and correlation < -.2 else "medium"
    return {
        "audio": {"duration_seconds": round(duration, 3), "sample_rate": rate, "channels": channels, "file_size": original.stat().st_size},
        "musical": {"bpm": None, "key": None, "scale": None, "key_confidence": None, "energy": round(min(1.0, rms * 3), 3)},
        "loudness": {"integrated_lufs": None, "true_peak_dbtp": None, "peak_dbfs": round(20 * math.log10(max(peak, 1e-9)), 2), "dynamic_range_db": round(dynamic_range, 2) if dynamic_range is not None else None, "clipping_detected": clipped > 0, "clipped_ratio": round(clipped / max(1, count), 8)},
        "stereo": {"correlation": round(correlation, 3) if correlation is not None else None, "width": round(width, 3) if width is not None else None, "mono_risk": mono_risk},
        "frequency_balance": bands,
        "preview": {"start_seconds": round(start_seconds, 2), "end_seconds": round(min(duration, start_seconds + preview_len), 2), "confidence": None},
        "insights": insights,
    }
