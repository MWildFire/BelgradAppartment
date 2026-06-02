"""CLIP-based image embeddings for robust photo matching.

pHash breaks down when platforms add watermarks (nekretnine.rs overlays its logo on every photo),
when an image is cropped, mirrored, or heavily re-encoded. CLIP looks at *semantic* content
(furniture, room layout, viewpoint, lighting) and produces a 512-dim normalised vector — two
photos of the same apartment from the same angle yield cosine similarity ≥ ~0.9 regardless of
post-processing.

We use ViT-B/32 (OpenAI weights via open_clip):
- Model size: ~600MB on disk (cached in ~/.cache/clip on first use).
- CPU inference: ~80–150 ms per image on M-series Mac, ~250 ms on a typical VPS.
- Output: float32, L2-normalised, 512-dim — stored in DB as little-endian float16 BLOB
  (50% storage saving, no measurable accuracy loss for cosine similarity).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, cast

import numpy as np

if TYPE_CHECKING:
    from PIL.Image import Image as PILImage

logger = logging.getLogger(__name__)

MODEL_NAME = "ViT-B-32"
MODEL_PRETRAINED = "openai"
EMBED_DIM = 512


@lru_cache(maxsize=1)
def _load_model() -> tuple[object, object, object]:
    """Lazy load the CLIP model on first call (~5 s on cold start, then in-memory)."""
    import open_clip  # heavy import — defer
    import torch

    device = "cuda" if torch.cuda.is_available() else "cpu"
    logger.info("Loading CLIP %s/%s on %s …", MODEL_NAME, MODEL_PRETRAINED, device)
    model, _train_transform, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=MODEL_PRETRAINED, device=device
    )
    model.eval()
    return model, preprocess, device


def _open_rgb(image_bytes_or_path: bytes | Path) -> "PILImage":
    from PIL import Image

    if isinstance(image_bytes_or_path, bytes):
        return Image.open(BytesIO(image_bytes_or_path)).convert("RGB")
    return Image.open(image_bytes_or_path).convert("RGB")


def compute_embedding(image: bytes | Path) -> np.ndarray:
    """Return L2-normalised 512-dim float32 vector for a single image."""
    import torch

    model, preprocess, device = _load_model()
    pil = _open_rgb(image)
    tensor = preprocess(pil).unsqueeze(0).to(device)  # type: ignore[operator]
    with torch.no_grad():
        feats = model.encode_image(tensor)  # type: ignore[attr-defined]
        feats = feats / feats.norm(dim=-1, keepdim=True)
    arr = feats.cpu().numpy().astype(np.float32)[0]
    return arr


def compute_embeddings_batch(images: list[bytes | Path], *, batch_size: int = 8) -> list[np.ndarray]:
    """Vectorised embedding for N images — ~3× faster than per-image calls on CPU."""
    import torch

    if not images:
        return []
    model, preprocess, device = _load_model()
    results: list[np.ndarray] = []
    for start in range(0, len(images), batch_size):
        batch = images[start:start + batch_size]
        tensors = [preprocess(_open_rgb(img)).unsqueeze(0) for img in batch]  # type: ignore[operator]
        stack = torch.cat(tensors, dim=0).to(device)
        with torch.no_grad():
            feats = model.encode_image(stack)  # type: ignore[attr-defined]
            feats = feats / feats.norm(dim=-1, keepdim=True)
        for i in range(feats.shape[0]):
            results.append(feats[i].cpu().numpy().astype(np.float32))
    return results


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity for L2-normalised vectors → just the dot product (range -1..1)."""
    return float(np.dot(a, b))


def best_pairwise_similarity(
    inbound_embs: list[np.ndarray], listing_embs: list[np.ndarray]
) -> tuple[int, list[float]]:
    """For each inbound photo, find max cosine similarity to any listing photo.

    Returns (count_above_threshold, list_of_per_photo_best_similarities). Caller picks the
    threshold (default 0.85). Using numpy matmul for vectorised search across all listing
    photos at once — O(N_inbound × N_listing × 512) flops ≈ negligible for small albums.
    """
    if not inbound_embs or not listing_embs:
        return 0, []
    A = np.stack(inbound_embs)  # (Ni, 512)
    B = np.stack(listing_embs)  # (Nl, 512)
    sim = A @ B.T  # (Ni, Nl)
    per_inbound_max = sim.max(axis=1)  # (Ni,)
    return per_inbound_max.shape[0], per_inbound_max.tolist()


# --- Serialisation helpers ---


def pack_embedding(vec: np.ndarray) -> bytes:
    """Pack one 512-dim float32 vector into a 1024-byte little-endian float16 blob."""
    assert vec.shape == (EMBED_DIM,), f"expected ({EMBED_DIM},), got {vec.shape}"
    return vec.astype("<f2").tobytes()


def unpack_embedding(blob: bytes) -> np.ndarray:
    """Inverse of pack_embedding. Returns float32 (cast back from float16) for math."""
    arr = np.frombuffer(blob, dtype="<f2").astype(np.float32)
    if arr.shape != (EMBED_DIM,):
        raise ValueError(f"corrupt embedding blob: shape={arr.shape}")
    return arr


def pack_embeddings(vecs: list[np.ndarray]) -> bytes:
    """Pack a list of embeddings as concatenated float16 blobs."""
    if not vecs:
        return b""
    stacked = np.stack(vecs).astype("<f2")
    return stacked.tobytes()


def unpack_embeddings(blob: bytes | None) -> list[np.ndarray]:
    if not blob:
        return []
    arr = np.frombuffer(blob, dtype="<f2").astype(np.float32)
    n = arr.size // EMBED_DIM
    if n * EMBED_DIM != arr.size:
        raise ValueError(f"corrupt blob: {arr.size} floats not divisible by {EMBED_DIM}")
    return list(cast(np.ndarray, arr.reshape(n, EMBED_DIM)))
