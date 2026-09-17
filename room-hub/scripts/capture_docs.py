"""Capture README images only from the synthetic standalone demos (no server)."""
from __future__ import annotations
import os
import shutil
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    assets = ROOT / 'docs' / 'assets'
    assets.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=os.getenv('CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('google-chrome'),
            args=['--no-sandbox', '--disable-dev-shm-usage'],
        )
        client = browser.new_page(viewport={'width': 1194, 'height': 834}, has_touch=True)
        client.set_content((ROOT / 'previews' / 'client_preview.html').read_text(encoding='utf-8'), wait_until='domcontentloaded')
        client.wait_for_selector('#grid .widget')
        client.wait_for_timeout(200)
        client.screenshot(path=str(assets / 'client.png'))
        client.locator('#grid [data-widget-id=tasks] h2').tap()
        client.wait_for_selector('#focus:not(.hidden) .todo-week-strip')
        client.screenshot(path=str(assets / 'todos.png'))
        manager = browser.new_page(viewport={'width': 1440, 'height': 1000})
        manager.set_content((ROOT / 'previews' / 'manager_preview.html').read_text(encoding='utf-8'), wait_until='domcontentloaded')
        manager.wait_for_selector('#previewFrame')
        frame = manager.locator('#previewFrame').element_handle().content_frame()
        frame.wait_for_selector('#grid .widget')
        manager.wait_for_timeout(200)
        manager.screenshot(path=str(assets / 'manager.png'))
        browser.close()
    print('Wrote three synthetic-data screenshots to docs/assets/.')


if __name__ == '__main__':
    main()
