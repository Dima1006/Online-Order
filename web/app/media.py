"""Photos: validate, shrink and re-encode uploads; serve them back.

Safety rules: the file type is decided by decoding the image (never by the file name or the browser's
type); every upload is re-encoded to JPEG, which removes metadata (GPS in phone photos) and any
hidden payload; huge images are refused before they are decoded fully; file names are random, so a
visitor can neither guess them nor write outside the uploads folder."""
import io
import secrets
import re
import threading
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse

from . import config as cfg
from .db import DB_PATH

Image.MAX_IMAGE_PIXELS = cfg.MAX_IMAGE_PIXELS
UPLOAD_DIR = Path(DB_PATH).parent / "uploads"
_NAME = re.compile(r"^[A-Za-z0-9_-]{16,40}\.jpg$")
_SIZES = {"item": (640, 640), "venue": (640, 640)}   # both are square: shown as circles or square crops
_slots = threading.BoundedSemaphore(2)   # at most two images are processed at the same time


class ImageError(ValueError):
    """The message is safe to show to the user."""


def _process(data: bytes, kind: str) -> bytes:
    if len(data) > cfg.MAX_UPLOAD_BYTES:
        raise ImageError(f"The photo is too big (the limit is {cfg.MAX_UPLOAD_BYTES // 1_000_000} MB).")
    if not data:
        raise ImageError("Choose a photo first.")
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ("JPEG", "PNG", "WEBP"):
                raise ImageError("Use a JPG, PNG or WebP photo.")
            img = ImageOps.exif_transpose(img)
            img.load()
            if img.mode not in ("RGB", "L"):  # flatten transparency onto white
                rgba = img.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.split()[-1])
                img = bg
            else:
                img = img.convert("RGB")
            img = ImageOps.fit(img, _SIZES[kind], Image.LANCZOS)   # centre crop to a square
            out = io.BytesIO()
            img.save(out, "JPEG", quality=82, optimize=True, progressive=True)
            return out.getvalue()
    except ImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
        raise ImageError("This file is not a valid photo. Use a JPG, PNG or WebP image.")


def _save(data: bytes, kind: str) -> str:
    with _slots:
        encoded = _process(data, kind)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = secrets.token_urlsafe(18) + ".jpg"
    (UPLOAD_DIR / name).write_bytes(encoded)
    return name


async def save_image(data: bytes, kind: str) -> str:
    """Returns the new file name. Raises ImageError with a message for the user."""
    return await run_in_threadpool(_save, data, kind)


def delete_image(name: str) -> None:
    if name and _NAME.fullmatch(name):
        try:
            (UPLOAD_DIR / name).unlink()
        except OSError:
            pass


async def media(request):
    name = request.path_params["name"]
    path = UPLOAD_DIR / name
    if not _NAME.fullmatch(name) or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})
