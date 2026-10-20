# Image Intelligence API

Upload **one image** → get **structured JSON** with everything that is *visible*
in it: image info, person/face **detection** (never identification), OCR text,
emails / phones / @usernames / social URLs written in the image, objects, QR
codes, scene, quality and safe EXIF metadata.

> **Privacy rules built in**
> * Faces are only **detected and counted**. They are never recognised, matched
>   against a database, or searched on Facebook / Instagram / LinkedIn or anywhere else.
> * Names, usernames and links are returned **only if they are written in the
>   image** (OCR) or encoded in a **QR code** in the image.
> * No sensitive attributes are guessed (age, gender, ethnicity, emotion...).
> * Uploads are analysed in memory and **not stored** (unless `SAVE_UPLOADS=true`).
> * EXIF GPS coordinates are hidden by default (only `gps_present` is returned).

---

## 1. Architecture

```
POST /analyze-image  (multipart/form-data, field "file")
        │
        ▼
 Validation (Pillow) ── size limit, real JPG/PNG/WEBP, corrupted-file check, EXIF rotation
        │
        ▼
 OpenCV array ──┬── Quality ............ OpenCV   blur (Laplacian variance), brightness, contrast, resolution
                ├── Face detection ..... OpenCV   YuNet (or Haar fallback): boxes + count, NO identity
                ├── Objects + persons .. YOLO     80 COCO classes; persons -> human_detection (HOG fallback)
                ├── OCR ................ EasyOCR  every readable line + confidence + box
                │      └─ preprocessing  OpenCV   upscale small images, CLAHE contrast
                ├── QR / barcodes ...... pyzbar + OpenCV QRCodeDetector
                ├── Text parsing ....... Regex    email, phone, @username, URLs, LinkedIn/Instagram/
                │                                 Facebook/X/YouTube/GitHub, name / job title / company
                ├── Metadata ........... Pillow   EXIF camera make/model, capture date, software, lens
                └── Scene .............. Rules    indoor/outdoor, office-like, room, street, vehicle,
                                                  nature, document/screenshot/profile-card-like
        │
        ▼
 Structured JSON (Pydantic model, documented in Swagger)
```

### Who does what

| Task | Handled by |
|---|---|
| Decode, validate, EXIF-rotate, format/size/channels | **Pillow** |
| Resize, colour conversion, blur/brightness/contrast, OCR preprocessing, colour statistics for the scene | **OpenCV** |
| Face boxes and count | **OpenCV** (YuNet model, Haar cascade fallback) |
| Person boxes/count + objects (laptop, mobile phone, car, chair, table, bottle, monitor, book, bag ...) | **YOLO** (Ultralytics YOLOv8). Without YOLO: persons only, via OpenCV HOG |
| Reading text | **OCR** (EasyOCR) |
| Email, phone, @username, website, social URLs, name / job title / company | **Regex** + simple rules on the OCR lines |
| QR codes and barcodes | **QR detector** (pyzbar, plus OpenCV QRCodeDetector) |
| Camera make/model, capture date, software, lens, GPS present | **Metadata parser** (Pillow EXIF) |
| Scene categories | Rules over YOLO labels + OCR + OpenCV colour statistics, with an `evidence` list |

