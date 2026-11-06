"""Optional integrations; a plain split deployment needs none of them."""

from dataclasses import dataclass
import os

from comfy_split.extension_sources import EXTENSIONS, INTEGRATIONS, NODE_SOURCES

NODE_PACKS = {name: source["repository"] for name, source in NODE_SOURCES.items()}
DEPLOYMENT_ENV = "SPLIT_DEPLOYMENT_ID"


def switch(environ, name):
    value = environ.get(name, "off").strip().lower()
    if value not in {"on", "off"}:
        raise ValueError(f"{name} must be on or off")
    return value == "on"


def selection(environ, name, allowed):
    values = tuple(dict.fromkeys(part.strip() for part in environ.get(name, "").split(",") if part.strip()))
    if set(values) - set(allowed):
        raise ValueError(f"{name} must select from: {', '.join(allowed)}")
    return values


@dataclass(frozen=True)
class Settings:
    extensions: tuple[str, ...] = ()
    node_packs: tuple[str, ...] = ()
    integrations: tuple[str, ...] = ()

    @classmethod
    def read(cls, environ=None):
        environ = os.environ if environ is None else environ
        return cls(
            selection(environ, "SPLIT_EXTENSIONS", EXTENSIONS),
            selection(environ, "SPLIT_NODE_PACKS", NODE_PACKS),
            tuple(name for name, item in INTEGRATIONS.items()
                  if switch(environ, item["setting"])),
        )

    def environment(self):
        return {"SPLIT_EXTENSIONS": ",".join(self.extensions),
                "SPLIT_NODE_PACKS": ",".join(self.node_packs),
                **{item["setting"]: "on" if name in self.integrations else "off"
                   for name, item in INTEGRATIONS.items()}}

    def secrets(self, environ):
        """Only selected providers receive credentials; names, never values."""
        # A node-pack reference selects files, not its adapter's credentials.
        wanted = [tuple(secret) for name in self.node_packs
                  for secret in _node_secrets(name)]
        wanted.extend(tuple(secret) for name in self.integrations
                      for secret in INTEGRATIONS[name].get("secrets", []))
        return [(name, key, environ[name].strip()) for name, key in dict.fromkeys(wanted)
                if environ.get(name, "").strip()]


def _node_secrets(name):
    source = NODE_SOURCES[name]
    # Sources with an independent runtime switch request secrets only via that switch.
    return () if "setting" in source else source.get("secrets", ())
