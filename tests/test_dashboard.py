import asyncio
import base64
import io
import json
import time
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from app import imaging, main, store


def fixture_image():
    img = Image.new("RGB", (400, 500), "white")
    d = ImageDraw.Draw(img)
    d.text((20,20), "ORIGINAL TEXT - NEVER CHANGE", fill="black")
    d.rectangle((40,100,359,399),fill=(80,120,150))
    d.rectangle((145,150,255,360),fill=(220,90,40))
    return img


REGION={"x":100,"y":200,"w":800,"h":600,"background":"A quiet garden","confidence":1}


class FakeProvider:
    configured = True
    def __init__(self): self.image_calls=0;self.text_calls=0
    async def analyse(self, image, caption_only=False):
        self.text_calls+=1
        return {"title":"A new setting", "caption":"A funny hook.\n\nA detailed caption about the original post.","review_note":"", "regions":[] if caption_only else [REGION]}
    async def edit(self, image, mask, background):
        self.image_calls+=1
        assert image.size == mask.size
        assert mask.mode == "RGBA"
        return imaging.png_bytes(Image.new("RGB",image.size,(20,200,70)))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(store,"DATA",tmp_path)
    monkeypatch.setattr(main,"provider",FakeProvider())
    async def idle_worker(): await asyncio.sleep(3600)
    monkeypatch.setattr(main,"worker",idle_worker)
    def protected(crop, model="u2netp"):
        mask=Image.new("L",crop.size,0)
        ImageDraw.Draw(mask).rectangle((105,50,215,260),fill=255)
        return mask
    monkeypatch.setattr(imaging,"subject_mask",protected)
    with TestClient(main.app) as c:
        yield c


def upload(client, name="example.jpeg"):
    b=client.post("/api/batches").json()["id"]
    r=client.post(f"/api/batches/{b}/posts",content=imaging.png_bytes(fixture_image()),headers={"x-filename":name})
    assert r.status_code==200,r.text
    return b,r.json()


def path(b,p,suffix=""):
    return f"/api/batches/{b}/posts/{p['id']}{suffix}"


def execute():
    post=store.claim()
    assert post
    asyncio.run(main.process(post))
    return store.get(post["id"])


def test_batch_pipeline_preserves_pixels_and_pairs_downloads(client):
    b,p=upload(client)
    response=client.post(path(b,p,"/run"),json={"mode":"all"})
    assert response.status_code==200
    # Duplicate queue clicks may not enqueue another paid request.
    assert client.post(path(b,p,"/run"),json={"mode":"all"}).status_code==409
    result=execute()
    assert result["status"]=="ready", result
    original=fixture_image()
    edited=Image.open(io.BytesIO(client.get(path(b,p,"/file/result")).content))
    mask=Image.open(io.BytesIO(client.get(path(b,p,"/file/mask")).content))
    a,z,m=np.array(original),np.array(edited),np.array(mask)
    assert np.array_equal(a[m==0],z[m==0])
    assert not np.array_equal(a[m==255],z[m==255])
    assert np.array_equal(a[:100],z[:100])
    updated="New hook.\n\nThe final edited caption."
    assert client.patch(path(b,p,"/caption"),json={"caption":updated}).status_code==200
    assert client.post(path(b,p,"/approve")).json()["approved"] is True
    raw=client.get(f"/api/batches/{b}/download")
    assert raw.status_code==200
    with zipfile.ZipFile(io.BytesIO(raw.content)) as z:
        assert z.read("001-example.txt").decode()==updated
        assert "001-example.png" in z.namelist()
        assert json.loads(z.read("posts.json"))[0]["reviewed"] is True


def test_caption_only_does_not_edit_images(client):
    b,p=upload(client)
    client.post(path(b,p,"/run"),json={"mode":"caption"})
    result=execute()
    assert result["caption"] and not result["has_result"]
    assert main.provider.image_calls==0


def test_manual_regions_still_generate_caption(client):
    b,p=upload(client)
    region={k:v for k,v in REGION.items() if k!='confidence'}
    assert client.put(path(b,p,"/layout"),json={"regions":[region]}).status_code==200
    client.post(path(b,p,"/run"),json={"mode":"all"})
    result=execute()
    assert result["status"]=="ready" and result["caption"]
    assert main.provider.text_calls==1


def test_mask_is_clipped_to_photo_areas(client):
    b,p=upload(client)
    client.put(path(b,p,"/layout"),json={"regions":[{k:v for k,v in REGION.items() if k!='confidence'}]})
    encoded=base64.b64encode(imaging.png_bytes(Image.new("L",(400,500),255))).decode()
    assert client.put(path(b,p,"/mask"),json={"image":encoded}).status_code==200
    mask=np.asarray(Image.open(store.directory(p)/"mask.png"))
    assert not mask[:100].any() and not mask[:, :40].any()
    assert mask[200,200]==255


