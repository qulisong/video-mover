# Video Mover: An Automated Multi-Platform Content Distribution Pipeline

[简体中文](./README.md) | [English](./README_en.md)

A configurable video-processing and publishing workflow for multi-platform content operations.

The project connects file monitoring, media processing, metadata generation, scheduling, and platform-specific publishing in one pipeline. It explores how repetitive content operations can be made more consistent and observable. Each stage is an independent module that can be combined, replaced, or disabled as needed.

> Use this project only with content and accounts you are authorized to manage. Users are responsible for complying with copyright requirements, platform policies, and applicable laws.

## Context

Multi-platform publishing often involves disconnected manual steps: receiving assets, normalizing media, preparing copy, scheduling posts, and uploading the same content through different interfaces. This creates repetitive work and inconsistent execution.

Video Mover models these steps as composable tasks that can be automated, monitored, and extended. The goal is not to replace the creative work, but to hand deterministic, repetitive labor — processing, copy drafts, scheduling, publishing — to a pipeline, keeping humans on asset selection and final content review.

## Capabilities

- **Directory watching and job triggers**: continuously monitors source folders with Watchdog and starts processing jobs as new files arrive; supports unattended 24/7 operation.
- **Content differentiation toolbox**: a configurable FFmpeg/OpenCV processing chain covering auto subtitles, custom titles, background music (BGM), picture-in-picture (PIP), silent-clip removal, mirroring, rotation, cropping, fade in/out, quality adjustments, and dozens of visual operations such as background blur, frame swapping, color shifting, and texture noise. Every feature is independently configurable and freely composable, aimed at re-creation and asset-normalization scenarios.
- **GPU acceleration**: uses NVIDIA hardware encoding on compatible machines to significantly shorten batch processing time.
- **AI-assisted copy drafts**: integrates Alibaba Cloud Bailian LLM to analyze video content and draft titles and tags, which are reviewed by a human before publishing.
- **Job queues and scheduling**: configures task queues and publishing timetables through APScheduler with unified execution state.
- **Multi-platform publishing adapters**: Playwright-driven browser automation for TikTok, Douyin, Kuaishou, Bilibili, WeChat Channels, and Xiaohongshu, reusing login sessions and filling upload forms automatically.
- **Extensible architecture**: plugins and handlers extend processing steps and upload adapters; adding a platform or a processing feature does not require changing the core flow.
- **Logging and status**: full task logs and execution status for failure diagnosis and process review.

## 📸 Screenshots

<p align="center">
  <a href="https://www.bilibili.com/video/BV1txQeYyEEz" target="_blank">
    <img src="./images/cover_demo.png" alt="Application UI demo" width="800"/>
  </a>
  <br>
  <em>Application UI (click the cover to watch the demo video on Bilibili)</em>
</p>

<p align="center">
  <img src="./images/cover_script.png" alt="Script execution" width="800"/>
  <br>
  <em>Script execution in progress</em>
</p>

## Workflow

```text
Content input
    -> File watcher
    -> Video processing
    -> Metadata generation
    -> Scheduling
    -> Platform adapters
    -> Execution logs
```

## Technical Design

- `main.py`: orchestration, file management, scheduling, and plugin entry point
- `Dedup/`: FFmpeg- and OpenCV-based media processing (implements the toolbox features above)
- `Upload/`: platform adapters and account configuration
- `my_apps.yaml`: application and network settings (input folders, target platforms, proxy, schedules)

Core technologies: Python, FFmpeg, OpenCV, Playwright, Watchdog, APScheduler, and LLM APIs.

## Quick Start

### Requirements

- Windows
- Python 3.12+
- Node.js 22.x
- FFmpeg available on `PATH`
- Chrome

### Installation

1. Clone the repository:

   ```bash
   git clone https://github.com/toki-plus/video-mover.git
   cd video-mover
   ```

2. Run `setup.bat` to install Python, Node.js, Playwright, and related dependencies. An isolated virtual environment is recommended.

