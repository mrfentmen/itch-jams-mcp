import sys
from pathlib import Path

# Make the top-level `server` module importable when running pytest from any cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
