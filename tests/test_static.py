"""Static files the browser fetches on its own.

The web manifest used to list icons from the site root, where nothing served
them, so installing Earr to a home screen showed a blank icon.
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient


def test_every_manifest_icon_is_served(client: TestClient):
    manifest = json.loads(Path("static/site.webmanifest").read_text())

    for icon in manifest["icons"]:
        # icon paths are relative to the manifest, which lives under /static
        response = client.get(f"/static/{icon['src']}")

        assert response.status_code == 200, icon["src"]
        assert response.headers["content-type"] == icon["type"]


def test_the_manifest_opens_the_app_not_the_static_folder():
    manifest = json.loads(Path("static/site.webmanifest").read_text())

    assert manifest["name"] == "Earr"
    assert manifest["start_url"] == "../"
