"""Offline Linux smoke of the unchanged production CLIP encoder, synthetic images only.

Download public weights separately. Run with HF_HUB_OFFLINE=1 and network disabled.
This measures execution/serialization, never apartment-recognition accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sys
import time
import warnings
from io import BytesIO
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.matching.clip_embed import (
    MODEL_NAME,
    MODEL_PRETRAINED,
    compute_embedding,
    compute_embeddings_batch,
    pack_embeddings,
    unpack_embeddings,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("HF_HUB_OFFLINE") != "1":
        raise SystemExit("Set HF_HUB_OFFLINE=1; weights must already be cached")
    import open_clip
    import torch
    import torchvision

    torch.set_num_threads(2)
    images = []
    for color in ("beige", "lightblue"):
        canvas = Image.new("RGB", (320, 240), color)
        draw = ImageDraw.Draw(canvas)
        draw.rectangle((25, 30, 120, 150), fill="white", outline="navy", width=4)
        draw.rectangle((155, 120, 280, 200), fill="brown")
        stream = BytesIO()
        canvas.save(stream, format="JPEG")
        images.append(stream.getvalue())
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        started = time.perf_counter()
        first = compute_embedding(images[0])
        cold = time.perf_counter() - started
        started = time.perf_counter()
        vectors = compute_embeddings_batch([images[0], images[0], images[1]], batch_size=2)
        batch = time.perf_counter() - started
    for vector in [first, *vectors]:
        assert vector.shape == (512,) and vector.dtype == np.float32
        assert np.isfinite(vector).all()
        assert abs(float(np.linalg.norm(vector)) - 1) < 1e-5
    assert np.allclose(first, vectors[0], atol=1e-5)
    assert np.allclose(vectors[0], vectors[1], atol=1e-6)
    blob = pack_embeddings(vectors)
    restored = unpack_embeddings(blob)
    assert len(blob) == 3 * 512 * 2 and len(restored) == 3
    assert all(np.allclose(a, b, atol=0.0002) for a, b in zip(vectors, restored, strict=True))
    receipt = {
        "result": "synthetic_execution_passed",
        "quality_evaluation": False,
        "actual_api_calls": 0,
        "system": platform.system(),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "torchvision": torchvision.__version__,
        "open_clip": open_clip.__version__,
        "model": MODEL_NAME,
        "pretrained": MODEL_PRETRAINED,
        "encoder_changed": False,
        "synthetic_photo_sha256": [hashlib.sha256(value).hexdigest() for value in images],
        "cold_seconds": round(cold, 3),
        "batch_three_seconds": round(batch, 3),
        "same_image_cosine": float(np.dot(vectors[0], vectors[1])),
        "warnings": sorted({str(warning.message) for warning in caught}),
        "checks": ["finite", "shape_512", "normalized", "batch_parity", "float16_roundtrip"],
    }
    args.receipt.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    main()
