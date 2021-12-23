import os.path
import re
from abc import ABC, abstractmethod
from configparser import ConfigParser
from itertools import islice
from pathlib import Path
from typing import Dict, Iterable, Optional, Set, Tuple, Union

import psutil  # type: ignore

from utils.log import get_logger

from .asset import ExportTableItem, ImportTableItem, UAsset
from .base import UEBase
from .context import get_ctx
from .properties import ObjectProperty, Property
from .stream import MemoryStream

logger = get_logger(__name__)

__all__ = (
    'AssetLoadException',
    'ModNotFound',
    'AssetNotFound',
    'AssetParseError',
    'AssetLoader',
    'load_file_into_memory',
    'ModResolver',
    'IniModResolver',
)

NO_FALLBACK = object()


class AssetLoadException(Exception):
    pass


class ModNotFound(AssetLoadException):
    def __init__(self, mod_name: str):
        super().__init__(f'Mod {mod_name} not found')


class AssetNotFound(AssetLoadException):
    def __init__(self, asset_name: str):
        super().__init__(f'Asset {asset_name} not found')


class ExportNotFound(AssetLoadException):
    def __init__(self, asset_name: str, export_name: str):
        super().__init__(f'Export {export_name} could not be found in asset {asset_name}')


class AssetParseError(AssetLoadException):
    def __init__(self, asset_name: str):
        super().__init__(f'Error parsing asset {asset_name}')


class ModResolver(ABC):
    '''Abstract class a mod resolver must implement.'''
    def initialise(self):
        pass

    @abstractmethod
    def get_name_from_id(self, modid: str) -> Optional[str]:
        pass

    @abstractmethod
    def get_id_from_name(self, name: str) -> Optional[str]:
        pass


class IniModResolver(ModResolver):
    '''Old-style mod resolution by hand-crafted mods.ini.'''
    mods_id_to_names: Dict[str, str]
    mods_names_to_ids: Dict[str, str]

    def __init__(self, filename='mods.ini'):
        self.filename = filename

    def initialise(self):
        config = ConfigParser(inline_comment_prefixes='#;')
        config.optionxform = lambda v: v  # keep exact case of mod names, please
        config.read(self.filename)
        self.mods_id_to_names = dict(config['ids'])
        self.mods_names_to_ids = dict((name.lower(), id) for id, name in config['ids'].items())
        # self.mods_id_to_longnames = dict(config['names'])
        return self

    def get_name_from_id(self, modid: str) -> Optional[str]:
        name = self.mods_id_to_names.get(modid, None)
        return name

    def get_id_from_name(self, name: str) -> Optional[str]:
        modid = self.mods_names_to_ids.get(name.lower(), None)
        return modid


class CacheManager(ABC):
    @abstractmethod
    def lookup(self, name) -> Optional[UAsset]:
        raise NotImplementedError

    @abstractmethod
    def add(self, name: str, asset: UAsset):
        raise NotImplementedError

    @abstractmethod
    def remove(self, name: str):
        raise NotImplementedError

    @abstractmethod
    def wipe(self, prefix: str = ''):
        raise NotImplementedError

    @abstractmethod
    def get_count(self):
        raise NotImplementedError


class DictCacheManager(CacheManager):
    '''A cache manager implementing the old unintelligent mechanism.'''
    def __init__(self):
        self.cache: Dict[str, UAsset] = dict()

    def lookup(self, name: str) -> Optional[UAsset]:
        return self.cache.get(name, None)

    def add(self, name: str, asset: UAsset):
        self.cache[name] = asset

    def remove(self, name):
        del self.cache[name]

    def wipe(self, prefix: str = ''):
        if not prefix:
            self.cache = dict()
        else:
            for name in list(key for key in self.cache if key.startswith(prefix)):
                del self.cache[name]

    def get_count(self):
        return len(self.cache)


