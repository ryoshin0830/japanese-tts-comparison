"""Normalize the saved recordings and render labeled and anonymous comparisons."""
import argparse
import concurrent.futures
import csv
from decimal import Decimal
import hashlib
import json
import os
import shutil
from pathlib import Path
import random
import re
import subprocess

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
W, H = 1920, 1080
def find_font(env_name, candidates):
    override = os.environ.get(env_name)
    for name in ([override] if override else candidates):
        if Path(name).is_file():
            return name
    raise RuntimeError(f"No font found. Set {env_name} to a suitable font file.")


FONT = find_font("JAPANESE_TTS_FONT", [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansJP-Regular.ttf",
])
LATIN = find_font("JAPANESE_TTS_LATIN_FONT", [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    FONT,
])
INK, MUTED, ACCENT = "#E9EEF9", "#A5B0C6", "#9BAEFF"
BG, PANEL = "#101624", "#1A2335"
FX = json.loads((ROOT / "metadata/exchange-rate.json").read_text())
BILLING = json.loads((ROOT / "metadata/billing.json").read_text())


def generation_metrics(meta):
    billed = next(row for row in BILLING["generations"] if row["slug"] == meta["slug"])
    cost = sum(Decimal(str(g["total_cost"])) for g in billed["generations"])
    success = next(a for a in reversed(meta["attempts"]) if a.get("http_status") == 200)
    yen = cost * Decimal(str(FX["jpy_per_usd"]))
    return {
        "cost_usd": float(cost), "estimated_cost_jpy": float(yen),
        "elapsed_seconds": success["elapsed_seconds"],
        "display": f"TTS実費  ${cost:.7f}  ≈ ¥{yen:.2f}     |     処理時間  {success['elapsed_seconds']:.2f} 秒",
    }


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(result.stderr[-3000:])
    return result


def probe(path):
    result = json.loads(run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)]).stdout)
    result.get("format", {}).pop("filename", None)
    return result


def wrap(text, font, width):
    rows, line = [], ""
    for char in text:
        if font.getlength(line + char) > width and line:
            # Prefer a nearby Japanese phrase boundary over splitting a word.
            cuts = [i + 1 for i, c in enumerate(line) if c in "。、！？"]
            cut = cuts[-1] if cuts and cuts[-1] >= len(line) - 14 else len(line)
            rows.append(line[:cut])
            line = line[cut:] + char
        else:
            line += char
    if line:
        rows.append(line)
    assert "".join(rows) == text
    return rows


def new_card():
    im = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 16, H), fill=ACCENT)
    d.text((86, 45), "JAPANESE TTS / SAME SCRIPT", font=ImageFont.truetype(LATIN, 25), fill=ACCENT)
    d.text((1570, 45), "2026.10.22", font=ImageFont.truetype(LATIN, 25), fill=MUTED)
    return im, d


def make_script_card(meta, sample, total, blind=False):
    image, d = new_card()
    name = f"SAMPLE {sample}" if blind else meta["name"]
    d.text((86, 105), name, font=ImageFont.truetype(LATIN, 60), fill=INK)
    sub = "匿名比較 / モデル名は別紙の対応表へ" if blind else f"Voice: {meta.get('voice') or 'provider default (no voice specified)'}"
    d.text((90, 178), sub, font=ImageFont.truetype(FONT, 24), fill=MUTED)
    metrics = generation_metrics(meta)
    d.text((90, 214), metrics["display"], font=ImageFont.truetype(FONT, 29), fill=ACCENT)
    d.rounded_rectangle((78, 254, 1842, 914), radius=24, fill=PANEL)
    d.text((112, 278), "共通原稿 — 読み仮名・読み補正なし", font=ImageFont.truetype(FONT, 27), fill=ACCENT)
    paras = (ROOT / "script.txt").read_text().strip().split("\n\n")
    font = ImageFont.truetype(FONT, 41)
    y = 340
    for para in paras:
        for row in wrap(para, font, 1680):
            d.text((112, y), row, font=font, fill=INK)
            y += 57
        y += 12
    if y > 904:
        raise ValueError(f"Japanese script does not fit: bottom={y}")
    d.text((90, 960), "音量のみ統一  −18 LUFS  /  速度変更なし  /  BGMなし", font=ImageFont.truetype(FONT, 28), fill=MUTED)
    d.text((90, 1006), f"円換算：1 USD = {FX['jpy_per_usd']:.2f} JPY（SMBC / 2026-10-22 19:35 JST / TTS）", font=ImageFont.truetype(FONT, 21), fill=MUTED)
    d.text((90, 1040), "処理時間：成功したAPIリクエストの送信〜音声受信・形式確認まで（動画編集を除く）", font=ImageFont.truetype(FONT, 19), fill=MUTED)
    d.text((1690, 1000), f"{sample} / {total}", font=ImageFont.truetype(LATIN, 27), fill=ACCENT)
    path = ROOT / "frames" / f"{'blind' if blind else 'labeled'}-{meta['slug']}.png"
    image.save(path)
    return path


