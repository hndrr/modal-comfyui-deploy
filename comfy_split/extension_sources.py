"""Read deployment metadata without importing any optional package."""
from pathlib import Path
import tomllib

_CATALOG = tomllib.loads(Path(__file__).with_name("extension_catalog.toml").read_text())
EXTENSIONS = _CATALOG["extensions"]
INTEGRATIONS = _CATALOG["integrations"]
LEGACY = _CATALOG["legacy"]
NODE_SOURCES = {
    name: INTEGRATIONS[source["integration"]] if "integration" in source else source
    for name, source in _CATALOG["node_packs"].items()
}
NODE_CONFLICTS = _CATALOG.get("node_conflicts", [])


def node_revision(repository):
    return next((source.get("revision") for source in NODE_SOURCES.values()
                 if repository == source["repository"]), None)


def enabled_sources(settings):
    return [(name, EXTENSIONS[name]) for name in settings.extensions] + [
        (name, INTEGRATIONS[name]) for name in settings.integrations]