3. **Dependency patches (critical step)**: to stay compatible with real platform interfaces, two small changes are required inside the installed third-party libraries in your virtual environment:
   - `f2/apps/tiktok/handler.py` (~line 389): change `params={"cursor": cursor, ...}` to `params={"cursor": int(cursor), ...}`;
   - `f2/utils/utils.py` (~lines 200 / 690): extend date-string parsing to accept both `YYYY-MM-DD` and datetime formats;
   - `tencent_uploader/main.py` (~line 191): raise the page-navigation wait timeout from 1500ms to 10000ms to avoid false failures on slow networks.

### Configuration

1. Configure input folders, target platforms, account files, proxy settings, and schedules in `my_apps.yaml`.
2. If AI-assisted copy is enabled, provide API credentials through secure local configuration — never commit them.
3. Initialize account sessions and cookies when running a platform adapter for the first time.

### Run

The default entry point downloads a single Douyin video without processing or uploading.
For this mode, install only the download dependencies in PowerShell:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-download.txt
$env:PYTHONUTF8 = '1'
& .\.venv\Scripts\python.exe .\main.py
```

Paste a video URL or share text when prompted, or pass it as the positional argument.
Standard `/video/ID` and `/note/ID` URLs, `v.douyin.com` short links, and popup URLs with a numeric
`modal_id` are supported. Popup URLs download only the selected video, not the
profile. Profile batch downloads are not supported.
Download only content you have permission to save.
Files go into a separate `<videoID>` folder under `Download\douyin` by default.
Each folder contains that video's MP4, covers, music, and all sidecars.
`--output` changes the root directory; video-ID subfolders are still created.
`start.bat` uses this entry point, prefers the project virtual environment, and no
longer deletes upload videos.

The default configuration is the credential-free `douyin.example.yaml`, not the
legacy `my_apps.yaml`. If authentication is required, copy it to the Git-ignored
`douyin.local.yaml`, fill in your Douyin web Cookie locally, and run:

```powershell
& .\.venv\Scripts\python.exe .\main.py --config .\douyin.local.yaml
```

Never commit or share cookies. This entry point does not perform browser login or
bypass access controls. The optional `douyin.uifid` string is forwarded as the
`uifid` request header. Custom `douyin.headers` are preserved rather than replaced
by F2 CLI defaults. Header names are case-insensitive; the standalone `douyin.uifid`
takes precedence over `headers.uifid`. Treat UIFID as sensitive, use only genuine
values from normal browser requests, and never commit or share it. Sending this
header does not guarantee that a 403 response will be resolved.

For browser-assisted login/download, install the updated download requirements and
Microsoft Edge, then run:

```powershell
& .\.venv\Scripts\python.exe .\main.py --browser-login
```

The separate Edge window opens first. Log in manually, then enter video or image-post
links in the terminal. The same browser stays open while works are processed serially.
Click the terminal to focus it before pasting, then press Enter. Interactive input
uses an asynchronous line editor with editing, paste, and history support without
blocking browser events.
Live download progress is active only during downloads, not while the input
editor is waiting for another link.
After each download, the program waits for another link. An optional positional
URL is processed first before entering the continuous input loop.

Enter `exit`/`quit` or press `Ctrl+C` to close the browser and end the session.
Closing the browser or ending terminal input also ends the session. Individual
invalid links or failed downloads are reported without closing the browser.

#### Single-video and image-post archives

Browser and direct-API modes share the same archive logic. Direct API access may
still fail with a platform signature-related 403. All files for a video live in
its ID subfolder, reused on subsequent runs. Legacy files in the root whose names
start with that ID followed by an underscore are moved into this folder when the
video is processed. Destination name conflicts are reported before moving files;
nothing is overwritten. Resolve conflicts manually before retrying.
In addition to `<videoID>_video.mp4`:

- Image posts (`aweme_type=68`) save `<workID>_images_0.*`, `_images_1.*`, etc.,
  indexed from zero in the original `images` array order. No MP4 is required.
  Metadata and manifests include `media_type=image_gallery` and the image count;
  metadata also preserves the original image fields. Missing, failed, or corrupt
  images cannot count as a successful image download. Existing sidecars remain
  available and re-entering the URL retries failures. Only still images are saved,
  not Live Photo motion clips or an automatically synthesized video.
- `<videoID>_detail_raw.json`: the complete matched work object, including fields
  not specially parsed. This is not the whole network response; no separately
  accessed cookies or request headers are added.
- `<videoID>_metadata.json`: description, author, original publication timestamp
  and UTC time, hashtags/topics, statistics, music, and video information.
  Missing fields are listed explicitly. Statistics are a capture-time snapshot.
- `<videoID>_desc.txt`: original description.
- `<videoID>_video_cover.*`, `_video_origin_cover.*`, `_video_dynamic_cover.*`,
  `_video_animated_cover.*`: each cover variant actually supplied by the API,
  not a frame extracted from the MP4. Missing variants produce no image file.
  Extensions follow media signatures: JPEG, PNG, WebP, GIF, or AVIF.
- `<videoID>_music_play_url.*`: available platform music (MP3, M4A, AAC, Ogg, WAV).
  It may differ from the final video mix; no MP4 audio extraction is performed.
- `<videoID>_archive.json`: scope, file sizes, SHA-256 hashes, and per-asset states
  (`downloaded`, `existing`, `not_provided`, `unavailable`, `failed`). `partial`
  indicates a missing video/gallery image or failed asset. `captured_response_archived` means
  this response and its available assets were archived, not all platform data.

Alternative media URLs are tried in order. Empty content, HTML/JSON error pages,
and unrecognized media are failures. Re-entering a URL retries failed assets;
existing assets are reused only when their size and hash match the previous
manifest. These checks do not replace full decoding validation or bypass access,
copyright, or download restrictions. Full work JSON can contain author identifiers,
temporary links and tracking parameters: keep it local, not publicly shared.
Comments, other author works, and proof of complete page/API coverage are excluded.

Three product files are also saved:

- `<videoID>_products.txt`: product names, IDs, genuine web/app links, field sources,
  and explicit coverage limitations.
- `<videoID>_products.json`: schema version 2 extraction results, candidate/recognized/
  unrecognized record counts, and the raw evidence field SHA-256.
- `<videoID>_products_raw.json`: original `anchor_info`, `anchors`, `promotions`, and
  `commerce_config_data`, missing/null/empty field states, and decoded JSON fields.

The original extra string is in `raw_fields.anchor_info.extra`; its decoded value
is in `decoded_fields["anchor_info.extra"].value`. Persistent browser sessions also
record the API host, path, and HTTP status, never signed query strings. Evidence
covers only these commerce fields; the full work object is saved separately in
`_detail_raw.json`. Cookies, request headers and the whole network response are
not archived. Raw evidence can contain temporary platform links
and tracking parameters; keep it local rather than sharing it publicly.
Product links are never constructed from IDs.

`found` means each extracted product has a web link, **not that all products have
been captured**. `partial` means incomplete information or parsing warnings, and
`not_found` means no recognizable product data in this response, not proof that
the video has no products. `coverage.all_video_products_verified` currently remains
`false`: the extractor does not automatically reconcile the page's complete product
list or a separate commerce API. This limitation appears in text reports and logs.
App links are distinguished from web links.

Existing nonempty target MP4s skip only video download; work data is still read to
refresh metadata/product files and backfill missing covers/music. Enter the same
URL again without deleting the MP4. Existing videos are identified by ID and filename
and are not decoded again. If the session expires, log in again
in the same window. Complete platform challenges manually and refresh if needed.
Login and per-video metadata waits time out after five minutes; idle link input
does not time out.

Session data stays in the Git-ignored `.browser-profile\douyin` directory.
Your regular browser profile and local YAML are not modified. Cookies are not
logged. To sign out of this dedicated session, log out in its browser window.
Signed API requests run in the real browser; their signatures are not replayed
or forged. Account cookies and API security headers are not forwarded to media
CDNs. Compatibility with all platform page versions is not guaranteed.

A download must produce a nonempty MP4 or report failure; existing target videos
are skipped explicitly. This check does not verify decoding or audio/video integrity.

The legacy TikTok processing/upload pipeline is now in `pipeline.py`. To use it,
install the full dependencies, configure its hard-coded account, paths and schedule,
prepare its download directory, and explicitly run `python main.py --pipeline`.

### Experimental local portrait-retouch previews

This is traditional image processing, **not AI face swapping or identity
replacement**. Use authorized footage only. No media is uploaded or published.
Install `requirements-portrait.txt` on top of the download dependencies, then run:

```powershell
& .\.venv\Scripts\python.exe -m pip install -r requirements-portrait.txt
& .\.venv\Scripts\python.exe .\portrait_preview.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --start 0 --duration 8 --strength 0.6
```

The default `--profile natural` preserves the original mild effect. For visibly
stronger smoothing, skin-tone adjustment, and face-local brightening, use
`--profile strong --strength 0.9`. Strength remains within `(0, 1]`.
Strong mode applies heavier bilateral smoothing, reduced skin chroma, and a
highlight-aware luma lift, with protective transitions around eyes, mouth, and
mask edges. Identity and facial geometry are unchanged; it is not global whitening.
It can look pale or lose skin texture and still requires human review.

CPU-only frontal-face detection requires a plausible eye pair to reduce product
packaging false positives. Skin-local smoothing and a small luma lift exclude
eyes and the central mouth; undetected frames are unchanged. This is not precise
portrait segmentation and can miss faces, misclassify objects, or flicker.

Outputs go to a new `edits\portrait-timestamp\` folder under the source video's
directory: `original.mp4`, `retouched.mp4`, left/right `comparison.mp4`, a
representative `comparison.jpg`, and `report.json` with parameters, hashes,
per-frame detections/edited-pixel counts, decoding validation, and limitations.
Reports also record the profile/settings and mean absolute channel/luma changes
on edited pixels, measured before lossy encoding.
Previews use 24 fps, a maximum width of 720 pixels, and H.264. Available source
audio is encoded to AAC when clipping and then reused unchanged in both outputs;
silent footage remains silent. Mask protection is checked before encoding;
lossy encoding may change pixels outside the mask.

No editable face is an explicit failure, not a successful unchanged preview.
The source is never modified. `--output` must name a new directory; existing
directories are never overwritten. The CLI and structured report support future
orchestration, but downloads/uploads are not chained automatically and every
sample remains `needs_human_review`.

### Local AI single-frame head inpainting

`head_inpaint.py` uses Stable Diffusion Inpainting, not a beauty filter, to
regenerate a fictional adult character's head/upper hair in one video frame.
This is a research prototype, **not video identity replacement**, temporal
consistency, lip synchronization, or publication/commercial-use clearance.
Use authorized footage only. On top of the existing download environment:

```powershell
& .\.venv\Scripts\python.exe -m pip install "torch>=2.6,<3" --index-url https://download.pytorch.org/whl/cpu
& .\.venv\Scripts\python.exe -m pip install "torchvision>=0.21,<1" --index-url https://download.pytorch.org/whl/cpu
& .\.venv\Scripts\python.exe -m pip install -r requirements-generative.txt
& .\.venv\Scripts\python.exe .\head_inpaint.py prepare-model
& .\.venv\Scripts\python.exe .\head_inpaint.py render `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --steps 24 --seed 42 --threads 8
```

