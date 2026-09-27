# Put your photos here

One folder per album. The folder name is the album name.

    photos/
      2024-07 Iceland/        ->  album "Iceland", dated July 2024
        DSC_0001.jpg
        Golden hour.jpg       ->  named files get a caption
      Tokyo/
        ...
      loose-photo.jpg         ->  shows up in Photos, but in no album

- A date at the start of a folder name (`2024`, `2024-07`, `2024-07-14`) sets the album date and is
  removed from the title. Without one, the date comes from the photos' EXIF data.
- Folders or files starting with `_` or `.` are ignored. Use `_drafts/` to hide work in progress.
- Name a photo `cover.jpg` to make it the album's cover (otherwise the first photo is used).
- JPG, PNG, WebP and TIFF are supported.
