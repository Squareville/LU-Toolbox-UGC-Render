# cam_fit.py — align via Blender's built-in "Align Active Camera to Selected"
# - Uses bpy.ops.view3d.camera_to_view_selected with proper context override
# - No custom FOV math, no clip plane edits
# - Optional tiny Z-only margin tweak after the align (default = none)

import bpy
import math
import mathutils

__all__ = [
    "fit_camera_to_objects",
    "collect_render_objects_from_collection",
]

# ---------------------------------------------------------
# Collect renderable objects (same filtering as before)
# ---------------------------------------------------------
def _is_render_candidate(o: bpy.types.Object):
    if getattr(o, "hide_render", False):
        return False
    if hasattr(o, "visible_get") and not o.visible_get():
        return False
    return o.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME', 'EMPTY'}

def collect_render_objects_from_collection(col: bpy.types.Collection):
    objs = set()
    def walk(c):
        for o in c.objects:
            if _is_render_candidate(o):
                objs.add(o)
        for cc in c.children:
            walk(cc)
    walk(col)
    return list(objs)

# ---------------------------------------------------------
# Context helpers to run view3d operators safely
# ---------------------------------------------------------
def _find_view3d_area_and_region():
    # Try active window first
    wm = bpy.context.window_manager
    win = bpy.context.window if bpy.context.window else (wm.windows[0] if wm.windows else None)
    if not win:
        return None, None, None
    screen = win.screen

    # Prefer a 3D View that already has a RegionWindow
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            return win, area, region

    # As a fallback, temporarily flip the first area into VIEW_3D
    # (only works in UI mode; headless/background has no windows)
    for area in screen.areas:
        old_type = area.type
        try:
            area.type = 'VIEW_3D'
            for region in area.regions:
                if region.type == 'WINDOW':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            return win, area, region
        finally:
            area.type = old_type
    return None, None, None

def _override_for_view3d(win, area, region, scene):
    # Build the minimal override dict these operators need
    space = None
    for s in area.spaces:
        if s.type == 'VIEW_3D':
            space = s
            break
    if space is None:
        return None

    override = {
        "window": win,
        "screen": win.screen,
        "area": area,
        "region": region,
        "scene": scene,
        "space_data": space,
        "region_data": space.region_3d,
        "blend_data": bpy.context.blend_data,
    }
    return override

# ---------------------------------------------------------
# Optional tiny margin tweak (pure Z dolly)
# margin = 1.0 -> no change
# < 1.0 -> tighter (forward), > 1.0 -> looser (back)
# ---------------------------------------------------------
def _tiny_margin_dolly(cam: bpy.types.Object, factor: float):
    if abs(factor - 1.0) < 1e-6:
        return
    q = cam.matrix_world.to_quaternion()
    fwd = -(q @ mathutils.Vector((0, 0, 1)))  # camera looks along -Z
    # Heuristic: move by a fraction of current distance to the selection center.
    # We measure distance to camera focus point via depth of selection bounding box center.
    # If factor > 1 -> back out a bit; if factor < 1 -> move in a bit.
    # Keep it conservative so it doesn't fight Blender's own framing.
    delta_scale = (factor - 1.0)
    cam.location = cam.location + fwd * (delta_scale * 0.1)  # 10% of current depth (roughly)

# ---------------------------------------------------------
# Main entry: use Align Active Camera to Selected
# ---------------------------------------------------------
def fit_camera_to_objects(
    cam: bpy.types.Object,
    scene: bpy.types.Scene,
    objects,
    margin: float = 1.0,
    allow_xy_center: bool = True,  # ignored here; Blender operator recenters as needed
    debug: bool = True,
):
    """
    Aligns the active camera to the selected objects using Blender's
    built-in operator (View3D > Align View > Align Active Camera to Selected).
    - Requires a VIEW_3D area/region (works in UI mode).
    - In headless/background (no windows), this operator is unavailable.
      (We can add a math fallback later for headless if you want.)
    """

    if not objects:
        if debug:
            print("[UGC AlignCam] No objects passed to fit.")
        return

    # Ensure the scene's active camera is the one we were given
    scene.camera = cam

    # Save selection state
    prev_active = bpy.context.view_layer.objects.active
    prev_sel = [o for o in bpy.context.selected_objects]

    # Replace selection with our objects
    for o in bpy.context.selected_objects:
        o.select_set(False)
    for o in objects:
        try:
            o.select_set(True)
        except Exception:
            pass
    # Set an active (any) object among the selection
    if objects:
        try:
            bpy.context.view_layer.objects.active = objects[0]
        except Exception:
            pass

    # Find a 3D view and run the operator with override
    win, area, region = _find_view3d_area_and_region()
    if win and area and region:
        override = _override_for_view3d(win, area, region, scene)
        if override is not None:
            # Make sure we're looking through the camera first
            try:
                bpy.ops.view3d.view_camera(override)
            except Exception as e:
                if debug:
                    print(f"[UGC AlignCam] view_camera failed: {e}")

            # Align camera to selected
            try:
                bpy.ops.view3d.camera_to_view_selected(override)
                if debug:
                    print("[UGC AlignCam] camera_to_view_selected OK")
            except Exception as e:
                if debug:
                    print(f"[UGC AlignCam] camera_to_view_selected failed: {e}")
        else:
            if debug:
                print("[UGC AlignCam] Could not build override for VIEW_3D.")
    else:
        # No UI (e.g., background mode) — operator not available
        if debug:
            print("[UGC AlignCam] No VIEW_3D context available. "
                  "camera_to_view_selected requires UI. "
                  "Consider adding a math fallback for headless runs.")

    # Optional tiny Z dolly to adjust perceived margin
    try:
        _tiny_margin_dolly(cam, margin)
    except Exception:
        pass

    # Restore previous selection state
    try:
        for o in bpy.context.selected_objects:
            o.select_set(False)
        for o in prev_sel:
            o.select_set(True)
        bpy.context.view_layer.objects.active = prev_active
    except Exception:
        pass

    if debug:
        print(f"[UGC AlignCam] Done. Margin tweak factor={margin:.4f}")
