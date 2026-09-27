#!/usr/bin/env python3
"""
set_photo_author.py

Batch-sets author metadata on .jpg/.jpeg and .png files.

Search order:
  1. Photos directly inside the directory the script is run from (top level only).
  2. Photos inside a "photos" subfolder of that directory, searched recursively
     (including all of its subfolders).

After processing photos, if a "site.json" file exists in the run directory,
optionally sets its "author" field to the same name.

Afterwards it offers to tag the same photos with a licence: answer y/n, then
pick from a numbered list of the usual Creative Commons terms (and All Rights
Reserved) ordered from least to most restrictive.

Only the original .jpg/.jpeg/.png files are touched. The generated .webp files
in _site/ are build output: a licence written into them would be stripped again
by the next build, so build.py's "metadata" setting decides what reaches
visitors (see README).

If run with no argument at all, the author name falls back to the "author"
field in site.json, as long as that field exists and isn't still the
placeholder "Your Name". If neither is available, the script exits with an
error asking for one.

Usage:
    python set_photo_author.py "Author Name"
    python set_photo_author.py            # falls back to site.json's "author"

Requires:
    pip install piexif pillow
"""

import argparse
import json
import os
import sys

try:
    import piexif
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo
except ImportError:
    print("Missing dependency. Install with:\n    pip install piexif pillow")
    sys.exit(1)

JPG_EXTS = (".jpg", ".jpeg")
PNG_EXT = ".png"
PHOTO_EXTS = JPG_EXTS + (PNG_EXT,)

# Licences offered at the end of a run, least to most restrictive. Each entry is the
# short form written into the Copyright tag: the full legal text lives at
# creativecommons.org and would add kilobytes to every photo for no benefit.
LICENSES = [
    ("CC0 1.0 Universal (CC0 1.0)",
     "public domain dedication; no rights reserved, anyone may use it freely"),
    ("Public Domain Mark 1.0 (PDM 1.0)",
     "no known copyright; a status marker rather than a licence"),
    ("Attribution 4.0 International (CC BY 4.0)",
     "credit required; commercial and derivative use allowed"),
    ("Attribution-ShareAlike 4.0 International (CC BY-SA 4.0)",
     "credit, and derivatives must carry the same licence"),
    ("Attribution-NoDerivatives 4.0 International (CC BY-ND 4.0)",
     "credit required; no crops or edits without permission"),
    ("Attribution-NonCommercial 4.0 International (CC BY-NC 4.0)",
     "credit required; no selling"),
    ("Attribution-NonCommercial-ShareAlike 4.0 International (CC BY-NC-SA 4.0)",
     "credit, non-commercial, and share alike"),
    ("Attribution-NonCommercial-NoDerivatives 4.0 International (CC BY-NC-ND 4.0)",
     "credit, non-commercial, no derivatives; the strictest CC option"),
    ("All Rights Reserved",
     "default copyright; no public licence granted"),
]


def confirm(prompt):
    """Ask a y/n question until the user gives a clear answer."""
    while True:
        answer = input(prompt).strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False
        print("Please answer 'y' or 'n'.")


def set_jpg_author(filepath, author):
    try:
        exif_dict = piexif.load(filepath)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif_dict["0th"][piexif.ImageIFD.Artist] = author.encode("utf-8")
    exif_bytes = piexif.dump(exif_dict)
    piexif.insert(exif_bytes, filepath)


def set_png_author(filepath, author):
    img = Image.open(filepath)
    img.load()  # force pixel data into memory before we overwrite the file
    existing_text = getattr(img, "text", {}) or {}
    meta = PngInfo()
    for key, value in existing_text.items():
        if isinstance(value, str):
            meta.add_text(key, value)
    meta.add_text("Author", author)
    img.save(filepath, pnginfo=meta)


def set_jpg_copyright(filepath, text):
    """Copyright lives in IFD0. piexif cannot write it to the Exif sub-IFD at all --
    piexif.ExifIFD has no Copyright entry, so d["Exif"][...] raises KeyError: 33432."""
    try:
        exif_dict = piexif.load(filepath)
    except Exception:
        exif_dict = {"0th": {}, "Exif": {}, "GPS": {}, "1st": {}, "thumbnail": None}
    exif_dict["0th"][piexif.ImageIFD.Copyright] = text.encode("utf-8")
    piexif.insert(piexif.dump(exif_dict), filepath)


def set_png_copyright(filepath, text):
    img = Image.open(filepath)
    img.load()  # force pixel data into memory before we overwrite the file
    existing_text = getattr(img, "text", {}) or {}
    meta = PngInfo()
    for key, value in existing_text.items():
        if isinstance(value, str):
            meta.add_text(key, value)
    meta.add_text("Copyright", text)
    img.save(filepath, pnginfo=meta)


def update_photo_licence(filepath, text):
    ext = os.path.splitext(filepath)[1].lower()
    if ext in JPG_EXTS:
        set_jpg_copyright(filepath, text)
    elif ext == PNG_EXT:
        set_png_copyright(filepath, text)