The model is the [SD 1.5 Inpainting community mirror](https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-inpainting),
pinned to revision `8a4288a76071f7280aedbdb3253bdb9e9d5d84bb`, with the
CreativeML OpenRAIL-M license and research-use positioning in its model card.
Model preparation downloads only public safetensors weights, configs, safety
checker and model documentation to Git-ignored `.models\sd15-inpainting`.
It does not read or upload videos. File sizes/hashes are recorded and checked
before rendering.

Rendering forces CPU/float32, local-only loading and Hugging Face/Transformers
offline mode, with safety checking retained. Failed/missing safety checks block
export. The default selects a large, reliable single frontal face among 24
candidate frames in the first 15 seconds; use `--seconds` to select a time.
The default character brief is a fictional adult Chinese woman with a natural
everyday appearance, black hair, and thin-frame glasses. This is a generation
brief, not an inference about the source person's nationality or identity.
`--prompt` and `--seed` allow controlled experiments.

Outputs under the video's `edits\head-still-timestamp\` are `original.png`,
`mask.png`, `generated.png`, `comparison.png`, the 512-pixel model input and
generated crop, and `report.json` with model/license, parameters, timing,
safety result, edit metrics, limitations and hashes. `--output` must be new.
PNG compositing preserves every pixel outside the mask exactly; the source
video is unchanged. Insufficient change is a failure, not an unchanged success.
The automatic ellipse is not semantic hair segmentation: long hair outside it
remains original, and hairline/neck/accessory/lighting seams may need correction.
Pixel changes do not prove aesthetic quality. Results remain
`needs_human_review`; no video or publication is generated automatically.

