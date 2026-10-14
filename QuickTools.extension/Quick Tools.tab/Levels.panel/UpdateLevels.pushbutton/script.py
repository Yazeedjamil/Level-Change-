# -*- coding: utf-8 -*-
"""Update host level elevations (and names) from the architectural link
WITHOUT moving the MEP elements that reference those levels.

How it works (one click, all-or-nothing):
  1. Park   - every element referencing a level that is going to change is
              re-referenced to a temporary level. Revit recalculates the
              offsets, so nothing moves.
  2. Update - each host level is moved (and optionally renamed) to match the
              linked level it is mapped to.  Nothing references it, so
              nothing moves.
  3. Restore- elements are put back on their original levels.  Offsets are
              recalculated again, so nothing moves.  The temp level is deleted.

Both park and restore verify every element's position; if anything would
move, the whole TransactionGroup is rolled back and the model is untouched.

Mapping: monitored host levels are matched through their Copy/Monitor
relationship; the rest are matched by name.  Any match can be overridden in
the table before applying.
"""
__title__ = "Update Levels\nfrom Link"
__author__ = "Quick Tools"
__doc__ = ("Update host levels (elevation and name) from the architectural "
           "link without moving any MEP element.")

import clr
clr.AddReference("System.Data")
from System.Data import DataTable            # noqa: E402

from Autodesk.Revit.DB import (             # noqa: E402
    BuiltInCategory, BuiltInParameter, ElementId, ElementMulticategoryFilter,
    FailureProcessingResult, FailureSeverity, FilteredElementCollector,
    IFailuresPreprocessor, Level, LocationCurve, LocationPoint,
    RevitLinkInstance, StorageType, Transaction, TransactionGroup, UnitUtils,
)
from System.Collections.Generic import List  # noqa: E402

from pyrevit import forms, revit, script     # noqa: E402

doc = revit.doc
output = script.get_output()
logger = script.get_logger()

TEMP_LEVEL_NAME = "ZZ_TEMP_HOLD"
TOL_FT = 0.0001          # ~0.03 mm : below this a level is "unchanged"

