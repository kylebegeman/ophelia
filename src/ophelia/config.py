from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = REPO_ROOT / "templates"
DEFAULT_RUNTIME_ROOT = Path.home() / "ophelia-runtime"
