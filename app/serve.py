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

if __name__ == "__main__":
    sys.argv = ["streamlit", "run", str(APP / "dashboard.py"), *sys.argv[1:]]
    sys.exit(cli.main())