#### Visible-person and outfit restyling

Run `head_inpaint.py prepare-person` to download the official PyTorch LRASPP
segmentation weights (about 12.5 MiB). They are stored under
`.models\person-lraspp`, checked by size/hash, and loaded with `weights_only=True`.
Rendering does not download missing models or upload media.

`render --scope person` replaces the visible segmented person plus a small
silhouette margin, not just the head. The default brief is a fictional adult
Chinese woman with a natural everyday look, modest long-sleeved knitwear, and
subtle makeup. This does not reconstruct hidden anatomy or ensure natural hands.
Person mode requires `--rights-confirmed` and explicit repeated
`--protect-box x,y,width,height` arguments in source-frame pixels and/or
`--protect-mask path.png`: an original-frame-sized, 8-bit grayscale PNG whose
nonblack pixels are protected. Empty, wrong-sized, color, or alpha masks fail.
Contour masks avoid unnecessarily retaining old clothing inside large boxes.
Their product/text/grasping coverage still needs manual review.

For this video's **0.5-second frame only**, use boxes `115,340,305,565`,
`440,1125,280,155`, `10,1180,155,100`, and `345,970,375,245` to protect the
product/grasping region, bottom-right overlay, bottom-left mark, and other hand.
These are manually reviewed coordinates, **not automatic product detection/OCR**;
they must be rechecked for other videos/times. All protected pixels and all pixels
outside the editing mask are verified unchanged. Approximate segmentation and
silhouette margins can still affect nearby background. Model input preserves
the crop aspect ratio approximately, with a maximum edge of 768 pixels.
The refined `edits\person-product-protection-05.png` sample is for this same frame
only. Combine it with boxes `440,1125,280,155`, `10,1180,155,100`,
`345,970,375,245`, and `270,335,55,110` to retain the overlays, other hand, and
fridge label without retaining a rectangular patch of the original clothing.

