"""Generate one fixed Japanese passage per configured OpenRouter TTS model."""
import argparse
import concurrent.futures
import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.request
import wave

from auth import openrouter_key

ROOT = Path(__file__).resolve().parents[1]
API = "https://openrouter.ai/api/v1"
MODELS = [
    ("gemini38", "google/gemini-3.8-flash-tts", "Gemini 3.8 Flash TTS", "Kore"),
    ("eleven4", "elevenlabs/eleven-v4", "Eleven v4", "sarah"),
    ("eleven4turbo", "elevenlabs/eleven-v4-turbo", "Eleven v4 Turbo", "sarah"),
    ("gemini38lite", "google/gemini-3.8-flash-lite-tts", "Gemini 3.8 Flash-Lite TTS", "Kore"),
    ("drama3", "fish-audio/drama-3", "Fish Audio Drama 3", None),
    ("fish21", "fish-audio/s2.1-pro", "Fish Audio S2.1 Pro", None),
    ("fish2", "fish-audio/s2-pro", "Fish Audio S2 Pro", None),
    ("qwenplus", "qwen/qwen-audio-3.0-tts-plus", "Qwen-Audio 3.0 TTS Plus", "longanlingxin"),
    ("qwenflash", "qwen/qwen-audio-3.0-tts-flash", "Qwen-Audio 3.0 TTS Flash", "loongjohn"),
    ("eleven3", "elevenlabs/eleven-v3", "Eleven v3", "sarah"),
    ("gemini31", "google/gemini-3.1-flash-tts-preview", "Gemini 3.1 Flash TTS Preview", "Kore"),
]


def clean_error(message, key):
    message = message.replace(key, "[REDACTED]")
    return re.sub(r"(?:sk-or-[A-Za-z0-9_-]+|ops_[A-Za-z0-9_-]+)", "[REDACTED]", message)[:1600]


def get_json(path, key):
    request = urllib.request.Request(API + path, headers={"Authorization": "Bearer " + key})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def generate(config, key, text, snapshot):
    slug, mid, name, voice = config
    meta_path = ROOT / "metadata" / f"{slug}.json"
    response_format = "pcm" if mid.startswith("google/") else "mp3"
    audio_path = ROOT / "audio/raw" / f"{slug}.{'wav' if response_format == 'pcm' else 'mp3'}"
    previous_attempts = []
    if meta_path.exists():
        old = json.loads(meta_path.read_text())
        if old.get("status") == "success" and audio_path.exists():
            print(json.dumps({"model": mid, "status": "cached"}), flush=True)
            return old
        previous_attempts = old.get("attempts", [])
    payload = {"model": mid, "input": text, "response_format": response_format}
    if voice:
        payload["voice"] = voice
    catalog = snapshot.get(mid)
    if catalog is None:
        raise RuntimeError(f"Model is missing from the saved catalog: {mid}")
    if voice and voice not in catalog.get("supported_voices", []):
        raise RuntimeError(f"Selected voice is not in the saved catalog: {mid}")
    metadata = {
        "slug": slug, "model": mid, "name": name, "voice": voice,
        "voice_note": "provider default; voice omitted" if voice is None else "catalog voice",
        "canonical_slug": catalog.get("canonical_slug"),
        "input_sha256": hashlib.sha256(text.encode()).hexdigest(),
        "input_characters": len(text), "input_utf8_bytes": len(text.encode()),
        "payload": payload, "attempts": previous_attempts, "status": "pending",
    }
    for attempt in range(1, 4):
        started = time.monotonic()
        record = {"number": len(metadata['attempts']) + 1, "response_format": response_format, "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        try:
            request = urllib.request.Request(
                API + "/audio/speech", data=json.dumps(payload, ensure_ascii=False).encode(),
                headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "X-Title": "Japanese TTS Comparison"},
            )
            with urllib.request.urlopen(request, timeout=180) as response:
                first = response.read(4096)
                record["first_read_seconds"] = round(time.monotonic() - started, 3)
                audio = first + response.read(20 * 1024 * 1024)
                record["http_status"] = response.status
                record["response_headers"] = {k: v for k, v in response.headers.items() if k.lower() in {
                    "content-type", "x-generation-id", "x-request-id", "x-openrouter-cost", "x-usage-cost",
                }}
                content_type = response.headers.get("Content-Type", "").lower()
            if "json" in content_type or not audio or audio.lstrip().startswith(b"{"):
                raise ValueError("No audio returned: " + clean_error(audio[:1200].decode(errors="replace"), key))
            if response_format == "pcm":
                raw_path = ROOT / "audio/raw" / f"{slug}.pcm"
                raw_path.write_bytes(audio)
                # Gemini speech streams are signed 16-bit little-endian, 24 kHz mono.
                # Adding a WAV header does not alter the samples.
                with wave.open(str(audio_path), "wb") as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(24000)
                    wav.writeframes(audio)
                metadata["raw_file"] = str(raw_path.relative_to(ROOT))
                metadata["pcm_format"] = {"sample_rate": 24000, "channels": 1, "sample_width": 2}
            else:
                audio_path.write_bytes(audio)
            probe = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,sample_rate,channels", "-of", "json", str(audio_path)],
                capture_output=True, text=True, timeout=20,
            )
            if probe.returncode:
                raise ValueError("Audio response is not decodable by ffprobe")
            info = json.loads(probe.stdout)
            duration = float(info["format"]["duration"])
            if duration < 3:
                raise ValueError(f"Audio is unexpectedly short: {duration:.2f} seconds")
            record.update({"elapsed_seconds": round(time.monotonic() - started, 3), "bytes": len(audio)})
            metadata["attempts"].append(record)
            metadata.update({"status": "success", "audio_file": str(audio_path.relative_to(ROOT)), "response_audio_sha256": hashlib.sha256(audio).hexdigest(), "audio_file_sha256": hashlib.sha256(audio_path.read_bytes()).hexdigest(), "probe": info})
            break
        except urllib.error.HTTPError as error:
            record.update({"http_status": error.code, "error": clean_error(error.read(4000).decode(errors="replace"), key), "elapsed_seconds": round(time.monotonic() - started, 3)})
            metadata["attempts"].append(record)
            if error.code not in {429, 500, 502, 503, 504, 524, 529} or attempt == 3:
                metadata["status"] = "failed"
                break
            time.sleep(2 * attempt)
        except Exception as error:
            record.update({"error": clean_error(str(error), key), "elapsed_seconds": round(time.monotonic() - started, 3)})
            metadata["attempts"].append(record)
            metadata["status"] = "failed"
            break
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2))
    print(json.dumps({"model": mid, "status": metadata["status"], "duration": metadata.get("probe", {}).get("format", {}).get("duration"), "attempts": metadata["attempts"][-1]}, ensure_ascii=False), flush=True)
    return metadata


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--auth-only", action="store_true")
    args = parser.parse_args()
    key = openrouter_key()
    auth = get_json("/auth/key", key)["data"]
    print(json.dumps({"authentication": "verified"}), flush=True)
    if args.auth_only:
        return
    text = (ROOT / "script.txt").read_text().strip()
    snapshot = {m["id"]: m for m in json.loads((ROOT / "metadata/models-speech.json").read_text())["data"]}
    (ROOT / "metadata/model-configs.json").write_text(json.dumps([
        {"slug": s, "model": m, "name": n, "voice": v} for s, m, n, v in MODELS
    ], ensure_ascii=False, indent=2))
    selected = [c for c in MODELS if not args.only or c[0] in args.only]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as executor:
        list(executor.map(lambda cfg: generate(cfg, key, text, snapshot), selected))


if __name__ == "__main__":
    main()
