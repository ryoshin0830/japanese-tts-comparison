"""Read an OpenRouter API key from the caller's environment."""
import os


def openrouter_key():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError("Set OPENROUTER_API_KEY before calling the API")
    return key
