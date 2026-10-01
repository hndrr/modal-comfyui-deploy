"""Install only an extension's static frontend, without native API registration."""
from importlib.resources import files
from pathlib import Path
import shutil

from comfy_split.extension_sources import EXTENSIONS, INTEGRATIONS


def install(name, root=Path('/opt/comfy-extensions')):
    source = {**EXTENSIONS, **INTEGRATIONS}[name]
    assets = files(source['web_package']).joinpath(source['web_directory'])
    destination = root / source['web_name']
    destination.mkdir()
    shutil.copytree(assets, destination / 'web')
    (destination / '__init__.py').write_text('NODE_CLASS_MAPPINGS = {}\nWEB_DIRECTORY = "./web"\n')


if __name__ == '__main__':
    import sys
    install(sys.argv[1])
