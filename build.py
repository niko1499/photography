#!/usr/bin/env python3
"""Build the site: photos/<album>/*.jpg  ->  _site/ (ready for GitHub Pages).

    python build.py           build; images that are already up to date are skipped
    python build.py --clean   wipe _site/ first and rebuild everything
    python build.py --reset   copy site-template.json over site.json (backs up the current one)

What it does
  1. Finds every image under photos/. The folder a photo sits in is its album.
  2. Also reads any urls.txt in an album folder (photos hosted anywhere), downloading each once
     and caching it like a local photo from then on.
  3. Writes a thumbnail (keeps the photo's shape) and a web-sized copy of each photo (WebP), plus a
     mid-size copy for landscape photos (they are shown two columns wide in Original view), with
     colours converted to sRGB and the photo's own metadata stripped -- except for Artist and
     Copyright, unless site.json's "metadata" setting says otherwise.
  4. Reads camera settings (EXIF) and writes everything to _site/data.json.
  5. Copies web/ (index.html, style.css, app.js) into _site/.

Only dependency: Pillow  (pip install -r requirements.txt). Fetching urls.txt photos uses only
Python's own standard library, so nothing extra is needed for that either.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import math
import random
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

Image.MAX_IMAGE_PIXELS = None  # your own photos: allow very large panoramas

ROOT = Path(__file__).resolve().parent
PHOTOS_DIR = ROOT / "photos"
WEB_DIR = ROOT / "web"
OUT_DIR = ROOT / "_site"
CONFIG_FILE = ROOT / "site.json"
TEMPLATE_FILE = ROOT / "site-template.json"   # what --reset restores site.json from

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}
URL_LIST_NAME = "urls.txt"
EXTERNAL_CACHE_NAME = ".external.json"
SORT_MODES = {"newest", "oldest", "name", "random"}
# How much of each photo's own metadata ends up in the published WebPs.
#   all      keep everything (including GPS) -- publishes where the photo was taken
#   none     strip everything
#   credits  strip everything except Artist and Copyright (the default)
METADATA_MODES = {"all", "none", "credits"}
EXIF_FIELDS = ["camera", "lens", "focal", "aperture", "shutter", "iso", "author", "date"]

DEFAULTS = {
    "title": "Photography",
    "subtitle": "by Your Name",
    "description": "A photo portfolio.",
    "author": "Your Name",
    "bio": "",
    "email": "",
    "links": [],
    "sort": "newest",
    "exif_fields": ["camera", "focal", "aperture", "shutter", "iso"],
    "metadata": "credits",
    "thumb_size": 480,
    "large_size": 2200,
    "quality": 80,
    "wide_ratio": 1.15,
}
# Settings the browser gets to see (the rest only affect the build).
PUBLIC_KEYS = ["title", "subtitle", "description", "author", "bio", "email", "links"]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    """'Île de Ré 2024!' -> 'ile-de-re-2024'. Falls back to a hash for e.g. Japanese."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return slug or "x" + hashlib.sha1(text.encode()).hexdigest()[:6]


def unique(base: str, used: set[str]) -> str:
    name, n = base, 2
    while name in used:
        name, n = f"{base}-{n}", n + 1
    used.add(name)
    return name


