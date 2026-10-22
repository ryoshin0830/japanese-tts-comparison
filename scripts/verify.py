"""Verify reproducibility, actual media streams, loudness, and secret-free artifacts."""
import concurrent.futures
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import subprocess

from PIL import Image, ImageChops, ImageStat

ROOT = Path(__file__).resolve().parents[1]


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


def decode(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], capture_output=True, text=True)
    if result.returncode or result.stderr.strip():
        raise AssertionError(f"Decode failed for {path.name}: {result.stderr[-600:]}")


def audio_check(config):
    meta = json.loads((ROOT / "metadata" / f"{config['slug']}.json").read_text())
    assert meta["status"] == "success", config["slug"]
    text = (ROOT / "script.txt").read_text().strip()
    assert meta["payload"]["input"] == text
    assert meta["input_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    path = ROOT / meta["audio_file"]
    decode(path)
    source_info = probe(path)
    duration = float(source_info["format"]["duration"])
    assert 20 < duration < 120, (config["slug"], duration)
    raw_hash = meta.get("audio_file_sha256", meta.get("audio_sha256"))
    assert hashlib.sha256(path.read_bytes()).hexdigest() == raw_hash
    loud = json.loads((ROOT / "metadata" / f"{config['slug']}-loudness.json").read_text())
    normalized = ROOT / loud["normalized_file"]
    assert hashlib.sha256(normalized.read_bytes()).hexdigest() == loud["sha256"]
    info = probe(normalized)
    stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
    assert stream["sample_rate"] == "48000" and stream["channels"] == 1
    assert abs(float(info["format"]["duration"]) - duration) < 0.1
    measured = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(normalized), "-af", "loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"], capture_output=True, text=True, check=True)
    metrics = json.loads(re.findall(r'\{\s*"input_i".*?\}', measured.stderr, re.S)[-1])
    assert abs(float(metrics["input_i"]) + 18) < 0.5, metrics
    assert float(metrics["input_tp"]) <= -1.3, metrics
    return {"slug": config["slug"], "duration_seconds": duration, "normalized_lufs": float(metrics["input_i"]), "normalized_true_peak_dbtp": float(metrics["input_tp"]), "decode": "passed", "same_script": True}


def clip_check(config):
    path = ROOT / "video/models" / f"{config['slug']}.mp4"
    decode(path)
    info = probe(path)
    stream = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert stream["width"] == 1920 and stream["height"] == 1080
    meta = json.loads((ROOT / "metadata" / f"{config['slug']}.json").read_text())
    expected = float(meta["probe"]["format"]["duration"]) + 2.8
    assert abs(float(info["format"]["duration"]) - expected) < 0.15
    segment = ROOT / "video/segments" / f"labeled-{config['slug']}.mp4"
    assert hashlib.sha256(segment.read_bytes()).digest() == hashlib.sha256(path.read_bytes()).digest()
    return {"slug": config["slug"], "file": str(path.relative_to(ROOT)), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "complete_decode": "passed", "matches_labeled_segment": True}


def main():
    configs = json.loads((ROOT / "metadata/model-configs.json").read_text())
    assert len(configs) == 11
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        audio = list(pool.map(audio_check, configs))
    manifest = json.loads((ROOT / "metadata/video-manifest.json").read_text())
    billing = json.loads((ROOT / "metadata/billing.json").read_text())
    exchange = json.loads((ROOT / "metadata/exchange-rate.json").read_text())
    assert exchange["jpy_per_usd"] > 0 and exchange["source_url"].startswith("https://")
    assert exchange["quoted_at"].startswith("2026-10-22")
    videos = []
    for mode, entry in manifest.items():
        path = ROOT / entry["video"]
        decode(path)
        info = probe(path)
        stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert stream["width"] == 1920 and stream["height"] == 1080 and stream["codec_name"] == "h264"
        expected = 20 + sum(c["duration_seconds"] for c in entry["chapters"])
        assert abs(float(info["format"]["duration"]) - expected) < 1
        assert len(entry["chapters"]) == 11
        assert entry["exchange_rate"] == exchange
        overlays = []
        for chapter in entry["chapters"]:
            slug = chapter["slug"]
            meta = json.loads((ROOT / "metadata" / f"{slug}.json").read_text())
            bill = next(row for row in billing["generations"] if row["slug"] == slug)
            cost = sum(Decimal(str(g["total_cost"])) for g in bill["generations"])
            yen = cost * Decimal(str(exchange["jpy_per_usd"]))
            success = next(a for a in reversed(meta["attempts"]) if a.get("http_status") == 200)
            metrics = chapter["generation_metrics"]
            assert Decimal(str(metrics["cost_usd"])) == cost
            assert Decimal(str(metrics["estimated_cost_jpy"])) == yen
            assert metrics["elapsed_seconds"] == success["elapsed_seconds"]
            assert f"${cost:.7f}" in metrics["display"] and f"¥{yen:.2f}" in metrics["display"]
            assert f"{success['elapsed_seconds']:.2f} 秒" in metrics["display"]
            frame = ROOT / "frames" / f"verified-{mode}-{slug}.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", str(chapter["start_seconds"] + 1), "-i", str(path), "-frames:v", "1", str(frame)], capture_output=True, check=True)
            reference = Image.open(ROOT / "frames" / f"{mode}-{slug}.png").convert("RGB")
            actual = Image.open(frame).convert("RGB")
            crop = (80, 210, 1500, 253)
            difference = ImageStat.Stat(ImageChops.difference(reference.crop(crop), actual.crop(crop)))
            assert max(difference.mean) < 8, (mode, slug, difference.mean)
            overlays.append({"slug": slug, "billing_and_timing_match": True, "video_overlay_matches_card": True, "pixel_mean_error": difference.mean})
        videos.append({"mode": mode, "file": entry["video"], "duration_seconds": float(info["format"]["duration"]), "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "complete_decode": "passed", "resolution": "1920x1080", "overlays": overlays})
    blind = json.loads((ROOT / "blind-answer-key.json").read_text())
    assert sorted(m["slug"] for m in blind) == sorted(c["slug"] for c in configs)
    assert len({m["sample"] for m in blind}) == 11
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        model_clips = list(pool.map(clip_check, configs))
    secret_pattern = re.compile(rb"(?:sk-or-v1-[A-Za-z0-9_-]{40,}|ops_[A-Za-z0-9_-]{40,})")
    text_files = [p for p in ROOT.rglob("*") if p.is_file() and p.suffix in {".md", ".txt", ".json", ".csv", ".py"}]
    assert not any(secret_pattern.search(p.read_bytes()) for p in text_files), "Secret-like credential in artifact"
    report = {"status": "passed", "model_count": len(audio), "same_input_for_all_models": True, "audio": audio, "videos": videos, "model_clips": model_clips, "exchange_rate": exchange, "secret_scan": {"files_scanned": len(text_files), "credential_patterns_found": False}}
    (ROOT / "metadata/verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