Additional outputs are `protected.png` and `generated_labeled.png` with an
independent AI-generated header. Prefer the labeled version for display;
`generated.png` is the unlabelled compositing base for exact pixel verification.
Reports record rights confirmation, protection boxes, prompt token counts,
segmentation provenance, protected-pixel checks, and outstanding review items.
Contour input size and SHA-256 are recorded; `protected.png` is the merged mask.
Overlong prompts fail explicitly rather than silently dropping instructions.

Safety checking does not guarantee legal/platform compliance: verify source and
model licenses, product claims, platform AI labeling and publication rules.
No real individual is intentionally impersonated; no automatic publication,
platform approval, or aesthetic satisfaction is claimed.

### Klein original-face restyling experiment (CPU still only)

For substantial hair/outfit changes, use a separate environment:

```powershell
& .\.venv\Scripts\python.exe -m venv .venv-klein
& .\.venv-klein\Scripts\python.exe -m pip install torch==2.14.1+cpu --index-url https://download.pytorch.org/whl/cpu
& .\.venv-klein\Scripts\python.exe -m pip install -r requirements-klein.txt
& .\.venv-klein\Scripts\python.exe .\klein_edit.py prepare-model
```

Pins Apache-2.0 `black-forest-labs/FLUX.2-klein-4B` at
`e7b7dc27f91deacad38e78976d1f2b499d76a294`; component downloads total about
14.9 GiB, excluding duplicate weight formats. Every local file hash is checked.
CPU float32 weights alone need about 30 GiB; startup requires at least 36 GiB
currently available RAM, with further runtime overhead. GPU/offload examples
are not evidence of CPU-only performance.

Prepare the existing SD safety-check model with `head_inpaint.py prepare-model`,
then run:

```powershell
& .\.venv-klein\Scripts\python.exe .\klein_edit.py render `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --seconds 0.5 --rights-confirmed `
  --edit-mask ".\Download\douyin\7679686176123636901\edits\klein-person-mask-refined-05.png" `
  --protect-mask ".\Download\douyin\7679686176123636901\edits\klein-protection-precise-05.png" `
  --output ".\Download\douyin\7679686176123636901\edits\klein-still-v1"
```

