"""Restore an optional private demonstration bundle; never read it from Git."""

import base64
from io import BytesIO
import os
from pathlib import Path
import re
import stat
from zipfile import ZipFile


def restore_demo_uploads(bundle: Path, upload_dir: Path | None) -> int:
    if not bundle.exists():
        return 0
    if upload_dir is None:
        raise ValueError("TASK_UPLOAD_DIR is required for the private demo bundle")
    encoded = bundle.read_bytes().strip()
    if len(encoded) > 1024 * 1024:
        raise ValueError("Demo bundle exceeds the supported size")
    archive_bytes = base64.b64decode(encoded, validate=True)
    with ZipFile(BytesIO(archive_bytes)) as archive:
        entries = archive.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 1000 or len(set(names)) != len(names):
            raise ValueError("Invalid demo bundle entry count")
        if sum(entry.file_size for entry in entries) > 64 * 1024 * 1024:
            raise ValueError("Expanded demo bundle exceeds the supported size")
        for entry in entries:
            if (not re.fullmatch(r"[0-9a-f]{32}", entry.filename)
                    or entry.file_size > 20 * 1024 * 1024
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Invalid demo attachment entry")
        # Validate the complete archive, including CRCs, before writing any file.
        payload = [(entry.filename, archive.read(entry)) for entry in entries]

    root = upload_dir.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for name, data in payload:
        target = root / name
        if target.is_symlink():
            raise ValueError("Demo attachment target must not be a symlink")
        if target.exists() and (not target.is_file() or target.read_bytes() != data):
            raise ValueError("Existing attachment differs from the demo bundle")
    for name, data in payload:
        target = root / name
        if not target.exists():
            with target.open("xb") as output:
                output.write(data)
    return len(payload)


if __name__ == "__main__":
    configured_root = os.environ.get("TASK_UPLOAD_DIR")
    count = restore_demo_uploads(
        Path("/etc/secrets/demo-uploads.b64"),
        Path(configured_root) if configured_root else None,
    )
    if count:
        print(f"Private demo attachments available: {count}", flush=True)
