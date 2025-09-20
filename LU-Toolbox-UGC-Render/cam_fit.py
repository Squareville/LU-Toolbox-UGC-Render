# cam_fit.py — precision camera fitting (scale-ratio solve)
# - No rotation/FOV changes
# - Uses evaluated mesh vertices for tight screen-space bounds
# - Moves camera in local XY (optional) to center, then along local Z exactly
# - Does NOT modify clip planes

import math
import bpy
import mathutils

__all__ = [
    "fit_camera_to_objects",
    "collect_render_objects_from_collection",
]

# ---------------------------------------------------------
# Collect renderable objects
# ---------------------------------------------------------
def collect_render_objects_from_collection(col: bpy.types.Collection):
    objs = set()
    def walk(c):
        for o in c.objects:
            if o.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME','EMPTY'}:
                objs.add(o)
        for cc in c.children:
            walk(cc)
    walk(col)
    return [o for o in objs if _is_render_candidate(o)]

def _is_render_candidate(o: bpy.types.Object):
    if getattr(o, "hide_render", False):
        return False
    if hasattr(o, "visible_get") and not o.visible_get():
        return False
    return o.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME'}

# ---------------------------------------------------------
# Geometry sampling — evaluated mesh verts (tight) + bbox fallback
# ---------------------------------------------------------
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

# ---------------------------------------------------------
# Camera / math helpers
# ---------------------------------------------------------
def _to_cam_space(points, cam: bpy.types.Object):
    M = cam.matrix_world.inverted()
    return [M @ p for p in points]

def _cam_axes(cam: bpy.types.Object):
    q = cam.matrix_world.to_quaternion()
    right = q @ mathutils.Vector((1,0,0))
    up    = q @ mathutils.Vector((0,1,0))
    fwd   = -(q @ mathutils.Vector((0,0,1)))  # camera looks along -Z
    return right, up, fwd

def _fov_y_from_camera(cam: bpy.types.Object, render: bpy.types.RenderSettings):
    cd = cam.data
    if cd.type == 'ORTHO':
        return None
    sw, sh = cd.sensor_width, cd.sensor_height
    rx = render.resolution_x * render.resolution_percentage / 100.0
    ry = render.resolution_y * render.resolution_percentage / 100.0
    aspect = rx / ry if ry else 1.0
    fit = cd.sensor_fit
    if fit == 'VERTICAL':
        return 2.0 * math.atan((sh * 0.5) / cd.lens)
    if fit == 'HORIZONTAL':
        fx = 2.0 * math.atan((sw * 0.5) / cd.lens)
        return 2.0 * math.atan(math.tan(fx * 0.5) / aspect)
    # AUTO
    sratio = sw / sh if sh else 1.0
    if aspect >= sratio:
        fx = 2.0 * math.atan((sw * 0.5) / cd.lens)
        return 2.0 * math.atan(math.tan(fx * 0.5) / aspect)
    else:
        return 2.0 * math.atan((sh * 0.5) / cd.lens)

def _fov_x_from_camera(cam: bpy.types.Object, render: bpy.types.RenderSettings):
    fy = _fov_y_from_camera(cam, render)
    if fy is None:
        return None
    rx = render.resolution_x * render.resolution_percentage / 100.0
    ry = render.resolution_y * render.resolution_percentage / 100.0
    aspect = rx / ry if ry else 1.0
    return 2.0 * math.atan(math.tan(fy * 0.5) * aspect)

