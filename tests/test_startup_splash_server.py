import importlib.util
import re
import threading
import urllib.request
from pathlib import Path


def _load_splash_server_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts/startup_splash_server.py"
    spec = importlib.util.spec_from_file_location("startup_splash_server", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_splash_server_serves_assets_and_records_dismissal(tmp_path):
    module = _load_splash_server_module()
    splash_root = tmp_path / "splash"
    splash_root.mkdir()
    (splash_root / "startup-splash.html").write_text("OpenFlight", encoding="utf-8")
    dismiss_file = tmp_path / "dismissed"
    server = module.create_server(splash_root, dismiss_file, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/startup-splash.html") as response:
            assert response.read() == b"OpenFlight"

        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/dismiss", data=b"", method="POST"
        )
        with urllib.request.urlopen(request) as response:
            assert response.status == 204
        assert dismiss_file.read_text(encoding="utf-8") == "dismissed\n"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


REPO_ROOT = Path(__file__).resolve().parents[1]
SPLASH_ASSETS = REPO_ROOT / "ui/public"


BODY_STACK = "'Nexa', 'A-Space', Arial, 'Liberation Sans', sans-serif"
DISPLAY_STACK = "'A-Space', Arial, 'Liberation Sans', sans-serif"


def _splash_style() -> str:
    splash = (SPLASH_ASSETS / "startup-splash.html").read_text(encoding="utf-8")
    style = splash[splash.index("<style>") + len("<style>") : splash.index("</style>")]
    return re.sub(r"/\*.*?\*/", "", style, flags=re.DOTALL)


def test_splash_uses_the_ui_stacks_without_font_files():
    style = _splash_style()
    assert "@font-face" not in style

    rules = re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"@font-face\s*\{[^}]*\}", "", style))
    stacks = {
        selector.strip(): re.search(r"font-family:\s*([^;]+);", body).group(1).strip()
        for selector, body in rules
        if "font-family" in body
    }
    # Text: the page's own stack (both :root rules), as in the UI's --font-body.
    root_stacks = re.findall(r":root\s*\{[^}]*font-family:\s*([^;]+);", style)
    assert len(root_stacks) == 2
    assert all(stack.strip() == BODY_STACK for stack in root_stacks)
    # Display: the brand and the title line only, as in the UI's --font-display.
    assert stacks[".startup-brand strong, h1"] == DISPLAY_STACK
    assert set(stacks) == {":root", ".startup-brand strong, h1"}


