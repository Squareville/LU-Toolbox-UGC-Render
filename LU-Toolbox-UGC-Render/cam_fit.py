# cam_fit.py — operator-first align; headless math fallback; optional force_headless
# - No rotation/FOV/clip edits
# - Framing Scale (fs): 1.0=as framed; <1 tighter; >1 looser
# - Works in UI and headless; can force headless via parameter

import bpy
import math
import mathutils
from bpy_extras.object_utils import world_to_camera_view

__all__ = [
    "fit_camera_to_objects",
    "collect_render_objects_from_collection",
]

# ----------------------- collection helpers -----------------------

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

# ----------------------- geometry sampling -----------------------

def _world_points_evaluated(objects, depsgraph):
    pts = []
    for obj in objects:
        if not _is_render_candidate(obj):
            continue
        ob_eval = obj.evaluated_get(depsgraph)
        if ob_eval.type == 'MESH':
            try:
                me = ob_eval.to_mesh()
            except RuntimeError:
                me = None
            if me:
                mw = ob_eval.matrix_world
                for v in me.vertices:
                    pts.append(mw @ v.co)
                ob_eval.to_mesh_clear()
        else:
            bb = getattr(obj, "bound_box", None)
            if bb:
                mw = obj.matrix_world
                for c in bb:
                    pts.append(mw @ mathutils.Vector(c))
    return pts

# ----------------------- camera helpers -----------------------

def _cam_axes(cam: bpy.types.Object):
    q = cam.matrix_world.to_quaternion()
    right = q @ mathutils.Vector((1,0,0))
    up    = q @ mathutils.Vector((0,1,0))
    fwd   = -(q @ mathutils.Vector((0,0,1)))  # camera looks along -Z
    return right, up, fwd

def _get_fov_xy(cam: bpy.types.Object, render: bpy.types.RenderSettings):
    cd = cam.data
    if cd.type == 'ORTHO':
        return (None, None)
    fx = getattr(cd, "angle_x", None)
    fy = getattr(cd, "angle_y", None)
    if fx and fy:
        return fx, fy
    # Fallback calc
    sw, sh = cd.sensor_width, cd.sensor_height
    rx = render.resolution_x * render.resolution_percentage / 100.0
    ry = render.resolution_y * render.resolution_percentage / 100.0
    aspect = rx / ry if ry else 1.0
    fit = cd.sensor_fit
    if fit == 'VERTICAL':
        fy = 2.0 * math.atan((sh * 0.5) / cd.lens)
        fx = 2.0 * math.atan(math.tan(fy * 0.5) * aspect)
    elif fit == 'HORIZONTAL':
        fx = 2.0 * math.atan((sw * 0.5) / cd.lens)
        fy = 2.0 * math.atan(math.tan(fx * 0.5) / max(1e-9, aspect))
    else:
        sratio = sw / sh if sh else 1.0
        if aspect >= sratio:
            fx = 2.0 * math.atan((sw * 0.5) / cd.lens)
            fy = 2.0 * math.atan(math.tan(fx * 0.5) / max(1e-9, aspect))
        else:
            fy = 2.0 * math.atan((sh * 0.5) / cd.lens)
            fx = 2.0 * math.atan(math.tan(fy * 0.5) * aspect)
    return fx, fy

# ----------------------- VIEW_3D context helpers -----------------------

def _find_view3d_area_and_region():
    wm = bpy.context.window_manager
    win = bpy.context.window if bpy.context.window else (wm.windows[0] if wm.windows else None)
    if not win:
        return None, None, None
    screen = win.screen
    for area in screen.areas:
        if area.type == 'VIEW_3D':
            for region in area.regions:
                if region.type == 'WINDOW':
                    for space in area.spaces:
                        if space.type == 'VIEW_3D':
                            return win, area, region
    # fallback flip (UI mode only)
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
    space = None
    for s in area.spaces:
        if s.type == 'VIEW_3D':
            space = s
            break
    if space is None:
        return None
    return {
        "window": win,
        "screen": win.screen,
        "area": area,
        "region": region,
        "scene": scene,
        "space_data": space,
        "region_data": space.region_3d,
        "blend_data": bpy.context.blend_data,
    }

# ----------------------- tiny Z dolly after framing -----------------------