# ---------------------------------------------------------
# Main fit (exact scale solve)
# ---------------------------------------------------------
def fit_camera_to_objects(
    cam: bpy.types.Object,
    scene: bpy.types.Scene,
    objects,
    margin: float = 1.02,
    allow_xy_center: bool = True,
    debug: bool = False,
):
    """
    Screen-tight fit using scale-ratio solve:
      1) optional XY centering in camera plane (world-units) to center bbox,
      2) compute r_i = max(|x|/(d*tanFx), |y|/(d*tanFy)) per point (d = -z),
      3) r_max = max r_i; target r_target = 1 / margin,
      4) compute scale s = r_target / r_max. Move camera by a single delta t so depths
         scale by s (d' = d - t). Using the limiting point depth d*, t = d* * (1 - 1/s).
    Notes:
      - Keeps rotation/FOV unchanged
      - Does NOT modify clip planes
      - Ignores camera shift_x/shift_y for centering (different unit space)
    """
    if not objects:
        return

    dg = bpy.context.evaluated_depsgraph_get()
    pts_world = _world_points_evaluated(objects, dg)
    if not pts_world:
        return

    r = scene.render
    rx = r.resolution_x * r.resolution_percentage / 100.0
    ry = r.resolution_y * r.resolution_percentage / 100.0
    aspect = rx / ry if ry else 1.0

    cd = cam.data

    # Ortho path: exact via ortho_scale + XY center
    if cd.type == 'ORTHO':
        cam_pts = _to_cam_space(pts_world, cam)
        # center to (0,0) in camera plane
        minx = min(p.x for p in cam_pts); maxx = max(p.x for p in cam_pts)
        miny = min(p.y for p in cam_pts); maxy = max(p.y for p in cam_pts)
        cx = (minx + maxx) * 0.5
        cy = (miny + maxy) * 0.5
        if allow_xy_center and (abs(cx) > 1e-9 or abs(cy) > 1e-9):
            right, up, _ = _cam_axes(cam)
            cam.location += right * cx + up * cy
            cam_pts = _to_cam_space(pts_world, cam)

        minx = min(p.x for p in cam_pts); maxx = max(p.x for p in cam_pts)
        miny = min(p.y for p in cam_pts); maxy = max(p.y for p in cam_pts)
        width  = (maxx - minx) * margin
        height = (maxy - miny) * margin
        cd.ortho_scale = max(width, height * aspect, 1e-6)

        if debug:
            print(f"[UGC Fit ORTHO] width={width:.4f}, height={height:.4f}, scale={cd.ortho_scale:.4f}")
        return

    # Perspective path:
    fy = _fov_y_from_camera(cam, r) or math.radians(50.0)
    fx = _fov_x_from_camera(cam, r) or 2.0 * math.atan(math.tan(fy * 0.5) * aspect)
    tanFx = max(math.tan(fx * 0.5), 1e-9)
    tanFy = max(math.tan(fy * 0.5), 1e-9)

    # Initial projection
    cam_pts = _to_cam_space(pts_world, cam)

    # Ensure everything is in front of camera by epsilon
    depths = [-p.z for p in cam_pts]
    d_min = min(depths)
    eps = 1e-4
    if d_min <= eps:
        # Move camera back just enough so nearest point is at eps
        _, _, fwd = _cam_axes(cam)
        cam.location += (-fwd) * (eps - d_min + 1e-4)
        cam_pts = _to_cam_space(pts_world, cam)

    # Optional XY centering (camera-plane)
    if allow_xy_center:
        minx = min(p.x for p in cam_pts); maxx = max(p.x for p in cam_pts)
        miny = min(p.y for p in cam_pts); maxy = max(p.y for p in cam_pts)
        cx = (minx + maxx) * 0.5
        cy = (miny + maxy) * 0.5
        if abs(cx) > 1e-9 or abs(cy) > 1e-9:
            right, up, _ = _cam_axes(cam)
            cam.location += right * cx + up * cy
            cam_pts = _to_cam_space(pts_world, cam)

    # Compute current max normalized extent r_max
    r_max = 0.0
    r_argmax_depth = None
    for p in cam_pts:
        d = max(1e-9, -p.z)
        rxn = abs(p.x) / (d * tanFx)
        ryn = abs(p.y) / (d * tanFy)
        r_here = max(rxn, ryn)
        if r_here > r_max:
            r_max = r_here
            r_argmax_depth = d

    if r_max <= 0.0 or r_argmax_depth is None:
        return

    # Target coverage for margin m is r_target = 1/m
    r_target = 1.0 / max(1.0, margin)
    # If r_max < r_target, object is too small (too much margin) -> move camera forward
    # If r_max > r_target, object is too large -> move camera back
    s = r_target / r_max  # desired scaling of normalized extents
    if abs(s - 1.0) < 1e-6:
        if debug:
            print(f"[UGC Fit PERSP] already tight (r_max={r_max:.4f}, target={r_target:.4f})")
        return

    # Depth scales inversely with image size; want d' = d / s for the limiting point
    d_star = r_argmax_depth
    d_prime = d_star / s
    t = d_star - d_prime  # positive => move forward by t; negative => move back by |t|

    # Apply along local Z
    _, _, fwd = _cam_axes(cam)
    cam.location += fwd * t  # fwd is +forward; positive t = forward (tighten)

    if debug:
        # Re-eval coverage
        cam_pts2 = _to_cam_space(pts_world, cam)
        r2 = 0.0
        for p in cam_pts2:
            d = max(1e-9, -p.z)
            r2 = max(r2, abs(p.x)/(d*tanFx), abs(p.y)/(d*tanFy))
        print(f"[UGC Fit PERSP] r_max_before={r_max:.4f}, r_max_after={r2:.4f}, target={r_target:.4f}, t={t:.6f}, margin={margin:.4f}")