The brief requests a low ponytail and the visible upper part of a modest,
opaque, long-sleeved, round-neck dress. Klein edits the full reference image;
local compositing enforces manual masks and, by default, mandatory original-face protection.
These example masks protect face/glasses, product, hands and text for this
frame only; they are not automatic detection. Protected and outside-mask pixels
are checked unchanged. Outputs include an AI-labeled preview, comparison, raw
model output, masks, timing, process peak memory, and report.
The existing local SD safety checker remains active (its CreativeML OpenRAIL-M
terms still need review); failed checks block export. No media upload or
automatic publication. Hairline/neck/clothing seams and aesthetics require
human review; pipeline completion is not appearance approval or video validation.

If segmentation misses garment patterns, manually correct the frame mask and use
`recompose` with the same mask/video arguments, `--previous-result ORIGINAL_RESULT`
and a new `--output` (omit `--seconds` and `--rights-confirmed`). This verifies
prior image/source hashes and recorded authorization, reuses `model_output.png`,
and reruns compositing and local safety checks without another model generation.
The report records its parent report hash and `model_inference_repeated=false`.
Mask corrections alone do not establish acceptable hair/outfit aesthetics.

One real CPU float32 run at 432×768 / 4 steps measured about 24.5 seconds
loading, 152.9 seconds inference, and 32.7 GiB process peak working set.
The outfit changes substantially, but exact original-face/product compositing
still retains some old fabric and does not fully achieve the low ponytail.
`klein-still-v5` needs refinement, not aesthetic approval. Only one generative
run was performed; later versions reuse its output to refine masks/compositing.

#### New-character previews using styling references only

`render --style-brief BRIEF.json` explicitly enables person redesign, allowing
the original face and glasses to change. Omitting it retains original-face
protection. This mode uses approximately 576x1024 at the source aspect ratio,
4 steps, and more CPU time/RAM than the older benchmark above. The brief uses:

```json
{
  "rights_confirmed": true,
  "reference_use": "style_only",
  "face_free_crops_reviewed": true,
  "style_description": "Long chestnut hair and a plain cocoa-brown crewneck top.",
  "references": [
    {"path": "reference.webp", "crop": [190, 820, 520, 520]}
  ]
}
```

Confirm these fields only with actual adaptation rights and manual verification
that the crops contain no faces. Paths resolve relative to the brief.
One or two crops are accepted; `[x,y,width,height]` must be within the image
and at least 32x32. Only cropped hair/fabric swatches and the target video frame
are passed to the local model, not full reference portraits. Face exclusion is
manually reviewed, not automatically detected. The prompt requests a new
fictional adult Chinese woman, but distinct identity still requires review.
Use new edit/protection masks protecting product, hands, and text, with at least
80% of the original face editable. The report records the style board, crop
coordinates, brief/reference hashes, and checks inputs remain unchanged.
New-character mode retains broad outer-silhouette feathering but uses tighter
feathering beside protected hands/products to reduce retained old clothing.
Both widths are recorded; original-face mode keeps the original compositor behavior.

Add `--style-brief BRIEF.json` to the render command with these new masks and a
new output directory. `recompose` follows the original report's face-protection
mode and resets appearance review to pending. Product boundaries, appearance,
and identity distinctness require human review. This is still a single-frame
preview, not video character replacement or automatic publication.

One actual new-mode run used 576x1024, 4 steps, and a style board of two face-free
crops. The report measured 43.2 seconds loading, 278.5 seconds in the inference
phase, and 37.5 GiB peak working set. `klein-style-person-v3` reuses that single
generation for compositing refinement; protected/outside pixels are unchanged.
Minor bottle/wrist seams and overly smooth facial skin remain. Its review status
is `needs_refinement`, not user aesthetic approval.

#### Previews requesting the same authorized reference person

Use `render --identity-reference PORTRAIT` instead of `--style-brief` when the
same authorized person is required, not a new character. These flags are mutually
exclusive. Here `--rights-confirmed` covers both source and reference-person
adaptation rights; public availability or an AI label is not authorization.
The reference must contain exactly one detected frontal face with an eye pair.
The complete face and a small margin are cropped for model conditioning;
the crop, coordinates, and original image hash are recorded. Use masks allowing
the old face to change and choose a new output directory.

