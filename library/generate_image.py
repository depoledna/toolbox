"""
Image generation using OpenRouter API.
"""
import os
import re
import time
import base64
from pathlib import Path
from dotenv import load_dotenv
import requests


def _sanitize_filename(prompt: str, max_length: int = 50) -> str:
    """Convert prompt to safe filename."""
    name = prompt.lower().replace(" ", "_")
    name = re.sub(r'[^a-z0-9_]', '', name)
    return name[:max_length].rstrip('_')


def _generate_single(
    prompt: str,
    filename: str,
    path: Path,
    model: str,
    api_key: str
) -> str:
    """Generate a single image and save it."""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}]
    }

    if "gemini" in model.lower():
        payload["modalities"] = ["image", "text"]

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        },
        json=payload
    )
    response.raise_for_status()
    data = response.json()

    images = data.get("choices", [{}])[0].get("message", {}).get("images", [])
    if not images:
        raise RuntimeError(f"No image generated for prompt: {prompt[:50]}...")

    image_url = images[0]["image_url"]["url"]
    base64_data = image_url.split(",", 1)[1]
    image_bytes = base64.b64decode(base64_data)

    output_path = path / filename
    output_path.write_bytes(image_bytes)

    return str(output_path)


def generate_image(
    prompt: str | list[str],
    filename: str | list[str] | None = None,
    path: str | Path | None = None,
    model: str = "google/gemini-2.5-flash-image",
    rate_limit: float = 3.0,
) -> str | list[str]:
    """Generate image(s) from text prompt(s) via OpenRouter API.

    generate_image("A cat")                              → ./a_cat.png
    generate_image("A cat", path="/tmp")                 → /tmp/a_cat.png
    generate_image(["A cat", "A dog"], path="./out")     → ["./out/a_cat.png", ...]
    generate_image(["A cat"], ["c.png"], path="./out")   → ["./out/c.png"]
    """
    load_dotenv(Path(__file__).parent.parent / ".env")

    api_key = os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_KEY not found in environment")

    # Resolve output path
    if path is None:
        output_dir = Path.cwd()
    else:
        output_dir = Path(path)
        output_dir.mkdir(parents=True, exist_ok=True)

    # Bulk generation
    if isinstance(prompt, list):
        if isinstance(filename, list) and len(filename) != len(prompt):
            raise ValueError(f"filename list ({len(filename)}) must match prompt list ({len(prompt)})")
        results = []
        for i, p in enumerate(prompt):
            if isinstance(filename, list):
                fn = filename[i]
            else:
                fn = f"{_sanitize_filename(p)}.png"
            result = _generate_single(p, fn, output_dir, model, api_key)
            results.append(result)
            if i < len(prompt) - 1 and rate_limit > 0:
                time.sleep(rate_limit)
        return results

    # Single generation
    if filename is None:
        filename = f"{_sanitize_filename(prompt)}.png"

    return _generate_single(prompt, filename, output_dir, model, api_key)
