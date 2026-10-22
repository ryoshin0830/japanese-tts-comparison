"""Independent ASR check; transcripts are not pronunciation or naturalness scores."""
import base64
import concurrent.futures
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

from auth import openrouter_key
from generate import clean_error

ROOT = Path(__file__).resolve().parents[1]


def transcribe(meta, key):
    out = ROOT / "metadata/transcripts" / f"{meta['slug']}.json"
    out.parent.mkdir(exist_ok=True)
    if out.exists():
        return json.loads(out.read_text())
    path = ROOT / meta["audio_file"]
    payload = {
        "model": "openai/whisper-large-v3-turbo",
        "input_audio": {"data": base64.b64encode(path.read_bytes()).decode(), "format": path.suffix[1:]},
        "language": "ja", "response_format": "json",
    }
    request = urllib.request.Request(
        "https://openrouter.ai/api/v1/audio/transcriptions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            result = json.load(response)
        saved = {"source_model": meta["model"], "asr_model": payload["model"], "elapsed_seconds": round(time.monotonic()-started, 3), "response": result}
    except urllib.error.HTTPError as error:
        saved = {"source_model": meta["model"], "status": "failed", "http_status": error.code, "error": clean_error(error.read(3000).decode(errors="replace"), key)}
    except Exception as error:
        saved = {"source_model": meta["model"], "status": "failed", "error": clean_error(str(error), key)}
    out.write_text(json.dumps(saved, ensure_ascii=False, indent=2))
    print(json.dumps({"slug": meta["slug"], "transcript": saved.get("response", {}).get("text"), "status": saved.get("status", "success"), "usage": saved.get("response", {}).get("usage")}, ensure_ascii=False), flush=True)
    return saved


def main():
    key = openrouter_key()
    configs = json.loads((ROOT / "metadata/model-configs.json").read_text())
    metas = [json.loads((ROOT / "metadata" / f"{cfg['slug']}.json").read_text()) for cfg in configs]
    metas = [m for m in metas if m["status"] == "success"]
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda m: transcribe(m, key), metas))


if __name__ == "__main__":
    main()
