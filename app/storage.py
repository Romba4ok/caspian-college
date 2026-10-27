"""Validated uploads stored outside the publicly served directory."""

from io import BytesIO
import os
from pathlib import Path, PurePosixPath
import re
from uuid import uuid4
import warnings
from xml.etree import ElementTree
from zipfile import ZipFile

from flask import current_app
from PIL import Image
from pypdf import PdfReader
from werkzeug.exceptions import RequestEntityTooLarge


MAX_FILE_BYTES = 20 * 1024 * 1024
MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".jpg": "image/jpeg",
    ".png": "image/png",
}
OFFICE_PARTS = {
    ".docx": ("word/document.xml", "wordprocessingml.document.main+xml", "document", "wordprocessingml"),
    ".xlsx": ("xl/workbook.xml", "spreadsheetml.sheet.main+xml", "workbook", "spreadsheetml"),
    ".pptx": ("ppt/presentation.xml", "presentationml.presentation.main+xml", "presentation", "presentationml"),
}


class UploadError(ValueError):
    pass


def storage_root():
    configured = current_app.config.get("TASK_UPLOAD_DIR")
    root = Path(configured or Path(current_app.instance_path) / "task-uploads").resolve()
    static = Path(current_app.static_folder).resolve()
    if root == static or static in root.parents:
        raise RuntimeError("Хранилище вложений должно находиться вне static")
    return root


def attachment_path(storage_key):
    if not re.fullmatch(r"[0-9a-f]{32}", storage_key or ""):
        raise UploadError("Некорректное имя файла в хранилище")
    root = storage_root()
    path = root / storage_key
    if path.is_symlink() or path.resolve().parent != root:
        raise UploadError("Некорректный путь файла в хранилище")
    return path


def _xml(data):
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise UploadError("Недопустимая структура документа")
    return ElementTree.fromstring(data)


def _validate_office(data, extension):
    main_part, content_suffix, root_tag, namespace = OFFICE_PARTS[extension]
    with ZipFile(BytesIO(data)) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise UploadError("Документ содержит повторяющиеся части")
        for entry in archive.infolist():
            if entry.flag_bits & 1 or ".." in PurePosixPath(entry.filename).parts or entry.filename.startswith(("/", "\\")):
                raise UploadError("Недопустимая структура документа")
            if "vbaproject" in entry.filename.lower():
                raise UploadError("Файлы с макросами не поддерживаются")

        def read_part(name, limit):
            entry = archive.getinfo(name)
            if entry.file_size > limit:
                raise UploadError("Недопустимый размер внутренней части документа")
            return archive.read(entry)

        types = _xml(read_part("[Content_Types].xml", 1024 * 1024))
        if types.tag != "{http://schemas.openxmlformats.org/package/2006/content-types}Types":
            raise UploadError("Некорректный тип документа Office")
        overrides = {entry.attrib.get("PartName"): entry.attrib.get("ContentType", "") for entry in types}
        expected = "application/vnd.openxmlformats-officedocument." + content_suffix
        if overrides.get("/" + main_part) != expected:
            raise UploadError("Содержимое Office не соответствует расширению")
        if any("macroenabled" in entry.attrib.get("ContentType", "").lower() for entry in types):
            raise UploadError("Файлы с макросами не поддерживаются")
        relationships = _xml(read_part("_rels/.rels", 1024 * 1024))
        if not any(
            entry.attrib.get("Type", "").endswith("/officeDocument")
            and entry.attrib.get("Target", "").lstrip("/") == main_part
            and entry.attrib.get("TargetMode", "Internal") != "External"
            for entry in relationships
        ):
            raise UploadError("Отсутствует основная часть документа Office")
        main = _xml(read_part(main_part, MAX_FILE_BYTES))
        namespaces = {
            f"http://schemas.openxmlformats.org/{namespace}/2006/main",
            f"http://purl.oclc.org/ooxml/{namespace}/main",
        }
        if main.tag not in {f"{{{ns}}}{root_tag}" for ns in namespaces}:
            raise UploadError("Содержимое документа не соответствует расширению")


