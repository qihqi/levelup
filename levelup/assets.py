"""Locate frontend files in a checkout or an installed distribution."""
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path


def static_dir() -> Path:
    source = Path(__file__).resolve().parent.parent / "static"
    if (source / "index.html").is_file():
        return source

    # RECORD paths account for both normal and --target wheel installations.
    try:
        installed = distribution("levelup")
    except PackageNotFoundError:
        installed = None
    if installed is not None:
        for entry in installed.files or ():
            if entry.parts[-4:] == ("share", "levelup", "static", "index.html"):
                index = Path(installed.locate_file(entry))
                if index.is_file():
                    return index.resolve().parent
    raise FileNotFoundError("Levelup web assets missing; restore static/ or reinstall the wheel.")
