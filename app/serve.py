"""Start the dashboard with its data already warming.

Streamlit imports the app only when the first visitor arrives, so that visitor would wait for every AWS
call. This launcher imports data.py first (which starts fetching in the background), then starts
Streamlit in the same process, so the dashboard reuses the warm caches.

    python app/serve.py [--server.port 8501] [any other streamlit flags]
"""
import sys
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

import data  # noqa: E402,F401  (import starts the warm-up; see the end of data.py)
from streamlit.web import cli  # noqa: E402

try:
    # Streamlit deliberately serves its JavaScript/CSS uncompressed (fine on a LAN). Over a long-distance
    # link the ≈450 KB main bundle plus dozens of chunks made the first visit take ~10 s, so compress them;
    # with a handful of viewers the extra CPU is negligible. Media and range requests are unaffected.
    from streamlit.web.server.starlette import starlette_gzip_middleware as _gzip

    _gzip._should_bypass_gzip = lambda path, base_url="": False
except ImportError:  # older Streamlit without the Starlette server: leave its defaults
    pass

if __name__ == "__main__":
    sys.argv = ["streamlit", "run", str(APP / "dashboard.py"), *sys.argv[1:]]
    sys.exit(cli.main())
