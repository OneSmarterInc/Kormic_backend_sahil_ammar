"""Bounded CSV intake and portable private source-file storage."""
import codecs
import csv
import uuid
from pathlib import Path
from django.conf import settings
from rest_framework.exceptions import APIException, ValidationError


class RosterTooLarge(APIException):
    status_code = 413
    default_detail = "Roster exceeds the upload size or row limit."
    default_code = "ROSTER_TOO_LARGE"


def csv_rows(upload):
    upload.seek(0)
    return csv.DictReader(codecs.iterdecode(upload, 'utf-8-sig'), strict=True)


def validate_csv(upload, required):
    maximum = settings.INSTITUTE_ROSTER_MAX_BYTES
    if not 0 < upload.size <= maximum:
        raise RosterTooLarge()
    name = Path(upload.name or '').name
    if Path(name).suffix.lower() != '.csv':
        raise ValidationError('Upload a .csv file.')
    if (upload.content_type or '').split(';')[0].lower() not in {'text/csv', 'application/csv', 'application/vnd.ms-excel', 'text/plain', 'application/octet-stream'}:
        raise ValidationError('Unsupported CSV content type.')
    total = 0
    for chunk in upload.chunks():
        total += len(chunk)
        if total > maximum:
            raise RosterTooLarge()
        if b'\x00' in chunk:
            raise ValidationError('CSV must contain UTF-8 text, not binary data.')
    try:
        reader = csv_rows(upload)
        header = reader.fieldnames or []
        if len(header) != len(set(header)) or not set(required).issubset(header):
            raise ValidationError('CSV headers must be unique and include: ' + ', '.join(required))
        for count, row in enumerate(reader, 1):
            if count > settings.INSTITUTE_ROSTER_MAX_ROWS:
                raise RosterTooLarge()
            if None in row:
                raise ValidationError('CSV row has more values than the header.')
    except (UnicodeError, csv.Error):
        raise ValidationError('Could not parse UTF-8 CSV.')
    finally:
        upload.seek(0)


def source_path(value):
    # Accept legacy absolute paths only when contained in the current media root.
    root = Path(settings.MEDIA_ROOT).resolve()
    path = Path(value)
    path = (path if path.is_absolute() else root / path).resolve()
    if path == root or not path.is_relative_to(root):
        raise ValueError('UNSAFE_STORED_FILE_PATH')
    return path


def store_source(institute_id, upload):
    relative = Path('institute_lists') / str(institute_id) / (uuid.uuid4().hex + '.csv')
    path = source_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        upload.seek(0)
        with path.open('xb') as destination:
            for chunk in upload.chunks():
                total += len(chunk)
                if total > settings.INSTITUTE_ROSTER_MAX_BYTES:
                    raise RosterTooLarge()
                destination.write(chunk)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        upload.seek(0)
    return {'source_file_path': relative.as_posix(), 'source_file_name': Path(upload.name).name,
            'source_file_content_type': 'text/csv', 'source_file_size': total}
