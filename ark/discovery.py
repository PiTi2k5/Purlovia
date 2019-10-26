from logging import NullHandler, getLogger
from pathlib import Path
from typing import *

import ue.hierarchy
from automate.ark import ArkSteamManager
from config import ConfigFile, get_global_config
from ue.loader import AssetLoader, AssetLoadException
from utils.cachefile import cache_data

from .asset import findSubComponentParentPackages
from .common import CHR_PKG, DCSC_PKG
from .tree import inherits_from, walk_parents

__all__ = [
    'SpeciesDiscoverer',
]

logger = getLogger(__name__)
logger.addHandler(NullHandler())


class ByRawData:
    '''Very fast/cheap method for bulk searching. Over-selects slightly.'''
    def __init__(self, loader: AssetLoader):
        self.loader = loader

    def is_species(self, assetname: str):
        '''Use binary string matching to check if an asset is a character.'''
        # Load asset as raw data
        mem, _ = self.loader.load_raw_asset(assetname)

        # Check for the presence of required string
        result = b'ShooterCharacterMovement' in mem.obj  # type: ignore # just a bad type definition

        return result

    def is_structure(self, assetname: str):
        '''Use binary string matching to check if an asset is a placeable structure.'''
        # Load asset as raw data
        mem, _ = self.loader.load_raw_asset(assetname)

        # Check for the presence of required string
        result = b'StructureMesh' in mem.obj  # type: ignore # just a bad type definition

        return result

    def is_inventory_item(self, assetname: str):
        '''Use binary string matching to check if an asset is an inventory item.'''
        # Load asset as raw data
        mem, _ = self.loader.load_raw_asset(assetname)

        # Check for the presence of required string
        result = b'DescriptiveNameBase' in mem.obj  # type: ignore # just a bad type definition

        return result


class ByInheritance:
    '''Totally accurate but expensive method, to be used to verify results from other discovery methods.'''
    def __init__(self, loader: AssetLoader):
        self.loader = loader

    def is_species(self, assetname: str):
        '''
        Load the asset fully and check that it inherits from Character and it or one of
        its parents has a component that inherits from DCSC.
        '''
        if not assetname.startswith('/Game'):
            return False

        asset = self.loader[assetname]

        # Must inherit from Character somewhere down the line
        if not inherits_from(asset, CHR_PKG):
            return False

        # Check all parents - if any has a sub-component that inherits from DCSC, we're good
        def check_component(assetname: str):
            if not assetname.startswith('/Game'):
                return False

            try:
                asset = self.loader[assetname]

                for cmpassetname in findSubComponentParentPackages(asset):
                    if not cmpassetname.startswith('/Game'):
                        continue
                    cmpasset = self.loader[cmpassetname]
                    if inherits_from(cmpasset, DCSC_PKG):
                        return True  # finish walk early

            except AssetLoadException as ex:
                logger.warning("Failed to check inheritance of potential species: %s", str(ex))
                return False  # abort early

        # Check this asset first
        if check_component(assetname):
            return True

        # Then check all parents in the tree
        found_dcsc = walk_parents(asset, check_component)

        return found_dcsc

    # def is_inventory_item(self, assetname: str):
    #     '''
    #     Load the asset fully and check that it inherits from PrimalItem and it or one of
    #     its parents has a component that inherits from DCSC.
    #     '''
    #     if not assetname.startswith('/Game'):
    #         return False

    #     asset = self.loader[assetname]


class SpeciesDiscoverer:
    def __init__(self, loader: AssetLoader):
        self.loader = loader
        self.testByRawData = ByRawData(loader)
        self.testByInheriance = ByInheritance(loader)

        self.global_excludes = tuple(set(get_global_config().optimisation.SearchIgnore))

    def _filter_species(self, assetname: str) -> bool:
        return self.testByRawData.is_species(assetname) and self.testByInheriance.is_species(assetname)

    def discover_vanilla_species(self) -> Iterator[str]:
        # Scan /Game, excluding /Game/Mods and any excludes from config
        for species in self.loader.find_assetnames('.*', '/Game', exclude=('/Game/Mods/.*', *self.global_excludes)):
            if self._filter_species(species):
                yield species

        # Scan /Game/Mods/<modid> for each of the official mods, skipping ones in SeparateOfficialMods
        official_modids = set(get_global_config().official_mods.ids())
        official_modids -= set(get_global_config().settings.SeparateOfficialMods)
        for modid in official_modids:
            for species in self.loader.find_assetnames('.*', f'/Game/Mods/{modid}', exclude=self.global_excludes):
                if self._filter_species(species):
                    yield species

    def discover_mod_species(self, modid: str) -> Iterator[str]:
        # Scan /Game/Mods/<modid>
        for species in self.loader.find_assetnames('.*', f'/Game/Mods/{modid}', exclude=self.global_excludes):
            if self._filter_species(species):
                yield species


def initialise_hierarchy(arkman: ArkSteamManager, config: ConfigFile = get_global_config()):
    version_key = _gather_version_data(arkman, config)
    loader = arkman.getLoader()
    gen_fn = lambda _: _generate_hierarchy(loader)
    data = cache_data(version_key, 'purlovia_asset_hierarchy', gen_fn, force_regenerate=config.dev.ClearHierarchyCache)
    ue.hierarchy.tree = data


def _gather_version_data(arkman: ArkSteamManager, config: ConfigFile):
    # Gather identities and versions of all involved components
    key = dict(core=dict(version=arkman.getGameVersion(), buildid=arkman.getGameBuildId()),
               mods=dict((modid, arkman.getModData(modid)['version']) for modid in config.mods))
    return key


def _generate_hierarchy(loader: AssetLoader):
    config = get_global_config()

    excludes = set(['/Game/Mods/.*', *config.optimisation.SearchIgnore])

    # Always load the internal hierarchy
    ue.hierarchy.tree.clear()
    ue.hierarchy.load_internal_hierarchy(Path('config') / 'hierarchy.yaml')

    # Scan /Game, excluding /Game/Mods and any excludes from config
    ue.hierarchy.explore_path('/Game', loader, excludes)

    # Scan /Game/Mods/<modid> for each of the official mods, skipping ones in SeparateOfficialMods
    official_modids = set(config.official_mods.ids())
    official_modids -= set(config.settings.SeparateOfficialMods)
    for modid in official_modids:
        ue.hierarchy.explore_path(f'/Game/Mods/{modid}', loader, excludes)

    # Scan /Game/Mods/<modid> for each configured mod
    for modid in config.mods:
        ue.hierarchy.explore_path(f'/Game/Mods/{modid}', loader, excludes)

    return ue.hierarchy.tree
