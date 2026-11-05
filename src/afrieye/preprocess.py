"""Image preprocessing shared by training, the server and (mirrored in JS) the web app.

Steps: find the bright fundus disc, crop to a square around it, resize, then subtract a
wide blur so lesions and vessels stand out regardless of camera colour cast.
The blur is three box-blur passes because that is simple to reproduce exactly in JavaScript.
"""
import numpy as np
from PIL import Image

BLUR_RADIUS_FRAC = 0.06  # radius of each box pass as a fraction of image size
GAIN = 4.0               # contrast gain applied after subtracting the blur


def crop_to_fundus(img: Image.Image, thumb: int = 256, thresh: int = 18) -> Image.Image:
    """Crop to the bounding box of non-black pixels and pad to a square."""
    img = img.convert("RGB")
    w, h = img.size
    scale = thumb / max(w, h)
    small = np.asarray(img.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BILINEAR))
    mask = small.max(axis=2) > thresh
    if mask.sum() < 0.02 * mask.size:
        box = (0, 0, w, h)
    else:
        ys, xs = np.where(mask)
        box = (int(xs.min() / scale), int(ys.min() / scale),
               min(w, int((xs.max() + 1) / scale)), min(h, int((ys.max() + 1) / scale)))
    crop = img.crop(box)
    cw, ch = crop.size
    side = max(cw, ch)
    canvas = Image.new("RGB", (side, side), (0, 0, 0))
    canvas.paste(crop, ((side - cw) // 2, (side - ch) // 2))
    return canvas


def box_blur_axis(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    """Edge-clamped box blur along one axis, using a running sum."""
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    p = np.pad(a, pad, mode="edge")
    c = np.cumsum(p, axis=axis, dtype=np.float64)
    hi = np.take(c, range(2 * r + 1, c.shape[axis]), axis=axis)
    lo = np.take(c, range(0, c.shape[axis] - 2 * r - 1), axis=axis)
    return ((hi - lo) / (2 * r + 1)).astype(np.float32)


def local_contrast(rgb: np.ndarray) -> np.ndarray:
    """uint8 HxWx3 -> uint8 HxWx3 with the wide background removed and a circular mask."""
    s = rgb.shape[0]
    a = rgb.astype(np.float32)
    r = max(1, int(round(s * BLUR_RADIUS_FRAC)))
    b = a
    for _ in range(3):
        b = box_blur_axis(box_blur_axis(b, r, 0), r, 1)
    out = np.clip((a - b) * GAIN + 128.0, 0, 255)
    yy, xx = np.mgrid[0:s, 0:s]
    c = (s - 1) / 2.0
    outside = (xx - c) ** 2 + (yy - c) ** 2 > (0.97 * s / 2.0) ** 2
    out[outside] = 128.0
    return np.round(out).astype(np.uint8)


def preprocess_pil(img: Image.Image, size: int) -> np.ndarray:
    sq = crop_to_fundus(img)
    rgb = np.asarray(sq.resize((size, size), Image.BILINEAR, reducing_gap=2.0))
    return local_contrast(rgb)


def to_input(batch_uint8: np.ndarray) -> np.ndarray:
    """N,H,W,3 uint8 -> N,3,H,W float32 in roughly [-1, 1]."""
    return ((batch_uint8.astype(np.float32) - 128.0) / 64.0).transpose(0, 3, 1, 2)


def photo_checks(img: Image.Image) -> dict:
    """Fast rule-based checks that catch photos that are not retinas at all, or are unusable.

    These are hand-written rules, not trained on anything. They complement the learned
    quality head, which only ever saw real fundus photos.
    """
    small = np.asarray(img.convert("RGB").resize((128, 128), Image.BILINEAR)).astype(np.float32)
    yy, xx = np.mgrid[0:128, 0:128]
    rr = np.hypot(xx - 63.5, yy - 63.5) / 64.0
    centre, corners = small[rr < 0.6], small[rr > 1.05]
    r, g, b = centre.mean(axis=0)
    lum = small.mean(axis=2)
    lap = (4 * lum[1:-1, 1:-1] - lum[:-2, 1:-1] - lum[2:, 1:-1] - lum[1:-1, :-2] - lum[1:-1, 2:])
    return {
        "centre_brightness": float(centre.mean()),
        "corner_brightness": float(corners.mean()),
        "red_dominance": float(r / max(1.0, b)),
        "sharpness": float(lap[rr[1:-1, 1:-1] < 0.8].std()),
    }


def photo_problem(c: dict):
    """Returns a nurse-readable retake reason, or None."""
    if c["centre_brightness"] < 25:
        return "Too dark. Check the light and retake."
    if c["centre_brightness"] > 235:
        return "Overexposed. Reduce the light and retake."
    if c["red_dominance"] < 1.25 or c["corner_brightness"] > 60:
        return "This does not look like a retina. Centre the pupil and retake."
    if c["sharpness"] < 1.5:
        return "Blurred. Hold still, refocus and retake."
    return None
