"""
App icon generation using OpenRouter API with Nano Banana Pro (Gemini 3 Pro Image).
"""
import os
import base64
from pathlib import Path

from dotenv import load_dotenv
import requests

from .generate_image import _sanitize_filename


STYLE_PRESETS = {
    "modern": "Clean, modern illustration with soft shadows, subtle gradients, and rounded shapes.",
    "3d-clay": "3D clay render with soft, touchable surfaces, matte material, and gentle ambient lighting.",
    "flat": "Flat design illustration with bold solid colors, clean geometric shapes, and no shadows.",
    "gradient": "Vibrant gradient illustration with smooth color transitions, glossy highlights, and depth.",
    "minimal": "Minimalist illustration with a single focal element, generous whitespace, and muted tones.",
    "playful": "Playful, colorful illustration with rounded bubbly shapes, warm palette, and friendly feel.",
}

MODEL = "google/gemini-3-pro-image-preview"


def _build_prompt(concept: str, style: str, background: str) -> str:
    style_desc = STYLE_PRESETS.get(style, STYLE_PRESETS["modern"])
    bg = background if background else "a smooth gradient"
    return (
        f"A centered illustration of {concept}. "
        f"{style_desc} "
        f"The background is {bg}, continuous, filling the entire canvas edge to edge. "
        f"No text, no words, no letters, no labels. "
        f"No border, no frame, no rounded corners, no transparency. "
        f"Single centered subject, square composition."
    )


def _generate(prompt: str, filename: str, path: Path, api_key: str) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "modalities": ["image", "text"],
    }
    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
    )
    response.raise_for_status()
    data = response.json()

    images = data.get("choices", [{}])[0].get("message", {}).get("images", [])
    if not images:
        raise RuntimeError(f"No image generated for: {prompt[:80]}...")

    image_url = images[0]["image_url"]["url"]
    base64_data = image_url.split(",", 1)[1]
    image_bytes = base64.b64decode(base64_data)

    out = path / filename
    out.write_bytes(image_bytes)
    return str(out)


def generate_icon(
    concept: str,
    style: str = "modern",
    background: str = "",
    filename: str | None = None,
    path: str | Path | None = None,
) -> str:
    """Generate a 1024x1024 app icon from a concept description.

    generate_icon("calendar with moon phases")                     → ./calendar_with_moon_phases.png
    generate_icon("music notes", style="3d-clay")                  → 3D clay style
    generate_icon("running shoe", background="deep blue gradient") → custom background
    """
    load_dotenv(Path(__file__).parent.parent / ".env")

    api_key = os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_KEY not found in environment")

    if path is None:
        output_dir = Path.cwd()
    else:
        output_dir = Path(path)
        output_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        filename = f"{_sanitize_filename(concept)}.png"

    prompt = _build_prompt(concept, style, background)
    return _generate(prompt, filename, output_dir, api_key)
