from logging import NullHandler, getLogger
from pathlib import Path
from typing import *

from ark.export_wiki.consts import KNOWN_KLASS_NAMES
from ark.export_wiki.exporters import PROXY_TYPE_MAP
from ark.export_wiki.map import WorldData
from ark.export_wiki.spawncontainers import get_spawn_entry_container_data
from ark.worldcomposition import SublevelDiscoverer
from automate.ark import ArkSteamManager
from automate.export import _save_as_json, _should_save_json
from automate.version import createExportVersion
from config import ConfigFile, get_global_config
from ue.asset import UAsset
from ue.gathering import gather_properties
from utils.strings import get_valid_filename

logger = getLogger(__name__)
logger.addHandler(NullHandler())

__all__ = [
    'export_map_data',
]


def export_map_data(arkman: ArkSteamManager, modids: Set[str], config: ConfigFile):
    logger.info('Wiki export beginning')
    if config.settings.SkipExtract or config.export_wiki.Skip:
        logger.info('(skipped)')
        return

    # Ensure the output directory exists
    outdir = config.settings.OutputPath
    outdir.mkdir(parents=True, exist_ok=True)

    # Export based on current config
    exporter = Exporter(arkman, modids, config)
    exporter.perform()

    logger.info('Export complete')


class Exporter:
    def __init__(self, arkman: ArkSteamManager, modids: Set[str], config: ConfigFile):
        self.config = config
        self.arkman = arkman
        self.modids = modids
        self.loader = arkman.createLoader()
        self.wc_discoverer = SublevelDiscoverer(self.loader)
        self.game_version = self.arkman.getGameVersion()

    def perform(self):
        self._prepare_versions()

        if self.config.export_wiki.ExportVanillaMaps:
            logger.info('Beginning export of vanilla maps')
            self._export_vanilla()

        for modid in self.modids:
            logger.info(f'Beginning mod {modid} export')
            self._export_mod(modid)

            # Remove assets with this mod's prefix from the cache
            prefix = '/Game/Mods/' + self.loader.get_mod_name('/Game/Mods/' + modid)
            self.loader.wipe_cache_with_prefix(prefix)

    def _prepare_versions(self):
        if not self.game_version:
            raise ValueError("Game not installed or ArkSteamManager not yet initialised")

    def _create_version(self, timestamp: str) -> str:
        return createExportVersion(self.game_version, timestamp)  # type: ignore

    def _export_vanilla(self):
        game_buildid = self.arkman.getGameBuildId()
        version = self._create_version(game_buildid)

        for asset_name in self.config.maps:
            self._export_level(asset_name, version)

    def _export_mod(self, modid: str):
        moddata = self.arkman.getModData(modid)
        if not moddata:
            raise ValueError("Mod not installed or ArkSteamManager not yet initialised")
        version = self._create_version(moddata['version'])

        if int(moddata.get('type', 1)) != 2:
            logger.debug(f'Skipping export of mod {modid}: not a map.')
            return

        map_list = moddata.get('maps', None)
        if map_list:
            for map_name in map_list:
                self._export_level(f'/Game/Mods/{modid}/{map_name}', version, moddata)
        else:
            logger.warning(f'Mod {modid} is missing its list of maps.')

    def _export_level(self, asset_name: str, version: str, moddata: Optional[Dict] = None):
        logger.info(f'Collecting data from a map: {asset_name}')
        # Gather data from the persistent level and create a container
        asset = self.loader[asset_name]
        world_data = WorldData(asset)
        self._gather_data_from_level(asset, world_data)
        del self.loader[asset_name]

        # Load sublevels and gather data from them
        for sublevel in self.wc_discoverer.discover_submaps(asset):
            subasset = self.loader[sublevel]
            self._gather_data_from_level(subasset, world_data)
            del self.loader[sublevel]

        # Gather spawn groups and save the data
        self._gather_spawn_groups(world_data)
        self._export_world_data(world_data, version, moddata)
        del world_data

    def _gather_data_from_level(self, level: UAsset, world_data: WorldData):
        for export in level.exports:
            if str(export.klass.value.name) not in KNOWN_KLASS_NAMES:
                continue

            proxy = gather_properties(export)  # type:ignore
            export_function = PROXY_TYPE_MAP.get(proxy.get_ue_type(), None)
            if export_function:
                export_function(world_data, proxy)  # type:ignore
            else:
                logger.error(f'Unsupported type: no export mapping exists for "{proxy.get_ue_type()}".')
            del proxy

    def _gather_spawn_groups(self, world: WorldData):
        for index in range(len(world.spawnGroups)):
            group_data = get_spawn_entry_container_data(self.loader, world.spawnGroups[index])
            if group_data:
                world.spawnGroups[index] = group_data.as_dict()

    def _export_world_data(self, world_data: WorldData, version: str, moddata: Optional[Dict] = None):
        values: Dict[str, Any] = dict()
        values['map'] = world_data.name

        if moddata:
            filename = f"{moddata['id']}-{moddata['name']}-{world_data.name}"
            filename = get_valid_filename(filename)
            title = moddata['title'] or moddata['name']
            values['mod'] = dict(id=moddata['id'], tag=moddata['name'], title=title)
        else:
            filename = get_valid_filename(world_data.name)

        values['version'] = version
        values.update(world_data.format_for_json())

        fullpath = (self.config.settings.OutputPath / self.config.export_wiki.PublishSubDir / filename).with_suffix('.json')
        self._save_json_if_changed(values, fullpath)

    def _save_json_if_changed(self, values: Dict[str, Any], fullpath: Path):
        changed, version = _should_save_json(values, fullpath)
        if changed:
            pretty = self.config.export_wiki.PrettyJson
            logger.info(f'Saving export to {fullpath} with version {version}')
            values['version'] = version
            _save_as_json(values, fullpath, pretty=pretty)
        else:
            logger.info(f'No changes to {fullpath}')