def natural_key(text: str) -> list:
    """Sort 'img2' before 'img10'."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", text.lower())]


# A leading date in a folder name: "2024", "2024-07", "2024-07-14" (then a space, _ or -)
DATE_PREFIX = re.compile(r"^(\d{4})(?:[-_.](\d{2}))?(?:[-_.](\d{2}))?(?=[\s_-]|$)[\s_-]*")


def split_album_name(folder: str) -> tuple[str, str | None]:
    """'2024-07 Iceland' -> ('Iceland', '2024-07').  'road-trip' -> ('Road Trip', None)."""
    date = None
    title = folder
    m = DATE_PREFIX.match(folder)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        valid = (mo is None or 1 <= int(mo) <= 12) and (d is None or 1 <= int(d) <= 31)
        if valid and folder[m.end():].strip():
            date = "-".join(x for x in (y, mo, d) if x)
            title = folder[m.end():]
    if " " not in title:
        title = title.replace("_", " ").replace("-", " ")
    title = " ".join(title.split())
    if title == title.lower():
        title = " ".join(w.capitalize() for w in title.split())
    return title or folder, date


# File names that are just camera noise, not a caption.
NOISE_WORDS = {
    "img", "dsc", "dscn", "dscf", "dsd", "pxl", "mvimg", "dji", "gopr", "image", "photo",
    "pano", "edit", "edited", "hdr", "lr", "copy", "screenshot", "final", "cover",
}


def photo_title(stem: str) -> str:
    """Name a file 'Golden hour.jpg' to get a caption. 'IMG_0234.jpg' gets none."""
    words = [w for w in re.split(r"[\s_\-.]+", stem) if w]
    meaningful = [w for w in words if w.lower() not in NOISE_WORDS and not w.isdigit()]
    if not any(len(w) >= 3 and w.isalpha() for w in meaningful):
        return ""
    text = " ".join(words)
    return text[:1].upper() + text[1:]


# --------------------------------------------------------------------------- #
# Reading a photo's metadata
# --------------------------------------------------------------------------- #
def _num(value):
    if isinstance(value, tuple) and len(value) == 2:  # older Pillow: (numerator, denominator)
        value = value[0] / value[1] if value[1] else None
    try:
        f = float(value)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def _text(value) -> str:
    if isinstance(value, bytes):
        value = value.decode("utf-8", "ignore")
    return str(value or "").strip("\x00 \t\r\n")


def read_exif(exif) -> dict:
    """Pick out the handful of EXIF fields worth showing. Never includes GPS."""
    try:
        sub = exif.get_ifd(0x8769)  # Exif sub-IFD
    except Exception:
        sub = {}
    out: dict[str, str] = {}

    make, model = _text(exif.get(0x010F)), _text(exif.get(0x0110))
    if model:
        first = make.split()[0].lower() if make else ""
        out["camera"] = model if (not first or model.lower().startswith(first)) else f"{make} {model}"
    if lens := _text(sub.get(0xA434)):
        out["lens"] = lens

    if (v := _num(sub.get(0x920A))) and v > 0:
        out["focal"] = f"{round(v)}mm"
    if (v := _num(sub.get(0x829D))) and v > 0:
        out["aperture"] = "f/" + f"{v:.1f}".rstrip("0").rstrip(".")
    if (v := _num(sub.get(0x829A))) and v > 0:
        out["shutter"] = f"1/{round(1 / v)}s" if v < 1 else f"{v:g}s"
    iso = sub.get(0x8827)
    if isinstance(iso, (tuple, list)):
        iso = iso[0] if iso else None
    if (v := _num(iso)) and v > 0:
        out["iso"] = f"ISO {int(v)}"

    if artist := _text(exif.get(0x013B)):  # Artist: who made the photo, if the file says
        out["author"] = artist

    raw = _text(sub.get(0x9003) or exif.get(0x0132))
    try:
        out["date"] = datetime.strptime(raw, "%Y:%m:%d %H:%M:%S").isoformat()
    except ValueError:
        pass
    return out


def scan(src: Path | bytes) -> tuple[int, int, dict]:
    """Cheap pass (no pixel decoding): display size after rotation + EXIF. src is a local path for
    a photo in photos/, or raw bytes already downloaded for a photo listed in a urls.txt."""
    with Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src) as im:
        w, h = im.size
        exif = im.getexif()
        if exif.get(0x0112, 1) in (5, 6, 7, 8):  # camera was held sideways
            w, h = h, w
        return w, h, read_exif(exif)


# --------------------------------------------------------------------------- #
# Making the images
# --------------------------------------------------------------------------- #
_SRGB = None


def _to_srgb(im: Image.Image, icc: bytes | None) -> Image.Image:
    """Convert e.g. iPhone Display-P3 / AdobeRGB to sRGB so colours look right everywhere."""
    global _SRGB
    if not icc:
        return im
    try:
        _SRGB = _SRGB or ImageCms.createProfile("sRGB")
        profile = ImageCms.ImageCmsProfile(io.BytesIO(icc))
        return ImageCms.profileToProfile(im, profile, _SRGB, outputMode=im.mode)
    except Exception:
        return im


def fit_long_edge(w: int, h: int, long_edge: int) -> tuple[int, int]:
    scale = min(1.0, long_edge / max(w, h))
    return max(1, round(w * scale)), max(1, round(h * scale))


def thumb_dims(w: int, h: int, short_edge: int) -> tuple[int, int]:
    """Thumbnail size: the short side is `short_edge` (never upscaled). The long side is capped
    at twice that so panoramas stay light. The photo's shape is kept, so the page can show the
    thumbnail cropped to a square (Grid view) or as-is (Original view)."""
    s = min(1.0, short_edge / min(w, h))
    tw, th = w * s, h * s
    cap = 2 * short_edge
    if max(tw, th) > cap:
        s2 = cap / max(tw, th)
        tw, th = tw * s2, th * s2
    return max(1, round(tw)), max(1, round(th))


def _gps_sources() -> list[Path]:
    """Local photos whose own EXIF carries a GPS IFD -- i.e. the ones that would give away the
    exact spot they were taken if "metadata" is "all"."""
    found = []
    for src in sorted(PHOTOS_DIR.rglob("*")):
        if src.suffix.lower() not in IMAGE_TYPES or any(
                part.startswith((".", "_")) for part in src.relative_to(PHOTOS_DIR).parts):
            continue
        try:
            with Image.open(src) as im:
                if 0x8825 in im.getexif():
                    found.append(src)
        except Exception:
            pass
    return found


def _warn_gps() -> None:
    """A loud, specific warning before anything is written: "all" publishes coordinates, and the
    photo's filename is right there in the build output anyway."""
    hits = _gps_sources()
    if not hits:
        return
    print()
    print("  " + "!" * 66)
    print("  WARNING: site.json has \"metadata\": \"all\".")
    print("  GPS coordinates will be published in the images below, which")
    print("  tells anyone who saves a photo exactly where it was taken:")
    print()
    for src in hits:
        print("      " + src.relative_to(PHOTOS_DIR).as_posix())
    print()
    print('  Use "credits" (the default) or "none" to strip location.')
    print("  " + "!" * 66)
    print()


def _metadata_for(exif, mode: str) -> bytes | None:
    """The EXIF block to embed in a published image, per site.json's "metadata" setting.

    Orientation (0x0112) is always dropped: render() calls exif_transpose(), so the pixels are
    already upright and keeping the tag would make a viewer rotate them a second time.

    "credits" builds a fresh block rather than copying the source's, so nothing -- GPS above all --
    can ride along inside a sub-IFD we did not think to clear.
    """
    if mode == "none":
        return None

    if mode == "all":
        # Only pull in sub-IFDs the photo actually has: calling get_ifd() for an absent one creates
        # it, which would write an empty GPS block into every photo that never had location data.
        for ifd in (0x8769, 0x8825):
            if ifd in exif:
                try:
                    exif.get_ifd(ifd)
                except Exception:
                    pass
        exif.pop(0x0112, None)
        return exif.tobytes() or None

    out = Image.Exif()
    if artist := _text(exif.get(0x013B)):
        out[0x013B] = artist
    # Copyright is read from either IFD: cameras put it in the Exif sub-IFD, but piexif cannot
    # write it there at all, so set_photo_author.py can only write IFD0. It is then written to BOTH,
    # because the spec puts it in the sub-IFD while Windows Explorer and plenty of other desktop
    # tools read IFD0 only -- with the sub-IFD alone, the field shows up empty in Explorer.
    try:
        sub = exif.get_ifd(0x8769)
    except Exception:
        sub = {}
    if copyright := _text(sub.get(0x8298) or exif.get(0x8298)):
        out[0x8298] = copyright
        out.get_ifd(0x8769)[0x8298] = copyright
    return out.tobytes() if out else None


def render(src: Path | bytes, thumb: Path, large: Path, mid: Path | None, size: tuple[int, int], cfg: dict) -> None:
    lw, lh = size
    q = cfg["quality"]
    with Image.open(io.BytesIO(src) if isinstance(src, (bytes, bytearray)) else src) as im:
        icc = im.info.get("icc_profile")
        # Read Orientation before _metadata_for() clears it: getexif() hands back the same cached
        # object every time, and draft() below needs to know the camera was held sideways.
        try:  # JPEG only: let the decoder shrink while reading (much faster on big files)
            swapped = im.getexif().get(0x0112, 1) in (5, 6, 7, 8)
            im.draft("RGB", (lh, lw) if swapped else (lw, lh))
        except Exception:
            pass
        exif_bytes = _metadata_for(im.getexif(), cfg["metadata"])
        im = ImageOps.exif_transpose(im)
        has_alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
        im = im.convert("RGBA" if has_alpha else "RGB")
        if im.size != (lw, lh):
            im = im.resize((lw, lh), Image.Resampling.LANCZOS, reducing_gap=2.0)

        tw, th = thumb_dims(lw, lh, cfg["thumb_size"])
        small = im if (tw, th) == im.size else im.resize((tw, th), Image.Resampling.LANCZOS, reducing_gap=2.0)

        outputs = [(large, im), (thumb, small)]
        if mid is not None:  # landscape photos are shown two columns wide in Original view, so they get a bigger copy
            mw, mh = fit_long_edge(lw, lh, 2 * cfg["thumb_size"])
            outputs.append((mid, im if (mw, mh) == im.size else im.resize((mw, mh), Image.Resampling.LANCZOS, reducing_gap=2.0)))

        for target, pic in outputs:
            target.parent.mkdir(parents=True, exist_ok=True)
            # The ICC profile is never carried over, in any mode: the pixels have been converted to
            # sRGB, so the source profile would describe the wrong colour space and shift the image.
            extra = {"exif": exif_bytes} if exif_bytes else {}
            _to_srgb(pic, icc).save(target, "WEBP", quality=q, method=4, **extra)