Each stage is isolated: if one fails (or its model isn't installed), the others
still run and the problem appears in `warnings`. `engines` shows what ran.

---

## 2. Project structure

```
image-intelligence/
├── app/
│   ├── main.py                 FastAPI app, model loading at startup, error handlers
│   ├── config.py               settings from .env
│   ├── api/image_api.py        POST /analyze-image, GET /health
│   ├── services/
│   │   ├── image_analyzer.py   runs the pipeline, isolates failures, builds the response
│   │   ├── ocr_service.py      EasyOCR + line grouping
│   │   ├── object_detector.py  YOLO (HOG fallback for persons)
│   │   ├── face_detector.py    YuNet / Haar, detection only
│   │   ├── qr_detector.py      pyzbar + OpenCV
│   │   ├── text_parser.py      regex: emails, phones, usernames, URLs, name/title/company
│   │   ├── scene_service.py    scene rules with evidence
│   │   └── metadata_service.py EXIF (GPS hidden by default)
│   ├── models/response_model.py  Pydantic response schema
│   └── utils/image_utils.py    validation, decoding, quality, OCR preprocessing
├── scripts/download_models.py  fetch YuNet / YOLO / EasyOCR models ahead of time
├── tests/
│   ├── make_samples.py         generates test images (profile card + QR, EXIF photo, broken file)
│   └── test_text_parser.py     regex unit tests
├── models/                     model files (created on first run)
├── uploads/                    only used when SAVE_UPLOADS=true
├── requirements.txt
├── .env.example  /  .env
└── README.md
```

---

## 3. Installation (Windows, Python 3.11+)

```powershell
cd image-intelligence
py -3.11 -m venv .venv            # any Python 3.11+ works
.venv\Scripts\activate
python -m pip install --upgrade pip

# Core (API, OpenCV, Pillow, QR) - small and fast
pip install fastapi "uvicorn[standard]" python-multipart pydantic numpy "opencv-python-headless>=4.9,<5" Pillow pyzbar

# AI models: CPU PyTorch first (much smaller than the default CUDA build), then YOLO + OCR
pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu
pip install ultralytics easyocr

# or everything at once
pip install -r requirements.txt

copy .env.example .env
```
Linux/macOS: same commands with `source .venv/bin/activate`; for pyzbar install
the system library: `sudo apt install libzbar0` / `brew install zbar`.

### Models (download once)
```powershell
python scripts/download_models.py
```
| Model | Size | Saved to | Used for |
|---|---|---|---|
| YuNet (OpenCV Zoo) | ~230 KB | `models/face_detection_yunet_2023mar.onnx` | face detection |
| YOLOv8n (Ultralytics) | ~6 MB | `models/yolov8n.pt` | persons + objects |
| EasyOCR (CRAFT + English recogniser) | ~100 MB | `models/easyocr/` | OCR |

Without the script, YOLO and EasyOCR download automatically on the first
start. For better accuracy (slower), set `YOLO_MODEL=yolov8s.pt`. For Hindi
text too, set `OCR_LANGUAGES=en,hi`.

---

## 4. Run
```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```
* Swagger UI: **http://127.0.0.1:8000/docs** → `POST /analyze-image` → *Try it out* → choose a file → *Execute*
* Health and active engines: `GET http://127.0.0.1:8000/health`

### curl
```bash
curl -X POST "http://127.0.0.1:8000/analyze-image" -F "file=@tests/samples/profile_card.png"
```
PowerShell:
```powershell
curl.exe -X POST "http://127.0.0.1:8000/analyze-image" -F "file=@tests/samples/profile_card.png"
```

### Postman
1. **POST** `http://127.0.0.1:8000/analyze-image`
2. **Body → form-data**: key `file`, change the type from *Text* to **File**, choose the image.
3. **Send**. The response is JSON (status 200), or `{"success": false, "error": "..."}` with 400 / 413.

---

## 5. Example response (abridged; real output for `tests/samples/profile_card.png`)
```json
{
  "success": true,
  "image": {"width": 1200, "height": 700, "format": "PNG", "channels": 3, "file_size": 41213, "orientation": "landscape"},
  "human_detection": {"person_present": false, "person_count": 0, "persons": [], "engine": "ultralytics-yolov8n.pt"},
  "face_detection": {"face_present": false, "face_count": 0, "faces": [], "engine": "opencv-yunet"},
  "visible_information": {
    "possible_name": "John Smith",
    "possible_job_title": "Software Engineer",
    "possible_company": "Acme Technologies Pvt Ltd",
    "username": "johnsmith",
    "email": "john@example.com",
    "phone": "+91 98765 43210",
    "linkedin_url": "https://linkedin.com/in/johnsmith"
  },
  "text": ["Acme Technologies Pvt Ltd", "John Smith", "Software Engineer", "@johnsmith",
           "linkedin.com/in/johnsmith", "john@example.com", "+91 98765 43210"],
  "objects": [],
  "qr_codes": [{"type": "url", "value": "https://linkedin.com/in/johnsmith", "symbology": "QRCODE"}],
  "scene": {"setting": "unknown", "categories": ["profile-card-like"], "description": "profile-card-like"},
  "quality": {"blur": "low", "brightness": "bright", "contrast": "normal", "resolution": "medium", "ocr_readability": "good"},
  "metadata": {"exif_present": false, "gps_present": false},
  "warnings": [],
  "processing_ms": {"ocr": 2100, "objects": 180, "total": 2550}
}
```
Field notes:
* `possible_*` fields are **guesses from visible text** (e.g. the largest
  "First Last" line is the possible name). Treat them as hints.
* `quality.blur`: `low` = sharp, `high` = blurry (`blur_score` = Laplacian variance).
* `scene.evidence` lists *why* each category was chosen.

---

## 6. Testing
```powershell
python tests/test_text_parser.py          # regex unit tests (or: python -m pytest tests -q)
python tests/make_samples.py              # creates tests/samples/*
# with the server running:
curl.exe -F "file=@tests/samples/profile_card.png" http://127.0.0.1:8000/analyze-image
curl.exe -F "file=@tests/samples/exif_photo.jpg"   http://127.0.0.1:8000/analyze-image   # EXIF camera + date, outdoor/nature
curl.exe -F "file=@tests/samples/not_an_image.jpg" http://127.0.0.1:8000/analyze-image   # 400 "Corrupted or unreadable image"
```

---

## 7. Configuration (`.env`)
| Variable | Default | Meaning |
|---|---|---|
| `MAX_UPLOAD_MB` | 15 | reject bigger files (413) |
| `MAX_PIXELS` | 40000000 | decompression-bomb guard |
| `SAVE_UPLOADS` | false | keep a copy in `uploads/` |
| `OCR_ENGINE` | easyocr | `none` disables OCR |
| `OCR_LANGUAGES` | en | e.g. `en,hi` |
| `OCR_MIN_CONFIDENCE` | 0.30 | drop weaker OCR words |
| `YOLO_MODEL` | yolov8n.pt | `yolov8s.pt` / `yolov8m.pt` for accuracy |
| `YOLO_CONFIDENCE` | 0.35 | detection threshold |
| `INCLUDE_GPS` | false | return EXIF GPS coordinates |
| `LOG_LEVEL` | INFO | logging level |

---

## 8. Troubleshooting
| Problem | Fix |
|---|---|
| `warnings: OCR unavailable` | `pip install easyocr` (needs PyTorch, see install). First start downloads ~100 MB. |
| `warnings: YOLO unavailable` | `pip install ultralytics`; persons still come from OpenCV HOG meanwhile. |
| pyzbar error `libzbar-64.dll` / `libiconv.dll` on Windows | Install the **Microsoft Visual C++ 2013 Redistributable (x64)**. Until then the OpenCV QR detector is used automatically (QR codes only, no barcodes). |
| `face_detection.engine: none` | Run `python scripts/download_models.py`, and use OpenCV 4.x (OpenCV 5 removed the bundled Haar files). |
| Slow first request | Models load at startup; the first OCR call warms up. Keep the server running. |
| OCR misses small text | Use a sharper / larger image; check `quality.blur` and `quality.ocr_readability`. |
| 400 `Unsupported image format` | Only JPG, JPEG, PNG and WEBP are accepted (HEIC/GIF/BMP are rejected). |
