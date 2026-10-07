#!/usr/bin/env python3
"""Shared configuration for the Diskover dashboard app and importer."""

import os


def _required_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name!r}. Secrets have no "
            "hardcoded default — set it in the environment (e.g. the systemd "
            "unit's EnvironmentFile)."
        )
    return value


DB_HOST = os.getenv("DISKOVER_DB_HOST", "localhost")
DB_PORT = int(os.getenv("DISKOVER_DB_PORT", 3306))
DB_USER = os.getenv("DISKOVER_DB_USER", "diskover")
DB_PASS = _required_env("DISKOVER_DB_PASS")
DB_NAME = os.getenv("DISKOVER_DB_NAME", "diskover_dashboard")
APP_SECRET_KEY = _required_env("APP_SECRET_KEY")

LDAP_HOST = os.getenv("LDAP_HOST", "nbi.ac.uk")
LDAP_PORT = int(os.getenv("LDAP_PORT", 3268))
LDAP_USE_SSL = os.getenv("LDAP_USE_SSL", "false").lower() == "true"
LDAP_BASE_DN = os.getenv("LDAP_BASE_DN", "DC=nbi,DC=ac,DC=uk")
LDAP_BIND_USER_DN = os.getenv("LDAP_BIND_USER_DN", "CN=ldapuser,OU=NBIPUsers,OU=NBIUsers,DC=nbi,DC=ac,DC=uk")
LDAP_BIND_USER_PASSWORD = _required_env("LDAP_BIND_USER_PASSWORD")
LDAP_ALLOWED_GROUP_DN = os.getenv(
    "LDAP_ALLOWED_GROUP_DN",
    "CN=PLAT-Informatics,OU=JICPlatforms,OU=NBIGroups,DC=nbi,DC=ac,DC=uk",
)
# Membership in ANY of these groups is required just to log in at all
# (checked before the allowed_group_dn admin check above, which is
# unaffected by this). Same groups used to gate flask-hpc-job-stats.
# Semicolon-separated since a DN itself contains commas.
LDAP_REQUIRED_GROUP_DNS = [
    dn.strip()
    for dn in os.getenv(
        "LDAP_REQUIRED_GROUP_DNS",
        "CN=jic-hpc-group,OU=NBIGroups,DC=nbi,DC=ac,DC=uk;"
        "CN=jic-hpc-training,OU=NBIGroups,DC=nbi,DC=ac,DC=uk",
    ).split(";")
    if dn.strip()
]


def get_db_config():
    return {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASS,
        "database": DB_NAME,
        "charset": "utf8mb4",
    }


def get_ldap_config():
    return {
        "host": LDAP_HOST,
        "port": LDAP_PORT,
        "use_ssl": LDAP_USE_SSL,
        "base_dn": LDAP_BASE_DN,
        "bind_user_dn": LDAP_BIND_USER_DN,
        "bind_user_password": LDAP_BIND_USER_PASSWORD,
        "allowed_group_dn": LDAP_ALLOWED_GROUP_DN,
        "required_group_dns": LDAP_REQUIRED_GROUP_DNS,
    }