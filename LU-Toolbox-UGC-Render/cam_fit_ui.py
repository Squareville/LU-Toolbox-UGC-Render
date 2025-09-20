# cam_fit_ui.py — UI camera fit using Blender operator, meshes-only selection
import bpy
from mathutils import Vector
from bpy_extras.object_utils import world_to_camera_view

__all__ = ["fit_camera_ui", "collect_render_objects_from_collection"]

# Reuse the same meshes-only collector so UI/headless are consistent.
def _is_render_candidate(o: bpy.types.Object):
    if o.type != 'MESH':
        return False
    if getattr(o, "hide_render", False):
        return False
    if hasattr(o, "visible_get") and not o.visible_get():
        return False
    return True

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

def _cam_axes(cam: bpy.types.Object):
    q = cam.matrix_world.to_quaternion()
    right = q @ Vector((1,0,0))
    up    = q @ Vector((0,1,0))
    fwd   = -(q @ Vector((0,0,1)))
    return right, up, fwd

def _screen_half_extent(scene, cam, objs):
    # Evaluate vertex set quickly (use bbox if you want even faster)
    dg = bpy.context.evaluated_depsgraph_get()
    pts = []
    for o in objs:
        ob_eval = o.evaluated_get(dg)
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
            bb = getattr(ob_eval, "bound_box", None)
            if bb:
                mw = ob_eval.matrix_world
                for c in bb:
                    pts.append(mw @ Vector(c))
    if not pts:
        return 0.5
    umin, umax, vmin, vmax = 1.0, 0.0, 1.0, 0.0
    for pt in pts:
        uvw = world_to_camera_view(scene, scene.camera, pt)
        u, v = float(uvw.x), float(uvw.y)
        umin = min(umin, u); umax = max(umax, u)
        vmin = min(vmin, v); vmax = max(vmax, v)
    return max(umax - 0.5, 0.5 - umin, vmax - 0.5, 0.5 - vmin)

def fit_camera_ui(context, cam, objects, framing_scale: float = 1.03, debug=True):
    """Use Blender's Align Active Camera to Selected, but only for mesh objects.
    Then Z-dolly to match framing_scale (no rotation/FOV change)."""
    scene = context.scene
    view3d = next((a for a in context.window.screen.areas if a.type == 'VIEW_3D'), None)
    if not view3d:
        print("[UGC UI Fit] No VIEW_3D area found.")
        return

    # Select meshes only
    for o in context.selected_objects:
        o.select_set(False)
    mesh_objs = [o for o in objects if _is_render_candidate(o)]
    if not mesh_objs:
        print("[UGC UI Fit] No mesh objects to frame.")
        return
    for o in mesh_objs:
        o.select_set(True)
    context.view_layer.objects.active = mesh_objs[0]

    # Ensure the active camera is the one we’re framing with.
    scene.camera = cam

    # Run operator in the 3D View context
    override = {
        "window": context.window,
        "screen": context.window.screen,
        "area": view3d,
        "region": next(r for r in view3d.regions if r.type == 'WINDOW'),
        "scene": scene,
        "view_layer": context.view_layer,
        "active_object": cam,
        "selected_objects": mesh_objs,
        "selected_editable_objects": mesh_objs,
    }
    bpy.ops.view3d.camera_to_view_selected(override)

    # Z-dolly for framing scale parity
    base = _screen_half_extent(scene, cam, mesh_objs)
    if base > 1e-6 and abs(framing_scale - 1.0) >= 1e-6:
        _, _, fwd = _cam_axes(cam)
        target = base / framing_scale
        origin = cam.location.copy()
        dir_vec = (-fwd) if (target < base) else (fwd)

        if debug:
            print(f"[UGC UI Fit] Dolly: base={base:.6f} target={target:.6f} "
                  f"dir={'backward(-fwd)' if (target < base) else 'forward(+fwd)'}")

        lo, hi, step = 0.0, 0.0, 0.05
        for _ in range(24):
            test = hi + step
            cam.location = origin + dir_vec * test
            h = _screen_half_extent(scene, cam, mesh_objs)
            if (target < base and h <= target) or (target > base and h >= target):
                hi = test
                break
            hi = test
            step *= 1.6
        if hi > 0.0:
            for _ in range(28):
                mid = 0.5 * (lo + hi)
                cam.location = origin + dir_vec * mid
                h = _screen_half_extent(scene, cam, mesh_objs)
                if (target < base and h > target) or (target > base and h < target):
                    lo = mid
                else:
                    hi = mid
            cam.location = origin + dir_vec * hi

    if debug:
        print(f"[UGC UI Fit] OK. pos={tuple(round(v,6) for v in cam.location)}")
