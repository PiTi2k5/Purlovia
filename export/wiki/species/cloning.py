# Verified with the blueprint, 29/04/2020
# Formulas:
#   - Cost:
#     Ceil(
#       (CloneElementCostPerLevelGlobalMultiplier x CloneElementCostPerLevel
#         x CharacterLevel) + (CloneBaseElementCost x CloneBaseElementCostGlobalMultiplier))
#   - Time:
#     (CloningTimePerElementShard / BabyMatureSpeedMultiplier) x Cost

from typing import Optional, cast

from ark.types import PrimalDinoCharacter
from automate.hierarchy_exporter import ExportModel, Field
from export.wiki.types import TekCloningChamber
from ue.gathering import gather_properties

__all__ = [
    'CloningData',
    'gather_cloning_data',
]

CLONING_CHAMBER_C = '/Game/PrimalEarth/Structures/TekTier/TekCloningChamber.TekCloningChamber_C'


class CloningData(ExportModel):
    '''
    Full cost is determined by Ceil(costBase + costLevel x CharacterLevel).
    Cloning time is determined by (timeBase + timeLevel x CharacterLevel) / BabyMatureSpeedMulti.
    '''

    costBase: float = Field(
        None,
        title="Base Cost to Clone",
    )
    costLevel: float = Field(
        None,
        title="Cost per Level",
    )
    timeBase: float = Field(
        None,
        title="Base Time to Clone",
    )
    timeLevel: float = Field(
        None,
        title="Time per Level",
    )


FLAGS_PREVENT_CLONE = [
    'bIsVehicle',
    'bIsRobot',
    'bUniqueDino',
    'bPreventCloning',
    'bPreventUploading',
    'bAutoTameable',
]


def can_be_cloned(species: PrimalDinoCharacter) -> bool:
    """
    Requirements for cloning are:
    - FLAGS_PREVENT_CLONE are all false
    - not DCSC->FreezeStatusValues
    - does not have a rider
    - clone base element cost higher or equal to 0
    """
    for flag in FLAGS_PREVENT_CLONE:
        if species.get(flag):
            return False
    return species.CloneBaseElementCost[0] >= 0 and species.AutoFadeOutAfterTameTime[0] == 0


def gather_cloning_data(species: PrimalDinoCharacter) -> Optional[CloningData]:
    if not can_be_cloned(species):
        return None

    loader = species.get_source().asset.loader
    chamber_a = loader[CLONING_CHAMBER_C]
    assert chamber_a.default_export
    chamber = cast(TekCloningChamber, gather_properties(chamber_a.default_export))

    cost_base = species.CloneBaseElementCost[0] * chamber.CloneBaseElementCostGlobalMultiplier[0]
    cost_level = species.CloneElementCostPerLevel[0] * chamber.CloneElementCostPerLevelGlobalMultiplier[
        0]  # skipped: CharacterLevel

    time_base = chamber.CloningTimePerElementShard[0] * cost_base  # skipped: BabyMatureSpeedMultiplier
    time_level = chamber.CloningTimePerElementShard[0] * cost_level  # skipped: BabyMatureSpeedMultiplier, CharacterLevel

    if cost_base == 0:
        # Free cloning, skip for sanity, it probably can't be obtained naturally.
        return None

    return CloningData(
        costBase=cost_base,
        costLevel=cost_level,
        timeBase=time_base,
        timeLevel=time_level,
    )
