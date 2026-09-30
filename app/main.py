import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import unquote, urlparse

import numpy as np
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field

from . import store
from .imaging import PIPELINE_VERSION, build_edit_mask, composite_background, detect_photo_areas, prepare_edit, restore_edit, pixel_box, read_upload
from .provider import OpenAIProvider, ProviderError

logger = logging.getLogger("vatc")
provider = OpenAIProvider()
STATIC = Path(__file__).parent / "static"
BUSY = {"queued","analysing","editing"}
MAX_UPLOAD = 12 * 1024 * 1024


def valid_id(value):
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise HTTPException(404, "This batch or post does not exist.") from None


def batch_exists(batch_id):
    valid_id(batch_id)
    with store.db() as c:
        row = c.execute("SELECT created FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row or row["created"] + store.RETENTION < time.time():
        raise HTTPException(404, "This batch has expired or does not exist. Start a new batch.")
    return row["created"]


def post_exists(batch_id, post_id):
    batch_exists(batch_id)
    valid_id(post_id)
    post = store.get(post_id)
    if not post or post["batch"] != batch_id:
        raise HTTPException(404, "This post does not exist in this batch.")
    return post


def idle(post):
    if post["status"] in BUSY:
        raise HTTPException(409, "This post is still processing. Wait for it to finish.")


def require_provider():
    if not provider.configured:
        raise HTTPException(503, "Generation is not connected yet. Add OPENAI_API_KEY in Render's Environment settings.")


def normalized_regions(regions, size):
    if not 1 <= len(regions) <= 8:
        raise ValueError("Select between one and eight photo areas.")
    out = []
    for r in regions:
        r = dict(r)
        for k in ("x","y","w","h"):
            r[k] = int(r[k])
        if not r.get("background", "").strip():
            raise ValueError("Describe a replacement background for each photo.")
        r["background"] = r["background"][:1500]
        b = pixel_box(r, size)
        for previous in out:
            p = pixel_box(previous,size)
            if min(b[2],p[2]) > max(b[0],p[0]) and min(b[3],p[3]) > max(b[1],p[1]):
                raise ValueError("Photo areas must not overlap.")
        out.append(r)
    return out


async def process(post):
    folder = store.directory(post)
    try:
        original = await asyncio.to_thread(lambda: Image.open(folder / "original.png").convert("RGB"))
        mode = post.get("mode", "all")
        if mode != "caption" and post.get("pipeline") != PIPELINE_VERSION:
            for old in [folder / "mask.png", folder / "result.png", *folder.glob("edited-*.png")]:
                old.unlink(missing_ok=True)
            post.update(regions=[], has_mask=False, has_result=False, layout_confirmed=False, pipeline=PIPELINE_VERSION)
        if mode == "caption" or not post.get("caption"):
            store.consume("text")
            info = await provider.analyse(original, True)
            post.update(title=info["title"][:100], caption=info["caption"], note=info["review_note"][:1200])
            store.save(post)
        if mode == "caption":
            post.update(status="ready" if post.get("has_result") else "uploaded", error="")
            store.save(post)
            return
        post.update(status="editing", stage="Editing the original post")
        store.save(post)
        try:
            envelope, detected = await asyncio.to_thread(detect_photo_areas, original)
        except ValueError as exc:
            post.update(status="review", error=str(exc))
            store.save(post)
            return
        if not post.get("layout_confirmed"):
            post["regions"] = detected
        mask_path = folder / "mask.png"
        if mask_path.exists():
            mask = Image.open(mask_path).convert("L")
            mask = Image.fromarray(np.minimum(np.asarray(mask),np.asarray(envelope)))
        elif post.get("layout_confirmed"):
            mask = await asyncio.to_thread(build_edit_mask, original, post["regions"])
        else:
            mask = envelope
        if not mask.getbbox():
            raise ValueError("No photo area is selected. Check the photo areas before editing.")
        await asyncio.to_thread(mask.save, mask_path)
        post["has_mask"] = True
        store.save(post)
        cached = folder / "edited-post.png"
        if not cached.exists():
            canvas, api_mask, unpad = await asyncio.to_thread(prepare_edit, original, mask)
            preferences = ""
            if post.get("layout_confirmed"):
                preferences = "; ".join(r["background"] for r in post["regions"])
            post["image_model"] = getattr(provider, "image_model", "test")
            post["image_quality"] = getattr(provider, "quality", "test")
            store.save(post)
            store.consume("image")
            raw = await provider.edit(canvas, api_mask, preferences)
            generated = Image.open(io.BytesIO(raw)).convert("RGB")
            generated = await asyncio.to_thread(restore_edit, generated, canvas.size, unpad, original.size)
            await asyncio.to_thread(generated.save, cached)
        combined = Image.open(cached).convert("RGB")
        result = await asyncio.to_thread(composite_background, original, combined, mask)
        await asyncio.to_thread(result.save, folder / "result.png")
        post.update(status="ready", stage="", error="", has_result=True, approved=False)
        store.save(post)
    except asyncio.CancelledError:
        post.update(status="failed", error="Processing was interrupted. Check usage before retrying.")
        store.save(post)
        raise
    except (ProviderError, ValueError) as exc:
        post.update(status="failed", error=str(exc))
        store.save(post)
    except Exception as exc:
        # Do not expose provider responses, credentials or file paths to clients/logs.
        logger.error("Post processing failed: %s", type(exc).__name__)
        post.update(status="failed", error="This post could not be processed. Try again, or adjust its photo areas and mask.")
        store.save(post)


async def worker():
    while True:
        post = store.claim()
        if post:
            await process(post)
        else:
            await asyncio.sleep(1)


def cleanup():
    with store.db() as c:
        rows = c.execute("SELECT id FROM batches WHERE created<?", (time.time()-store.RETENTION,)).fetchall()
        for row in rows:
            posts = c.execute("SELECT data FROM posts WHERE batch=?", (row["id"],)).fetchall()
            if any(json.loads(p["data"])["status"] in BUSY for p in posts):
                continue
            shutil.rmtree(store.DATA / row["id"], ignore_errors=True)
            c.execute("DELETE FROM posts WHERE batch=?", (row["id"],))
            c.execute("DELETE FROM batches WHERE id=?", (row["id"],))
        c.execute("DELETE FROM usage WHERE day<date('now','-7 days')")


async def janitor():
    while True:
        await asyncio.to_thread(cleanup)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    store.initialize()
    tasks = [asyncio.create_task(worker()) for _ in range(max(1,min(4,int(os.getenv("WORKER_CONCURRENCY","2")))))]
    tasks.append(asyncio.create_task(janitor()))
    yield
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in {"GET","HEAD","OPTIONS"}:
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (origin and urlparse(origin).netloc != request.headers.get("host")):
            return JSONResponse({"detail":"Cross-site requests are not allowed."}, status_code=403)
        try:
            if int(request.headers.get("content-length","0")) > 18*1024*1024:
                return JSONResponse({"detail":"This upload is too large."},status_code=413)
        except ValueError:
            return JSONResponse({"detail":"Invalid request length."},status_code=400)
    response = await call_next(request)
    response.headers.update({
        "X-Content-Type-Options":"nosniff", "Referrer-Policy":"no-referrer",
        "X-Frame-Options":"DENY",
        "Content-Security-Policy":"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        "Cache-Control":"no-store" if request.url.path.startswith("/api/") else "no-cache"
    })
    return response


@app.get("/healthz")
def health():
    return {"status":"ok"}


@app.get("/api/config")
def config():
    return {"configured":provider.configured, "retention_hours":store.RETENTION//3600, "max_file_mb":12}


@app.post("/api/batches")
def create_batch():
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        count = c.execute("SELECT count(*) FROM batches").fetchone()[0]
        if count >= 500:
            raise HTTPException(429,"The workspace is full. Please try again after older batches expire.")
        batch = str(uuid.uuid4())
        c.execute("INSERT INTO batches VALUES(?,?)", (batch,time.time()))
    return {"id":batch}


@app.get("/api/batches/{batch_id}")
def get_batch(batch_id: str):
    created = batch_exists(batch_id)
    with store.db() as c:
        posts = [json.loads(r["data"]) for r in c.execute("SELECT data FROM posts WHERE batch=? ORDER BY created", (batch_id,))]
    return {"id":batch_id,"expires":created+store.RETENTION,"posts":posts}


@app.post("/api/batches/{batch_id}/posts")
async def upload(batch_id: str, request: Request):
    batch_exists(batch_id)
    if shutil.disk_usage(store.DATA).free < 300*1024*1024:
        raise HTTPException(507,"Temporary storage is full. Please try again later.")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_UPLOAD:
            raise HTTPException(413,"Each image must be 12 MB or smaller.")
    try:
        img = await asyncio.to_thread(read_upload, bytes(raw))
    except Exception:
        raise HTTPException(400,"Upload a valid JPG, PNG or WebP, 128–8,000 pixels per side and up to 20 megapixels.") from None
    post = {"id":str(uuid.uuid4()), "batch":batch_id, "created":time.time(), "updated":time.time(),
            "pipeline":PIPELINE_VERSION, "filename":Path(unquote(request.headers.get("x-filename","post.png"))).name[:150],
            "title":"", "caption":"", "regions":[], "note":"", "error":"", "status":"uploaded",
            "has_result":False, "has_mask":False,"approved":False,"width":img.width,"height":img.height}
    folder = store.directory(post)
    folder.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(img.save, folder / "original.png")
    thumb = img.copy()
    thumb.thumbnail((600,600))
    await asyncio.to_thread(thumb.save, folder / "thumb.jpg", quality=85)
    with store.db() as c:
        c.execute("BEGIN IMMEDIATE")
        if c.execute("SELECT count(*) FROM posts").fetchone()[0] >= 1000:
            shutil.rmtree(folder)
            raise HTTPException(429,"Temporary storage has reached its post limit. Please try again later.")
        c.execute("INSERT INTO posts VALUES(?,?,?,?)", (post["id"],batch_id,post["created"],json.dumps(post)))
    return post


class Run(BaseModel):
    mode: str = Field(default="all", pattern="^(all|image|caption|retry)$")


@app.post("/api/batches/{batch_id}/posts/{post_id}/run")
async def run_post(batch_id: str, post_id: str, payload: Run):
    require_provider()
    post = post_exists(batch_id,post_id)
    idle(post)
    mode = post.get("mode","all") if payload.mode == "retry" else payload.mode
    if payload.mode == "image":
        folder = store.directory(post)
        for p in folder.glob("edited-*.png"):
            p.unlink()
    if mode != "caption":
        post["has_result"] = False
    post.update(status="queued", mode=mode, error="", approved=False)
    store.save(post)
    return post


class Caption(BaseModel):
    caption: str = Field(max_length=2200)


@app.patch("/api/batches/{batch_id}/posts/{post_id}/caption")
async def caption(batch_id: str, post_id: str, payload: Caption):
    post = post_exists(batch_id,post_id)
    idle(post)
    post.update(caption=payload.caption, approved=False)
    store.save(post)
    return post


@app.post("/api/batches/{batch_id}/posts/{post_id}/approve")
async def approve(batch_id: str, post_id: str):
    post = post_exists(batch_id,post_id)
    idle(post)
    if not post["has_result"] or not post["caption"].strip():
        raise HTTPException(400,"Finish the image and caption before approving.")
    post["approved"] = not post["approved"]
    store.save(post)
    return post


class Region(BaseModel):
    x: int = Field(ge=0, le=1000)
    y: int = Field(ge=0, le=1000)
    w: int = Field(gt=0, le=1000)
    h: int = Field(gt=0, le=1000)
    background: str = Field(min_length=1,max_length=1500)


class Layout(BaseModel):
    regions: list[Region] = Field(min_length=1,max_length=8)


@app.put("/api/batches/{batch_id}/posts/{post_id}/layout")
async def layout(batch_id: str, post_id: str, payload: Layout):
    post = post_exists(batch_id,post_id)
    idle(post)
    try:
        regions = normalized_regions([r.model_dump() for r in payload.regions], (post["width"],post["height"]))
    except ValueError as exc:
        raise HTTPException(400,str(exc)) from None
    folder = store.directory(post)
    for p in [folder / "mask.png", folder / "result.png", *folder.glob("edited-*.png")]:
        p.unlink(missing_ok=True)
    post.update(pipeline=PIPELINE_VERSION,regions=regions,layout_confirmed=True,has_mask=False,has_result=False,approved=False,status="uploaded",error="")
    store.save(post)
    return post


class Mask(BaseModel):
    image: str = Field(max_length=15_000_000)


@app.put("/api/batches/{batch_id}/posts/{post_id}/mask")
async def mask(batch_id: str, post_id: str, payload: Mask):
    post = post_exists(batch_id,post_id)
    idle(post)
    folder = store.directory(post)
    try:
        raw = base64.b64decode(payload.image.split(",")[-1],validate=True)
        image = Image.open(io.BytesIO(raw)).convert("L")
        if image.size != (post["width"],post["height"]):
            raise ValueError()
        image = image.point(lambda p: 255 if p > 127 else 0)
        original = Image.open(folder / "original.png").convert("RGB")
        envelope = build_edit_mask(original, post["regions"])
        image = Image.fromarray(np.minimum(np.array(image),np.array(envelope)))
        if not image.getbbox():
            raise ValueError()
    except Exception:
        raise HTTPException(400,"Paint at least one background area inside a photo. The mask must match the original dimensions.") from None
    image.save(folder / "mask.png")
    for p in folder.glob("edited-*.png"):
        p.unlink()
    (folder / "result.png").unlink(missing_ok=True)
    post.update(has_mask=True,has_result=False,layout_confirmed=True,approved=False,status="uploaded",error="")
    store.save(post)
    return post


@app.get("/api/batches/{batch_id}/posts/{post_id}/file/{kind}")
def file(batch_id: str, post_id: str, kind: str):
    post = post_exists(batch_id,post_id)
    names = {"original":"original.png","result":"result.png","thumb":"thumb.jpg","mask":"mask.png"}
    if kind not in names:
        raise HTTPException(404)
    path = store.directory(post) / names[kind]
    if not path.exists():
        raise HTTPException(404,"This file is not ready yet.")
    return FileResponse(path, media_type="image/jpeg" if kind == "thumb" else "image/png")


@app.delete("/api/batches/{batch_id}/posts/{post_id}")
async def delete(batch_id: str,post_id: str):
    post = post_exists(batch_id,post_id)
    idle(post)
    shutil.rmtree(store.directory(post),ignore_errors=True)
    with store.db() as c:
        c.execute("DELETE FROM posts WHERE id=?",(post_id,))
    return {"deleted":True}


def download_name(index, post):
    stem = re.sub(r"[^a-zA-Z0-9_-]", "-", Path(post["filename"]).stem)[:60].strip("-") or "post"
    return f"{index:03d}-{stem}"


@app.get("/api/batches/{batch_id}/download")
def download(batch_id: str):
    batch = get_batch(batch_id)
    ready = [p for p in batch["posts"] if p["has_result"] and p["caption"].strip() and p["status"] == "ready"]
    if not ready:
        raise HTTPException(400,"No complete posts are ready to download.")
    # Spool ZIP to disk instead of holding an entire 150-post batch in memory.
    import tempfile
    archive = tempfile.TemporaryFile()
    manifest=[]
    try:
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_STORED) as z:
            for index,post in enumerate(ready,1):
                name = download_name(index,post)
                path = store.directory(post) / "result.png"
                if not path.exists():
                    continue
                z.write(path,name+".png")
                z.writestr(name+".txt",post["caption"])
                manifest.append({"image":name+".png","caption_file":name+".txt","original":post["filename"],"reviewed":post["approved"]})
            z.writestr("posts.json",json.dumps(manifest,ensure_ascii=False,indent=2))
        archive.seek(0)
    except Exception:
        archive.close()
        raise
    def chunks():
        try:
            while chunk := archive.read(1024*1024):
                yield chunk
        finally:
            archive.close()
    return StreamingResponse(chunks(),media_type="application/zip",headers={"Content-Disposition":'attachment; filename="VATC-Media-posts.zip"'})


app.mount("/assets",StaticFiles(directory=STATIC),name="static")


@app.get("/")
def home():
    return FileResponse(STATIC / "index.html")
