# -*- coding: utf-8 -*-
"""
Update level elevations WITHOUT moving MEP elements.

Problem: every duct/pipe/tray/fitting/equipment stores its position as
"Reference Level + Offset".  When a level's elevation changes, everything
that references it moves by the same delta.  Because elements on one floor
reference different levels (FFL / SSL / BoS) that each move by a different
amount, connected runs tear apart.

Fix: temporarily "park" every MEP element on a dummy level that is NOT
going to move (Revit recalculates the offset, so nothing moves), then change
the real levels, then put every element back on its original level (offsets
are recalculated again, so nothing moves).

Usage (pyRevit / RevitPythonShell, Revit 2020+):
  1. Set MODE = "PARK" and run.   -> all MEP elements now reference ZZ_TEMP_HOLD,
                                     original levels are saved to <model>_level_park.json
  2. Edit the level elevations to the architect's new values
     (or accept them through Coordination Review).  Nothing moves.
  3. Set MODE = "RESTORE" and run. -> original reference levels are written back,
                                     temp level is deleted if nothing uses it.

Safety: both passes compare every element's location before/after and roll
the transaction back if anything moved.  Make a backup / detached copy first.
"""
import json
import os

from System.Collections.Generic import List
from Autodesk.Revit.DB import (
    BuiltInCategory, BuiltInParameter, ElementId, ElementMulticategoryFilter,
    FilteredElementCollector, Level, LocationCurve, LocationPoint,
    StorageType, Transaction,
)

MODE = "PARK"            # "PARK" before editing levels, "RESTORE" after
TEMP_LEVEL_NAME = "ZZ_TEMP_HOLD"

doc = __revit__.ActiveUIDocument.Document
STORE = os.path.splitext(doc.PathName)[0] + "_level_park.json"

# Categories to process.  Add/remove as needed for the discipline.
CATS = [
    BuiltInCategory.OST_DuctCurves, BuiltInCategory.OST_FlexDuctCurves,
    BuiltInCategory.OST_DuctFitting, BuiltInCategory.OST_DuctAccessory,
    BuiltInCategory.OST_DuctTerminal, BuiltInCategory.OST_DuctInsulations,
    BuiltInCategory.OST_PipeCurves, BuiltInCategory.OST_FlexPipeCurves,
    BuiltInCategory.OST_PipeFitting, BuiltInCategory.OST_PipeAccessory,
    BuiltInCategory.OST_PlumbingFixtures, BuiltInCategory.OST_Sprinklers,
    BuiltInCategory.OST_CableTray, BuiltInCategory.OST_CableTrayFitting,
    BuiltInCategory.OST_Conduit, BuiltInCategory.OST_ConduitFitting,
    BuiltInCategory.OST_MechanicalEquipment, BuiltInCategory.OST_ElectricalEquipment,
    BuiltInCategory.OST_ElectricalFixtures, BuiltInCategory.OST_LightingFixtures,
    BuiltInCategory.OST_LightingDevices, BuiltInCategory.OST_DataDevices,
    BuiltInCategory.OST_FireAlarmDevices, BuiltInCategory.OST_CommunicationDevices,
    BuiltInCategory.OST_SecurityDevices, BuiltInCategory.OST_NurseCallDevices,
    BuiltInCategory.OST_TelephoneDevices, BuiltInCategory.OST_GenericModel,
    BuiltInCategory.OST_SpecialityEquipment,
]

# Parameters that position an element relative to a level.
# "Schedule Level" is deliberately excluded: it is informational only.
LEVEL_PARAMS = [
    BuiltInParameter.RBS_START_LEVEL_PARAM,          # ducts, pipes, trays, conduits
    BuiltInParameter.INSTANCE_REFERENCE_LEVEL_PARAM, # fittings / accessories
    BuiltInParameter.FAMILY_LEVEL_PARAM,             # level-based family instances
]


def eid_int(eid):
    try:
        return eid.Value          # Revit 2024+
    except AttributeError:
        return eid.IntegerValue   # Revit <= 2023