class UsageBasedCacheManager(CacheManager):
    '''
    A cache manager that prioritises the most recently used entries.

    We use the guaranteed ordering of Python dicts to track the most recently used entries.
    '''
    def __init__(self, max_count=3000, max_memory=6 * 1024 * 1024 * 1024, keep_count=500):
        self.cache: Dict[str, UAsset] = dict()
        self.max_count = max_count
        self.max_memory = max_memory
        self.keep_count = keep_count

        self.highest_memory_seen = 0

    def lookup(self, name: str):
        '''
        Lookup an asset in the cache.

        Note that this marks it as recently used, and hence less likely to be purged.
        '''
        # Pull out the value, if present
        result = self.cache.pop(name, None)
        if result:
            # Re-insert at the end
            self.cache[name] = result

        return result

    def add(self, name: str, asset: UAsset):
        '''
        Add an asset to the cache, replacing any previous asset with the same name.

        Note that this marks it as recently used, and hence less likely to be purged.
        '''
        # Discard any previous version
        self.cache.pop(name, None)

        # Add to the end of the cache
        self.cache[name] = asset

        # Check if we have too many assets
        self._maybe_purge()

    def remove(self, name: str):
        '''
        Remove the named asset from the cache.
        '''
        logger.debug('Removing cache entry: %s', name)
        found = self.cache.pop(name, None)
        if not found:
            logger.warning('Attempt to remove asset that was not found: %s', name)

    def wipe(self, prefix: str = ''):
        '''
        Remove cache entries that begin with the given prefix.

        An empty or None prefix wipes the entire cache.
        '''
        if not prefix:
            logger.debug('Wiping cache completely')
            # Full wipe
            self.cache = dict()
        else:
            logger.debug('Wiping cache with prefix: %s', prefix)
            to_cull = list(key for key in self.cache if key.startswith(prefix))
            for name in to_cull:
                del self.cache[name]

    def get_count(self):
        return len(self.cache)

    def _maybe_purge(self):
        mem_used = psutil.Process().memory_info().rss
        if mem_used > self.highest_memory_seen:
            self.highest_memory_seen = mem_used

        cache_count = len(self.cache)

        if cache_count >= self.max_count:
            logger.debug("Asset cache purge due to too many items")
            self._purge(cache_count - self.keep_count)
        # elif mem_used >= self.max_memory and cache_count > self.keep_count:
        #     logger.info("Asset cache purge due to high memory usage (with %d items)", cache_count)
        #     self._purge(cache_count - self.keep_count)

    def _purge(self, amount: int):
        to_cull = list(islice(self.cache, amount))
        for name in to_cull:
            del self.cache[name]


class ContextAwareCacheWrapper(CacheManager):
    def __init__(self, submanager: CacheManager):
        self.manager = submanager

    def lookup(self, name) -> Optional[UAsset]:
        current_ctx = get_ctx()
        asset = self.manager.lookup(name)
        if not asset:
            return None

        # Ensure the found asset satisfies the requirements of the current parsing context
        if not asset.is_context_satisfied(current_ctx):
            logger.debug("Re-parsing asset for more data: %s", name)
            return None

        return asset

    def add(self, name: str, asset: UAsset):
        return self.manager.add(name, asset)

    def remove(self, name: str):
        return self.manager.remove(name)

    def wipe(self, prefix: str = ''):
        self.manager.wipe(prefix)

    def get_count(self):
        return self.manager.get_count()


