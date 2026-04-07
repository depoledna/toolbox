"""
Image editing using OpenRouter API.

Sends an existing image + text prompt to a multimodal model
and returns the edited image.
"""
import os
import time
import base64
import mimetypes
from pathlib import Path
from dotenv import load_dotenv
import requests


_MIME_FALLBACK = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _read_image_as_data_uri(image_path: Path) -> str:
    """Read an image file and return a base64 data URI."""
    mime_type = mimetypes.guess_type(str(image_path))[0]
    if mime_type is None:
        mime_type = _MIME_FALLBACK.get(image_path.suffix.lower(), "image/png")
    raw = image_path.read_bytes()
    encoded = base64.b64encode(raw).decode()
    return f"data:{mime_type};base64,{encoded}"


def _edit_single(
    image_path: Path,
    prompt: str,
    filename: str,
    output_dir: Path,
    model: str,
    api_key: str,
) -> str:
    """Edit a single image and save the result."""
    data_uri = _read_image_as_data_uri(image_path)

    payload = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {"type": "text", "text": prompt},
                ],
            }
        ],
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
        raise RuntimeError(f"No edited image returned for: {image_path.name}")

    image_url = images[0]["image_url"]["url"]
    base64_data = image_url.split(",", 1)[1]
    image_bytes = base64.b64decode(base64_data)

    out = output_dir / filename
    out.write_bytes(image_bytes)
    return str(out)


def edit_image(
    image: str | Path | list[str | Path],
    prompt: str | list[str],
    filename: str | list[str] | None = None,
    path: str | Path | None = None,
    model: str = "google/gemini-2.5-flash-image",
    rate_limit: float = 3.0,
) -> str | list[str]:
    """Edit image(s) with a text prompt via OpenRouter API.

    edit_image("photo.png", "Make B&W")                          → ./photo_edited.png
    edit_image("photo.png", "Remove bg", path="/tmp")            → /tmp/photo_edited.png
    edit_image(["a.png", "b.png"], "Add vignette", path="./out") → ["./out/a_edited.png", ...]
    edit_image(["a.png", "b.png"], ["Warmer", "Cooler"])         → ["./a_edited.png", ...]
    """
    load_dotenv(Path(__file__).parent.parent / ".env")

    api_key = os.getenv("OPENROUTER_KEY")
    if not api_key:
        raise ValueError("OPENROUTER_KEY not found in environment")

    # Bulk editing
    if isinstance(image, list):
        prompts = prompt if isinstance(prompt, list) else [prompt] * len(image)
        if len(prompts) != len(image):
            raise ValueError(
                f"prompt list ({len(prompts)}) must match image list ({len(image)})"
            )
        if isinstance(filename, list) and len(filename) != len(image):
            raise ValueError(
                f"filename list ({len(filename)}) must match image list ({len(image)})"
            )

        results = []
        for i, img in enumerate(image):
            img_path = Path(img)
            out_dir = Path(path) if path else img_path.parent
            out_dir.mkdir(parents=True, exist_ok=True)

            if isinstance(filename, list):
                fn = filename[i]
            else:
                fn = f"{img_path.stem}_edited.png"

            result = _edit_single(img_path, prompts[i], fn, out_dir, model, api_key)
            results.append(result)
            if i < len(image) - 1 and rate_limit > 0:
                time.sleep(rate_limit)
        return results

    # Single editing
    img_path = Path(image)
    if path is None:
        output_dir = img_path.parent
    else:
        output_dir = Path(path)
        output_dir.mkdir(parents=True, exist_ok=True)

    if filename is None:
        filename = f"{img_path.stem}_edited.png"

    return _edit_single(img_path, prompt, filename, output_dir, model, api_key)
