"""JSON config next to whisper_flow.py.

Release builds ship a config.json; the dev folder has none and keeps its
code defaults. Fields:
  edition : "offline" | "api"   (release edition)
  refine  : bool                (cloud polish on/off; default True)
  api_key : str                 (DeepSeek key entered by the user)
  ask_api : bool                (api edition: prompt on start until set/never)
"""

import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def load():
    try:
        # utf-8-sig: the PowerShell settings panel writes the file with a BOM.
        with open(CONFIG_PATH, "r", encoding="utf-8-sig") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (OSError, ValueError):
        return {}


def save(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
