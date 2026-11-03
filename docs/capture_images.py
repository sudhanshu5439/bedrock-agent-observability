"""Screenshots of the architecture diagram and each dashboard tab.

Needs the dashboard running and Chrome installed.
    .venv\\Scripts\\python docs\\capture_images.py                     # -> docs/img, from :8501
    .venv\\Scripts\\python docs\\capture_images.py --url http://localhost:8502 --out docs/screenshots
For public screenshots, run that second dashboard with DASHBOARD_REDACT=1 (see README).
"""
import argparse
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

DOCS = Path(__file__).resolve().parent
IMG = DOCS / "img"
URL = "http://localhost:8501"
TABS = ["Overview", "Configuration", "Metrics", "Sessions & traces", "Audit trail", "Guardrails & errors", "Live test"]


def trim(path, pad=24):
    """Crop the empty white space below and to the right of the content."""
    im = Image.open(path).convert("RGB")
    bbox = ImageChops.difference(im, Image.new("RGB", im.size, (255, 255, 255))).getbbox()
    if bbox:
        im.crop((0, 0, im.width, min(im.height, bbox[3] + pad))).save(path)


def settle(page, seconds):
    time.sleep(seconds)
    page.wait_for_function("!document.querySelector('[data-testid=stStatusWidget]')", timeout=180000)
    time.sleep(2)


def main(sessions):
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True)

        page = browser.new_page(viewport={"width": 1640, "height": 1200}, device_scale_factor=2)
        page.goto((DOCS / "architecture.html").as_uri())
        page.locator(".diagram svg").screenshot(path=IMG / "architecture.png")
        print("architecture.png")
        page.close()

        page = browser.new_page(viewport={"width": 1440, "height": 3000})
        page.goto(URL, timeout=60000)
        page.wait_for_selector("text=Conversations", timeout=180000)
        settle(page, 3)
        for i, tab in enumerate(TABS):
            page.get_by_role("tab", name=tab).click()
            settle(page, 10 if tab in ("Configuration", "Metrics", "Audit trail", "Guardrails & errors") else 4)
            out = IMG / f"tab{i}_{tab.split()[0].lower()}.png"
            page.screenshot(path=out)
            trim(out)
            print(out.name)
        page.close()

        for name, sid in sessions.items():
            page = browser.new_page(viewport={"width": 1440, "height": 3600})
            page.goto(f"{URL}/?session={sid}", timeout=60000)
            page.wait_for_selector("text=Conversations", timeout=180000)
            page.get_by_role("tab", name="Sessions & traces").click()
            settle(page, 15)
            out = IMG / f"session_{name}.png"
            page.screenshot(path=out)
            # keep only the session detail (below the session list)
            im = Image.open(out)
            im.crop((0, 960, im.width, im.height)).save(out)
            trim(out)
            print(out.name)
            page.close()
        browser.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL)
    ap.add_argument("--out", default=str(IMG))
    args = ap.parse_args()
    URL, IMG = args.url.rstrip("/"), Path(args.out).resolve()
    IMG.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(DOCS.parent / "app"))
    import data  # noqa: E402

    calls = data.calls(data.trace_items(168))
    pick = lambda agent, text: calls[(calls.agent_name == agent) & calls.prompt.str.contains(text, na=False)].iloc[0].session_id
    main({"hr": pick("HR Assistant", "E102"), "it": pick("IT Support", "INC-500")})
