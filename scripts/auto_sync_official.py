#!/usr/bin/env python3
"""
auto_sync_official.py
Automatically scans official OpenAI models and merges new models into Codex catalog.
Preserves all custom/third-party models (Gemini, Claude, DeepSeek, GLM, MiniMax, etc.).
"""

import copy
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
DEFAULT_CATALOG = CODEX_HOME / "model-catalog-cli-proxy.bridge-test.json"
NATIVE_CACHE = CODEX_HOME / "models_cache.json"
CONFIG_PATH = CODEX_HOME / "config.toml"

# Known official templates & specifications for GPT-6 models
OFFICIAL_TEMPLATES = {
    "gpt-6-sol": {
        "slug": "gpt-6-sol",
        "display_name": "GPT-6-Sol",
        "description": "Built to power complex coding and agentic workflows.",
        "priority": 3,
        "base_template": "gpt-5.6-sol",
        "context_window": 1050000,
        "max_context_window": 1050000,
        "default_reasoning_level": "low",
    },
    "gpt-6-luna": {
        "slug": "gpt-6-luna",
        "display_name": "GPT-6-Luna",
        "description": "Our most efficient model for focused, high-volume tasks.",
        "priority": 5,
        "base_template": "gpt-5.6-luna",
        "context_window": 1050000,
        "max_context_window": 1050000,
        "default_reasoning_level": "medium",
    },
}

def resolve_active_catalog() -> Path:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("model_catalog_json"):
                        parts = line.split("=", 1)
                        if len(parts) == 2:
                            val = parts[1].strip().strip('"').strip("'")
                            p = Path(os.path.expanduser(val))
                            if p.exists():
                                return p
        except Exception:
            pass
    if DEFAULT_CATALOG.exists():
        return DEFAULT_CATALOG
    alt = CODEX_HOME / "model-catalog-cli-proxy.json"
    if alt.exists():
        return alt
    return DEFAULT_CATALOG

def discover_new_official_models(current_slugs: set) -> list:
    discovered = []
    
    # 1. Check known templates that are missing
    for slug, spec in OFFICIAL_TEMPLATES.items():
        if slug not in current_slugs:
            discovered.append({"source": "template", "spec": spec})
            
    # 2. Check native models_cache.json if Codex updated it from cloud
    if NATIVE_CACHE.exists():
        try:
            with open(NATIVE_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                native_models = data.get("models", [])
                for m in native_models:
                    slug = m.get("slug")
                    # Target official GPT/Codex/o-series
                    if slug and slug not in current_slugs and any(slug.startswith(p) for p in ("gpt-", "o1", "o3", "o4", "codex-")):
                        # Avoid duplicates
                        if not any(d.get("spec", {}).get("slug") == slug for d in discovered):
                            discovered.append({"source": "native_cache", "model": m})
        except Exception as e:
            print(f"[sync] warning reading native cache: {e}", file=sys.stderr)
            
    return discovered

def sync_catalog(catalog_path: Path = None, dry_run: bool = False) -> dict:
    if catalog_path is None:
        catalog_path = resolve_active_catalog()
        
    if not catalog_path.exists():
        return {"status": "error", "error": f"Catalog file not found: {catalog_path}"}
        
    try:
        with open(catalog_path, "r", encoding="utf-8") as f:
            cat = json.load(f)
    except Exception as e:
        return {"status": "error", "error": f"Failed to read catalog JSON: {e}"}
        
    models = cat.get("models", [])
    current_slugs = {m.get("slug") for m in models if m.get("slug")}
    
    to_add = discover_new_official_models(current_slugs)
    if not to_add:
        return {
            "status": "unchanged",
            "message": "All official models already present in catalog.",
            "catalog": str(catalog_path),
            "model_count": len(models),
        }
        
    # Build models by cloning templates or using native schema
    model_map = {m.get("slug"): m for m in models}
    new_entries = []
    
    for item in to_add:
        if item["source"] == "native_cache":
            entry = copy.deepcopy(item["model"])
            new_entries.append(entry)
        elif item["source"] == "template":
            spec = item["spec"]
            base_slug = spec.get("base_template")
            base = model_map.get(base_slug)
            if not base:
                # Fallback to any gpt model
                for m in models:
                    if m.get("slug", "").startswith("gpt-"):
                        base = m
                        break
            if base:
                entry = copy.deepcopy(base)
                entry["slug"] = spec["slug"]
                entry["display_name"] = spec["display_name"]
                entry["description"] = spec["description"]
                entry["priority"] = spec.get("priority", 10)
                if "context_window" in spec:
                    entry["context_window"] = spec["context_window"]
                if "max_context_window" in spec:
                    entry["max_context_window"] = spec["max_context_window"]
                if "default_reasoning_level" in spec:
                    entry["default_reasoning_level"] = spec["default_reasoning_level"]
                new_entries.append(entry)
                
    if not new_entries:
        return {"status": "unchanged", "message": "No new entries could be constructed."}
        
    # Reassemble catalog: place new official entries with official models
    # Preserving third-party models
    updated_models = []
    inserted = False
    added_slugs = [e["slug"] for e in new_entries]
    
    for m in models:
        updated_models.append(m)
        if m.get("slug") == "gpt-reserve" and not inserted:
            for e in new_entries:
                updated_models.append(e)
            inserted = True
            
    if not inserted:
        # Prepend to top
        for e in reversed(new_entries):
            updated_models.insert(0, e)
            
    cat["models"] = updated_models
    
    if not dry_run:
        timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
        backup_path = catalog_path.with_name(f"{catalog_path.name}.bak-{timestamp}")
        shutil.copy2(catalog_path, backup_path)
        
        # Atomic write
        tmp_path = catalog_path.with_name(f"{catalog_path.name}.tmp-{os.getpid()}")
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(cat, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, catalog_path)
        
    return {
        "status": "applied" if not dry_run else "planned",
        "catalog": str(catalog_path),
        "added_models": added_slugs,
        "total_models": len(updated_models),
    }

if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    res = sync_catalog(dry_run=dry)
    print(json.dumps(res, indent=2, ensure_ascii=False))
