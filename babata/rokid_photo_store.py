"""Append-only photo archive for the Rokid gateway.

The archive exists only when the gateway is given a store directory. Each
accepted photo keeps the bounded, metadata-free JPEG that was already sent to
the model, a JSON sidecar for that photo, and one manifest line per event, so a
second machine can mirror the archive by reading the manifest alone.

Storage is best effort. A missing directory, a full disk or a permission error
returns ``None`` and logs no image data; it never changes what the glasses hear.
No URL, image host or original upload is kept here.
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
import threading
from datetime import datetime
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, UnidentifiedImageError

from babata.rokid_photos import JPEG_DATA_PREFIX

logger = logging.getLogger(__name__)

LOCAL_ZONE = ZoneInfo("Asia/Shanghai")
MANIFEST_NAME = "manifest.jsonl"
ARCHIVE_VERSION = 1
MAX_DESCRIPTION_CHARS = 4000
STORE_DIR_MODE = 0o750
STORE_FILE_MODE = 0o640


def decode_data_url(value: str) -> bytes:
    """Return the bytes of a normalized JPEG data URL without guessing."""
    if not isinstance(value, str) or not value.startswith(JPEG_DATA_PREFIX):
        raise ValueError("A normalized JPEG data URL is required")
    try:
        return base64.b64decode(value[len(JPEG_DATA_PREFIX) :], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("A normalized JPEG data URL is required") from None


def _write_atomic(path: Path, payload: bytes) -> None:
    temporary = path.with_name("." + path.name + ".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, STORE_FILE_MODE)
    os.replace(temporary, path)


def _append_line(root: Path, entry: dict) -> None:
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    descriptor = os.open(
        root / MANIFEST_NAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, STORE_FILE_MODE
    )
    with open(descriptor, "a", encoding="utf-8") as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


def _jpeg_size(jpeg: bytes) -> tuple[int | None, int | None]:
    try:
        with Image.open(BytesIO(jpeg)) as image:
            return image.size
    except (UnidentifiedImageError, OSError, ValueError):
        return None, None


class PhotoArchive:
    """Store accepted photos under ``root``; disabled when ``root`` is empty."""

    def __init__(self, root: str | os.PathLike | None = None, *, zone=LOCAL_ZONE):
        self.root = Path(root) if root else None
        self.zone = zone
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def save(
        self,
        jpeg: bytes,
        *,
        session: str,
        user_id: str,
        kind: str,
        source_mime: str | None = None,
    ) -> dict | None:
        """Write one photo plus its metadata; return the stored record."""
        if self.root is None:
            return None
        try:
            return self._save(
                jpeg, session=session, user_id=user_id, kind=kind, source_mime=source_mime
            )
        except Exception as error:  # Storage must never break the answer.
            logger.warning("Rokid photo archive write failed: %s", type(error).__name__)
            return None

    def describe(self, record: dict | None, text: str | None) -> None:
        """Attach the model's description to an already stored photo."""
        if record is None or self.root is None or not isinstance(text, str):
            return
        description = text.strip()[:MAX_DESCRIPTION_CHARS]
        if not description:
            return
        try:
            entry = {
                "version": ARCHIVE_VERSION,
                "event": "described",
                "id": record["id"],
                "date": record["date"],
                "file": record["file"],
                "description": description,
            }
            with self._lock:
                _write_atomic(
                    self.root / record["file"].replace(".jpg", ".json"),
                    json.dumps(
                        {**record, "description": description}, ensure_ascii=False, sort_keys=True
                    ).encode("utf-8"),
                )
                _append_line(self.root, entry)
        except Exception as error:
            logger.warning("Rokid photo description write failed: %s", type(error).__name__)

    def _save(
        self,
        jpeg: bytes,
        *,
        session: str,
        user_id: str,
        kind: str,
        source_mime: str | None,
    ) -> dict:
        now = datetime.now(self.zone)
        photo_id = f"photo-{now:%Y%m%d-%H%M%S}-{secrets.token_hex(3)}"
        day = f"{now:%Y-%m-%d}"
        width, height = _jpeg_size(jpeg)
        record = {
            "version": ARCHIVE_VERSION,
            "event": "stored",
            "id": photo_id,
            "taken_at": now.isoformat(timespec="seconds"),
            "date": day,
            "session": session,
            "user_id": user_id,
            "kind": kind,
            "source_mime": source_mime,
            "bytes": len(jpeg),
            "sha256": hashlib.sha256(jpeg).hexdigest(),
            "width": width,
            "height": height,
            "file": f"{day}/{photo_id}.jpg",
            "description": None,
        }
        with self._lock:
            # Directory modes are set once here and never chmod-ed again: the gateway
            # runs without CAP_FSETID, so a later chmod would drop the setgid bit that
            # shares the archive group with the mirroring user.
            directory = self.root / day
            self.root.mkdir(parents=True, exist_ok=True, mode=STORE_DIR_MODE)
            directory.mkdir(parents=True, exist_ok=True, mode=STORE_DIR_MODE)
            _write_atomic(directory / f"{photo_id}.jpg", jpeg)
            _write_atomic(
                directory / f"{photo_id}.json",
                json.dumps(record, ensure_ascii=False, sort_keys=True).encode("utf-8"),
            )
            _append_line(
                self.root, {key: value for key, value in record.items() if key != "description"}
            )
            return record
