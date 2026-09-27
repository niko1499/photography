'use strict';
// No framework, no dependencies. Reads data.json (written by build.py) and draws it.
//
//   #/                 all photos
//   #/albums           album covers
//   #/albums/<slug>    one album

const $ = (sel) => document.querySelector(sel);
const view = $('#view');
const lb = $('#lightbox');
const lbImg = $('#lb-img');
const lbStage = $('#lb-stage');
const about = $('#about');
const BASE_TITLE = document.title;

let data = { site: {}, photos: [], albums: [] };
let albums = new Map();

// View mode: 'grid' = square tiles, 'fit' = every photo in its original proportions.
let mode = 'grid';
try { if (localStorage.getItem('view') === 'fit') mode = 'fit'; } catch { /* storage blocked: fine */ }

// ---- tiny helpers -----------------------------------------------------------
/** h('a', {href: '#'}, 'text', child) -> element. Builds DOM safely (no innerHTML). */
function h(tag, props = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null || v === false) continue;
    el.setAttribute(k, v === true ? '' : v);
  }
  el.append(...kids.flat().filter((k) => k != null && k !== false));
  return el;
}

const plural = (n) => `${n} photo${n === 1 ? '' : 's'}`;
const setTitle = (part) => { document.title = part ? `${part} – ${BASE_TITLE}` : BASE_TITLE; };

/** 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD' -> readable, in the visitor's language. */
function fmtDate(d) {
  if (!d) return '';
  const [y, m, day] = d.split('-').map(Number);
  if (!m) return String(y);
  const opts = day ? { year: 'numeric', month: 'long', day: 'numeric' } : { year: 'numeric', month: 'long' };
  return new Date(y, m - 1, day || 1).toLocaleDateString(undefined, opts);
}

function loadedImg(src, alt = '') {
  const img = h('img', { alt, loading: 'lazy', decoding: 'async' });
  img.addEventListener('load', () => img.classList.add('in'), { once: true });
  img.src = src;
  if (img.complete) img.classList.add('in'); // already cached (e.g. after switching view)
  return img;
}

function notice(heading, ...paragraphs) {
  return h('div', { class: 'notice' }, h('h1', {}, heading), ...paragraphs);
}

// ---- views --------------------------------------------------------------------
function emptyState() {
  return notice(
    'No photos yet',
    h('p', {}, 'Add images to a folder inside ', h('code', {}, 'photos/'), ' (for example ',
      h('code', {}, 'photos/Iceland/'), '), then push. Each folder becomes an album.'),
  );
}

// Landscape photos are two columns wide in Original view, so they use their bigger copy there.
const tileSrc = (p) => (mode === 'fit' && p.mid) || p.thumb;
const shape = (p) => ({ ratio: p.w / p.h, wide: Boolean(p.mid) });

function photoTile(p, i, showAlbum) {
  const album = showAlbum && p.album ? albums.get(p.album) : null;
  return h('a', { class: 'tile', href: p.src, 'data-i': i },
    loadedImg(tileSrc(p), p.title || `Photo ${i + 1}`),
    album && h('span', { class: 'tag' }, album.title));
}

// Original-proportions layout ("fit" mode).
//
// The columns are grouped into lanes two columns wide. A lane is filled one row at a time:
//   - a landscape photo takes a whole row, so it is as wide as two portraits side by side;
//   - two portraits share a row and are sized to one common height so the row is exactly lane-wide.
// Every row therefore fills its lane exactly: nothing overlaps, nothing is cropped or stretched and there
// are no gaps between pictures. Each new row goes into the shortest lane, so the only unevenness is
// along the bottom edge of the page.
//
// items: [{ratio, wide}]   width: usable width (px)   gap: px between tiles   minCol: smallest column (px)
// returns {rects: [{x, y, w, h}] in item order, height, lanes}
// <pack>
function packLanes(items, width, gap, minCol) {
  const cols = Math.max(1, Math.floor((width + gap) / (minCol + gap))); // what the square grid would use
  const per = cols >= 2 ? 2 : 1;                                        // columns per lane
  const lanes = Math.max(1, Math.floor(cols / per));
  const colW = (width - (lanes * per - 1) * gap) / (lanes * per);
  const laneW = per * colW + (per - 1) * gap;

  // Portraits pair up in order: 1st with 2nd, 3rd with 4th, ... A leftover one goes last, at the bottom edge.
  const pairable = [];
  items.forEach((it, i) => { if (per === 2 && !it.wide) pairable.push(i); });
  const slot = new Map(pairable.map((i, k) => [i, k]));
  const rows = [];
  const tail = [];
  items.forEach((it, i) => {
    if (!slot.has(i)) { rows.push([i]); return; } // landscape, or a lane only one column wide
    const k = slot.get(i);
    if (k % 2) return;                             // already placed with the previous portrait
    if (pairable[k + 1] !== undefined) rows.push([i, pairable[k + 1]]);
    else tail.push([i]);
  });
  rows.push(...tail);

  const bottoms = new Array(lanes).fill(0);
  const rects = new Array(items.length);
  for (const row of rows) {
    let lane = 0; // shortest lane, leftmost on ties
    for (let l = 1; l < lanes; l++) if (bottoms[l] < bottoms[lane] - 1e-6) lane = l;
    const x = lane * (laneW + gap);
    const y = bottoms[lane];
    let h;
    if (row.length === 2) {
      const [a, b] = row;
      h = (laneW - gap) / (items[a].ratio + items[b].ratio);
      const wa = h * items[a].ratio;
      rects[a] = { x, y, w: wa, h };
      rects[b] = { x: x + wa + gap, y, w: laneW - gap - wa, h };
    } else {
      const i = row[0];
      const w = per === 1 || items[i].wide ? laneW : colW;
      h = w / items[i].ratio;
      rects[i] = { x, y, w, h };
    }
    bottoms[lane] += h + gap;
  }
  return { rects, height: Math.max(0, Math.max(...bottoms) - gap), lanes };
}
// </pack>

