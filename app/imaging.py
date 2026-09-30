"""Local masking/compositing. Generated pixels can only enter allowed backgrounds."""
import io
import threading

import numpy as np
from PIL import Image, ImageFilter, ImageOps

Image.MAX_IMAGE_PIXELS = 20_000_000
_session = None
_segmentation_lock = threading.Lock()


def read_upload(raw: bytes) -> Image.Image:
    with Image.open(io.BytesIO(raw)) as img:
        if img.format not in {"JPEG", "PNG", "WEBP"}:
            raise ValueError("Please upload a JPG, PNG or WebP image.")
        if getattr(img, "n_frames", 1) != 1:
            raise ValueError("Animated images are not supported.")
        if img.width * img.height > 20_000_000 or min(img.size) < 128 or max(img.size) > 8000:
            raise ValueError("Use an image between 128 and 8,000 pixels per side, up to 20 megapixels.")
        img = ImageOps.exif_transpose(img).convert("RGBA")
        out = Image.new("RGBA", img.size, "white")
        out.alpha_composite(img)
        return out.convert("RGB")


def png_bytes(img):
    stream = io.BytesIO()
    img.save(stream, "PNG")
    return stream.getvalue()


def pixel_box(region, size):
    w, h = size
    x0, y0 = round(region["x"] * w / 1000), round(region["y"] * h / 1000)
    x1, y1 = round((region["x"] + region["w"]) * w / 1000), round((region["y"] + region["h"]) * h / 1000)
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h) or min(x1-x0, y1-y0) < 32:
        raise ValueError("A photo area is too small or outside the post. Adjust the photo areas.")
    return x0, y0, x1, y1


def subject_mask(crop: Image.Image, model="u2netp") -> Image.Image:
    # Shared CPU session is serialised to avoid memory spikes on a small host.
    global _session
    from rembg import new_session, remove
    with _segmentation_lock:
        if _session is None:
            _session = new_session(model, providers=["CPUExecutionProvider"])
        mask = remove(crop, session=_session, only_mask=True)
    # Conservatively keep uncertain edge pixels as original pixels.
    return mask.convert("L").point(lambda p: 255 if p >= 24 else 0).filter(ImageFilter.MaxFilter(5))


def build_edit_mask(original, regions, model="u2netp"):
    """White means editable. Everything else is copied from the original."""
    allowed = Image.new("L", original.size, 0)
    coverages = []
    for region in regions:
        box = pixel_box(region, original.size)
        crop = original.crop(box)
        protected = subject_mask(crop, model)
        coverage = np.asarray(protected).mean() / 255
        coverages.append(coverage)
        background = ImageOps.invert(protected)
        # Preserve white rounded template corners. Only inspect the small corner
        # neighbourhood; a light-coloured photographic background remains editable.
        a = np.array(background)
        rgb = np.asarray(crop)
        radius = max(2, round(min(crop.size) * .065))
        for ys in [slice(0, radius), slice(-radius, None)]:
            for xs in [slice(0, radius), slice(-radius, None)]:
                corner = rgb[ys, xs]
                a[ys, xs][np.all(corner >= 247, axis=2)] = 0
        allowed.paste(Image.fromarray(a), box)
    return allowed, coverages


def composite_background(original, generated, allowed):
    """No generative reconstruction of the layout or protected foreground."""
    generated = generated.convert("RGB").resize(original.size, Image.Resampling.LANCZOS)
    allowed = allowed.convert("L")
    if allowed.size != original.size:
        raise ValueError("The edit mask must match the original image size.")
    return Image.composite(generated, original.convert("RGB"), allowed)


def crop_request(original, allowed, region):
    box = pixel_box(region, original.size)
    crop, editable = original.crop(box), allowed.crop(box)
    # Fixed model sizes are letterboxed, never stretched; unpadding reverses this.
    ratio = crop.width / crop.height
    target = (1536, 1024) if ratio > 1.2 else (1024, 1536) if ratio < .83 else (1024, 1024)
    scaled = ImageOps.contain(crop, target, Image.Resampling.LANCZOS)
    offset = ((target[0]-scaled.width)//2, (target[1]-scaled.height)//2)
    canvas = Image.new("RGB", target, (230, 230, 230))
    canvas.paste(scaled, offset)
    # OpenAI mask is transparent where editing is permitted.
    alpha = Image.new("L", target, 255)
    alpha.paste(ImageOps.invert(editable.resize(scaled.size, Image.Resampling.NEAREST)), offset)
    api_mask = Image.new("RGBA", target, (255,255,255,255))
    api_mask.putalpha(alpha)
    unpad = (offset[0], offset[1], offset[0]+scaled.width, offset[1]+scaled.height)
    return canvas, api_mask, unpad, box
