"""Download and extract pdf.js pre-built viewer into assets/pdfjs/.

Usage: python tools/fetch_pdfjs.py
"""
import io
import shutil
import urllib.request
import zipfile
from pathlib import Path

VERSION = "4.9.155"
URL = f"https://github.com/nicbarker/pdfjs-serverless/releases/download/v{VERSION}/pdfjs-{VERSION}-dist.zip"
FALLBACK_URL = f"https://github.com/nicbarker/pdfjs-serverless/archive/refs/tags/v{VERSION}.zip"

# The official Mozilla release
MOZILLA_URL = f"https://github.com/nicbarker/pdfjs-serverless/releases/download/v{VERSION}/pdfjs-{VERSION}-dist.zip"

ASSETS = Path(__file__).resolve().parents[1] / "src" / "knowledge_base_builder" / "assets" / "pdfjs"


def fetch_mozilla_pdfjs():
    """Fetch the official Mozilla pdf.js pre-built release."""
    # Use the official Mozilla GitHub release
    ver = "4.7.76"  # Latest stable ES5-compatible build
    url = f"https://github.com/nicbarker/pdfjs-serverless/releases/download/v{ver}/pdfjs-{ver}-dist.zip"

    print(f"Trying Mozilla pdfjs-dist from npm via cdn...")
    # Use cdnjs/unpkg for the pre-built files
    base = f"https://cdn.jsdelivr.net/npm/pdfjs-dist@{ver}"
    files = {
        "build/pdf.min.mjs": f"{base}/build/pdf.min.mjs",
        "build/pdf.worker.min.mjs": f"{base}/build/pdf.worker.min.mjs",
    }

    ASSETS.mkdir(parents=True, exist_ok=True)
    build_dir = ASSETS / "build"
    build_dir.mkdir(exist_ok=True)

    for rel, url in files.items():
        dest = ASSETS / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"  {rel}...")
        urllib.request.urlretrieve(url, str(dest))
        print(f"    {dest.stat().st_size:,} bytes")

    # Create a minimal viewer.html that loads pdf.js
    web_dir = ASSETS / "web"
    web_dir.mkdir(exist_ok=True)
    viewer_html = web_dir / "viewer.html"
    viewer_html.write_text(VIEWER_HTML, encoding="utf-8")
    print(f"  web/viewer.html written ({len(VIEWER_HTML):,} bytes)")

    print("Done.")