let relayout = null;
const watcher = new ResizeObserver(() => relayout && relayout());

/** Lay tiles out as a square grid (CSS does the work) or, in 'fit' mode, with packLanes(). */
function mosaic(nodes, items, extraClass = '') {
  const el = h('div', { class: ['grid', extraClass, mode === 'fit' ? 'packed' : ''].filter(Boolean).join(' ') });
  if (mode !== 'fit') {
    const frag = document.createDocumentFragment();
    nodes.forEach((n) => frag.append(n));
    el.append(frag);
    return el;
  }
  const probe = h('div', { class: 'probe' }); // its width is the CSS minimum column width
  el.append(probe, ...nodes);
  let lastWidth = 0;
  relayout = (force) => {
    const width = el.clientWidth;
    if (!width || (!force && Math.abs(width - lastWidth) < 0.5)) return;
    lastWidth = width;
    const gap = parseFloat(getComputedStyle(el).getPropertyValue('--gap')) || 2;
    const { rects, height } = packLanes(items, width - 2 * gap, gap, probe.offsetWidth);
    nodes.forEach((n, i) => {
      const r = rects[i];
      n.style.cssText = `left:${gap + r.x}px;top:${gap + r.y}px;width:${r.w}px;height:${r.h}px`;
    });
    el.style.height = `${height + 2 * gap}px`;
  };
  watcher.observe(el);
  return el;
}

