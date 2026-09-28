"""PyInstaller entry script of the ``cremind-connect`` bundle (see cremind-connect.spec)."""

from cremind_tag.connect.main import main

if __name__ == "__main__":
    raise SystemExit(main())
