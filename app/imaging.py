"""Local masking/compositing. Generated pixels can only enter allowed backgrounds."""
import io
import math

import numpy as np
from PIL import Image, ImageFilter, ImageOps
from scipy.ndimage import label, find_objects, binary_fill_holes

Image.MAX_IMAGE_PIXELS = 20_000_000
PIPELINE_VERSION = "direct-photo-edit-v2"


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



def detect_photo_areas(original):
    """Find solid image panels on a white template without language-model coordinates.

    Thin text is removed before connected-component detection. Holes inside a
    panel are filled, but exterior white corners and gutters stay protected.
    Ambiguous/light panels require manual review instead of a guessed rectangle.
    """
    preview = original.copy()
    preview.thumbnail((1200, 1600))
    w, h = preview.size
    ink = Image.fromarray((np.min(np.asarray(preview), axis=2) < 239).astype("uint8") * 255)
    kernel = max(3, round(min(w,h) / 120) | 1)
    solid = np.asarray(ink.filter(ImageFilter.MinFilter(kernel)).filter(ImageFilter.MaxFilter(kernel))) > 0
    labels, _ = label(solid)
    allowed = np.zeros((h,w), dtype=bool)
    regions = []
    for ident, bounds in enumerate(find_objects(labels), 1):
        if bounds is None:
            continue
        ys, xs = bounds
        bw, bh = xs.stop-xs.start, ys.stop-ys.start
        component = labels[bounds] == ident
        if bw < .12*w or bh < .10*h or bw*bh < .02*w*h or component.mean() < .55:
            continue
        allowed[bounds] |= binary_fill_holes(component)
        regions.append(dict(x=round(xs.start/w*1000), y=round(ys.start/h*1000),
            w=round(bw/w*1000), h=round(bh/h*1000), confidence=1,
            background="A subtle, realistic background change matching the original setting and perspective"))
    if not 1 <= len(regions) <= 8 or allowed.mean() > .9:
        raise ValueError("Check the photo areas. This layout could not be separated safely from the text.")
    mask = Image.fromarray(allowed.astype("uint8")*255).resize(original.size, Image.Resampling.NEAREST)
    return mask, sorted(regions, key=lambda r: (r['y']//10, r['x']))


def build_edit_mask(original, regions):
    envelope, _ = detect_photo_areas(original)
    selected = Image.new("L", original.size, 0)
    for region in regions:
        selected.paste(255, pixel_box(region, original.size))
    return Image.fromarray(np.minimum(np.asarray(envelope), np.asarray(selected)))


def prepare_edit(original, allowed):
    """Keep the complete post; pad to the image editor's size grid, never crop it."""
    scale = min(1, 1536/max(original.size))
    scale = max(scale, math.sqrt(655360/(original.width*original.height)))
    size = tuple(max(1, round(v*scale)) for v in original.size)
    target = tuple(math.ceil(v/16)*16 for v in size)
    if max(target)/min(target) > 3:
        raise ValueError("This post is too tall or wide for editing. Use an aspect ratio up to 3:1.")
    canvas = Image.new("RGB", target, "white")
    canvas.paste(original.resize(size, Image.Resampling.LANCZOS), (0,0))
    alpha = Image.new("L", target, 255)
    alpha.paste(ImageOps.invert(allowed.resize(size, Image.Resampling.NEAREST)), (0,0))
    mask = Image.new("RGBA", target, "white")
    mask.putalpha(alpha)
    return canvas, mask, (0,0,*size)


def restore_edit(generated, canvas_size, unpad, original_size):
    if generated.size != canvas_size:
        raise ValueError("The image editor changed the canvas size. The result was withheld; please retry.")
    return generated.convert("RGB").crop(unpad).resize(original_size, Image.Resampling.LANCZOS)


def composite_background(original, generated, allowed):
    """Only copy edited photo pixels; template pixels come from the original."""
    if generated.size != original.size or allowed.size != original.size:
        raise ValueError("The edited post and mask must match the original dimensions.")
    return Image.composite(generated.convert("RGB"), original.convert("RGB"), allowed.convert("L"))