/** Photo mosaic. Clicking a tile opens the viewer on that list of photos. */
function grid(list, showAlbum) {
  const el = mosaic(list.map((p, i) => photoTile(p, i, showAlbum)), list.map(shape));
  el.addEventListener('click', (e) => {
    const a = e.target.closest('a.tile');
    if (!a || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return; // let "open in new tab" work
    e.preventDefault();
    openViewer(list, Number(a.dataset.i));
  });
  return el;
}

function photosView() {
  return data.photos.length ? grid(data.photos, true) : emptyState();
}

function albumsView() {
  if (!data.albums.length) {
    return data.photos.length
      ? notice('No albums yet', h('p', {}, 'Put photos in a folder inside ', h('code', {}, 'photos/'),
        ' and each folder shows up here as an album.'))
      : emptyState();
  }
  const covers = data.albums.map((a) => data.photos[a.cover]);
  const nodes = data.albums.map((a, i) => {
    const date = fmtDate(a.date);
    return h('a', { class: 'tile', href: `#/albums/${a.slug}`, 'data-i': i },
      loadedImg(tileSrc(covers[i])),
      h('div', { class: 'cap' },
        h('b', {}, a.title),
        h('span', {}, plural(a.photos.length)),
        date && h('span', {}, date)));
  });
  return mosaic(nodes, covers.map(shape), 'albums');
}

function albumView(slug) {
  const a = albums.get(slug);
  if (!a) { location.replace('#/albums'); return null; }
  const list = a.photos.map((i) => data.photos[i]);
  const date = fmtDate(a.date);
  const back = h('a', { class: 'back', href: '#/albums' });
  back.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15 5l-7 7 7 7"/></svg>Albums'; // static markup
  setTitle(a.title);
  return h('div', {},
    h('div', { class: 'head' }, back, h('h1', {}, a.title),
      h('div', { class: 'meta' }, h('span', {}, plural(list.length)), date && h('span', {}, date))),
    grid(list, false));
}

// ---- routing ----------------------------------------------------------------
function route(scrollToTop) {
  watcher.disconnect();
  relayout = null;
  const [tab, arg] = location.hash.replace(/^#\/?/, '').split('/');
  let node;
  let active = 'photos';
  if (tab === 'albums') {
    active = 'albums';
    if (arg) node = albumView(arg);
    else { node = albumsView(); setTitle('Albums'); }
  } else {
    node = photosView();
    setTitle('');
  }
  if (!node) return; // redirecting
  view.replaceChildren(node);
  if (relayout) relayout();
  document.querySelectorAll('nav a').forEach((a) => {
    if (a.dataset.tab === active) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
  if (scrollToTop) window.scrollTo(0, 0);
}

// ---- view mode toggle ------------------------------------------------------------
const modeButtons = document.querySelectorAll('.modes button');
const barBottom = () => $('.bar').getBoundingClientRect().bottom;

function syncModeButtons() {
  modeButtons.forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.mode === mode)));
}

/** The tile sitting nearest the top of the screen, so we can keep it there after the layout changes. */
function topTile() {
  const limit = barBottom();
  let best = null;
  for (const t of view.querySelectorAll('a.tile[data-i]')) {
    const r = t.getBoundingClientRect();
    if (r.bottom > limit && (!best || r.top < best.top - 1)) best = { i: t.dataset.i, top: r.top };
  }
  return best && { i: best.i, offset: best.top - barBottom() };
}

function setMode(next) {
  if (next === mode) return;
  const anchor = topTile();
  mode = next;
  try { localStorage.setItem('view', mode); } catch { /* storage blocked: fine */ }
  syncModeButtons();
  route(false);
  const t = anchor && view.querySelector(`a.tile[data-i="${anchor.i}"]`);
  if (t) window.scrollBy(0, t.getBoundingClientRect().top - barBottom() - anchor.offset);
}

modeButtons.forEach((b) => b.addEventListener('click', () => setMode(b.dataset.mode)));

// ---- zoom --------------------------------------------------------------------
// Scales --tile and --album (see style.css), so it works the same way in both view modes.
const ZOOM_MIN = 50;
const ZOOM_MAX = 200;
const ZOOM_DEFAULT = 100;
const ZOOM_SNAP = 10; // how close to 100% counts as "close enough" to snap to it
const zoomInput = $('#zoom-range');

/** Move the handle and redraw at the new size. Assumes v is already clamped. */
function applyZoom(v) {
  zoomInput.value = v;
  view.style.setProperty('--zoom', v / 100);
  if (relayout) relayout(true); // force: the container itself hasn't resized, only --zoom has
}

const clampZoom = (v) => Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, v));

// Dragging the slider itself is the one place that snaps near 100%, so it stays a deliberate,
// occasional "click" into place rather than a magnet that keyboard/wheel steps keep bumping into.
zoomInput.addEventListener('input', () => {
  let v = clampZoom(Number(zoomInput.value));
  if (Math.abs(v - ZOOM_DEFAULT) <= ZOOM_SNAP) v = ZOOM_DEFAULT;
  applyZoom(v);
});

/** A single step from the keyboard or the wheel: clamp only, no snapping. */
function nudgeZoom(delta) {
  applyZoom(clampZoom(Number(zoomInput.value) + delta));
}

// Ctrl +/- zoom the photos instead of the whole page. Disabled while a dialog covers them.
addEventListener('keydown', (e) => {
  if (!e.ctrlKey || lb.open || about.open) return;
  if (e.key === '+' || e.key === '=') { e.preventDefault(); nudgeZoom(10); }
  else if (e.key === '-' || e.key === '_') { e.preventDefault(); nudgeZoom(-10); }
});

// Ctrl+scroll also zooms, but only over the photos themselves — scrolling the header does nothing.
view.addEventListener('wheel', (e) => {
  if (!e.ctrlKey) return;
  e.preventDefault();
  nudgeZoom(-Math.sign(e.deltaY) * 5);
}, { passive: false });

// ---- photo viewer ------------------------------------------------------------
const viewer = { list: [], i: 0 };

function openViewer(list, i) {
  viewer.list = list;
  viewer.i = i;
  lb.classList.toggle('single', list.length < 2);
  showPhoto();
  lb.showModal();
  history.pushState({ viewer: true }, ''); // so the Back button closes the viewer
}

