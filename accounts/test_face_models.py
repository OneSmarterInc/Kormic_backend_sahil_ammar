import io
import tempfile
import zipfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from accounts.face_models import install_models


class ModelArchiveTests(TestCase):
    def archive(self, entries):
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as bundle:
            for name, content in entries:
                bundle.writestr(name, content)
        stream.seek(0)
        return stream

    def test_rejects_missing_models(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'missing'):
                install_models(self.archive([]), directory)

    def test_rejects_bad_checksum_before_replacing_existing_model(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'a.onnx'
            target.write_bytes(b'original')
            with patch('accounts.face_models.MODEL_HASHES', {'a.onnx': 'incorrect'}):
                with self.assertRaisesRegex(ValueError, 'checksum'):
                    install_models(self.archive([('a.onnx', b'bad')]), directory)
            self.assertEqual(target.read_bytes(), b'original')

    def test_archive_paths_cannot_escape_destination(self):
        import hashlib
        payload = b'model'
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / 'models'
            with patch('accounts.face_models.MODEL_HASHES', {'a.onnx': hashlib.sha256(payload).hexdigest()}):
                install_models(self.archive([('../../a.onnx', payload), ('../unsafe.py', b'code')]), destination)
            self.assertEqual((destination / 'a.onnx').read_bytes(), payload)
            self.assertFalse((Path(directory) / 'a.onnx').exists())
            self.assertFalse((Path(directory) / 'unsafe.py').exists())
