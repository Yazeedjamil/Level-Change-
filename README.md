# Level Tools – Update Levels from Link

A pyRevit tool that updates the elevations (and names) of the levels in an
MEP model to match the architectural link **without moving a single duct,
pipe, tray, fitting or piece of equipment**.

## Why

Every MEP element is stored as *Reference Level + Offset*. When a level's
elevation changes, everything referencing it moves by the same amount.
Elements on one floor reference a mix of levels (FFL, SSL, BoS) that each move
by a different amount, so connected runs tear apart.

The tool parks the elements on a temporary level (Revit recalculates the
offsets, so nothing moves), updates the real levels, then restores the
elements to their original levels. Level identities are preserved, so plan
views, sheets, spaces, schedules, filters and Copy/Monitor relationships are
untouched.

## Install

1. Copy `LevelTools.extension` into your pyRevit extensions folder
   (default: `%APPDATA%\pyRevit\Extensions`), or add this repository folder
   as an extension path in pyRevit Settings.
2. Reload pyRevit. A **LevelTools** tab appears with the button
   **Update Levels from Link**.

Requires pyRevit (IronPython) on Revit 2020 or newer.

## Use

1. Reload the updated architectural link. If Revit asks for Coordination
   Review, choose **Postpone**.
2. Click **Update Levels from Link**.
3. Pick the architectural link. The table lists every host level with the
   linked level it is matched to, the new elevation and the delta:
   * `monitored` – matched through the Copy/Monitor relationship
   * `name` – matched by name
   * `unmatched` – no counterpart found; the level is left as is
4. Double-click a row or use **Change match...** to override a match
   (for example when the architect deleted and redrew a level).
   **Clear match** leaves a level untouched.
5. Untick *Rename host levels to match link names* if you only want
   elevations changed.
6. Click **Apply** and confirm.

The whole operation is one undo step. If any element would move, the tool
rolls everything back and reports which elements were affected.

## Notes

* Elements hosted on faces or on linked elements (for example diffusers on a
  linked ceiling) follow their host, not the level. They are skipped and
  reported; review them separately.
* Elements inside groups may refuse the re-reference. They are reported as
  "moved with their level".
* Coordination Review warnings clear by themselves once the host levels
  agree with the link again. Re-monitoring a level whose linked counterpart
  was deleted stays a manual step (Copy/Monitor ▸ Monitor).
* `park_mep_levels.py` is the earlier two-run version of the same logic for
  RevitPythonShell. Use it only if pyRevit is not available.
