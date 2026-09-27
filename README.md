
# PhotoGrid

A photo website for GitHub Pages. Drop photos into folders, push, and the site updates itself.

- **Photos** tab: every photo as a square tile. Click one to open it full size.
- **Albums** tab: one album per folder, with a cover, photo count and date.
- **View controls** next to the tabs: switch between a square grid and each photo's real proportions, and zoom the grid from 50% to 200%.
- Lightweight, Dark, edge-to-edge mosaic in the style of the HTML5 UP "Multiverse" theme.

## Preview on your computer

You need Python 3.9 or newer.

```
pip install -r requirements.txt
python build.py
python -m http.server -d _site
```

Then open http://localhost:8000. 
(Opening `_site/index.html` by Double-clicking doesn't work, because browsers block a page from reading its own data file. The page tells you this if you try.)

## Other Tools and Scripts

`python build.py --reset` copies `site-template.json` over `site.json`. There for developer reset after testing changes.

`set_photo_author.py` is a separate helper that writes authorship tags into your **originals** --
`python set_photo_author.py "Your Name"`. It tags the `.jpg`/`.jpeg`/`.png` files under `photos/`
(and the top level) with an `Artist` , `Copyright` What reaches visitors is decided by the
`metadata` setting in site.json during a normal build.

# Requirements
Python
https://www.python.org/downloads/
Pillow>=10.0
```pip install pillow```
https://pypi.org/project/pillow/



## Publish it

1. Put this folder in a GitHub repository and push it to the `main` branch. (If your default branch is `master`, change `branches: [main]` in `.github/workflows/pages.yml`.)
2. In the repository go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**. You only do this once.
3. Edit `site.json` (title, bio, links), add photos to `photos/`, and push. `site.json`

The workflow in `.github/workflows/pages.yml` builds the site and deploys it. Your address will be `https://<user>.github.io/<repo>/`. Progress shows under the **Actions** tab.

## Adding photos

One folder per album. The folder name is the album name.

```
photos/
  2024-07 Iceland/         album "Iceland", dated July 2024
    DSC_0001.jpg
    Golden hour.jpg        a caption, because the file has a real name
    cover.jpg              used as the album cover
    urls.txt               photos hosted anywhere -- see "Externally hosted photos" below
  Tokyo/
  road-trip/               album "Road Trip"
  loose-photo.jpg          appears in Photos but in no album
  _drafts/                 ignored (anything starting with _ or .)
```

| You do | You get |
| --- | --- |
| Put photos in `photos/<Name>/` | An album called `<Name>` |
| Start a folder name with `2024`, `2024-07` or `2024-07-14` | That album date, removed from the title |
| Use no date in the folder name | The date of the earliest photo (from its EXIF data) |
| Name a file `Golden hour.jpg` | The caption "Golden hour" in the viewer |
| Keep camera names like `IMG_0234.jpg` | No caption |
| Name a file `cover.jpg` | That photo as the album cover (otherwise the first photo) |
| Put a folder inside a folder | A separate album named after the inner folder |
| Delete a photo or folder | It disappears from the site on the next push |

Supported: JPG, PNG, WebP, TIFF. iPhone HEIC files need converting to JPG first.

Photos in an album are ordered by file name (`01-`, `02-` prefixes work). Albums are ordered newest first, with undated albums after.


## Externally hosted photos

A photo doesn't have to live in this repository. Put a file named `urls.txt` in an album folder
(or in `photos/` itself, for photos with no album) with one photo per line:

```
https://photos.immich.app/share/VllMG2ik…                              2025-06-14  Old Signal
https://lh3.googleusercontent.com/pw/AP1Gcz…=w2000                    Golden hour over the lake
```

A line is a URL, then optionally a **date** (`2025-06-14`) and a **caption** after it. Both are
optional, in that order. 
Blank lines and lines starting
with `#` are ignored. 
The first time a URL is built, it's downloaded once and turned into the same
thumbnail/full-size/double-width copies as a local photo -- from then on it behaves exactly like
one: it sorts by its own date if it has one (or its album's date otherwise), can be an album
cover, shows up in both view modes, and so on.

The full-size original is fetched at build time only; it is never stored in the repository, which
is the main reason to use this instead of just adding the file to `photos/` -- a folder of RAW
scans or screenshots can stay out of your git history entirely, at the cost of needing internet
access the first time each one is built.

Any *public* link works -- no account, no API key


**A share link to an album brings in the whole album**, not just one photo -- for both Google
Photos and Immich. The build asks the host what the link covers, then fetches every photo at full
size in the order the album lists them, with each photo's real date and camera details.

Where you put the line decides where the photos land:

- **In an album folder** (`photos/Iceland/urls.txt`) they join that album, alongside its local
  photos. A caption or date on that line is ignored, since it would otherwise apply to every photo.
- **In `photos/` itself** (`photos/urls.txt`) a brand-new album is created for it, named after
  whatever you typed on the line or, failing that, after the name the host gives the album.

Meta data from photos shared by public link will be stripped. f
Some quality may be lost as well in public linking.
Links may break
API linking for meta data support is a planned feature. 


## Settings (`site.json`)

| Key | Meaning |
| --- | --- |
| `title`, `subtitle` | Header text. The title is bold, the subtitle is light. Also used for the browser tab. |
| `description` | Text search engines show under your site name |
| `author`, `bio`, `email`, `links` | Shown in the About panel. Leave `bio`, `email` and `links` empty and the About button disappears. Separate bio paragraphs with a blank line (`\n\n`). |
| `sort` | Order of the Photos tab: `newest` (by the date stored in each photo), `oldest`, `name`, or `random` (reshuffled on every build). A photo with no date of its own is placed as if taken at the start of its album's date (the date in the folder name, or else the earliest photo in the album). Undated photos in an undated album, or in no album, come last. |
| `exif_fields` | What the viewer shows and in what order. Choose from `camera`, `lens`, `focal`, `aperture`, `shutter`, `iso`, `author`, `date`. Use `[]` to show nothing. `author` comes from the photo's own EXIF `Artist` tag, and is always shown at the far right whatever order you list it in. |
| `metadata` | How much of each photo's own EXIF is written into the published `.webp` images: `none` strips all of it, `credits` (the default) keeps just `Artist` and `Copyright`, `all` keeps everything **including GPS**, which publishes exactly where each photo was taken -- the build prints a large warning naming the affected files when you use `all`. The colour profile is never kept in any mode, and `Orientation` is always dropped, because the build already rotates the pixels and converts them to sRGB. |
| `thumb_size` | Thumbnail width and height in pixels (default 480) |
| `large_size` | Longest edge of the full-size copy in pixels (default 2200) |
| `quality` | WebP quality, 1 to 100 (default 80) |

If you change `thumb_size`, `large_size` or `quality`, the next build regenerates every image automatically.



## How it works

```
photos/  ──►  build.py  ──►  _site/  ──►  GitHub Pages
                 │            ├─ index.html, style.css, app.js   (from web/)
                 │            ├─ data.json   (albums, photos, EXIF, your settings)
                 │            └─ img/thumb/, img/mid/, img/large/   (WebP)
                 └─ the only dependency is Pillow
```

- `build.py` finds every image (and every `urls.txt`), turns folders into albums, writes the image sizes and `data.json`. Metadata is kept out of `data.json` entirely, and only `Artist` and `Copyright` reach the published images -- see `metadata` above to change that.
- `web/app.js` fetches `data.json` and draws the page. Views use the URL hash (`#/albums/iceland`), so it works under any repository path with no server setup.
- A local photo's original stays in your repository; visitors only ever download the web-sized copies. A `urls.txt` photo's original isn't stored anywhere in the build at all -- it's downloaded, turned into those same web-sized copies, and the original bytes are then discarded.

### Project layout

```
photos/                  your photos (one folder per album)
site.json                your settings
build.py                 the build script
requirements.txt         Pillow
web/index.html           page shell
web/style.css            theme; colours and sizes are variables at the top
web/app.js               tabs, view modes, zoom, mosaic, albums, viewer, About panel
.github/workflows/       automatic deploy
```

## Planned Features

- **Search or tags:** filter `data.photos` in `photosView()`.
- **Sort by date assending/decending**
- **Creative sorts like hue**
- **API log in to Google Photos / Immich for meta data retreiving of photos or private linking.**
- **Link one image to multiple albums**


## Tips

- GitHub recommends keeping a repository under about 1 GB. If your originals are large, resize them to around 3000 px on the long edge before adding them (the site only uses 2200 px anyway), or keep them out of the repository entirely with `urls.txt` -- see "Externally hosted photos" above.
- A bad or corrupt image is skipped with a warning in the build log rather than stopping the deploy.
- Deploys can take a minute or two. `data.json` is cached by browsers for up to ten minutes, so a very recent change can take a moment to appear for returning visitors.

## Credits

- The look is inspired by [Multiverse](https://html5up.net/multiverse), as used in [rampatra/photography](https://github.com/rampatra/photography). 
- The Albums idea comes from [sunbliss/photorama](https://github.com/sunbliss/photorama). 

All code here is new and none of it is copied from either project.

## License
- Source code is under MIT license. Credit links and/or forks of repo are requested and appreciated. (See LICENSE.txt)
- Images are the creative works of Nikolas Gamarra distribution of the images without credit or for comercial purposes is not permitted (See LICENSE.txt)