# Categories whose elements are parked/restored.  Extend if needed.
CATS = [
    BuiltInCategory.OST_DuctCurves, BuiltInCategory.OST_FlexDuctCurves,
    BuiltInCategory.OST_DuctFitting, BuiltInCategory.OST_DuctAccessory,
    BuiltInCategory.OST_DuctTerminal,
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


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class Abort(Exception):
    pass


def eid_int(eid):
    try:
        return eid.Value          # Revit 2024+
    except AttributeError:
        return eid.IntegerValue   # Revit <= 2023


def to_mm(ft):
    try:
        from Autodesk.Revit.DB import UnitTypeId
        return UnitUtils.ConvertFromInternalUnits(ft, UnitTypeId.Millimeters)
    except Exception:
        from Autodesk.Revit.DB import DisplayUnitType
        return UnitUtils.ConvertFromInternalUnits(ft, DisplayUnitType.DUT_MILLIMETERS)


def fmt_mm(ft):
    return "{:,.0f}".format(to_mm(ft))


def norm(name):
    return "".join(ch for ch in name.lower() if ch.isalnum())


class SwallowWarnings(IFailuresPreprocessor):
    """Hide the 'elements have moved / coordination review' style warnings."""
    def PreprocessFailures(self, fa):
        for msg in fa.GetFailureMessages():
            if msg.GetSeverity() == FailureSeverity.Warning:
                fa.DeleteWarning(msg)
        return FailureProcessingResult.Continue


def quiet(t):
    opts = t.GetFailureHandlingOptions()
    opts.SetFailuresPreprocessor(SwallowWarnings())
    t.SetFailureHandlingOptions(opts)
    return t


def level_param(e):
    """Writable level-type parameter of the element, or None (hosted elements)."""
    for bip in LEVEL_PARAMS:
        p = e.get_Parameter(bip)
        if p and not p.IsReadOnly and p.StorageType == StorageType.ElementId:
            return p
    return None


def loc_key(e):
    """Position fingerprint used to prove nothing moved (0.1 mm precision)."""
    loc = e.Location
    if isinstance(loc, LocationCurve):
        c = loc.Curve
        pts = [c.GetEndPoint(0), c.GetEndPoint(1)]
    elif isinstance(loc, LocationPoint):
        pts = [loc.Point]
    else:
        return None
    return [(round(p.X, 4), round(p.Y, 4), round(p.Z, 4)) for p in pts]


def collect_elements():
    f = ElementMulticategoryFilter(List[BuiltInCategory](CATS))
    return (FilteredElementCollector(doc).WherePasses(f)
            .WhereElementIsNotElementType().ToElements())


def host_levels():
    lv = list(FilteredElementCollector(doc).OfClass(Level))
    lv.sort(key=lambda l: l.ProjectElevation, reverse=True)
    return lv


def find_temp_level():
    for lvl in FilteredElementCollector(doc).OfClass(Level):
        if lvl.Name == TEMP_LEVEL_NAME:
            return lvl
    return None


# --------------------------------------------------------------------------- #
# mapping model
# --------------------------------------------------------------------------- #
class LinkLevel(object):
    def __init__(self, level, z_shift):
        self.level = level
        self.name = level.Name
        # elevation expressed in HOST internal coordinates
        self.internal = level.ProjectElevation + z_shift

    @property
    def label(self):
        return "{}   ({} mm)".format(self.name, fmt_mm(self.internal))


class Row(object):
    def __init__(self, host):
        self.host = host
        self.target = None      # LinkLevel or None
        self.source = ""        # how the match was made

    @property
    def delta(self):
        if self.target is None:
            return 0.0
        return self.target.internal - self.host.ProjectElevation

    @property
    def changes_elevation(self):
        return self.target is not None and abs(self.delta) > TOL_FT

    def changes_name(self, rename):
        return rename and self.target is not None and self.target.name != self.host.Name

    def changes(self, rename):
        return self.changes_elevation or self.changes_name(rename)


def link_levels_of(link_inst):
    ldoc = link_inst.GetLinkDocument()
    if ldoc is None:
        return {}
    z = link_inst.GetTotalTransform().Origin.Z
    out = {}
    for lvl in FilteredElementCollector(ldoc).OfClass(Level):
        out[eid_int(lvl.Id)] = LinkLevel(lvl, z)
    return out


def auto_match(rows, link_levels):
    by_norm = {}
    for ll in link_levels.values():
        by_norm.setdefault(norm(ll.name), ll)
    for r in rows:
        r.target, r.source = None, "unmatched"
        # 1. Copy/Monitor relationship
        try:
            if r.host.IsMonitoringLinkElement():
                for mid in r.host.GetMonitoredLinkElementIds():
                    ll = link_levels.get(eid_int(mid))
                    if ll:
                        r.target, r.source = ll, "monitored"
                        break
        except Exception as ex:          # pragma: no cover
            logger.debug("monitor lookup failed for %s: %s", r.host.Name, ex)
        if r.target:
            continue
        # 2. name
        ll = by_norm.get(norm(r.host.Name))
        if ll:
            r.target, r.source = ll, "name"


# --------------------------------------------------------------------------- #
# the three passes
# --------------------------------------------------------------------------- #
def park(changing_ids):
    """Move elements off the changing levels. Returns (temp_level, {eid: level_id})."""
    parked, before, failed = {}, {}, []
    t = quiet(Transaction(doc, "Park elements on temp level"))
    t.Start()
    temp = find_temp_level()
    if temp is None:
        temp = Level.Create(doc, 0.0)
        temp.Name = TEMP_LEVEL_NAME
    for e in collect_elements():
        p = level_param(e)
        if p is None:
            continue
        lid = p.AsElementId()
        if eid_int(lid) not in changing_ids:
            continue
        key = loc_key(e)
        try:
            ok = p.Set(temp.Id)
        except Exception:
            ok = False
        if not ok:
            failed.append(eid_int(e.Id))
            continue
        parked[eid_int(e.Id)] = eid_int(lid)
        before[eid_int(e.Id)] = key
    doc.Regenerate()
    moved = [i for i, k in before.items()
             if k != loc_key(doc.GetElement(ElementId(i)))]
    if moved:
        t.RollBack()
        raise Abort("Parking would have moved {} element(s). First ids: {}"
                    .format(len(moved), moved[:25]))
    t.Commit()
    return temp, parked, failed


def update_levels(rows, rename):
    t = quiet(Transaction(doc, "Update level elevations / names"))
    t.Start()
    taken = set(l.Name for l in host_levels())
    renamed, skipped_names = 0, []
    for r in rows:
        lvl = r.host
        was_pinned = lvl.Pinned
        if was_pinned:
            lvl.Pinned = False
        if r.changes_elevation:
            # Elevation is relative to the level's Elevation Base; apply the
            # delta so the base setting does not matter.
            lvl.Elevation = lvl.Elevation + r.delta
        if r.changes_name(rename):
            new = r.target.name
            if new in taken:
                skipped_names.append((lvl.Name, new))
            else:
                taken.discard(lvl.Name)
                lvl.Name = new
                taken.add(new)
                renamed += 1
        if was_pinned:
            lvl.Pinned = True
    t.Commit()
    return renamed, skipped_names


def restore(temp, parked):
    before = {}
    t = quiet(Transaction(doc, "Restore element reference levels"))
    t.Start()
    for eid, lid in parked.items():
        e = doc.GetElement(ElementId(eid))
        p = level_param(e) if e else None
        if p is None:
            continue
        before[eid] = loc_key(e)
        if not p.Set(ElementId(lid)):
            t.RollBack()
            raise Abort("Could not restore element {} to its level.".format(eid))
    doc.Regenerate()
    moved = [i for i, k in before.items()
             if k != loc_key(doc.GetElement(ElementId(i)))]
    if moved:
        t.RollBack()
        raise Abort("Restoring would have moved {} element(s). First ids: {}"
                    .format(len(moved), moved[:25]))
    # delete the temp level only if nothing references it any more
    still = 0
    for e in collect_elements():
        p = level_param(e)
        if p is not None and p.AsElementId() == temp.Id:
            still += 1
    if still == 0:
        doc.Delete(temp.Id)
    t.Commit()
    return still


def run(rows, rename):
    """rows: only the rows that change. Returns a report string."""
    changing_ids = set(eid_int(r.host.Id) for r in rows if r.changes_elevation)
    tg = TransactionGroup(doc, "Update levels from link")
    tg.Start()
    try:
        temp, parked, failed = park(changing_ids)
        renamed, skipped_names = update_levels(rows, rename)
        still = restore(temp, parked)
        tg.Assimilate()
    except Abort as ex:
        tg.RollBack()
        return False, "ABORTED - nothing was changed.\n\n{}".format(ex)
    except Exception as ex:
        tg.RollBack()
        return False, "ERROR - nothing was changed.\n\n{}".format(ex)

    lines = ["Done.",
             "Levels moved:            {}".format(len(changing_ids)),
             "Levels renamed:          {}".format(renamed),
             "Elements parked/restored:{}".format(len(parked))]
    if failed:
        lines.append("")
        lines.append("WARNING: {} element(s) could not be re-referenced (groups / "
                     "read-only) and MOVED with their level. Ids: {}"
                     .format(len(failed), failed[:30]))
    if skipped_names:
        lines.append("")
        lines.append("Not renamed because the name already exists: " +
                     ", ".join("{} -> {}".format(a, b) for a, b in skipped_names))
    if still:
        lines.append("")
        lines.append("WARNING: {} element(s) still reference {} - temp level kept."
                     .format(still, TEMP_LEVEL_NAME))
    return True, "\n".join(lines)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #
class LevelWindow(forms.WPFWindow):
    def __init__(self, links):
        forms.WPFWindow.__init__(self, script.get_bundle_file("ui.xaml"))
        self.links = links
        self.rows = [Row(l) for l in host_levels()]
        self.visible = []
        self.link_levels = {}
        self.result = None

        self.cb_link.ItemsSource = [l.Name for l in links]
        # pre-select the link that looks architectural
        idx = 0
        for i, l in enumerate(links):
            if "ARC" in l.Name.upper():
                idx = i
                break
        self.cb_link.SelectedIndex = idx       # fires link_changed -> auto-match

    # -- state ------------------------------------------------------------- #
    @property
    def rename(self):
        return bool(self.chk_rename.IsChecked)

    def current_link(self):
        i = self.cb_link.SelectedIndex
        return self.links[i] if i >= 0 else None

    def link_changed(self, sender, args):
        link = self.current_link()
        self.link_levels = link_levels_of(link) if link else {}
        auto_match(self.rows, self.link_levels)
        self.refresh()

    # -- table ------------------------------------------------------------- #
    def refresh(self, sender=None, args=None):
        only_changed = bool(self.chk_only_changed.IsChecked)
        self.visible = [r for r in self.rows
                        if not only_changed or r.changes(self.rename)]
        dt = DataTable()
        for col in ("Host level", "Current (mm)", "Link level", "New (mm)",
                    "Delta (mm)", "Matched by", "Action"):
            dt.Columns.Add(col)
        for r in self.visible:
            if r.target is None:
                dt.Rows.Add(r.host.Name, fmt_mm(r.host.Elevation), "-", "-", "-",
                            r.source, "untouched")
                continue
            act = []
            if r.changes_elevation:
                act.append("move")
            if r.changes_name(self.rename):
                act.append("rename")
            dt.Rows.Add(r.host.Name, fmt_mm(r.host.Elevation), r.target.name,
                        fmt_mm(r.host.Elevation + r.delta), fmt_mm(r.delta),
                        r.source, ", ".join(act) or "no change")
        self.grid.ItemsSource = dt.DefaultView
        n_move = sum(1 for r in self.rows if r.changes_elevation)
        n_ren = sum(1 for r in self.rows if r.changes_name(self.rename))
        n_un = sum(1 for r in self.rows if r.target is None)
        self.lbl_status.Text = (
            "{} level(s) will move, {} will be renamed, {} unmatched (left as is). "
            "Double-click a row or use 'Change match...' to override."
            .format(n_move, n_ren, n_un))

    def selected_row(self):
        i = self.grid.SelectedIndex
        if i < 0 or i >= len(self.visible):
            forms.alert("Select a level row first.")
            return None
        return self.visible[i]

    # -- buttons ----------------------------------------------------------- #
    def auto_match(self, sender, args):
        auto_match(self.rows, self.link_levels)
        self.refresh()

    def change_match(self, sender, args):
        r = self.selected_row()
        if r is None:
            return
        lls = sorted(self.link_levels.values(), key=lambda x: x.internal, reverse=True)
        labels = [ll.label for ll in lls]
        pick = forms.SelectFromList.show(
            labels, title="Link level for '{}'".format(r.host.Name),
            button_name="Use this level", multiselect=False)
        if pick:
            r.target = lls[labels.index(pick)]
            r.source = "manual"
            self.refresh()

    def clear_match(self, sender, args):
        r = self.selected_row()
        if r is None:
            return
        r.target, r.source = None, "cleared"
        self.refresh()

    def cancel(self, sender, args):
        self.Close()

    def apply(self, sender, args):
        todo = [r for r in self.rows if r.changes(self.rename)]
        if not todo:
            forms.alert("Nothing to do - all matched levels already agree with the link.")
            return
        self.result = (todo, self.rename)
        self.Close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    links = [l for l in FilteredElementCollector(doc).OfClass(RevitLinkInstance)
             if l.GetLinkDocument() is not None]
    if not links:
        forms.alert("No loaded Revit links found in this model.", exitscript=True)

    win = LevelWindow(links)
    win.ShowDialog()
    if win.result is None:
        return
    todo, rename = win.result

    n_move = sum(1 for r in todo if r.changes_elevation)
    n_ren = sum(1 for r in todo if r.changes_name(rename))
    if not forms.alert(
            "{} level(s) will be moved and {} renamed.\n\n"
            "All MEP elements on the moving levels are parked and restored so "
            "that nothing moves. The whole operation is one undo step and is "
            "rolled back completely if any element would move.\n\nContinue?"
            .format(n_move, n_ren),
            title="Update levels from link", yes=True, no=True):
        return

    ok, report = run(todo, rename)
    print(report)
    forms.alert(report, title="Update levels from link",
                warn_icon=not ok)


main()
