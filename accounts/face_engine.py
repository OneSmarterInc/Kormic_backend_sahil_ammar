"""CPU SCRFD + ArcFace inference using the supplied, checksum-pinned weights.

RGB images only; raw captures are processed in memory and never saved here.
"""
import base64
import io
import threading
from functools import lru_cache

import numpy as np
from PIL import Image, ImageOps
from django.conf import settings

from accounts.face_models import MODEL_HASHES, digest


class FaceScanError(ValueError):
    pass


@lru_cache(maxsize=2)
def session(name):
    from pathlib import Path
    import onnxruntime as ort
    path = Path(settings.FACE_MODEL_DIRECTORY) / name
    if not path.is_file() or digest(path) != MODEL_HASHES[name]:
        raise RuntimeError('Face model is missing or incompatible')
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=options, providers=['CPUExecutionProvider'])


def infer(model, value):
    try:
        return model.run(None, {model.get_inputs()[0].name: value})
    except Exception as exc:
        # ONNX-specific errors must never return tensors/captures in an API response.
        raise RuntimeError('Face inference unavailable') from exc


def decode(encoded):
    if not isinstance(encoded, str) or len(encoded) > 1400000:
        raise FaceScanError('Capture is too large. Please retry.')
    try:
        data = base64.b64decode(encoded, validate=True)
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > 2000000 or min(image.size) < 112:
                raise FaceScanError('Use a camera image between 112 pixels and 2 megapixels.')
            return ImageOps.exif_transpose(image).convert('RGB')
    except (ValueError, OSError) as exc:
        raise FaceScanError('The captured image could not be read.') from exc


def detect(image):
    scale = min(640 / image.width, 640 / image.height)
    resized = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.BILINEAR)
    canvas = np.zeros((640, 640, 3), dtype=np.float32)
    canvas[:resized.height, :resized.width] = np.asarray(resized)
    model = session('det_10g.onnx')
    outputs = infer(model, ((canvas - 127.5) / 128).transpose(2, 0, 1)[None])
    boxes, scores, points = [], [], []
    for i, stride in enumerate((8, 16, 32)):
        score = outputs[i].reshape(-1)
        selected = np.where(score >= 0.6)[0]
        y, x = np.mgrid[:640 // stride, :640 // stride]
        centers = np.repeat(np.stack((x, y), axis=-1).reshape(-1, 2) * stride, 2, axis=0)
        distances = outputs[i + 3].reshape(-1, 4)[selected] * stride
        centers = centers[selected]
        boxes.extend(np.column_stack((centers - distances[:, :2], centers + distances[:, 2:])))
        scores.extend(score[selected])
        points.extend(outputs[i + 6].reshape(-1, 5, 2)[selected] * stride + centers[:, None, :])
    if not boxes:
        raise FaceScanError('No clear face found. Face the camera in good lighting.')
    boxes, scores, points = np.array(boxes), np.array(scores), np.array(points)
    order = scores.argsort()[::-1]
    keep = []
    while len(order):
        current, rest = order[0], order[1:]
        keep.append(current)
        overlap = np.maximum(0, np.minimum(boxes[current, 2:], boxes[rest, 2:]) - np.maximum(boxes[current, :2], boxes[rest, :2]) + 1)
        intersection = overlap.prod(axis=1)
        area = (boxes[:, 2:] - boxes[:, :2] + 1).prod(axis=1)
        order = rest[intersection / (area[current] + area[rest] - intersection) < 0.4]
    if len(keep) != 1:
        raise FaceScanError('Only one person may appear in the camera.')
    bbox = boxes[keep[0]] / scale
    if min(bbox[2:] - bbox[:2]) < 80:
        raise FaceScanError('Move closer to the camera.')
    return points[keep[0]] / scale


def embedding(image, points):
    # ArcFace's canonical five-point alignment; solve a similarity transform.
    target = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                       [41.5493, 92.3655], [70.7299, 92.2041]])
    matrix, rhs = [], []
    for (x, y), (u, v) in zip(points, target):
        matrix.extend(([x, -y, 1, 0], [y, x, 0, 1]))
        rhs.extend((u, v))
    a, b, tx, ty = np.linalg.lstsq(matrix, rhs, rcond=None)[0]
    inverse = np.linalg.inv([[a, -b, tx], [b, a, ty], [0, 0, 1]])
    aligned = image.transform((112, 112), Image.Transform.AFFINE,
                              tuple(inverse[:2].reshape(-1)), Image.Resampling.BILINEAR)
    pixels = np.asarray(aligned, dtype=np.float32)
    if pixels.std() < 12 or pixels.mean() < 25 or pixels.mean() > 235:
        raise FaceScanError('Improve the lighting and try again.')
    model = session('w600k_r50.onnx')
    result = infer(model, ((pixels - 127.5) / 127.5).transpose(2, 0, 1)[None])[0].reshape(-1)
    norm = np.linalg.norm(result)
    if result.size != 512 or not np.isfinite(result).all() or norm == 0:
        raise RuntimeError('Invalid face-model output')
    return (result / norm).tolist()


_inference_lock = threading.Lock()


def measure(encoded):
    with _inference_lock:
        image = decode(encoded)
        points = detect(image)
        eye_distance = float(np.linalg.norm(points[1] - points[0]))
        if eye_distance < 15:
            raise FaceScanError('Move closer to the camera.')
        yaw = float((points[2, 0] - (points[0, 0] + points[1, 0]) / 2) / eye_distance)
        return yaw, embedding(image, points)


def similarity(first, second):
    a, b = np.asarray(first), np.asarray(second)
    if a.shape != (512,) or b.shape != (512,) or not np.isfinite(a).all() or not np.isfinite(b).all():
        raise FaceScanError('Face credentials could not be read. Contact support.')
    return float(np.dot(a, b) / max(np.linalg.norm(a) * np.linalg.norm(b), 1e-10))
