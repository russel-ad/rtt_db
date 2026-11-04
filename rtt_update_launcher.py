"""PyInstaller entry for the stable, Qt-free updater/launcher."""
from app_update.launcher import main

if __name__ == '__main__':
    raise SystemExit(main())
