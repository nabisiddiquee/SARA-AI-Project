"""Pydantic response models (shown in Swagger UI)."""

from __future__ import annotations

from pydantic import BaseModel, Field


class BBox(BaseModel):
    x: int
    y: int
    width: int
    height: int


class ImageInfo(BaseModel):
    width: int
    height: int
    format: str
    channels: int
    mode: str
    file_size: int = Field(description="bytes")
    orientation: str = Field(description="landscape / portrait / square")
    exif_rotation_applied: bool = False


class Detection(BaseModel):
    label: str
    confidence: float | None = None
    bbox: BBox | None = None


class HumanDetection(BaseModel):
    person_present: bool
    person_count: int
    persons: list[Detection] = []
    engine: str


class FaceDetection(BaseModel):
    face_present: bool
    face_count: int
    faces: list[Detection] = []
    engine: str
    note: str = "Faces are only detected and counted, never identified or matched."


class TextLine(BaseModel):
    text: str
    confidence: float
    bbox: BBox | None = None


class VisibleInformation(BaseModel):
    possible_name: str | None = None
    possible_job_title: str | None = None
    possible_company: str | None = None
    username: str | None = None
    usernames: list[str] = []
    email: str | None = None
    emails: list[str] = []
    phone: str | None = None
    phones: list[str] = []
    website: str | None = None
    websites: list[str] = []
    linkedin_url: str | None = None
    instagram_url: str | None = None
    facebook_url: str | None = None
    twitter_url: str | None = None
    youtube_url: str | None = None
    github_url: str | None = None
    source: str = "Only text/QR codes visibly present in the image."


class QRCode(BaseModel):
    type: str
    value: str
    symbology: str = "QRCODE"
    bbox: BBox | None = None


class Scene(BaseModel):
    setting: str = Field(description="indoor / outdoor / unknown")
    categories: list[str] = []
    description: str
    evidence: list[str] = []


class Quality(BaseModel):
    blur: str
    blur_score: float
    brightness: str
    brightness_value: float
    contrast: str
    contrast_value: float
    resolution: str
    megapixels: float
    ocr_readability: str


class Metadata(BaseModel):
    exif_present: bool
    camera_make: str | None = None
    camera_model: str | None = None
    capture_date: str | None = None
    software: str | None = None
    lens_model: str | None = None
    exif_orientation: int | None = None
    gps_present: bool = False
    gps: dict | None = None


class AnalysisResponse(BaseModel):
    success: bool = True
    image: ImageInfo
    human_detection: HumanDetection
    face_detection: FaceDetection
    visible_information: VisibleInformation
    text: list[str]
    text_details: list[TextLine] = []
    objects: list[Detection]
    qr_codes: list[QRCode]
    scene: Scene
    quality: Quality
    metadata: Metadata
    engines: dict[str, str]
    warnings: list[str] = []
    processing_ms: dict[str, int] = {}


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