VIEWER_HTML = r"""<!DOCTYPE html>
<html dir="ltr" lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PDF Viewer</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
html,body{height:100%;overflow:hidden;background:#525659;font-family:sans-serif}
#toolbar{display:flex;align-items:center;gap:8px;padding:4px 8px;background:#474747;color:#fff;font-size:13px;height:32px;user-select:none}
#toolbar button{background:none;border:1px solid #666;color:#ccc;padding:2px 8px;cursor:pointer;border-radius:3px;font-size:13px}
#toolbar button:hover{background:#555}
#toolbar input[type=number]{width:48px;text-align:center;background:#333;color:#fff;border:1px solid #666;border-radius:3px;padding:1px 4px;font-size:13px}
#toolbar select{background:#333;color:#fff;border:1px solid #666;border-radius:3px;padding:1px 4px;font-size:13px}
#viewer{position:absolute;top:32px;bottom:0;left:0;right:0;overflow:auto;background:#525659}
.page-container{margin:8px auto;background:#fff;box-shadow:0 2px 8px rgba(0,0,0,.3);position:relative}
canvas{display:block}
#loading{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);color:#aaa;font-size:16px}
</style>
</head>
<body>
<div id="toolbar">
  <button id="prev" title="Previous">&lsaquo;</button>
  <input id="pageNum" type="number" value="1" min="1">
  <span>/ <span id="pageCount">-</span></span>
  <button id="next" title="Next">&rsaquo;</button>
  <span style="flex:1"></span>
  <select id="zoom">
    <option value="page-width">Page Width</option>
    <option value="page-fit">Page Fit</option>
    <option value="0.5">50%</option>
    <option value="0.75">75%</option>
    <option value="1" selected>100%</option>
    <option value="1.25">125%</option>
    <option value="1.5">150%</option>
    <option value="2">200%</option>
  </select>
</div>
<div id="viewer"><div id="loading">Loading PDF&hellip;</div></div>

<script type="module">
const params = new URLSearchParams(location.search);
const fileUrl = params.get('file');
if (!fileUrl) { document.getElementById('loading').textContent = 'No PDF URL provided.'; }

const pdfjsBase = location.pathname.replace(/\/web\/viewer\.html$/, '');
const pdfjsLib = await import(pdfjsBase + '/build/pdf.min.mjs');
pdfjsLib.GlobalWorkerOptions.workerSrc = pdfjsBase + '/build/pdf.worker.min.mjs';

const viewer = document.getElementById('viewer');
const pageNumEl = document.getElementById('pageNum');
const pageCountEl = document.getElementById('pageCount');
const zoomEl = document.getElementById('zoom');
let pdfDoc = null, currentPage = 1, rendering = false;

async function loadPdf() {
  try {
    pdfDoc = await pdfjsLib.getDocument(fileUrl).promise;
  } catch (e) {
    document.getElementById('loading').textContent = 'Failed to load PDF: ' + e.message;
    return;
  }
  pageCountEl.textContent = pdfDoc.numPages;
  pageNumEl.max = pdfDoc.numPages;
  document.getElementById('loading').remove();
  renderAllPages();
}

function getScale() {
  const val = zoomEl.value;
  if (val === 'page-width' || val === 'page-fit') return val;
  return parseFloat(val);
}

async function renderAllPages() {
  viewer.innerHTML = '';
  for (let i = 1; i <= pdfDoc.numPages; i++) {
    const page = await pdfDoc.getPage(i);
    const scaleVal = getScale();
    let scale;
    const vp0 = page.getViewport({ scale: 1 });
    if (scaleVal === 'page-width') {
      scale = (viewer.clientWidth - 16) / vp0.width;
    } else if (scaleVal === 'page-fit') {
      scale = Math.min((viewer.clientWidth - 16) / vp0.width, (viewer.clientHeight - 16) / vp0.height);
    } else {
      scale = scaleVal;
    }
    const viewport = page.getViewport({ scale });
    const container = document.createElement('div');
    container.className = 'page-container';
    container.style.width = viewport.width + 'px';
    container.style.height = viewport.height + 'px';
    container.dataset.page = i;
    const canvas = document.createElement('canvas');
    canvas.width = viewport.width * (devicePixelRatio || 1);
    canvas.height = viewport.height * (devicePixelRatio || 1);
    canvas.style.width = viewport.width + 'px';
    canvas.style.height = viewport.height + 'px';
    const ctx = canvas.getContext('2d');
    ctx.scale(devicePixelRatio || 1, devicePixelRatio || 1);
    container.appendChild(canvas);
    viewer.appendChild(container);
    await page.render({ canvasContext: ctx, viewport }).promise;
  }
}

viewer.addEventListener('scroll', () => {
  const pages = viewer.querySelectorAll('.page-container');
  const scrollTop = viewer.scrollTop + viewer.clientHeight / 3;
  for (const p of pages) {
    if (p.offsetTop + p.offsetHeight > scrollTop) {
      currentPage = parseInt(p.dataset.page);
      pageNumEl.value = currentPage;
      break;
    }
  }
});

document.getElementById('prev').onclick = () => { if (currentPage > 1) { currentPage--; gotoPage(currentPage); } };
document.getElementById('next').onclick = () => { if (currentPage < pdfDoc.numPages) { currentPage++; gotoPage(currentPage); } };
pageNumEl.onchange = () => { const p = parseInt(pageNumEl.value); if (p >= 1 && p <= pdfDoc.numPages) { currentPage = p; gotoPage(p); } };
zoomEl.onchange = () => renderAllPages().then(() => gotoPage(currentPage));

function gotoPage(n) {
  pageNumEl.value = n;
  const el = viewer.querySelector(`[data-page="${n}"]`);
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

if (fileUrl) loadPdf();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    fetch_mozilla_pdfjs()
