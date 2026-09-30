# VATC Media — Post Studio

A shared, no-login dashboard for uploading image posts, replacing photo backgrounds, and generating funny, informative Instagram captions. One uploaded image produces one edited post and one caption, even when the post contains multiple photographs.

## Deploy to Render

[Deploy the dashboard](https://dashboard.render.com/select-repo?type=blueprint)

1. In Render, choose **New → Blueprint** and connect **bememeapp/VATC-Media**. Grant GitHub access only to this repository.
2. Render reads `render.yaml`. Review the paid web-service and 5 GB temporary disk charges before deploying. The starting web service has 512 MB RAM and processes one post at a time. Workspace fees, disk and AI usage are separate.
3. Paste your OpenAI **API project** key into the `OPENAI_API_KEY` field. Never put it in GitHub, screenshots or a chat.
4. Deploy. The build installs dependencies and downloads the subject-protection model. Open the resulting `onrender.com` URL.
5. Upload **one** of your original posts and generate it. Check its photo areas, foreground edges, caption and OpenAI usage before running larger batches.

The supplied Blueprint matches the existing Oregon service and uses Render's `0.5c-512mb` plan (0.5 CPU / 512 MB RAM), starting at $7/month plus $1.25/month for the 5 GB disk, before tax and usage overages. This is a lower-cost trial configuration: real batch memory use and throughput must be verified before team rollout. Large images may exceed this memory limit. A free instance cannot attach the persistent disk.

No Supabase, Instagram connection or additional ChatGPT subscription is required. This app uses separately billed OpenAI API requests. Users do not need OpenAI accounts: all generations use the server owner's API key and billing.

## Workflow

1. Upload JPG, PNG or WebP files, up to 12 MB / 20 megapixels each. Uploads are sent separately, so a batch is not one huge request.
2. Click **Generate batch**. A persistent queue processes one post at a time with the starting configuration. Closing the browser does not cancel a batch.
3. Open **Review post**. Compare the original, edit the caption, and mark reviewed when satisfied.
4. If a layout is uncertain, use **Photo areas** to draw one rectangle per photo and describe each backdrop. Save, then generate again.
5. In **Edit mask**, blue means editable background. Paint **Protect subject** over missed foreground details, or **Edit background** over an area that should change. Save, then generate again.
6. Download each image and copy its caption, or **Download all** for PNGs, matching text files and a pairing manifest. Partial batches download completed pairs only. Review status is recorded in the manifest; it is not a publication approval gate.

Batch links contain unpredictable identifiers. Keep the current batch URL to return or share it. Different visitors are not shown one another's batches. Anyone with a batch link can see or change its contents. The dashboard itself is public, as requested.

## Image fidelity and limitations

- The app decodes the original, locates photograph panels, and runs the small U2Net model directly through ONNX Runtime to protect foreground subjects. Avoiding the general-purpose matting imports reduces runtime memory. `rembg` downloads the model during the build; Render's `U2NET_HOME` must point inside the deployed source directory so the running service can access it.
- OpenAI receives individual photo crops and editing masks, never an instruction to redraw the post template. Generated backgrounds are composited into the original, preserving protected pixels exactly in the final lossless PNG.
- **Detection and segmentation are estimates.** A missed foreground detail can change, and an incorrect photo rectangle can include text. Check the regions/mask; use the built-in correction tools. Hair, feathers, artwork and collage text need special attention. No claim is made that every automatically detected subject is perfectly protected.
- Masked image models can generate content outside the supplied mask, so the final local composite enforces it again.
- The default economical image model is `gpt-image-1-mini`, medium quality. Text/vision uses `gpt-5-mini`. Both are configurable through Render environment variables; account availability must be checked with a live request. Switching from ChatGPT's Instant mode is not a guarantee of identical output.
- Captions are instructed to avoid unsupported claims and attributed meme claims. There is no automated web fact-checking. Human review is still needed.
- Automated tests use a fake provider for the job pipeline and check protected-pixel integrity. Real OpenAI output, cost and hosting capacity require a separate live test with a configured API key.

## Temporary storage and queue

One web-service process owns a SQLite queue, image files and worker tasks. The Render disk keeps active batches through restarts; content is deleted after 24 hours (cleanup runs every minute and waits for active processing). Expired batches are immediately inaccessible. There is no permanent post library. Daily request counters are retained for seven days.

Use **one process and one instance**. Do not increase Uvicorn workers or horizontally scale this SQLite/disk deployment. Keep `WORKER_CONCURRENCY=1` on the 512 MB server. Capacity, peak memory and latency for the planned 50–150 posts/day must be measured on real batches before increasing concurrency.

A process restart marks in-flight jobs interrupted instead of automatically charging for them again. Explicit retries reuse completed photo edits when possible. Image regeneration intentionally makes new paid requests. Timeout errors tell users to check usage because a timed-out request may have been billed.

Application files expire independently of OpenAI's own API data-retention rules. Responses use `store:false`; this is not a promise of zero provider retention.

## Cost controls

`MAX_DAILY_IMAGE_EDITS=400` and `MAX_DAILY_TEXT_CALLS=800` are enforced globally, including retries, and reset at midnight UTC. A two-photo post can use two image edits. These are request caps, **not dollar caps**. Configure an appropriate spend limit in your OpenAI account as well. A public link is not access control, and anyone using it can spend the owner's budget up to these limits. Runtime failures are not retried automatically.

The app also bounds individual images, pending stored posts (1,000), and active batches (500). Unknown batch IDs are inaccessible; keys and raw provider errors are never sent to browsers. Public access remains a deliberate product choice, not a guarantee against abuse.

## Local development

Requires Python 3.12. Create a virtual environment, install `requirements.txt`, and set the variables in `.env.example` in your shell or run Uvicorn with an appropriate environment loader. The app does not automatically load `.env` files.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/warmup.py
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

Without an API key, the UI supports uploads and previews while explicitly showing that generation is not connected. It does not manufacture AI results.

```sh
.venv/bin/python -m pytest -q
```

`requirements.txt` pins dependencies used for verification. `requirements.in` lists the direct dependency constraints.