def is_fresh(src: Path, *outputs: Path) -> bool:
    if not all(o.exists() for o in outputs):
        return False
    return min(o.stat().st_mtime for o in outputs) >= src.stat().st_mtime


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def _json_with_comments(text: str) -> dict:
    """Parse JSON that may contain // and /* */ comments, which is not standard JSON but makes
    site.json self-explanatory. Comments are only stripped outside string literals, so a value
    like "https://example.com" survives intact."""
    out = []
    i, n, in_str, esc = 0, len(text), False, False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if text.startswith("//", i):
            while i < n and text[i] != "\n":   # a line comment ends at the newline
                i += 1
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(c)
        i += 1
    return json.loads("".join(out))


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(_json_with_comments(CONFIG_FILE.read_text(encoding="utf-8")))
        except json.JSONDecodeError as e:
            sys.exit(f"site.json is not valid JSON: {e}")
    if cfg["sort"] not in SORT_MODES:
        sys.exit(f'site.json: "sort" must be one of {sorted(SORT_MODES)}')
    if cfg["metadata"] not in METADATA_MODES:
        sys.exit(f'site.json: "metadata" must be one of {sorted(METADATA_MODES)}')
    unknown = [f for f in cfg["exif_fields"] if f not in EXIF_FIELDS]
    if unknown:
        print(f"warning: unknown exif_fields ignored: {unknown} (choose from {EXIF_FIELDS})")
        cfg["exif_fields"] = [f for f in cfg["exif_fields"] if f in EXIF_FIELDS]
    return cfg