This path explicitly requests the same facial proportions, features, and
expression, rather than a new fictional person. Generation is not a pixel-exact
face copy and cannot guarantee identity consistency. The report keeps
`identity_consistency_verified=false` until human verification. Safety checks,
product/hand/text protection, input-integrity checks, local-only processing,
and no automatic publication remain unchanged. Review likeness and naturalness
on a single frame before attempting video; this does not validate temporal identity.

The actual `klein-identity-person-v1` run conditioned on the complete reference
face at 576x1024 / 4 steps: 18.6 seconds loading, 307.1 seconds in the inference
phase, and 33.2 GiB peak working set. Eye/nose/facial proportions still deviate.
Its status is `needs_identity_refinement`: the same-person requirement has
**not** been verified. Passing tests or requesting identity fidelity in a prompt
is not acceptance of the resulting likeness.

### Full-duration head-edit experiment (local CPU)

`head_video.py` extends a reviewed head-only still into a shot-aware experimental
video. The current local plan targets `7679686176123636901`: reuse the first
frontal preview, generate subsequent profile/frontal-turn keyframes, and preserve
product close-ups. Original frame 180 and both generation approaches were blocked
by the safety checker; no complete v1 video was exported. With explicit user
approval, v2 replaces close-up frames `[155,203)` with product frames `[56,104)`
from the same source, retaining full duration and original audio.
This is not unattended identity replacement for arbitrary videos.
The final v4 reuses cached generation, aligns head position and reuses the selected
frontal face for the turn. Pose switching avoids blending two differently oriented
faces, but visible popping, old-hair remnants and background repairs remain.

```powershell
& .\.venv-klein\Scripts\python.exe .\head_video.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --plan ".\Download\douyin\7679686176123636901\edits\head-video-plan-v2.json" `
  --output ".\Download\douyin\7679686176123636901\edits\klein-head-video-v4" `
  --keyframes-dir ".\Download\douyin\7679686176123636901\edits\head-video-keyframes-v2" `
  --alignment ".\Download\douyin\7679686176123636901\edits\head-video-alignment-v1.json" `
  --reviewed-source-shots
```

Uses the installed Klein and safety models without downloads, uploads, or
publication. The output directory must not exist. Plans/masks are manually
prepared local inputs, not distributed with the repository. A plan confirms
rights and mask review, records the source SHA-256, fps, `approved_still`,
`identity_crop: [x,y,width,height]`, and contiguous non-overlapping `shots`
covering every frame. Shot ranges are `[start,stop)`. Use `preserve: true` for
product shots; person shots contain increasing `keys`, each with a `frame`
and a source-pixel head `polygon`, or `reuse` pointing to a reviewed still.
Separately approved shot replacements require `shot_replacement_confirmed: true`
and `replace_with_start`, the start frame of an equal-length section of the same
source. Replacement source frames are recorded individually, cannot coexist with
`preserve` or generation keys, and are not counted as head-only edits.
Conditioning uses a crop of the selected experimental character; reference
likeness is not thereby verified.

Hash-validated keyframe caches survive downstream tracking/encoding failures.
Do not change inputs or the plan while running; a new plan requires a new cache.
Bidirectional DIS flow accumulates coordinate maps, resets at shot boundaries,
and interpolates between pose keyframes. Insufficient tracking area/consistency
fails explicitly instead of silently skipping person frames. Every frame receives
a local safety check by default. Original product shots can also trigger the
generated-image classifier. Only after manual source review and explicit user
agreement, `--reviewed-source-shots` uses manual review for untouched/replaced
original footage while retaining per-frame automated checks for every AI-edited
frame; any failed AI-frame check still stops export. The report distinguishes
these scopes and must not claim that all frames passed automated checks.
Current v4 has 98 automatically checked AI frames and 147 manually reviewed source frames.
Optional `--alignment` records manually reviewed correspondences/head contours,
similarity transforms, old-hair background repairs and pose-switch frames.
Alignment inputs are hashed; keep them immutable after rendering and use a new
file for future adjustments. Export checks unchanged pixels outside propagated masks on head-edit frames
before encoding, fully decodes the result to verify frame count/dimensions/fps,
and compares retained audio packet hashes. Only then is the result directory
published locally with `head-only.mp4`, `contact-sheet.png`, and a per-frame report.

