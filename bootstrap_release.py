"""Seed a new private API database from the public listings release."""
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from urllib.request import urlopen

import ranker

RELEASE = "https://github.com/SamiHaraketi/wedding-planner-directory/releases/download/database/live.sqlite"


def seed(target: Path):
    if target.exists():
        with sqlite3.connect(target) as db:
            ranker.init(db)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as directory:
        staging = Path(directory) / "seed.sqlite"
        with urlopen(RELEASE, timeout=60) as source, staging.open("wb") as output:
            shutil.copyfileobj(source, output)
        with sqlite3.connect(staging) as db:
            if db.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise RuntimeError("Downloaded directory database failed its integrity check")
            ranker.init(db)
            if not db.execute("SELECT 1 FROM public_listings LIMIT 1").fetchone():
                raise RuntimeError("Downloaded directory contains no eligible listings")
        try:
            os.link(staging, target)
        except FileExistsError:
            pass


if __name__ == "__main__":
    seed(Path(os.environ["WEDDING_DB"]))
