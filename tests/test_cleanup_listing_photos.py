import gzip
import json
import sqlite3

import pytest

from scripts.cleanup_listing_photos import candidates


def test_only_archived_unreferenced_directories_are_selected(tmp_path):
    database = tmp_path / "otz.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE listings (platform TEXT, external_id TEXT)")
    connection.execute("INSERT INTO listings VALUES ('NEKRETNINE', 'protected')")
    connection.commit()
    connection.close()
    root = tmp_path / "listings"
    for name in ("protected", "purged", "unrelated"):
        path = root / "NEKRETNINE" / name
        path.mkdir(parents=True)
        (path / "000.jpg").write_bytes(b"photo")
    archive = tmp_path / "archive.gz"
    with gzip.open(archive, "wt") as stream:
        for name in ("protected", "purged"):
            stream.write(json.dumps({"platform": "NEKRETNINE", "external_id": name}) + "\n")
    paths, rows = candidates(archive, database, root)
    assert rows == 2
    assert paths == [root / "NEKRETNINE" / "purged"]


def test_archive_path_traversal_is_rejected_before_removal(tmp_path):
    database = tmp_path / "otz.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE listings (platform TEXT, external_id TEXT)")
    connection.close()
    root = tmp_path / "listings"
    (root / "NEKRETNINE").mkdir(parents=True)
    archive = tmp_path / "archive.gz"
    with gzip.open(archive, "wt") as stream:
        stream.write(json.dumps({"platform": "NEKRETNINE", "external_id": "safe"}) + "\n")
        stream.write(json.dumps({"platform": "NEKRETNINE", "external_id": "../escape"}) + "\n")
    with pytest.raises(ValueError, match="unsafe archive row"):
        candidates(archive, database, root)