def choose_licence():
    """Show the LICENSES list, least to most restrictive, and return the chosen text."""
    print()
    print("Choose a licence, from least to most restrictive:")
    print()
    width = len(str(len(LICENSES)))
    for i, (name, note) in enumerate(LICENSES, 1):
        print(f"  {i:>{width}}. {name}")
        print(f"      {note}")
    print()
    print("  0. skip; leave the photos without a licence")
    print()
    while True:
        answer = input(f"  Your choice (0-{len(LICENSES)}): ").strip()
        if answer.isdigit() and 0 <= int(answer) <= len(LICENSES):
            if int(answer) == 0:
                return None
            return LICENSES[int(answer) - 1][0]
        print(f"  Please enter a number from 0 to {len(LICENSES)}.")


def apply_licence(photos):
    """Offer to tag the same photos with a licence, chosen from a numbered list."""
    if not photos:
        print("\nNo photos to licence.")
        return
    print()
    if not confirm(f"Also tag those {len(photos)} photo(s) with a licence? [y/n]: "):
        return
    text = choose_licence()
    if text is None:
        print("Skipped; no licence written.")
        return
    print(f'\nWriting the copyright "{text}" to {len(photos)} photo(s)...')
    ok = 0
    for path in photos:
        try:
            update_photo_licence(path, text)
            print(f"  \u2713 {path}")
            ok += 1
        except Exception as e:
            print(f"  \u2717 {path}: {e}")
    print(f"Licensed {ok} of {len(photos)} photo(s).")


def find_top_level_photos(run_dir):
    photos = []
    for entry in os.listdir(run_dir):
        full_path = os.path.join(run_dir, entry)
        if os.path.isfile(full_path) and entry.lower().endswith(PHOTO_EXTS):
            photos.append(full_path)
    return photos


def find_photos_folder_photos(run_dir):
    photos_dir = os.path.join(run_dir, "photos")
    photos = []
    if os.path.isdir(photos_dir):
        for root, _dirs, files in os.walk(photos_dir):
            for name in files:
                if name.lower().endswith(PHOTO_EXTS):
                    photos.append(os.path.join(root, name))
    return photos


def update_photo(filepath, author):
    ext = os.path.splitext(filepath)[1].lower()
    if ext in JPG_EXTS:
        set_jpg_author(filepath, author)
    elif ext == PNG_EXT:
        set_png_author(filepath, author)


def read_site_json(run_dir):
    """Return (data, path). data is a dict, or None if the file is missing,
    unreadable, or not a JSON object."""
    site_json_path = os.path.join(run_dir, "site.json")
    if not os.path.isfile(site_json_path):
        return None, site_json_path
    try:
        with open(site_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None, site_json_path
    if not isinstance(data, dict):
        return None, site_json_path
    return data, site_json_path


def get_author_from_site_json(run_dir):
    """Fallback author lookup used when no CLI argument is given."""
    data, _ = read_site_json(run_dir)
    if data is None:
        return None
    value = data.get("author")
    if isinstance(value, str) and value.strip() and value != "Your Name":
        return value
    return None


def update_site_json(run_dir, author):
    data, site_json_path = read_site_json(run_dir)
    if data is None:
        if os.path.isfile(site_json_path):
            print(f"Could not read {site_json_path} as a JSON object; skipping.")
        return
    if data.get("author") == author:
        print(f'site.json "author" is already "{author}"; nothing to change.')
        return
    if not confirm(f'\nFound {site_json_path}. Set its "author" field to "{author}" too? [y/n]: '):
        return
    try:
        data["author"] = author
        with open(site_json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"Updated {site_json_path}")
    except Exception as e:
        print(f"Failed to update site.json: {e}")


def main():
    # The ticks below are not in cp1252, which is what a plain Windows console uses, and printing
    # one there raises rather than warning. Ask for UTF-8 and fall back to "?" rather than dying
    # half way through a batch. Same guard build.py uses.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="Batch-set author metadata on photos.")
    parser.add_argument(
        "author",
        nargs="?",
        default=None,
        help='Author name to write, e.g. "Jane Doe". If omitted, falls back to '
             'the "author" field in site.json (unless it\'s still "Your Name").',
    )
    # The licence step now always offers itself, so --license is just accepted and ignored
    # rather than becoming an "unrecognised argument" for anyone who typed it before.
    parser.add_argument("--license", action="store_true",
                        help=argparse.SUPPRESS)
    args = parser.parse_args()
    author = args.author

    run_dir = os.getcwd()

    if author is None:
        author = get_author_from_site_json(run_dir)
        if author is None:
            parser.error(
                'No author given, and site.json has no usable "author" field '
                '(missing, empty, or still "Your Name"). Pass a name instead, '
                'e.g.: python set_photo_author.py "Jane Doe"'
            )
        print(f'No author argument given; using "{author}" from site.json.')

    print(f'This will set the author metadata to "{author}" on all .jpg/.jpeg/.png files in:')
    print(f"  - {run_dir} (top level)")
    print(f"  - {os.path.join(run_dir, 'photos')} (and all its subfolders)")
    print("Existing author metadata on those files will be overwritten.")
    if not confirm("Proceed? [y/n]: "):
        print("Aborted. No files were changed.")
        return

    photos = find_top_level_photos(run_dir) + find_photos_folder_photos(run_dir)

    if not photos:
        print("No .jpg/.jpeg/.png photos found.")
    else:
        print(f"Found {len(photos)} photo(s). Updating...")
        for path in photos:
            try:
                update_photo(path, author)
                print(f"  \u2713 {path}")
            except Exception as e:
                print(f"  \u2717 {path}: {e}")

    update_site_json(run_dir, author)

    apply_licence(photos)


if __name__ == "__main__":
    main()