class AssetLoader:
    def __init__(self,
                 *,
                 modresolver: ModResolver,
                 assetpath='.',
                 cache_manager: CacheManager = None,
                 rewrites: Dict[str, str] = dict(),
                 mod_aliases: Dict[str, Set[str]] = dict()):
        self.cache: CacheManager = cache_manager or ContextAwareCacheWrapper(UsageBasedCacheManager())
        self.asset_path = Path(assetpath)
        self.absolute_asset_path = self.asset_path.absolute().resolve()  # need both absolute and resolve here
        self.modresolver = modresolver
        self.modresolver.initialise()
        self.rewrites_to_path = rewrites
        self.rewrites_to_asset = {v: k for k, v in rewrites.items()}
        self.mod_to_aliases = mod_aliases
        self.alias_to_mods: Dict[str, str] = dict()
        for mod_tag, aliases in mod_aliases.items():
            for alias in aliases:
                self.alias_to_mods[alias] = mod_tag

        self.max_memory = 0
        self.max_cache = 0

    def clean_asset_name(self, name: str) -> str:
        # Remove class name, if present
        if '.' in name:
            name = name[:name.index('.')]

        # Clean it up and break it into its parts
        name = name.strip().strip('/').strip('\\').replace('\\', '/')
        parts = name.split('/')

        # Convert mod names to numbers
        if len(parts) > 2 and parts[1].lower() == 'mods' and parts[2].isnumeric():
            mod_name = self.modresolver.get_name_from_id(parts[2])
            if not mod_name:
                raise ModNotFound(parts[2])
            parts[2] = mod_name

        # Change Content back to name, for cache consistency
        if parts and parts[0].lower() == 'content':
            parts[0] = 'Game'

        result = '/' + '/'.join(parts)

        return result

    def wipe_cache(self) -> None:
        self.cache.wipe()

    def wipe_cache_with_prefix(self, prefix: str) -> None:
        self.cache.wipe(prefix)

    def convert_asset_name_to_path(self, name: str, partial=False, ext='.uasset', check_exists=True) -> Optional[Path]:
        '''Get the filename from which an asset can be loaded.'''
        name = self.clean_asset_name(name)

        # Handle any asset path rewrites
        for prefix_from, prefix_to in self.rewrites_to_path.items():
            if name.startswith(prefix_from):
                name = prefix_to + name[len(prefix_from):]
                break

        parts = name.strip('/').split('/')

        # Convert mod names to numbers
        if len(parts) > 2 and parts[1].lower() == 'mods' and not parts[2].isnumeric():
            modid = self.modresolver.get_id_from_name(parts[2])
            if not modid:
                raise ModNotFound(parts[2])
            parts[2] = modid

        # Game is replaced with Content
        if parts and parts[0].lower() == 'game':
            parts[0] = 'Content'

        if not partial:
            parts[-1] += ext

        # Check and return if it exists as-is
        fullpath = Path(self.asset_path, *parts)
        if not check_exists:
            return fullpath

        if partial and fullpath.is_dir():
            return fullpath
        if not partial and fullpath.is_file():
            return fullpath

        # Check for case-insensitive match
        foundPath = find_caseinsensitive_path(self.asset_path, *parts)

        return foundPath

    def get_mod_name(self, assetname: str) -> Optional[str]:
        assert assetname is not None
        assetname = self.clean_asset_name(assetname)
        parts = assetname.strip('/').split('/')
        if len(parts) < 3:
            return None
        if parts[0].lower() != 'game' or parts[1].lower() != 'mods':
            return None
        mod: Optional[str] = parts[2]
        if mod:
            if mod.isnumeric():
                mod = self.modresolver.get_name_from_id(mod)
            else:
                mod = self.alias_to_mods.get(mod, mod)
        return mod

    def get_mod_id(self, assetname: str) -> Optional[str]:
        assert assetname is not None
        assetname = self.clean_asset_name(assetname)
        parts = assetname.strip('/').split('/')
        if len(parts) < 3:
            return None
        if parts[0].lower() != 'game' or parts[1].lower() != 'mods':
            return None
        mod: Optional[str] = parts[2]
        if mod and not mod.isnumeric():
            mod = self.modresolver.get_id_from_name(self.alias_to_mods.get(mod, mod))
        return mod

    def find_assetnames(self,
                        toppath='/',
                        *,
                        include: Union[str, Iterable[str]] = None,
                        exclude: Union[str, Iterable[str]] = None,
                        extension: Union[str, Iterable[str]] = '.uasset',
                        return_extension=False,
                        invert=False):

        includes: Tuple[str, ...] = tuple(include, ) if isinstance(include, str) else tuple(include or ())
        excludes: Tuple[str, ...] = tuple(exclude, ) if isinstance(exclude, str) else tuple(exclude or ())
        extensions: Tuple[str, ...] = tuple((extension, )) if isinstance(extension, str) else tuple(extension or ())
        extensions = tuple(ext.lower() for ext in extensions)
        assert extensions

        toppath = self.convert_asset_name_to_path(toppath, partial=True)
        for path, _, files in os.walk(toppath):
            for filename in files:
                fullpath = os.path.join(path, filename)
                name, ext = os.path.splitext(fullpath)

                if ext.lower() not in extensions:
                    continue

                partialpath = str(Path(fullpath).relative_to(self.asset_path).with_suffix(''))
                assetname = self.clean_asset_name(partialpath)

                # Handle any asset path rewrites
                for prefix_from, prefix_to in self.rewrites_to_asset.items():
                    if assetname.startswith(prefix_from):
                        assetname = prefix_to + assetname[len(prefix_from):]
                        break

                result = (assetname, ext) if return_extension else assetname

                # Apply filtering, starting with forced inclusions
                matched = True
                if any(re.match(include, assetname) for include in includes):
                    # ...skip the exclusion test
                    pass
                # Then handle exclusions with a lower priority
                elif any(re.match(exclude, assetname) for exclude in excludes):
                    matched = False

                # Yield or skip this entry (force bool because xor behaves differently with non-bools)
                if matched ^ bool(invert):
                    yield result

    def load_related(self, obj: UEBase) -> UAsset:
        if isinstance(obj, Property):
            return self.load_related(obj.value)
        if isinstance(obj, ObjectProperty):
            return self.load_related(obj.value.value)
        if isinstance(obj, ImportTableItem):
            assetname = str(obj.namespace.value.name.value)
            loader = obj.asset.loader
            asset = loader[assetname]
            return asset

        raise ValueError(f"Unsupported type for load_related '{type(obj)}'")

    def load_class(self, fullname: str, fallback=NO_FALLBACK, quiet=False) -> ExportTableItem:
        (assetname, cls_name) = fullname.split('.')
        assetname = self.clean_asset_name(assetname)
        asset = self.load_asset(assetname, quiet=quiet)
        for export in asset.exports:
            if str(export.name) == cls_name:
                return export

        if fallback is not NO_FALLBACK:
            return fallback

        raise ExportNotFound(assetname, cls_name)

    def _load_raw_asset_from_file(self, filename: str) -> memoryview:
        '''Load an asset given its filename into memory without parsing it.'''
        if not os.path.isabs(filename):
            filename = os.path.join(self.asset_path, filename)
        try:
            mem = load_file_into_memory(filename)
        except FileNotFoundError:
            raise AssetNotFound(filename)
        return mem

    def load_raw_asset(self, name: str) -> Tuple[memoryview, str]:
        '''
        Load an asset given its asset name into memory without parsing it.
        Returns (memoryview, ext).
        '''
        name = self.clean_asset_name(name)
        mem = None
        for ext in ('.uasset', '.umap'):
            path = self.convert_asset_name_to_path(name, ext=ext)
            if path and path.is_file():
                mem = load_file_into_memory(path)
                return (mem, ext)

        raise AssetNotFound(name)

    def load_asset(self, assetname: str, quiet=False, use_cache=True, cache_result=True) -> UAsset:
        '''Load and parse the given asset, or fetch it from the cache if already loaded.'''
        assetname = self.clean_asset_name(assetname)
        asset = (use_cache and self.cache.lookup(assetname)) or \
            self._load_asset(assetname, quiet=quiet, cache_result=cache_result)

        # Keep track of some stats
        mem_used = psutil.Process().memory_info().rss
        if mem_used > self.max_memory:
            self.max_memory = mem_used
        cache_used = self.cache.get_count()
        if cache_used > self.max_cache:
            self.max_cache = cache_used

        return asset

    def __getitem__(self, assetname: str) -> UAsset:
        '''Load and parse the given asset, or fetch it from the cache if already loaded.'''
        return self.load_asset(assetname)

    def __delitem__(self, assetname: str) -> None:
        '''Remove the specified assetname from the cache.'''
        assetname = self.clean_asset_name(assetname)
        self.cache.remove(assetname)

    def partially_load_asset(self, assetname: str, cache_result=True) -> UAsset:
        asset = self._load_asset(assetname, doNotLink=True, cache_result=cache_result)
        return asset

    def _load_asset(self, assetname: str, doNotLink=False, quiet=False, cache_result=True) -> UAsset:
        if not quiet:
            logger.debug("Loading asset: %s", assetname)
        mem, ext = self.load_raw_asset(assetname)
        try:
            stream = MemoryStream(mem, 0, len(mem))
            asset = UAsset(stream)
            asset.loader = self
            asset.assetname = assetname
            asset.name = assetname.split('/')[-1]
            asset.file_ext = ext

            try:
                asset.deserialise()
                if doNotLink:
                    return asset
                asset.link()
            except Exception as ex:
                raise AssetParseError(assetname) from ex
        finally:
            mem.release()

        leafname = assetname.split('/')[-1]

        # Check only exports with no namespace (top-level ones)
        top_exports = [export for export in asset.exports.values if str(export.namespace) == 'None']

        # Look for a BP-style Default__<assetname> export
        exports = [export for export in top_exports if str(export.name).startswith('Default__')]
        if len(exports) > 1:
            logger.warning(f'Found more than one Default__ entry in {assetname}!')
        asset.default_export = exports[0] if exports else None
        if asset.default_export:
            asset.default_class = asset.default_export.klass.value

        if not asset.default_export:
            # Fall back to an export named the same as the asset with no namespace
            exports = [export for export in top_exports if str(export.name).lower() == leafname.lower()]
            if len(exports) > 1:
                logger.warning(f'Found more than <assetname> export in {assetname}!')
            else:
                asset.default_export = exports[0] if exports else None

        if cache_result:
            self.cache.add(assetname, asset)

        return asset


def find_caseinsensitive_path(base: Path, *parts: str) -> Optional[Path]:
    if not parts:
        return base

    current: Optional[Path] = base
    for part in parts:
        if not current:
            return None
        current = current / part
        current = find_caseinsensitive_path_match(current)
    return current


PATH_NO_MATCH = Path('THERE_IS_NO_SUCH_FILE')
path_match_cache: Dict[Path, Optional[Path]] = dict()


def find_caseinsensitive_path_match(path: Path) -> Optional[Path]:
    '''
    Find a match for the given path by matching its last element case-insensitively.
    '''
    cached = path_match_cache.get(path, PATH_NO_MATCH)
    if cached is not PATH_NO_MATCH:
        return cached

    logger.debug(f"Uncached case-insensitive search: {path}")

    if path.exists():
        path_match_cache[path] = path
        return path

    parent: Path = path.parent
    name = path.name.lower()
    found: Path
    for found in parent.iterdir():
        if found.name.lower() == name:
            path_match_cache[path] = found
            return found

    return None


def load_file_into_memory(filename):
    with open(filename, 'rb') as f:
        data = f.read()
        mem = memoryview(data)
    return mem