**Limitations:** this is flow-driven texture animation, not a video identity or
lip-sync model. Profile/frontal consistency, blinking, mouth motion, seams and
aesthetics need review. Propagated masks are not automatic garment/hand/occlusion
segmentation, and pre-encoding mask checks do not establish semantic protection.
Lossy MP4 encoding can change outside pixels. Model and safety-component licenses
apply separately; passing safety checks is not platform approval or publication clearance.

### Natural clothing-color preview with the original face

For a conservative alternative to generative restyling, run:

```powershell
& .\.venv\Scripts\python.exe .\clothing_preview.py `
  ".\Download\douyin\7679686176123636901\7679686176123636901_video.mp4" `
  --seconds 0.5 `
  --clothing-mask ".\Download\douyin\7679686176123636901\edits\clothing-soft-blue-mask-05.png" `
  --output ".\Download\douyin\7679686176123636901\edits\natural-clothing-v2"
```

Only `requirements-portrait.txt` is needed; no generative model or media upload.
The nonempty, source-sized 8-bit grayscale PNG mask defines the clothing-color
region. Masks overlapping the detected face fail explicitly. Lab chroma changes
soften the original blue while retaining lightness and fabric detail; no smoothing, geometry,
new garment, hairstyle, or makeup is applied. Face/outside-mask pixels are checked
unchanged. Original/styled PNGs, comparison, mask, and report are exported without
overwriting existing directories. This example mask is manually reviewed for the
blue knitwear in this video's 0.5-second frame only, not automatic clothing detection.
Keep products, hands, and text outside the mask. Other frames need new review.
This is a still preview requiring aesthetic review, not automated video restyling
or a guarantee of improved appearance.

## Current Limitations

- Platform adapters may require updates when third-party pages or interfaces change.
- Some upstream dependencies have version-specific compatibility requirements.
- The project does not yet include comprehensive automated tests or CI.
- API keys, cookies, and account configuration must never be committed to the repository.

## What This Project Demonstrates

The primary focus is workflow decomposition and cross-system orchestration: turning file watching, media processing, LLM calls, scheduling, and browser automation into an observable, recoverable pipeline — defining task boundaries, integrating third-party systems, recording failures, and leaving clear extension points. It is not intended as a showcase of a single media-processing algorithm. Details such as "upstream libraries need manual patches to stay compatible with live platform interfaces" also illustrate the hidden integration costs typical of real-world automation projects.

## 📂 More Projects

- [ai-highlight-clip](https://github.com/toki-plus/ai-highlight-clip) — Long-video smart triage: Whisper transcription + LLM scoring + human review
- [ai-ttv-workflow](https://github.com/toki-plus/ai-ttv-workflow) — Desktop text-to-video workflow with human-in-the-loop checkpoints
- [ai-video-workflow](https://github.com/toki-plus/ai-video-workflow) — Multi-model AIGC video pipeline orchestrating image, video and music services
- [ai-mixed-cut](https://github.com/toki-plus/ai-mixed-cut) — Video re-creation workflow via structured asset library and script reassembly
- [ai-trader-for-mt4](https://github.com/toki-plus/ai-trader-for-mt4) — LLM×MT4 controlled-execution framework: constrained tools, risk rules, state management
- [ai-trader-for-mt5](https://github.com/toki-plus/ai-trader-for-mt5) — AI trading assistant and EA engineering framework for MetaTrader 5
- [auto-usps-tracker](https://github.com/toki-plus/auto-usps-tracker) — Batch shipment tracking and Excel reporting for cross-border e-commerce
- [AB-Video-Deduplicator](https://github.com/toki-plus/AB-Video-Deduplicator) — Experimental video re-creation tool based on high-frame-rate blending
- [netease-downloader](https://github.com/toki-plus/netease-downloader) — Netease Cloud Music desktop downloader: QR login, queue, ID3 tagging

## License

See [LICENSE](./LICENSE).
