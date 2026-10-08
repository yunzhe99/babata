"""Bounded, in-memory photo input for Rokid and the private model endpoint.

There is no URL fetch, filesystem lookup, image hosting or image logging here.
Only the gateway accepts original JPEG/PNG files. The private model endpoint
accepts the small metadata-free JPEG that this module produces.
"""

import base64
import binascii
from io import BytesIO

from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError

MAX_SOURCE_BYTES = 6 * 1024 * 1024
MAX_SOURCE_BASE64 = 4 * ((MAX_SOURCE_BYTES + 2) // 3)
MAX_PHOTO_BODY_BYTES = MAX_SOURCE_BASE64 + 128 * 1024
MAX_JPEG_PIXELS = 24_000_000
MAX_PNG_PIXELS = 12_000_000
# Pillow resizes RGBA/LA through a full premultiplied copy. Bound that input
# separately to keep a single photo below the gateway's 256 MiB memory limit.
MAX_ALPHA_PIXELS = 6_000_000
MAX_SOURCE_EDGE = 12000
MAX_OUTPUT_EDGE = 2048
MAX_OUTPUT_BYTES = 2 * 1024 * 1024
JPEG_DATA_PREFIX = "data:image/jpeg;base64,"
MAX_IMAGE_DATA_URL = len(JPEG_DATA_PREFIX) + 4 * ((MAX_OUTPUT_BYTES + 2) // 3)
PHOTO_ERROR = (
    "A valid JPEG or PNG photo is required; limits: 6 MiB, JPEG 24 MP, "
    "PNG 12 MP, alpha PNG 6 MP, edge 12000 pixels."
)
NORMALIZED_ERROR = "A normalized JPEG data URL within the image limits is required."
# Photos do not need large text metadata. Pillow's default permits 64 MiB of
# decompressed PNG text, independent of the encoded-file and pixel limits.
PngImagePlugin.MAX_TEXT_CHUNK = 256 * 1024
PngImagePlugin.MAX_TEXT_MEMORY = 1024 * 1024


class PhotoValidationError(ValueError):
    pass


def _decode_base64(value: str, limit: int, error: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4 * ((limit + 2) // 3):
        raise PhotoValidationError(error)
    try:
        raw = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error):
        raise PhotoValidationError(error) from None
    if not raw or len(raw) > limit or base64.b64encode(raw).decode("ascii") != value:
        raise PhotoValidationError(error)
    return raw


def decode_normalized_jpeg(value: str) -> bytes:
    """Cheap schema check; actual decoding is run in a worker thread later."""
    if not isinstance(value, str) or not value.startswith(JPEG_DATA_PREFIX):
        raise PhotoValidationError(NORMALIZED_ERROR)
    raw = _decode_base64(value[len(JPEG_DATA_PREFIX) :], MAX_OUTPUT_BYTES, NORMALIZED_ERROR)
    if not raw.startswith(b"\xff\xd8\xff") or not raw.endswith(b"\xff\xd9"):
        raise PhotoValidationError(NORMALIZED_ERROR)
    return raw


def validate_normalized_jpeg(value: str) -> None:
    """Reject external URLs, malformed images, oversized images and metadata."""
    raw = decode_normalized_jpeg(value)
    try:
        with Image.open(BytesIO(raw), formats=["JPEG"]) as source:
            if (
                source.format != "JPEG"
                or source.mode != "RGB"
                or max(source.size) > MAX_OUTPUT_EDGE
                or min(source.size) < 1
                or source.getexif()
                or source.info.get("icc_profile")
                or source.info.get("comment")
                or source.info.get("xmp")
                or any(marker != "APP0" for marker, _ in source.applist)
            ):
                raise PhotoValidationError(NORMALIZED_ERROR)
            source.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise PhotoValidationError(NORMALIZED_ERROR) from None


def normalize_photo(data_base64: str, mime_type: str) -> str:
    """Validate real image data, orient, downscale, strip metadata and re-encode."""
    expected = {"image/jpeg": "JPEG", "image/png": "PNG"}.get(mime_type)
    if expected is None:
        raise PhotoValidationError(PHOTO_ERROR)
    raw = _decode_base64(data_base64, MAX_SOURCE_BYTES, PHOTO_ERROR)
    try:
        # Verify the complete original container before any lossy downsampling.
        with Image.open(BytesIO(raw), formats=["JPEG", "PNG"]) as source:
            pixels = source.width * source.height
            has_alpha = "A" in source.getbands() or "transparency" in source.info
            pixel_limit = (
                MAX_JPEG_PIXELS
                if source.format == "JPEG"
                else MAX_ALPHA_PIXELS
                if has_alpha
                else MAX_PNG_PIXELS
            )
            if (
                source.format != expected
                or min(source.size) < 1
                or max(source.size) > MAX_SOURCE_EDGE
                or pixels > pixel_limit
                or getattr(source, "n_frames", 1) != 1
            ):
                raise PhotoValidationError(PHOTO_ERROR)
            source.verify()
        with Image.open(BytesIO(raw), formats=[expected]) as source:
            if expected == "JPEG":
                source.draft("RGB", (MAX_OUTPUT_EDGE, MAX_OUTPUT_EDGE))
            # Resize before transpose/conversion so we never clone a 24 MP buffer.
            source.thumbnail(
                (MAX_OUTPUT_EDGE, MAX_OUTPUT_EDGE),
                Image.Resampling.LANCZOS,
                reducing_gap=1.0,
            )
            ImageOps.exif_transpose(source, in_place=True)
            with Image.new("RGB", source.size, "white") as clean:
                if "A" in source.getbands() or "transparency" in source.info:
                    with source.convert("RGBA") as rgba:
                        clean.paste(rgba, mask=rgba.getchannel("A"))
                else:
                    with source.convert("RGB") as rgb:
                        clean.paste(rgb)
                for quality in (85, 75, 65, 55):
                    output = BytesIO()
                    clean.save(output, format="JPEG", quality=quality)
                    encoded = output.getvalue()
                    if len(encoded) <= MAX_OUTPUT_BYTES:
                        break
        if len(encoded) > MAX_OUTPUT_BYTES:
            raise PhotoValidationError(PHOTO_ERROR)
        return JPEG_DATA_PREFIX + base64.b64encode(encoded).decode("ascii")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError):
        raise PhotoValidationError(PHOTO_ERROR) from None


def model_user_content(body):
    """Preserve text-only history shape; photos use the Responses content shape."""
    image = getattr(body, "image_data_url", None)
    if image is None:
        return body.message
    return [
        {"type": "input_text", "text": body.message},
        {"type": "input_image", "image_url": image, "detail": "auto"},
    ]