function showPhoto() {
  const { list, i } = viewer;
  const p = list[i];
  const n = list.length;
  const album = p.album && albums.get(p.album);

  lbImg.src = p.src;
  lbImg.alt = p.title || `Photo ${i + 1} of ${n}`;
  $('#lb-count').textContent = n > 1 ? `${i + 1} / ${n}` : '';

  const name = p.title || (album && album.title) || '';
  const sub = p.title && album ? album.title : '';
  $('#lb-where').replaceChildren(...[name && h('b', {}, name), sub && h('span', {}, sub)].filter(Boolean));
  // Author always sits at the far right, whatever order exif_fields is in.
  const exif = Object.entries(p.exif || {});
  $('#lb-exif').replaceChildren(
    ...exif.filter(([k]) => k !== 'author').map(([k, v]) => h('span', {}, k === 'date' ? fmtDate(v.slice(0, 10)) : v)),
    ...exif.filter(([k]) => k === 'author').map(([, v]) => h('span', {}, v)));

  for (const d of [1, -1]) { // warm the cache for the next/previous photo
    const near = list[(i + d + n) % n];
    if (near) new Image().src = near.src;
  }
}

function step(d) {
  const n = viewer.list.length;
  if (n < 2) return;
  viewer.i = (viewer.i + d + n) % n;
  showPhoto();
}

$('#lb-close').addEventListener('click', () => lb.close());
$('#lb-prev').addEventListener('click', () => step(-1));
$('#lb-next').addEventListener('click', () => step(1));
lb.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowRight') step(1);
  else if (e.key === 'ArrowLeft') step(-1);
});
lb.addEventListener('close', () => {
  lbImg.removeAttribute('src');
  if (history.state && history.state.viewer) history.back();
});
addEventListener('popstate', () => { if (lb.open) lb.close(); });

// Swipe left/right on touch screens; click on the empty area around the photo to close.
let start = null;
let swiped = false;
lbStage.addEventListener('pointerdown', (e) => { start = { x: e.clientX, y: e.clientY }; });
lbStage.addEventListener('pointercancel', () => { start = null; });
lbStage.addEventListener('pointerup', (e) => {
  if (!start) return;
  const dx = e.clientX - start.x;
  const dy = e.clientY - start.y;
  start = null;
  if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy) * 1.5) {
    swiped = true;
    step(dx < 0 ? 1 : -1);
  }
});
lb.addEventListener('click', (e) => {
  if (swiped) { swiped = false; return; }
  if (e.target === lb || e.target === lbStage) lb.close();
});

// ---- about drawer & footer -----------------------------------------------------
function initAbout(site) {
  const paragraphs = (site.bio || '').split(/\n\s*\n/).map((s) => s.trim()).filter(Boolean);
  const links = [...(site.links || [])];
  if (site.email) links.push({ label: 'Email', url: `mailto:${site.email}` });
  if (!paragraphs.length && !links.length) return; // nothing to show, so no About button

  $('#about-body').append(
    h('h2', {}, site.author || site.title),
    ...paragraphs.map((t) => h('p', {}, t)),
    links.length ? h('ul', {}, ...links.map((l) => h('li', {},
      h('a', { href: l.url, ...(l.url.startsWith('http') ? { target: '_blank', rel: 'noopener' } : {}) }, l.label)))) : null,
  );
  const open = $('#about-open');
  open.hidden = false;
  open.addEventListener('click', () => about.showModal());
  $('#about-close').addEventListener('click', () => about.close());
  about.addEventListener('click', (e) => { if (e.target === about) about.close(); });
}

// ---- start ---------------------------------------------------------------------
async function init() {
  try {
    const res = await fetch(`data.json?v=${document.documentElement.dataset.build}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch (err) {
    view.replaceChildren(location.protocol === 'file:'
      ? notice('Open this site through a web server',
        h('p', {}, 'Browsers block a page opened straight from disk from reading its photo list. Run ',
          h('code', {}, 'python build.py'), ' then ', h('code', {}, 'python -m http.server -d _site'),
          ' and visit ', h('code', {}, 'http://localhost:8000'), '.'))
      : notice('Couldn’t load the photo list',
        h('p', {}, 'data.json is missing or unreadable. If you just deployed, wait a minute and reload; otherwise check the build log.')));
    return;
  }
  albums = new Map(data.albums.map((a) => [a.slug, a]));
  initAbout(data.site);
  $('.modes').hidden = $('.zoom').hidden = !data.photos.length; // nothing to lay out yet
  syncModeButtons();
  if (data.site.author) $('#foot').textContent = `© ${new Date().getFullYear()} ${data.site.author}`;
  addEventListener('hashchange', () => route(true));
  route(false);
}

init();