def reset_config() -> int:
    """Put site.json back to site-template.json, overwriting whatever is there now.

    The template is the version kept under version control, so this is "undo my local edits to the
    settings". The current site.json is saved aside first, since anything only it knows -- a link
    you just added, say -- is otherwise lost.
    """
    if not TEMPLATE_FILE.exists():
        sys.exit(f"{TEMPLATE_FILE.name} is missing, so there is nothing to reset site.json to")

    previous = {}
    if CONFIG_FILE.exists():
        try:
            previous = _json_with_comments(CONFIG_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            previous = {}  # unreadable anyway, so there's nothing worth preserving
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CONFIG_FILE.with_name(f"site.json.bak-{stamp}")
        shutil.copyfile(CONFIG_FILE, backup)
        print(f"current site.json backed up to {backup.name}")
    else:
        print("no site.json yet, writing one from the template")

    # Validate the template before it overwrites anything, so a broken one can't destroy a good file.
    try:
        template = _json_with_comments(TEMPLATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        sys.exit(f"{TEMPLATE_FILE.name} is not valid JSON: {e}")
    if not isinstance(template, dict):
        sys.exit(f"{TEMPLATE_FILE.name}: expected a JSON object")

    shutil.copyfile(TEMPLATE_FILE, CONFIG_FILE)

    changed = [k for k in sorted(set(previous) | set(template)) if previous.get(k) != template.get(k)]
    if changed:
        print(f"site.json reset from {TEMPLATE_FILE.name}: " + ", ".join(changed))
    else:
        print(f"site.json already matched {TEMPLATE_FILE.name}")
    print("run: python build.py")
    return 0


def discover() -> list[Path]:
    """All images under photos/, skipping anything whose path has a part starting with . or _"""
    found = []
    for p in PHOTOS_DIR.rglob("*"):
        rel = p.relative_to(PHOTOS_DIR)
        if any(part.startswith((".", "_")) for part in rel.parts):
            continue
        if p.is_file() and p.suffix.lower() in IMAGE_TYPES:
            found.append(p)
    return sorted(found, key=lambda p: natural_key(p.relative_to(PHOTOS_DIR).as_posix()))


def discover_url_files() -> list[Path]:
    """Every urls.txt under photos/ (same one-per-album idea as local photos, same skip rule)."""
    found = [p for p in PHOTOS_DIR.rglob(URL_LIST_NAME)
             if p.is_file() and not any(part.startswith((".", "_")) for part in p.relative_to(PHOTOS_DIR).parts)]
    return sorted(found, key=lambda p: natural_key(p.relative_to(PHOTOS_DIR).as_posix()))


def ext_outputs_ready(ext_cache: dict, img_dir: Path, key: str) -> bool:
    """True if this externally hosted photo is already downloaded and rendered, so it can be taken
    from the cache without touching the network. All three output sizes must be on disk, since the
    mid-size copy is only made for landscape photos."""
    meta = ext_cache.get(key)
    if not meta or not meta.get("rel"):
        return False
    rel = meta["rel"]
    return ((img_dir / "thumb" / rel).exists() and (img_dir / "large" / rel).exists()
            and (not meta["wide"] or (img_dir / "mid" / rel).exists()))


def read_url_list(path: Path) -> list[tuple[str, str, str | None]]:
    """Each line in a urls.txt is one externally hosted photo: a URL, then optionally a date
    (2025-05-31) and a caption after it. Blank lines and lines starting with # are ignored, so you
    can leave notes for yourself. The date is optional, and only worth typing for a host that
    doesn't keep camera data in what it serves -- otherwise it's read from the photo itself."""
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        date = parts[1] if len(parts) > 1 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", parts[1]) else None
        caption = " ".join(parts[2:] if date else parts[1:]).strip()
        out.append((parts[0], caption, date))
    return out


# --------------------------------------------------------------------------- #
# Fetching a public photo from a URL
# --------------------------------------------------------------------------- #
# A urls.txt URL can be either the image bytes themselves, or a *public web page* that shows one
# (a Google Photos or Immich share link, a blog post, ...). fetch_photo() handles both: it reads
# the page's own metadata for the image it advertises, then asks that host for the largest version
# it serves without logging in. No API key, no account, no OAuth -- if you can see the photo in a
# logged-out browser, this can fetch it.
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# Ceiling on any single download, so a misdirected URL can't fill the runner's disk.
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024
# Only the start of a page is read for its metadata; a photo-sharing page is easily megabytes of
# script, and scanning all of it would be pure waste.
MAX_HTML_BYTES = 2 * 1024 * 1024


def http_get(url: str, tries: int = 3, timeout: int = 30, max_bytes: int = MAX_DOWNLOAD_BYTES) -> tuple:
    """GET one URL, returning (final_url_after_redirects, content_type, body). Retries a couple of
    times, because network hiccups happen. Sends a desktop-browser User-Agent, since some hosts
    (Google's photo CDN among them) refuse plain script-like requests."""
    request = urllib.request.Request(url, headers={"User-Agent": BROWSER_UA, "Accept": "*/*"})
    last_error: Exception | None = None
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                if len(body) > max_bytes:
                    raise RuntimeError(f"response was bigger than the {max_bytes // 1024 // 1024}MB limit")
                return response.geturl(), response.headers.get("Content-Type", ""), body
        except Exception as e:  # network hiccups happen; a couple of retries is cheap insurance
            last_error = e
            if attempt < tries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"download failed after {tries} attempts ({last_error})")


# Reading a page's metadata. Tags are matched one at a time rather than with a single pattern
# stretched across the document: a lazy ".*?" between two attributes backtracks catastrophically
# on a megabyte page and can take minutes, while finditer() over <meta ...> stays linear.
_HEAD_TAG = re.compile(r"<(?:meta|link)\b[^>]*>", re.IGNORECASE)
_ATTR = re.compile(r"""([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")
_JSON_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL)
_TITLE_TAG = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)

# The metadata a page uses to advertise its own picture, best first. "image_src" is the older
# Google/Facebook convention, still emitted by plenty of sites.
_IMAGE_META = ("og:image:secure_url", "og:image:url", "og:image", "twitter:image:src", "twitter:image")
# Titles that are site furniture rather than a caption, so they don't end up under a photo.
_JUNK_TITLES = {"public share", "shared album", "google photos", "photos", "photo",
                "immich", "flickr", "photostream"}


def _tag_attrs(tag: str) -> dict:
    """Every attribute of one HTML tag, as a dict with lowercase keys."""
    out = {}
    for m in _ATTR.finditer(tag):
        out[m.group(1).lower()] = m.group(2) or m.group(3) or m.group(4) or ""
    return out


def page_metadata(html: str) -> dict:
    """Pull the two things we care about out of a page: the image URLs it advertises (in
    preference order) and its title, which makes a decent caption for the photo."""
    images: list[str] = []

    def add(url: str) -> None:
        url = (url or "").strip()
        if url and url not in images:
            images.append(url)

    title = ""
    for tag in _HEAD_TAG.findall(html):
        a = _tag_attrs(tag)
        label = (a.get("property") or a.get("name") or "").lower()
        if label in _IMAGE_META:
            add(a.get("content", ""))
        elif a.get("rel", "").lower() == "image_src":
            add(a.get("href", ""))
        elif label in ("og:title", "twitter:title") and not title:
            title = a.get("content", "").strip()
    for blob in _JSON_LD.findall(html):  # schema.org markup, for the sites that publish it
        try:
            data = json.loads(blob.strip())
        except ValueError:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            add(str(node.get("contentUrl") or ""))
            thumb = node.get("thumbnailUrl")
            add(thumb[0] if isinstance(thumb, list) and thumb else str(thumb or ""))

    if not title:
        m = _TITLE_TAG.search(html)
        title = m.group(1).strip() if m else ""
    if title.lower() in _JUNK_TITLES or len(title) > 80:
        title = ""
    return {"images": images, "title": title}


def image_upgrades(url: str) -> list[str]:
    """Larger versions of one image URL that the same host may also serve, best guess first.

    Hosts put a small, social-preview-sized copy in their page metadata but keep the full-size one
    a URL away. These are the two patterns in the wild:
      - Immich (and the self-hosted servers that copy it) publish a shared photo as
        /api/assets/<id>/thumbnail?key=<share key>. The key is part of the public share link and
        needs no account, and `size=fullsize` is the largest a share key gets without logging in.
      - Google Photos hands out lh3.googleusercontent.com links whose trailing "=w600-h315-p-k"
        encodes a size; "=s0" means "don't resize", i.e. give me the original.
    """
    out = []
    if re.search(r"/api/assets/[^/]+/thumbnail", url) and re.search(r"[?&]key=", url):
        out.append(url + ("&" if "?" in url else "?") + "size=fullsize")
    if "googleusercontent.com" in url:
        # Google's photo URLs end in a size token: "=w600-h315-p-k", or nothing at all when the page
        # just handed over the bare photo. Either way "=s0" means "don't resize" -- the original.
        stripped = re.sub(r"=[^=]*$", "", url)
        if stripped != url or "/pw/" in url:
            out.append(stripped + "=s0")
    return out


# A shared Google Photos page embeds the whole album's photo list in its HTML, inside one of these
# script callbacks, as a big nested array. Each entry starts like
#   ["<photo id>",["<lh3 photo url>",<width>,<height>, ...]] ... ,<taken ms>,"<hash>",<tz offset ms>,
# where <taken> is when the photo was taken and <tz offset> is its local offset in milliseconds, so
# the two together give the local time the shutter fired. This is how a shared album is listed in
# full without a login -- there is no public API for it, only this.
_GOOGLE_PHOTOS = re.compile(
    r'\["(AF1Qip[A-Za-z0-9_-]{30,})",\s*'
    r'\["(https://lh3\.googleusercontent\.com/pw/[^"]+)",(\d+),(\d+),'
    r'.*?\],(\d{13}),"[^"]*",(-?\d+),',
    re.DOTALL)


def google_shared_photos(html: str) -> list[dict]:
    """The photos a shared Google Photos album lists for itself, oldest first, as
    [{id, url, w, h, date}] -- or [] for any page that isn't one.

    Only /pw/ (photo) URLs are taken, which is what leaves videos and the album's own cover art
    out of the list. If Google ever stops publishing this, or reshapes it, this returns [] and the
    link falls back to importing the single cover image, which is how this worked before.
    """
    out, seen = [], set()
    for photo_id, url, w, h, taken_ms, tz_ms in _GOOGLE_PHOTOS.findall(html):
        if photo_id in seen:
            continue
        seen.add(photo_id)
        date = None
        try:  # taken is UTC milliseconds, plus the photo's own zone offset to get local time
            local = datetime.fromtimestamp(int(taken_ms) / 1000, timezone.utc) + \
                timedelta(milliseconds=int(tz_ms))
            date = local.replace(tzinfo=None, microsecond=0).isoformat()
        except (ValueError, OverflowError, OSError):
            pass
        out.append({"id": photo_id, "url": url, "w": int(w), "h": int(h), "date": date})
    return out


def google_album_title(html: str) -> str:
    """The name Google gives a shared album, taken from the page's <title> and stripped of the
    " - Google Photos" suffix it always ends with. Empty if there isn't a usable one."""
    m = _TITLE_TAG.search(html)
    if not m:
        return ""
    title = m.group(1).strip()
    for suffix in (" - Google Photos", " | Google Photos", " - Photos"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    return "" if title.lower() in _JUNK_TITLES or len(title) > 80 else title



def _decode_image(data: bytes) -> tuple[int, int] | None:
    """(width, height) if these bytes really are an image Pillow can read, else None. verify()
    decodes just enough of the file to prove it, which is much cheaper than a full load."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
        with Image.open(io.BytesIO(data)) as im:
            return im.width, im.height
    except Exception:
        return None


# An Immich public share advertises each photo as
#   /api/assets/<id>/thumbnail?key=<share key>
# and that same asset id and key, unauthenticated, also return the full metadata record:
#   /api/assets/<id>?key=<share key>
# The share key is the public token from the /share/... link, not a private API key, so this needs
# no account and no secret -- it is the same capability the share page itself hands to any visitor.
IMMICH_ASSET_URL = re.compile(
    r"^(?P<server>https?://[^/]+)/api/assets/(?P<id>[0-9a-z-]{8,})/thumbnail\?(?P<query>.*\bkey=[^&]+.*)$",
    re.IGNORECASE)


IMMICH_SHARE_URL = re.compile(r"^(?P<server>https?://[^/]+)/share/(?P<key>[A-Za-z0-9_\-]{16,})/?$",
                              re.IGNORECASE)


def immich_exif(asset: dict) -> dict:
    """One Immich asset record -> this project's own EXIF field names, so the result drops straight
    into data.json. Best effort: a field Immich doesn't have, or has renamed, is simply absent."""
    info = asset.get("exifInfo") or {}

    def text(*keys) -> str:
        for k in keys:  # the first non-empty one wins, so a server rename costs one field, not all
            if v := _text(info.get(k) or asset.get(k)):
                return v
        return ""

    out: dict[str, str] = {}
    make, model = text("make"), text("model")
    if model:
        first = make.split()[0].lower() if make else ""
        out["camera"] = model if (not first or model.lower().startswith(first)) else f"{make} {model}"
    if lens := text("lensModel"):
        out["lens"] = lens
    if (v := _num(info.get("focalLength"))) and v > 0:
        out["focal"] = f"{round(v)}mm"
    if (v := _num(info.get("fNumber"))) and v > 0:
        out["aperture"] = "f/" + f"{v:.1f}".rstrip("0").rstrip(".")
    exposure = info.get("exposureTime")
    if isinstance(exposure, str) and exposure.strip():
        out["shutter"] = exposure if exposure.endswith("s") else f"{exposure}s"
    elif (v := _num(exposure)) and v > 0:
        out["shutter"] = f"1/{round(1 / v)}s" if v < 1 else f"{v:g}s"
    if (v := _num(info.get("iso"))) and v > 0:
        out["iso"] = f"ISO {int(v)}"

    # The date the photo was actually taken, in order of preference. GPS and the photo's pixel
    # dimensions are deliberately not read: GPS is never published by this project, and this
    # record's width/height are the un-rotated originals, which would fight the orientation the
    # downloaded file already carries.
    for k in ("dateTimeOriginal", "localDateTime", "fileCreatedAt"):
        if raw := text(k):
            try:
                out["date"] = datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None).isoformat()
            except ValueError:
                pass
            if "date" in out:
                break
    return out


def immich_metadata(image_url: str) -> dict:
    """Camera details and the real capture date for an Immich shared photo, or {} if this URL isn't
    an Immich shared photo or the server won't say.

    This matters because the JPEG Immich serves for a share link has had its EXIF stripped (measured:
    zero EXIF keys), so the date and camera are simply not in the downloaded file. The server still
    has them, and hands them to anyone holding that photo's share link. If the endpoint ever stops
    answering -- it is undocumented, so it may -- this returns {} and the photo falls back to
    whatever the downloaded file carried, exactly as before.
    """
    m = IMMICH_ASSET_URL.match(image_url or "")
    if not m:
        return {}
    try:
        # One try and a short timeout: this is a bonus, and a slow build helps nobody.
        _, ctype, body = http_get(f"{m['server']}/api/assets/{m['id']}?{m['query']}",
                                  tries=1, timeout=15)
        if "json" not in ctype.lower():
            return {}
        asset = json.loads(body)
    except Exception:
        return {}  # 401/404/timeout/offline -- all of which just mean "no extra detail"
    return immich_exif(asset) if isinstance(asset, dict) else {}


def immich_shared_photos(server: str, key: str) -> tuple[list[dict], str]:
    """The photos an Immich share link covers, in the order the album lists them, as
    [{id, url, exif}] -- plus the album's name. Empty if the link isn't an Immich share, or the
    server won't talk.

    Two requests, both carrying the public share key and no account, which is exactly the access
    any visitor to the share page has:
      GET /api/shared-links/me?key=  what the link points at. An INDIVIDUAL link (one photo) lists
                                     its asset inline; an ALBUM link instead names the album.
      GET /api/albums/<id>?key=      an album share's contents, with full metadata already in them.
    This is how Immich's own web app loads a share page, so it is as public as that page is.
    """
    try:
        _, ctype, body = http_get(f"{server}/api/shared-links/me?key={key}", tries=1, timeout=20)
        if "json" not in ctype.lower():
            return [], ""
        link = json.loads(body)
    except Exception:
        return [], ""  # not an Immich share, or a password/expiry we can't get past
    if not isinstance(link, dict):
        return [], ""

    album = link.get("album") or {}
    title = _text(album.get("albumName"))
    assets = link.get("assets") or []
    if not assets and album.get("id"):  # an album share names the album; go and read it
        try:
            _, ctype, body = http_get(f"{server}/api/albums/{album['id']}?key={key}",
                                      tries=1, timeout=20)
            if "json" in ctype.lower():
                assets = (json.loads(body) or {}).get("assets") or []
        except Exception:
            assets = []
    if not isinstance(assets, list):
        return [], title

    out = []
    for asset in assets:
        if not isinstance(asset, dict) or asset.get("type") not in (None, "IMAGE"):
            continue  # videos and anything else aren't photos this site can show
        asset_id = asset.get("id")
        if not asset_id:
            continue
        out.append({
            "id": asset_id,
            "url": f"{server}/api/assets/{asset_id}/thumbnail?key={key}&size=fullsize",
            "exif": immich_exif(asset),
        })
    return out, title



def fetch_photo(url: str, fetched: tuple | None = None) -> tuple[bytes, dict]:
    """Get the photo a public URL points at. Returns (image_bytes, info), where info carries the
    image URL it resolved to, a caption if the page offered one, and a short note on how it was
    found -- all of which go into the build log and the cache.

    Handles both shapes a urls.txt line can take: the image bytes directly (all this used to
    support), or a public page displaying a photo, whose advertised image is read and then
    upgraded to the largest size that host serves publicly.

    `fetched` is an already-made http_get() result for this same URL, so a caller that has had to
    look at the page itself doesn't pay for it twice.
    """
    final_url, ctype, body = fetched if fetched else http_get(url)
    if _decode_image(body):  # the common case, and it costs the one request we already made
        return body, {"resolved": final_url, "caption": "", "note": "direct image link", "exif": {}}

    if "html" not in ctype.lower() and not body[:2048].lstrip()[:1] == b"<":
        raise RuntimeError(f"not an image, and not an HTML page either (Content-Type: {ctype or 'unknown'})")

    meta = page_metadata(body[:MAX_HTML_BYTES].decode("utf-8", "ignore"))
    if not meta["images"]:
        if re.search(r"/photos?/[0-9a-f-]{8,}", url, re.IGNORECASE):
            raise RuntimeError("this looks like a private photo page rather than a public share "
                               "link -- share the photo in your photo app and use the /share/... "
                               "URL it gives you")
        raise RuntimeError("no image advertised by the page (looked for og:image, twitter:image, "
                           "link rel=image_src and schema.org markup)")

    # Each candidate is tried at its advertised size and at any larger size the same host offers,
    # and the biggest that actually decodes wins -- the advertised size is usually a small
    # social-preview thumbnail, so stopping at it would throw away most of the photo.
    best = None  # (pixels, data, width, height, url, note)
    for candidate in meta["images"]:
        absolute = urllib.parse.urljoin(final_url, candidate)
        variants = [absolute] + image_upgrades(absolute)
        for n, try_url in enumerate(variants):
            try:
                _, _, data = http_get(try_url)
            except Exception:
                continue
            size = _decode_image(data)
            if not size:
                continue
            note = "public page" if n == 0 else "public page, upgraded to the largest public size"
            if best is None or size[0] * size[1] > best[0]:
                best = (size[0] * size[1], data, size[0], size[1], try_url, note)
    if not best:
        raise RuntimeError("the page advertises an image but none of its versions could be "
                           "downloaded (the host may be blocking non-browser requests)")

    _, data, _, _, resolved, note = best
    # The photo's own file often arrives stripped of EXIF (Immich serves share photos that way), so
    # ask the host what it knows about it. Best effort: {} for any other host, or if it declines.
    return data, {"resolved": resolved, "caption": meta["title"], "note": note,
                  "exif": immich_metadata(resolved)}


def date_key(d: str) -> str:
    parts = d.split("-")
    return "-".join(parts + ["00"] * (3 - len(parts)))


def period_start(d: str) -> str:
    """'2026' -> '2026-01-01T00:00:00', '2026-09' -> '2026-09-01T00:00:00', '2026-09-03' -> '2026-09-03T00:00:00'"""
    y, m, day = (d.split("-") + ["01", "01"])[:3]
    return f"{y}-{m}-{day}T00:00:00"


def make_album(key, albums: dict, used_album_slugs: set, title: str, date: str | None,
               slug_base: str) -> dict:
    """Create (or return) one album under any key. The first time a key is seen the album is made
    with this title; after that it is returned unchanged."""
    if key not in albums:
        albums[key] = {"slug": unique(slugify(slug_base), used_album_slugs),
                       "title": title, "date": date, "entries": []}
    return albums[key]


def get_album(rel_dir: tuple, albums: dict, used_album_slugs: set) -> dict | None:
    """The album for a folder path (empty tuple = no album). The title and date come from the folder
    name. The slug covers the full folder path, e.g. Japan/Tokyo, so two different "Tokyo" folders
    never collide."""
    if not rel_dir:
        return None
    title, date = split_album_name(rel_dir[-1])
    return make_album(rel_dir, albums, used_album_slugs, title, date, slug_base="-".join(rel_dir))


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    # A caption or filename can hold anything, including emoji, and the default Windows console
    # encoding can't print those -- it would raise mid-build and lose the photo. Ask for UTF-8 and
    # fall back to '?' rather than ever raising on output.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description="Build the photo site into _site/")
    ap.add_argument("--clean", action="store_true", help="wipe _site/ first and rebuild everything")
    ap.add_argument("--reset", action="store_true",
                    help="copy site-template.json over site.json (backs up the current one), then stop")
    args = ap.parse_args()

    if args.reset:
        return reset_config()

    started = time.time()
    cfg = load_config()  # "metadata" comes from site.json
    PHOTOS_DIR.mkdir(exist_ok=True)
    if args.clean and OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    # Regenerate images if size/quality settings changed since last build.
    img_dir = OUT_DIR / "img"
    stamp = json.dumps({"layout": 4, **{k: cfg[k] for k in ("thumb_size", "large_size", "quality", "wide_ratio", "metadata")}}, sort_keys=True)
    stamp_file = img_dir / ".stamp"
    if stamp_file.exists() and stamp_file.read_text() != stamp:
        shutil.rmtree(img_dir)
    img_dir.mkdir(parents=True, exist_ok=True)
    stamp_file.write_text(stamp)

    if cfg["metadata"] == "all":
        _warn_gps()

    # Photos from a urls.txt are fetched once and, from then on, treated exactly like a local photo --
    # this little cache is what lets a later build recognise "already downloaded" without a network call.
    # It lives alongside the images above, so it resets on --clean or whenever the settings above change.
    ext_cache_file = img_dir / EXTERNAL_CACHE_NAME
    try:
        ext_cache: dict = json.loads(ext_cache_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        ext_cache = {}
    seen_ext_keys: set[str] = set()

    # ---- photos -> entries -------------------------------------------------
    albums: dict[tuple, dict] = {}
    used_album_slugs: set[str] = set()
    used_paths: set[str] = set()
    entries: list[dict] = []
    made = cached = 0

    for src in discover():
        rel_dir = src.relative_to(PHOTOS_DIR).parent.parts
        try:
            w, h, exif = scan(src)
            album = get_album(rel_dir, albums, used_album_slugs)

            folder = album["slug"] if album else "_root"
            name = slugify(src.stem)
            n = 2
            while f"{folder}/{name}" in used_paths:
                name, n = f"{slugify(src.stem)}-{n}", n + 1
            used_paths.add(f"{folder}/{name}")
            rel = f"{folder}/{name}.webp"

            lw, lh = fit_long_edge(w, h, cfg["large_size"])
            wide = lw / lh >= cfg["wide_ratio"]
            thumb, large = img_dir / "thumb" / rel, img_dir / "large" / rel
            mid = img_dir / "mid" / rel if wide else None
            if is_fresh(src, *[p for p in (thumb, large, mid) if p]):
                cached += 1
            else:
                render(src, thumb, large, mid, (lw, lh), cfg)
                made += 1
                print(f"  + {src.relative_to(PHOTOS_DIR).as_posix()}")
        except Exception as e:  # one bad file should not sink the whole build
            print(f"warning: skipped {src.relative_to(PHOTOS_DIR).as_posix()} ({type(e).__name__}: {e})")
            continue

        entry = {
            "date": exif.get("date"),
            "sort_date": exif.get("date"),  # replaced below for undated photos in a dated album
            "stem": src.stem.lower(),
            "rec": {
                "src": f"img/large/{rel}",
                "thumb": f"img/thumb/{rel}",
                "w": lw,
                "h": lh,
                "title": photo_title(src.stem),
                "album": album["slug"] if album else None,
                "exif": {f: exif[f] for f in cfg["exif_fields"] if f in exif},
            },
        }
        if wide:
            entry["rec"]["mid"] = f"img/mid/{rel}"
        entries.append(entry)
        if album:
            album["entries"].append(entry)

    # ---- externally hosted photos (urls.txt) -----------------------------------
    def add_one(key: str, album: dict | None, exif: dict, w: int, h: int, wide: bool,
                shown_date: str | None, caption: str) -> None:
        """Put one fetched photo into the site, exactly as a local photo would go in."""
        exif = dict(exif)  # a copy: the caller may hand in the shared cache's own dict
        if shown_date:
            exif["date"] = shown_date
        lw, lh = fit_long_edge(w, h, cfg["large_size"])
        entry = {
            "date": exif.get("date"),
            "sort_date": exif.get("date"),
            "stem": key,
            "rec": {
                "src": f"img/large/{rel}",
                "thumb": f"img/thumb/{rel}",
                "w": lw,
                "h": lh,
                "title": caption or ext_cache[key].get("caption", ""),
                "album": album["slug"] if album else None,
                "exif": {f: exif[f] for f in cfg["exif_fields"] if f in exif},
            },
        }
        if wide:
            entry["rec"]["mid"] = f"img/mid/{rel}"
        entries.append(entry)
        if album:
            album["entries"].append(entry)

    def claim(key: str, album: dict | None) -> str:
        """A unique output path for one photo within its album."""
        folder = album["slug"] if album else "_root"
        name, n = key, 2
        while f"{folder}/{name}" in used_paths:
            name, n = f"{key}-{n}", n + 1
        used_paths.add(f"{folder}/{name}")
        seen_ext_keys.add(key)
        return f"{folder}/{name}.webp"

    for url_file in discover_url_files():
        rel_dir = url_file.relative_to(PHOTOS_DIR).parent.parts
        for url, caption, date in read_url_list(url_file):
            album = get_album(rel_dir, albums, used_album_slugs)
            # One urls.txt line can be a whole shared album. Lines that turned out to be albums are
            # remembered, so later builds re-read just those (photos you add to the album in Google
            # show up on the next push) while an ordinary photo still costs no requests at all.
            marker = "album-" + hashlib.sha1(url.encode()).hexdigest()[:12]
            plain_key = "ext-" + hashlib.sha1(url.encode()).hexdigest()[:12]
            fetched, shared, album_title, source = None, [], "", ""
            try:
                if marker in ext_cache or not ext_outputs_ready(ext_cache, img_dir, plain_key):
                    fetched = http_get(url)
                    # Not named `html`: that would shadow the stdlib module of the same name, which
                    # the page assembly further down needs for html.escape().
                    page_html = fetched[2][:MAX_HTML_BYTES].decode("utf-8", "ignore")
                    shared = google_shared_photos(page_html)
                    source = "Google Photos"
                    album_title = google_album_title(page_html) if shared else ""
                    if not shared:  # not a Google album; see if it's an Immich share instead
                        m = IMMICH_SHARE_URL.match(fetched[0])
                        if m:
                            shared, album_title = immich_shared_photos(m["server"], m["key"])
                            source = "Immich"
            except Exception:
                fetched = None  # let the ordinary path below report the failure properly

            if len(shared) > 1:  # a shared album: one entry per photo, in the order it lists them
                if not album:
                    # The line sits in photos/ itself, so there's no folder to name the album after.
                    # Make one, titled by whatever was typed on the line, or else by the name the
                    # host gives the album, so the photos show up together on the Albums page.
                    title = caption or album_title or "Shared album"
                    album = make_album((source, url), albums, used_album_slugs, title, None, title)
                    print(f"  {url}: {source} shared album of {len(shared)} photos, added as the "
                          f"album {title!r}")
                else:
                    if caption or date:
                        print(f"note: {url} is a shared album of {len(shared)} photos, so a caption or "
                              f"date on that line is ignored -- it would apply to every photo in it")
                    print(f"  {url}: {source} shared album of {len(shared)} photos, added to "
                          f"{album['title']!r}")
                ext_cache[marker] = {"url": url, "album": len(shared), "slug": album["slug"],
                                     "source": source}
                seen_ext_keys.add(marker)
                for i, spec in enumerate(shared, 1):
                    # Keyed on the host's own photo id, so the cache survives both rebuilds and the
                    # album gaining or losing photos.
                    key = "ext-" + hashlib.sha1(f"{url}::{spec['id']}".encode()).hexdigest()[:12]
                    rel = claim(key, album)
                    thumb, large = img_dir / "thumb" / rel, img_dir / "large" / rel
                    try:
                        if ext_outputs_ready(ext_cache, img_dir, key):
                            cached += 1
                            add_one(key, album, ext_cache[key]["exif"], ext_cache[key]["w"],
                                    ext_cache[key]["h"], ext_cache[key]["wide"], None, "")
                            continue
                        image_url = spec["url"]
                        if source != "Immich":  # Google's listing has no size token, so ask for the original
                            image_url += "=s0"
                        data = http_get(image_url)[2]
                        if not _decode_image(data):
                            raise RuntimeError("the downloaded bytes were not a readable image")
                        # scan(), not _decode_image(): the host may hand over un-rotated pixels plus
                        # an orientation tag (Immich does), and this needs the shape the photo is
                        # actually meant to be seen at, not the shape it happens to be stored at.
                        w, h, exif = scan(data)
                        # Whatever the host told us about the photo, for anything the file lacks.
                        for field, value in spec.get("exif", {}).items():
                            exif.setdefault(field, value)
                        if spec.get("date"):
                            exif.setdefault("date", spec["date"])
                        lw, lh = fit_long_edge(w, h, cfg["large_size"])
                        wide = lw / lh >= cfg["wide_ratio"]
                        render(data, thumb, large, img_dir / "mid" / rel if wide else None, (lw, lh), cfg)
                        ext_cache[key] = {"url": url, "w": w, "h": h, "exif": exif, "wide": wide,
                                          "rel": rel, "resolved": image_url, "caption": "",
                                          "note": f"{source} shared album"}
                        made += 1
                        print(f"  + album photo {i}/{len(shared)}: {w}x{h}")
                        add_one(key, album, exif, w, h, wide, spec.get("date", ""), "")
                    except Exception as e:  # one bad photo shouldn't sink the album, or the build
                        print(f"warning: skipped a photo in {url} ({type(e).__name__}: {e})")
                continue

            try:
                key = plain_key
                rel = claim(key, album)
                thumb, large = img_dir / "thumb" / rel, img_dir / "large" / rel
                if ext_outputs_ready(ext_cache, img_dir, key):
                    cached += 1
                    add_one(key, album, ext_cache[key]["exif"], ext_cache[key]["w"],
                            ext_cache[key]["h"], ext_cache[key]["wide"],
                            f"{date}T00:00:00" if date else None, caption)
                    continue
                data, found = fetch_photo(url, fetched)
                w, h, exif = scan(data)
                from_file = dict(exif)  # what the photo's own bytes carried, for the log below
                # The host's own record of the photo, for anything the file didn't keep.
                for field, value in found["exif"].items():
                    exif.setdefault(field, value)
                lw, lh = fit_long_edge(w, h, cfg["large_size"])
                wide = lw / lh >= cfg["wide_ratio"]
                render(data, thumb, large, img_dir / "mid" / rel if wide else None, (lw, lh), cfg)
                ext_cache[key] = {"url": url, "w": w, "h": h, "exif": exif, "wide": wide,
                                  "rel": rel, "resolved": found["resolved"],
                                  "caption": found["caption"], "note": found["note"]}
                made += 1
                print(f"  + {url} ({w}x{h} via {found['note']})")
                if found["caption"] and not caption:
                    print(f"      caption from the page: {found['caption']!r}")
                for field, value in found["exif"].items():
                    if field not in from_file:  # the file itself had nothing, so this is new
                        print(f"      {field} from the host: {value}")
                # A date typed in urls.txt wins over the host's record and the file's own EXIF, so
                # you can always override.
                add_one(key, album, exif, w, h, wide, f"{date}T00:00:00" if date else None, caption)
            except Exception as e:  # a dead link or a blocked host shouldn't sink the whole build
                print(f"warning: skipped {url} ({type(e).__name__}: {e})")
                continue

    ext_cache = {k: v for k, v in ext_cache.items() if k in seen_ext_keys}  # drop URLs no longer listed
    ext_cache_file.write_text(json.dumps(ext_cache), encoding="utf-8")

    # ---- album dates ---------------------------------------------------------
    # An album's date is the date in its folder name, or failing that the earliest date among its photos.
    # A photo with no date of its own is sorted as if taken at the start of its album's date, so scans
    # without EXIF data stay with their album instead of dropping to the bottom of the Photos tab.
    for a in albums.values():
        a["date"] = a["date"] or next(iter(sorted(e["date"][:7] for e in a["entries"] if e["date"])), None)
        for e in a["entries"]:
            if not e["date"] and a["date"]:
                e["sort_date"] = period_start(a["date"])

    # ---- ordering ------------------------------------------------------------
    order = list(entries)  # already natural-sorted by path
    if cfg["sort"] in ("newest", "oldest"):
        dated = sorted((e for e in order if e["sort_date"]), key=lambda e: e["sort_date"],
                       reverse=cfg["sort"] == "newest")
        order = dated + [e for e in order if not e["sort_date"]]
    elif cfg["sort"] == "random":
        random.shuffle(order)
    position = {id(e): i for i, e in enumerate(order)}

    out_albums = []
    for a in albums.values():
        if not a["entries"]:
            continue
        cover = next((e for e in a["entries"] if e["stem"].startswith("cover")), a["entries"][0])
        out_albums.append({
            "slug": a["slug"],
            "title": a["title"],
            "date": a["date"],
            "cover": position[id(cover)],
            "photos": [position[id(e)] for e in a["entries"]],
        })
    dated_albums = sorted((a for a in out_albums if a["date"]), key=lambda a: date_key(a["date"]), reverse=True)
    other_albums = sorted((a for a in out_albums if not a["date"]), key=lambda a: natural_key(a["title"]))

    data = {
        "site": {k: cfg[k] for k in PUBLIC_KEYS},
        "photos": [e["rec"] for e in order],
        "albums": dated_albums + other_albums,
    }

    # ---- assemble _site/ -----------------------------------------------------
    for child in OUT_DIR.iterdir():  # keep img/ (it is the cache), replace the rest
        if child.name != "img":
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    shutil.copytree(WEB_DIR, OUT_DIR, dirs_exist_ok=True)

    page = (OUT_DIR / "index.html").read_text(encoding="utf-8")
    for key, value in {
        "title": cfg["title"],
        "subtitle": cfg["subtitle"],
        "description": cfg["description"],
        "build": str(int(started)),
    }.items():
        page = page.replace("{{" + key + "}}", html.escape(value, quote=True))
    (OUT_DIR / "index.html").write_text(page, encoding="utf-8")
    (OUT_DIR / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    # ---- remove generated images whose source photo is gone -------------------
    keep = {p for e in entries for p in (e["rec"]["src"], e["rec"]["thumb"], e["rec"].get("mid")) if p}
    for f in img_dir.rglob("*"):
        if f.is_file() and f.name not in (".stamp", EXTERNAL_CACHE_NAME) and f.relative_to(OUT_DIR).as_posix() not in keep:
            f.unlink()
    for d in sorted((p for p in img_dir.rglob("*") if p.is_dir()), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass  # not empty

    print(f"Built {len(entries)} photos in {len(data['albums'])} albums "
          f"({made} new, {cached} unchanged) in {time.time() - started:.1f}s -> {OUT_DIR.name}/")
    if not entries:
        print("No photos yet. Add images to photos/<album name>/ and run this again.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
