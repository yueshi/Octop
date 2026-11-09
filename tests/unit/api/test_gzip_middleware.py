"""The dashboard gzip middleware must only touch text assets."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from octop.api.middleware.gzip import SelectiveGZipMiddleware

BIG = "x" * 4096


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SelectiveGZipMiddleware, minimum_size=1024, compresslevel=6)

    @app.get("/assets/app.js")
    def asset() -> Response:
        return Response(BIG, media_type="text/javascript")

    @app.get("/")
    def index() -> Response:
        return Response(f"<html>{BIG}</html>", media_type="text/html")

    @app.get("/api/data")
    def api_data() -> dict[str, str]:
        return {"data": BIG}

    @app.get("/logo.png")
    def logo() -> Response:
        return Response(b"\x89PNG" + b"x" * 4096, media_type="image/png")

    return app


def test_compresses_text_assets() -> None:
    with TestClient(_app()) as client:
        for path in ("/assets/app.js", "/"):
            response = client.get(path, headers={"Accept-Encoding": "gzip"})
            assert response.status_code == 200
            assert response.headers["content-encoding"] == "gzip"


def test_api_binary_and_range_stay_uncompressed() -> None:
    with TestClient(_app()) as client:
        api = client.get("/api/data", headers={"Accept-Encoding": "gzip"})
        assert api.status_code == 200
        assert "content-encoding" not in api.headers

        png = client.get("/logo.png", headers={"Accept-Encoding": "gzip"})
        assert png.status_code == 200
        assert "content-encoding" not in png.headers

        ranged = client.get(
            "/assets/app.js",
            headers={"Accept-Encoding": "gzip", "Range": "bytes=0-9"},
        )
        assert "content-encoding" not in ranged.headers