def test_uncertain_detection_requires_review_before_image_charge(client,monkeypatch):
    async def uncertain(*args):
        return {"title":"Uncertain", "caption":"Caption", "review_note":"", "regions":[{**REGION,"confidence":.4}]}
    monkeypatch.setattr(main.provider,"analyse",uncertain)
    b,p=upload(client)
    client.post(path(b,p,"/run"),json={"mode":"all"})
    result=execute()
    assert result["status"]=="review"
    assert main.provider.image_calls==0


def test_missing_key_uploads_work_generation_is_honestly_unavailable(client,monkeypatch):
    monkeypatch.setattr(main.provider,"configured",False)
    b,p=upload(client)
    assert client.get("/api/config").json()["configured"] is False
    assert client.post(path(b,p,"/run"),json={"mode":"all"}).status_code==503
    assert client.get(path(b,p,"/file/result")).status_code==404


def test_file_validation_batch_isolation_and_cross_site(client):
    b,p=upload(client)
    other=client.post("/api/batches").json()["id"]
    assert client.get(path(other,p,"/file/original")).status_code==404
    assert client.post(f"/api/batches/{b}/posts",content=b"not an image").status_code==400
    assert client.post("/api/batches",headers={"origin":"https://evil.example"}).status_code==403
    assert client.get(path(b,p,"/file/queue.sqlite")).status_code==404
    assert client.get("/").headers["content-security-policy"]


def test_daily_usage_limit_survives_restart(client,monkeypatch):
    monkeypatch.setenv("MAX_DAILY_IMAGE_EDITS","2")
    store.consume("image");store.consume("image")
    store.initialize()
    with pytest.raises(ValueError,match="daily generation limit"):
        store.consume("image")


def test_expiry_deletes_content(client):
    b,p=upload(client)
    folder=store.directory(p)
    with store.db() as c:
        c.execute("UPDATE batches SET created=? WHERE id=?",(time.time()-store.RETENTION-10,b))
    assert client.get(f"/api/batches/{b}").status_code==404
    main.cleanup()
    assert not folder.exists()
    assert store.get(p["id"]) is None


def test_restart_does_not_repeat_inflight_paid_request(client):
    b,p=upload(client)
    client.post(path(b,p,"/run"),json={"mode":"all"})
    store.claim()
    store.initialize()
    assert store.get(p["id"])["status"]=="failed"
    assert store.claim() is None


def test_invalid_and_overlapping_regions_rejected(client):
    b,p=upload(client)
    region={k:v for k,v in REGION.items() if k!='confidence'}
    assert client.put(path(b,p,"/layout"),json={"regions":[region,region]}).status_code==400
    assert client.put(path(b,p,"/layout"),json={"regions":[{**region,"x":900}]}).status_code==400


@pytest.mark.parametrize("count",[4,8])
def test_variable_batch_sizes_return_one_pair_per_upload(client,count):
    b=client.post("/api/batches").json()["id"]
    for index in range(count):
        p=client.post(f"/api/batches/{b}/posts",content=imaging.png_bytes(fixture_image()),headers={"x-filename":f"post-{index}.png"}).json()
        client.post(path(b,p,"/run"),json={"mode":"all"})
    for _ in range(count):
        assert execute()["status"]=="ready"
    with zipfile.ZipFile(io.BytesIO(client.get(f"/api/batches/{b}/download").content)) as z:
        assert len([n for n in z.namelist() if n.endswith('.png')])==count
        assert len([n for n in z.namelist() if n.endswith('.txt')])==count
        assert len(json.loads(z.read('posts.json')))==count


def test_retry_reuses_successful_photo_edits(client,monkeypatch):
    b,p=upload(client)
    regions=[{"x":100,"y":200,"w":400,"h":600,"background":"Garden"},
             {"x":500,"y":200,"w":400,"h":600,"background":"Garden"}]
    client.put(path(b,p,"/layout"),json={"regions":regions})
    original_edit=main.provider.edit
    calls=0
    async def fail_second(*args):
        nonlocal calls
        calls+=1
        if calls==2: raise main.ProviderError("Test failure")
        return await original_edit(*args)
    monkeypatch.setattr(main.provider,"edit",fail_second)
    client.post(path(b,p,"/run"),json={"mode":"image"})
    assert execute()["status"]=="failed"
    assert (store.directory(p)/"edited-0.png").exists()
    client.post(path(b,p,"/run"),json={"mode":"retry"})
    assert execute()["status"]=="ready"
    assert calls==3  # first succeeds, second fails, only second is retried
