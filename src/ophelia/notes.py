from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List

from .config import DEFAULT_RUNTIME_ROOT


def add_note(runtime_root: Path, target_type: str, target_id: str, text: str, author: str | None = None, source: str = "cli") -> Dict[str, object]:
    note = {
        "target_type": target_type,
        "target_id": target_id,
        "author": author or os.environ.get("USER") or "unknown",
        "source": source,
        "timestamp": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "text": text,
    }
    path = _notes_path(runtime_root, target_type, target_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    notes = json.loads(path.read_text()) if path.exists() else []
    notes.append(note)
    path.write_text(json.dumps(notes, indent=2, sort_keys=True) + "\n")
    return note


def list_notes(runtime_root: Path, target_type: str, target_id: str) -> List[Dict[str, object]]:
    path = _notes_path(runtime_root, target_type, target_id)
    return json.loads(path.read_text()) if path.exists() else []


def _notes_path(runtime_root: Path, target_type: str, target_id: str) -> Path:
    safe_id = target_id.replace("/", "_")
    return runtime_root / "notes" / target_type / f"{safe_id}.json"