def level_param(e):
    """Writable, level-type parameter of the element, or None (hosted elements)."""
    for bip in LEVEL_PARAMS:
        p = e.get_Parameter(bip)
        if p and not p.IsReadOnly and p.StorageType == StorageType.ElementId:
            return p
    return None


def loc_key(e):
    """Position fingerprint used to prove nothing moved (feet, 0.1 mm precision)."""
    loc = e.Location
    if isinstance(loc, LocationCurve):
        c = loc.Curve
        pts = [c.GetEndPoint(0), c.GetEndPoint(1)]
    elif isinstance(loc, LocationPoint):
        pts = [loc.Point]
    else:
        return None
    return [(round(p.X, 4), round(p.Y, 4), round(p.Z, 4)) for p in pts]


def collect():
    f = ElementMulticategoryFilter(List[BuiltInCategory](CATS))
    return (FilteredElementCollector(doc).WherePasses(f)
            .WhereElementIsNotElementType().ToElements())


def find_temp_level():
    for lvl in FilteredElementCollector(doc).OfClass(Level):
        if lvl.Name == TEMP_LEVEL_NAME:
            return lvl
    return None


def run_pass(name, assign):
    """assign(e, p) -> True if the parameter was changed.  Rolls back on movement."""
    before, changed, skipped = {}, 0, 0
    t = Transaction(doc, name)
    t.Start()
    try:
        for e in collect():
            p = level_param(e)
            if p is None:
                skipped += 1
                continue
            key = loc_key(e)
            if assign(e, p):
                changed += 1
                before[eid_int(e.Id)] = key
        doc.Regenerate()
        moved = [i for i, k in before.items()
                 if k != loc_key(doc.GetElement(ElementId(i)))]
        if moved:
            t.RollBack()
            print("ABORTED - {} element(s) would have moved, nothing was changed. "
                  "First ids: {}".format(len(moved), moved[:25]))
            return None
        t.Commit()
    except Exception:
        t.RollBack()
        raise
    print("{}: {} element(s) re-referenced, {} hosted/read-only element(s) left "
          "alone.".format(name, changed, skipped))
    return changed


if MODE == "PARK":
    if os.path.exists(STORE):
        raise Exception("{} already exists - run RESTORE first or delete it.".format(STORE))
    store = {}
    t = Transaction(doc, "Create temp level")
    t.Start()
    temp = find_temp_level()
    if temp is None:
        temp = Level.Create(doc, 0.0)
        temp.Name = TEMP_LEVEL_NAME
    t.Commit()

    def park(e, p):
        if p.AsElementId() == temp.Id:
            return False
        store[str(eid_int(e.Id))] = eid_int(p.AsElementId())
        return p.Set(temp.Id)

    if run_pass("Park MEP elements on temp level", park) is not None:
        with open(STORE, "w") as fh:
            json.dump(store, fh)
        print("Original levels saved to {}".format(STORE))
        print("Now edit the level elevations, then run again with MODE = 'RESTORE'.")

elif MODE == "RESTORE":
    with open(STORE) as fh:
        store = json.load(fh)
    temp = find_temp_level()

    def restore(e, p):
        lid = store.get(str(eid_int(e.Id)))
        if lid is None or p.AsElementId() == ElementId(int(lid)):
            return False
        return p.Set(ElementId(int(lid)))

    if run_pass("Restore MEP reference levels", restore) is not None:
        still = [e for e in collect()
                 if level_param(e) is not None and temp is not None
                 and level_param(e).AsElementId() == temp.Id]
        if temp is not None and not still:
            t = Transaction(doc, "Delete temp level")
            t.Start()
            doc.Delete(temp.Id)
            t.Commit()
            os.remove(STORE)
            print("Temp level deleted. Done.")
        else:
            print("WARNING: {} element(s) still reference {} - temp level NOT deleted "
                  "(deleting a level deletes everything hosted on it). Check them "
                  "first.".format(len(still), TEMP_LEVEL_NAME))
else:
    raise Exception("MODE must be 'PARK' or 'RESTORE'")
