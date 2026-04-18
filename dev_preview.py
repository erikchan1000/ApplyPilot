"""Zero-dependency live-preview server for resume PDF styling.

Serves the rendered HTML from pdf.py, watches for source changes,
and auto-reloads the browser via a polling script.

Usage:
    python dev_preview.py [path_to_txt_resume]

Defaults to ~/.applypilot/tailored_resumes/BMO_Software_Developer.txt
"""

import hashlib
import importlib
import json
import sys
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

WATCH_FILES = [
    Path(__file__).parent / "src" / "applypilot" / "scoring" / "pdf.py",
]
DEFAULT_RESUME = (
    Path.home() / ".applypilot" / "tailored_resumes" / "BMO_Software_Developer.txt"
)
PORT = 8787

_current_html = ""
_content_hash = ""
_lock = threading.Lock()

RELOAD_SCRIPT = """
<script>
(function() {
  let lastHash = document.body.dataset.hash;
  setInterval(async () => {
    try {
      const r = await fetch('/__hash');
      const h = await r.text();
      if (h !== lastHash) { location.reload(); }
    } catch(e) {}
  }, 500);
})();
</script>
"""


def _render(resume_path: Path) -> str:
    import applypilot.scoring.pdf as pdf_mod
    from applypilot.config import load_profile

    importlib.reload(pdf_mod)
    text = resume_path.read_text(encoding="utf-8")
    resume = pdf_mod.parse_resume(text)
    try:
        profile = load_profile()
    except FileNotFoundError:
        profile = None
    html = pdf_mod.build_html(resume, profile=profile)
    return html


def _rebuild(resume_path: Path) -> None:
    global _current_html, _content_hash
    try:
        html = _render(resume_path)
        h = hashlib.md5(html.encode()).hexdigest()[:12]
        injected = html.replace(
            "</body>",
            f'<script>document.body.dataset.hash="{h}";</script>{RELOAD_SCRIPT}</body>',
        )
        with _lock:
            _current_html = injected
            _content_hash = h
        print(f"  rebuilt ({h})")
    except Exception as e:
        print(f"  rebuild error: {e}")


def _watch_loop(resume_path: Path) -> None:
    mtimes: dict[str, float] = {}
    for f in WATCH_FILES:
        if f.exists():
            mtimes[str(f)] = f.stat().st_mtime

    while True:
        time.sleep(0.5)
        for f in WATCH_FILES:
            if not f.exists():
                continue
            mt = f.stat().st_mtime
            key = str(f)
            if key not in mtimes or mt != mtimes[key]:
                mtimes[key] = mt
                print(f"  change detected: {f.name}")
                _rebuild(resume_path)


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/__hash":
            with _lock:
                h = _content_hash
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(h.encode())
        elif self.path == "/" or self.path == "/index.html":
            with _lock:
                body = _current_html
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(body.encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def main():
    resume_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RESUME
    if not resume_path.exists():
        print(f"Resume not found: {resume_path}")
        sys.exit(1)

    print(f"Resume: {resume_path}")
    _rebuild(resume_path)

    watcher = threading.Thread(target=_watch_loop, args=(resume_path,), daemon=True)
    watcher.start()

    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Serving at http://localhost:{PORT}")
    print("Edit pdf.py and the browser will auto-reload.\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
