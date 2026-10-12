#!/usr/bin/env python3
"""
Institute-specific settings: names, links, branding and storage layout.

Everything that differs between institutes lives in one folder, INSTITUTE_DIR
(default: ./institute, which holds JIC's own settings):

    institute.yaml   names, links, Diskover/Isilon details, storage layout
    static/          optional — logos/favicons; a file here overrides the
                     same-named file in the app's built-in static/
    templates/       optional — any template here replaces the built-in one
                     (e.g. a data_management.html with your own guidance)

Shared by app.py and the import/crawl scripts. Kept free of DB/LDAP secrets
(unlike config.py) so the crawler can use it on a laptop without them.
"""

import copy
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
INSTITUTE_DIR = os.path.abspath(os.environ.get("INSTITUTE_DIR") or os.path.join(HERE, "institute"))
CONFIG_PATH = os.path.join(INSTITUTE_DIR, "institute.yaml")
STATIC_DIR = os.path.join(INSTITUTE_DIR, "static")
TEMPLATES_DIR = os.path.join(INSTITUTE_DIR, "templates")

# Generic defaults for anything institute.yaml leaves out — deliberately not
# JIC's values, so a minimal config for another site doesn't inherit them.
DEFAULTS = {
    "institute": {
        "name": "Research Institute",
        "short_name": "",
        "app_title": "Storage Dashboard",
        "footer": "Storage dashboard",
        "login_heading": "Sign in",
        "login_hint": "Your directory (LDAP) username, e.g. jdoe",
        "home_url": "",
        "analytics_id": "",
        "app_switcher": [],
        "data_management_guide": False,
    },
    "branding": {
        "header_logo_dark": "",
        "header_logo_light": "",
        "app_logo": "great_data_management.png",
        "login_logo": "",
        "login_background": "",
        "favicon_svg": "favicon.svg",
        "favicon_png": "favicon-32.png",
        "apple_touch_icon": "apple-touch-icon.png",
        "app_logo_sound": "",
    },
    "storage": {
        "owner_area_patterns": [],
        "user_home_patterns": [],
        "area_display_names": {},
        "area_label_prefixes": [],
        "tiers": [],
        "tier_aliases": {},
        "keep_tiers": [],
        "trends_tier_names": {},
    },
    "access": {
        "admin_group_dn": "",
        "required_group_dns": [],
        "group_cn_prefixes": [],
        "group_cn_suggestion_prefix": "",
    },
    "diskover": {
        "url": "",
        "index_roots": {},
        "extra_subdir_indices": [],
    },
    "isilon": {
        "file_prefix": "daily-",
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(out.get(key), dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        elif value is not None:
            out[key] = value
    return out


def load():
    if not os.path.isfile(CONFIG_PATH):
        raise RuntimeError(
            f"No institute config at {CONFIG_PATH!r}. Set INSTITUTE_DIR to a folder "
            "containing institute.yaml (see institute-template/)."
        )
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return _merge(DEFAULTS, yaml.safe_load(f) or {})


SETTINGS = load()
INSTITUTE = SETTINGS["institute"]
BRANDING = SETTINGS["branding"]
STORAGE = SETTINGS["storage"]
ACCESS = SETTINGS["access"]
DISKOVER = SETTINGS["diskover"]
ISILON = SETTINGS["isilon"]


def like_patterns(substrings):
    """["Research Groups", ...] -> ("%Research Groups%", ...) for SQL LIKE."""
    return tuple(f"%{s}%" for s in substrings)