def _tiny_margin_dolly(cam: bpy.types.Object, framing_scale: float, pts_world=None):
    """
    Pure Z move:
      framing_scale = 1.0 -> no change
      < 1.0 (tighter)  -> move forward
      > 1.0 (looser)   -> move back
    """
    if abs(framing_scale - 1.0) < 1e-6:
        return
    _, _, fwd = _cam_axes(cam)
    # Estimate distance to subject via median positive depth if points provided
    est = 1.0
    if pts_world:
        M = cam.matrix_world.inverted()
        depths = []
        for p in pts_world:
            dz = -(M @ p).z
            if dz > 1e-6:
                depths.append(dz)
        if depths:
            depths.sort()
            est = depths[len(depths)//2]
    delta = (framing_scale - 1.0) * 0.1 * est
    cam.location += (-fwd) * delta  # >1 => back (looser), <1 => forward (tighter)

# ----------------------- math fallback (headless) -----------------------

def _headless_fit_z_only(cam: bpy.types.Object, scene: bpy.types.Scene, pts_world, framing_scale: float):
    """
    Z-only tight fit based on FOV (no UI). We solve so r_max ≈ 1.0 (touch edge),
    then apply 'framing_scale' (>1 looser, <1 tighter).
    """
    if not pts_world:
        print("[UGC HeadlessFit] No points to frame.")
        return
    r = scene.render
    fx, fy = _get_fov_xy(cam, r)
    tanFx = max(math.tan((fx or math.radians(50.0)) * 0.5), 1e-9)
    tanFy = max(math.tan((fy or math.radians(50.0)) * 0.5), 1e-9)

    def r_max_at(loc):
        old = cam.location.copy()
        cam.location = loc
        M = cam.matrix_world.inverted()
        rmax = 0.0
        for p in pts_world:
            cp = M @ p
            d = max(1e-9, -cp.z)
            rmax = max(rmax, abs(cp.x)/(d*tanFx), abs(cp.y)/(d*tanFy))
        cam.location = old
        return rmax

    base = cam.location.copy()
    _, _, fwd = _cam_axes(cam)

    cur = r_max_at(base)
    target = 1.0
    go_forward = cur < target

    lo_t = 0.0; hi_t = 0.0
    lo_v = cur;  hi_v = cur
    step = 0.1
    for _ in range(40):
        test_t = hi_t + (step if go_forward else -step)
        v = r_max_at(base + fwd * test_t)
        crossed = (v >= target) if go_forward else (v <= target)
        if crossed:
            lo_t, hi_t = hi_t, test_t
            lo_v, hi_v = hi_v, v
            break
        hi_t, hi_v = test_t, v
        step *= 2.0
    else:
        cam.location = base + fwd * hi_t
        print(f"[UGC HeadlessFit] Could not bracket; moved {hi_t:+.6f} (cur={cur:.4f})")
        _tiny_margin_dolly(cam, framing_scale, pts_world)
        return

    for _ in range(40):
        mid_t = 0.5 * (lo_t + hi_t)
        v = r_max_at(base + fwd * mid_t)
        if go_forward:
            if v < target:
                lo_t, lo_v = mid_t, v
            else:
                hi_t, hi_v = mid_t, v
        else:
            if v > target:
                lo_t, lo_v = mid_t, v
            else:
                hi_t, hi_v = mid_t, v

    final_t = hi_t
    cam.location = base + fwd * final_t
    _tiny_margin_dolly(cam, framing_scale, pts_world)
    print(f"[UGC HeadlessFit] r_max: start={cur:.4f} -> target≈1.0, move={final_t:+.6f}, framing_scale={framing_scale:.4f}")

# ----------------------- main entry -----------------------

def fit_camera_to_objects(
    cam: bpy.types.Object,
    scene: bpy.types.Scene,
    objects,
    margin: float = 1.0,          # framing scale
    allow_xy_center: bool = False,  # ignored here (kept for API compat)
    debug: bool = True,
    force_headless: bool = False,
):
    if not objects:
        print("[UGC AlignCam] No objects passed to fit.")
        return

    scene.camera = cam
    dg = bpy.context.evaluated_depsgraph_get()
    pts_world = _world_points_evaluated(objects, dg)

    # Force headless path if requested
    if force_headless:
        _headless_fit_z_only(cam, scene, pts_world, margin)
        return

    # Try UI operator path
    win, area, region = _find_view3d_area_and_region()
    if win and area and region:
        override = _override_for_view3d(win, area, region, scene)
        if override:
            # Save selection
            prev_active = bpy.context.view_layer.objects.active
            prev_sel = list(bpy.context.selected_objects)

            # Prepare selection
            for o in prev_sel:
                o.select_set(False)
            for o in objects:
                try: o.select_set(True)
                except: pass
            if objects:
                try: bpy.context.view_layer.objects.active = objects[0]
                except: pass

            # Look through camera and align
            try:
                bpy.ops.view3d.view_camera(override)
            except Exception as e:
                print(f"[UGC AlignCam] view_camera failed: {e}")
            try:
                bpy.ops.view3d.camera_to_view_selected(override)
                print("[UGC AlignCam] camera_to_view_selected OK")
            except Exception as e:
                print(f"[UGC AlignCam] camera_to_view_selected failed: {e}")

            # Tiny dolly based on user scale
            _tiny_margin_dolly(cam, margin, pts_world)

            # Restore selection
            try:
                for o in bpy.context.selected_objects:
                    o.select_set(False)
                for o in prev_sel:
                    o.select_set(True)
                bpy.context.view_layer.objects.active = prev_active
            except:
                pass

            print(f"[UGC AlignCam] Done. Framing Scale={margin:.4f}")
            return

    # No UI available → headless
    _headless_fit_z_only(cam, scene, pts_world, margin)