def validate_content(data, extension):
    try:
        if extension in (".png", ".jpg"):
            expected = "PNG" if extension == ".png" else "JPEG"
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as image:
                    if image.format != expected:
                        raise UploadError("Изображение не соответствует расширению")
                    image.verify()
                with Image.open(BytesIO(data)) as image:
                    image.load()
        elif extension == ".pdf":
            if not data.startswith(b"%PDF-"):
                raise UploadError("Содержимое не является PDF")
            reader = PdfReader(BytesIO(data), strict=True)
            if "/Root" not in reader.trailer:
                raise UploadError("Некорректная структура PDF")
            catalog = reader.trailer["/Root"]
            if catalog.get("/Type") != "/Catalog" or "/Pages" not in catalog:
                raise UploadError("Отсутствует каталог страниц PDF")
        else:
            _validate_office(data, extension)
    except UploadError:
        raise
    except Exception as error:
        raise UploadError("Файл повреждён или его содержимое не соответствует расширению") from error


def save_upload(upload):
    """Compatibility single-file API uses the same current content/policy checks."""
    with UploadBatch() as batch:
        batch.prepare(upload)
        if not batch.prepared:
            raise UploadError("Выберите файл")
        metadata = batch.prepared[0].install()
        batch.keep()
        return metadata


def discard_upload(storage_key):
    attachment_path(storage_key).unlink(missing_ok=True)


class UploadBatch:
    """Request-owned private staging; only its new files are removed on failure."""

    def __init__(self):
        self.prepared = []
        self.installed = []
        self.directory = None
        self.committed = False

    def __enter__(self):
        return self

    def prepare(self, upload):
        if upload is None or not upload.filename:
            return
        filename, extension = _upload_name(upload.filename)
        policy = _current_policy()
        if extension.lstrip(".") not in policy.allowed_extensions:
            raise UploadError("Этот формат вложений отключён в настройках")
        data = upload.stream.read(policy.max_file_bytes + 1)
        if not data:
            raise UploadError("Пустые файлы не поддерживаются")
        if len(data) > policy.max_file_bytes:
            raise RequestEntityTooLarge("Файл превышает действующий лимит размера")
        validate_content(data, extension)
        root = storage_root()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        pending = root / ".pending"
        if pending.is_symlink() or pending.resolve().parent != root:
            raise UploadError("Недопустимый путь временного хранилища")
        pending.mkdir(exist_ok=True, mode=0o700)
        if self.directory is None:
            self.directory = pending / uuid4().hex
            self.directory.mkdir(mode=0o700)
        path = self.directory / uuid4().hex
        self.prepared.append(PreparedUpload(self, path, filename, extension, len(data)))
        with path.open("xb") as output:
            output.write(data)

    def keep(self):
        self.committed = True

    def __exit__(self, exc_type, exc, tb):
        if not self.committed:
            for key in self.installed:
                discard_upload(key)
        for item in self.prepared:
            item.path.unlink(missing_ok=True)
        if self.directory is not None:
            self.directory.rmdir()
            try:
                self.directory.parent.rmdir()
            except OSError:
                pass  # Another request may still own a staging subdirectory.


def _current_policy():
    from .settings import get_upload_policy, SettingsError
    try:
        return get_upload_policy()
    except SettingsError:
        raise UploadError("Загрузка недоступна: проверьте файловые настройки системы") from None


def _upload_name(value):
    filename = value.replace("\\", "/").split("/")[-1]
    filename = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127).strip()
    extension = Path(filename).suffix.lower()
    if not filename or len(filename) > 255:
        raise UploadError("Недопустимое имя файла")
    if extension not in MIME_TYPES:
        raise UploadError("Разрешены только PDF, DOCX, XLSX, PPTX, JPG и PNG")
    return filename, extension


class PreparedUpload:
    def __init__(self, batch, path, filename, extension, size):
        self.batch, self.path = batch, path
        self.filename, self.extension, self.size = filename, extension, size
        self.used = False

    def install(self):
        from shutil import copyfileobj
        if self.used:
            raise UploadError("Файл уже прикреплён")
        policy = _current_policy()
        if self.extension.lstrip(".") not in policy.allowed_extensions or self.size > policy.max_file_bytes:
            raise UploadError("Вложение не соответствует действующим ограничениям формата или размера")
        if self.path.is_symlink() or self.path.resolve().parent != self.batch.directory.resolve():
            raise UploadError("Недопустимый путь временного файла")
        key = uuid4().hex
        target = attachment_path(key)
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.batch.installed.append(key)
        with os.fdopen(descriptor, "wb") as output, self.path.open("rb") as source:
            copyfileobj(source, output)
        self.used = True
        return dict(original_filename=self.filename,storage_key=key,
            content_type=MIME_TYPES[self.extension],size_bytes=self.size)