def make_info_card(title, subtitle, lines, filename):
    image, d = new_card()
    d.text((88, 140), title, font=ImageFont.truetype(FONT, 62), fill=INK)
    d.text((92, 245), subtitle, font=ImageFont.truetype(FONT, 31), fill=ACCENT)
    y = 380
    for line in lines:
        for row in wrap(line, ImageFont.truetype(FONT, 38), 1680):
            d.text((94, y), row, font=ImageFont.truetype(FONT, 38), fill=INK)
            y += 66
        y += 30
    if y > 1040:
        raise ValueError("Information card exceeds image bounds")
    path = ROOT / "frames" / filename
    image.save(path)
    return path


def normalize(meta):
    source = ROOT / meta["audio_file"]
    output = ROOT / "audio/normalized" / f"{meta['slug']}.wav"
    metrics_path = ROOT / "metadata" / f"{meta['slug']}-loudness.json"
    if output.exists() and metrics_path.exists():
        return
    analysis = run(["ffmpeg", "-hide_banner", "-i", str(source), "-af", "loudnorm=I=-18:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"])
    found = re.findall(r'\{\s*"input_i".*?\}', analysis.stderr, re.S)
    if not found:
        raise ValueError("loudnorm analysis did not return measurement data")
    first = json.loads(found[-1])
    if float(first["input_i"]) < -65:
        raise ValueError("Generated audio is effectively silent")
    filt = (
        "loudnorm=I=-18:TP=-1.5:LRA=11:"
        f"measured_I={first['input_i']}:measured_TP={first['input_tp']}:"
        f"measured_LRA={first['input_lra']}:measured_thresh={first['input_thresh']}:"
        f"offset={first['target_offset']}:linear=true:print_format=json"
    )
    result = run(["ffmpeg", "-y", "-hide_banner", "-i", str(source), "-af", filt, "-ar", "48000", "-ac", "1", "-c:a", "pcm_s16le", str(output)])
    measurement = re.findall(r'\{\s*"input_i".*?\}', result.stderr, re.S)
    final = json.loads(measurement[-1])
    metrics = {"original": first, "normalized": final, "normalized_file": str(output.relative_to(ROOT)), "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "probe": probe(output)}
    metrics_path.write_text(json.dumps(metrics, indent=2))
    print(json.dumps({"normalized": meta["slug"], "input_lufs": first["input_i"], "output_lufs": final["output_i"]}), flush=True)


def encode_still(image, output, audio=None, duration=8):
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-loop", "1", "-framerate", "24", "-i", str(image)]
    if audio:
        command += ["-i", str(audio), "-af", "adelay=2000,apad=pad_dur=0.8"]
        duration = float(probe(audio)["format"]["duration"]) + 2.8
    else:
        command += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono"]
    command += ["-t", f"{duration:.6f}", "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "23", "-pix_fmt", "yuv420p", "-r", "24", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1", "-movflags", "+faststart", str(output)]
    run(command)
    return duration


def timestamp(seconds):
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def add_chapters(output, chapters, mode):
    total = float(probe(output)["format"]["duration"])
    entries = [{"start_seconds": 0, "title": "Introduction"}] + chapters
    entries.append({"start_seconds": chapters[-1]["start_seconds"] + chapters[-1]["duration_seconds"], "title": "Reading checklist"})
    lines = [";FFMETADATA1", "title=Japanese TTS Comparison " + mode, "comment=AI-generated speech. Same script. Loudness normalized. 2026-10-22."]
    for index, entry in enumerate(entries):
        end = entries[index+1]["start_seconds"] if index+1 < len(entries) else total
        title = entry["title"].replace("\\", "\\\\").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={int(entry['start_seconds']*1000)}", f"END={int(end*1000)}", "title=" + title]
    metadata = ROOT / "metadata" / f"chapters-{mode}.ffmetadata"
    metadata.write_text("\n".join(lines) + "\n")
    temporary = output.with_name(output.stem + "-chapters.mp4")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(output), "-i", str(metadata), "-map", "0:v:0", "-map", "0:a:0", "-map_metadata", "1", "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart", str(temporary)])
    temporary.replace(output)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()
    for folder in ["frames", "audio/normalized", "video/segments", "video/models"]:
        (ROOT / folder).mkdir(parents=True, exist_ok=True)
    configs = json.loads((ROOT / "metadata/model-configs.json").read_text())
    successes, failures = [], []
    for cfg in configs:
        path = ROOT / "metadata" / f"{cfg['slug']}.json"
        if not path.exists():
            failures.append({**cfg, "status": "not_generated"})
            continue
        meta = json.loads(path.read_text())
        (successes if meta["status"] == "success" else failures).append(meta)
    if not successes:
        raise RuntimeError("No successful audio to render")
    for n, meta in enumerate(successes, 1):
        make_script_card(meta, str(n).zfill(2), len(successes))
    if args.preview:
        print("Preview cards rendered")
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(normalize, successes))
    (ROOT / "metadata/failures.json").write_text(json.dumps(failures, ensure_ascii=False, indent=2))
    blind_order = list(successes)
    random.Random(20261022).shuffle(blind_order)
    blind_map = [{"sample": chr(65+n), "model": m["model"], "name": m["name"], "voice": m.get("voice"), "slug": m["slug"]} for n, m in enumerate(blind_order)]
    (ROOT / "blind-answer-key.json").write_text(json.dumps(blind_map, ensure_ascii=False, indent=2))
    manifests = {}
    for mode, order in [("labeled", successes), ("blind", blind_order)]:
        intro = make_info_card(
            "日本語TTS 聞き比べ" if mode == "labeled" else "日本語TTS 匿名比較",
            f"{len(order)}モデル / 同じ262文字の原稿 / 2026年10月22日",
            ["読み仮名なしの同一原稿。各モデルの初回成功音声を比較します。", "音量のみ統一。各サンプルは画面表示から2秒後に再生します。", "各画面にTTS実費（USD・参考円換算）と処理時間を表示。", "処理時間は成功リクエストの送信〜音声受信・形式確認。編集時間は除きます。", "モデルと声の組み合わせの比較です。統計的なランキングではありません。"],
            f"intro-{mode}.png",
        )
        outro = make_info_card(
            "比較のチェックポイント",
            "原稿と想定される読みは同梱のファイルで確認できます。",
            ["地名：かずさいちのみや / しもうさなかやま / にほんばし", "難読語：しにせ / だし / うなじゅう / なまもの / しょうが", "日付・数字：ついたち / みっかかん / はつか / 金額", "匿名版のモデル対応は blind-answer-key.json に保存しています。"],
            f"outro-{mode}.png",
        )
        parts, chapters = [], []
        intro_mp4 = ROOT / "video/segments" / f"intro-{mode}.mp4"
        encode_still(intro, intro_mp4, duration=10)
        parts.append(intro_mp4)
        at = 10.0
        tasks = []
        for n, meta in enumerate(order):
            sample = chr(65+n) if mode == "blind" else str(n+1).zfill(2)
            frame = make_script_card(meta, sample, len(order), blind=mode == "blind")
            clip = ROOT / "video/segments" / f"{mode}-{meta['slug']}.mp4"
            audio = ROOT / "audio/normalized" / f"{meta['slug']}.wav"
            duration = float(probe(audio)["format"]["duration"]) + 2.8
            chapters.append({"start_seconds": round(at, 3), "start": timestamp(at), "sample": sample, "title": f"SAMPLE {sample}" if mode == "blind" else meta["name"], "slug": meta["slug"], "duration_seconds": round(duration, 3), "generation_metrics": generation_metrics(meta)})
            at += duration
            parts.append(clip)
            tasks.append((frame, clip, audio))
        def render(task):
            frame, clip, audio = task
            encode_still(frame, clip, audio=audio)
            print(json.dumps({"rendered": clip.name}), flush=True)
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(render, tasks))
        if mode == "labeled":
            for meta in order:
                shutil.copy2(ROOT / "video/segments" / f"labeled-{meta['slug']}.mp4", ROOT / "video/models" / f"{meta['slug']}.mp4")
        outro_mp4 = ROOT / "video/segments" / f"outro-{mode}.mp4"
        encode_still(outro, outro_mp4, duration=10)
        parts.append(outro_mp4)
        concat = ROOT / "metadata" / f"concat-{mode}.txt"
        concat.write_text("".join(f"file '{p}'\n" for p in parts))
        output = ROOT / "video" / f"japanese-tts-{mode}.mp4"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(concat), "-c", "copy", "-movflags", "+faststart", str(output)])
        add_chapters(output, chapters, mode)
        manifests[mode] = {"video": str(output.relative_to(ROOT)), "exchange_rate": FX, "chapters": chapters, "probe": probe(output)}
    (ROOT / "metadata/video-manifest.json").write_text(json.dumps(manifests, ensure_ascii=False, indent=2))
    if not (ROOT / "evaluation-sheet.csv").exists():
        with (ROOT / "evaluation-sheet.csv").open("x", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample", "自然さ_1から5", "読み間違い", "アクセントの違和感", "間の違和感", "声の好み_1から5", "メモ"])
            for sample in blind_map:
                writer.writerow([sample["sample"], "", "", "", "", "", ""])
    print("Comparison videos created", flush=True)


if __name__ == "__main__":
    main()
