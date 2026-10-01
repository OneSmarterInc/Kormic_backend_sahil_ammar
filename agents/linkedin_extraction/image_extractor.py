from pathlib import Path
from PIL import Image, ImageOps
from django.conf import settings
import pytesseract
import shutil
from functools import lru_cache

@lru_cache(maxsize=1)
def _local_ocr():
    from rapidocr_onnxruntime import RapidOCR
    return RapidOCR()

def _read(image, config, timeout):
    if shutil.which(pytesseract.pytesseract.tesseract_cmd):
        return pytesseract.image_to_string(image, config=config, timeout=timeout)
    import numpy as np
    results, _ = _local_ocr()(np.asarray(image.convert("RGB")))
    return "\n".join(row[1] for row in results or [])



def _configure_tesseract():
    if getattr(settings, 'TESSERACT_CMD', ''):
        pytesseract.pytesseract.tesseract_cmd = getattr(settings, 'TESSERACT_CMD', '')


def extract_text_from_image(image_path: str) -> str:
    """Replace this function if you want to use another OCR/image extraction library."""
    _configure_tesseract()
    path = Path(image_path)
    with Image.open(path) as img:
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        # Full-screen screenshots have several text columns. PSM 6 forced
        # unrelated sidebar/card text onto the same line as profile fields.
        scale = min(2.0, 1600 / max(img.size))
        if scale > 1:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
        text = _read(img, config="--psm 3", timeout=60)
        if len((text or "").strip()) < 30:
            fallback = _read(ImageOps.autocontrast(img.convert("L")), config="--psm 11", timeout=60)
            if len((fallback or "").strip()) > len((text or "").strip()):
                text = fallback
    return (text or "").strip()


def reread_image_region(image_path: str, region: str, mode: str) -> str:
    """Read-only agent tool. Region/mode are enums, never shell/OCR arguments."""
    if region not in {"full", "top", "bottom", "center"} or mode not in {"sparse", "block"}:
        raise ValueError("Invalid OCR tool arguments")
    _configure_tesseract()
    with Image.open(image_path) as original:
        img = ImageOps.exif_transpose(original).convert("RGB")
        width, height = img.size
        boxes = {"full": (0, 0, width, height), "top": (0, 0, width, int(height * .6)),
                 "bottom": (0, int(height * .4), width, height),
                 "center": (int(width * .15), int(height * .15), int(width * .85), int(height * .85))}
        img = img.crop(boxes[region])
        scale = min(2, 2200 / max(img.size))
        if scale > 1:
            img = img.resize((int(img.width * scale), int(img.height * scale)), Image.Resampling.LANCZOS)
        img = ImageOps.autocontrast(img.convert("L"))
        return _read(img, config="--psm 11" if mode == "sparse" else "--psm 6", timeout=60).strip()
