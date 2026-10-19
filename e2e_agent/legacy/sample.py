"""The original Flask registration demo, with absolute resource paths."""

from contextlib import contextmanager
from pathlib import Path
from threading import Thread


def create_app():
    from flask import Flask, Response
    app = Flask(__name__)
    html_path = Path(__file__).resolve().parents[1] / "sample_site" / "register.html"

    @app.get("/")
    def index():
        return Response(html_path.read_text(encoding="utf-8"), mimetype="text/html")

    return app


@contextmanager
def sample_server(port: int = 0):
    from werkzeug.serving import make_server
    server = make_server("127.0.0.1", port, create_app(), threaded=True)
    thread = Thread(target=server.serve_forever, name="registration-example", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
