"""Model installation and validation for the supplied Kormic liveness models.

Model weights are operator-provided data, never Python code from an archive.
"""
from pathlib import Path
import hashlib
import shutil
import tempfile
import zipfile

MODEL_HASHES = {
    'w600k_r50.onnx': '4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43',
    'det_10g.onnx': '5838f7fe053675b1c7a08b633df49e7af5495cee0493c7dcf6697200b85b5b91',
    '2d106det.onnx': 'f001b856447c413801ef5c42091ed0cd516fcd21f2d6b79635b1e733a7109dbf',
}


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def install_models(archive, destination):
    destination = Path(destination).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle, tempfile.TemporaryDirectory(dir=destination) as stage:
        members = {}
        for item in bundle.infolist():
            name = Path(item.filename).name
            if name not in MODEL_HASHES:
                continue
            if name in members:
                raise ValueError(f'Duplicate model in archive: {name}')
            if item.file_size > 250 * 1024 * 1024:
                raise ValueError(f'Model exceeds size limit: {name}')
            members[name] = item
        if set(members) != set(MODEL_HASHES):
            raise ValueError('Archive is missing required face-recognition models')
        for name, item in members.items():
            # Flatten the allowlisted names; never extract archive-provided paths.
            target = Path(stage) / name
            with bundle.open(item) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
            if digest(target) != MODEL_HASHES[name]:
                raise ValueError(f'Model checksum mismatch: {name}')
        for name in members:
            (Path(stage) / name).replace(destination / name)
    return destination


def validate_models(directory):
    import onnxruntime as ort
    results = []
    for name, expected in MODEL_HASHES.items():
        path = Path(directory) / name
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f'Missing or incompatible model: {name}')
        options = ort.SessionOptions()
        options.intra_op_num_threads = 1
        options.inter_op_num_threads = 1
        session = ort.InferenceSession(str(path), sess_options=options,
                                       providers=['CPUExecutionProvider'])
        results.append({'model': name, 'sha256': expected,
                        'inputs': [node.shape for node in session.get_inputs()],
                        'outputs': [node.shape for node in session.get_outputs()]})
        del session
    return results
