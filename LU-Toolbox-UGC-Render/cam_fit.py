# cam_fit.py — camera fitting utilities for LU Toolbox UGC Render
import math
import bpy
import mathutils

__all__ = [
    "fit_camera_to_objects",
    "collect_render_objects_from_collection",
]

def collect_render_objects_from_collection(col: bpy.types.Collection):
    """Flatten renderable objects in a collection hierarchy."""
    objs = set()
    def walk(c):
        for o in c.objects:
            if o.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME','EMPTY'}:
                objs.add(o)
        for cc in c.children:
            walk(cc)
    walk(col)
    return list(objs)

# ------------------ FOV helpers ------------------

def fov_y_from_camera(cam: bpy.types.Object, render: bpy.types.RenderSettings):
    cdata = cam.data
    if cdata.type == 'ORTHO':
        return None

    sensor_fit = cdata.sensor_fit
    sensor_width = cdata.sensor_width
    sensor_height = cdata.sensor_height

    res_x = render.resolution_x * render.resolution_percentage / 100.0
    res_y = render.resolution_y * render.resolution_percentage / 100.0
    aspect = res_x / res_y if res_y != 0 else 1.0

    if sensor_fit == 'VERTICAL':
        return 2.0 * math.atan((sensor_height * 0.5) / cdata.lens)
    elif sensor_fit == 'HORIZONTAL':
        fx = 2.0 * math.atan((sensor_width * 0.5) / cdata.lens)
        half_fx = fx * 0.5
        half_fy = math.atan(math.tan(half_fx) / aspect)
        return 2.0 * half_fy
    else:
        sensor_ratio = sensor_width / sensor_height if sensor_height != 0 else 1.0
        if aspect >= sensor_ratio:  # horizontal fit
            fx = 2.0 * math.atan((sensor_width * 0.5) / cdata.lens)
            half_fx = fx * 0.5
            half_fy = math.atan(math.tan(half_fx) / aspect)
            return 2.0 * half_fy
        else:  # vertical fit
            return 2.0 * math.atan((sensor_height * 0.5) / cdata.lens)

def fov_x_from_camera(cam: bpy.types.Object, render: bpy.types.RenderSettings):
    fy = fov_y_from_camera(cam, render)
    if fy is None:
        return None
    res_x = render.resolution_x * render.resolution_percentage / 100.0
    res_y = render.resolution_y * render.resolution_percentage / 100.0
    aspect = res_x / res_y if res_y != 0 else 1.0
    half_fy = fy * 0.5
    half_fx = math.atan(math.tan(half_fy) * aspect)
    return 2.0 * half_fx

# ------------------ Core fit ------------------

def _gather_world_bbox_points(objects):
    pts = []
    for obj in objects:
        # Must be visible to the camera; you can relax this if needed.
        if not obj.visible_get():
            continue
        if obj.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME'}:
            mw = obj.matrix_world
            bb = getattr(obj, "bound_box", None)
            if bb:
                for c in bb:
                    pts.append(mw @ mathutils.Vector(c))
    return pts

def _project_to_cam_space(points, cam: bpy.types.Object):
    M = cam.matrix_world.inverted()
    return [M @ p for p in points]

def _center_in_camera_plane(cam_pts):
    minx = min(p.x for p in cam_pts)
    maxx = max(p.x for p in cam_pts)
    miny = min(p.y for p in cam_pts)
    maxy = max(p.y for p in cam_pts)
    cx = (minx + maxx) * 0.5
    cy = (miny + maxy) * 0.5
    return cx, cy, minx, maxx, miny, maxy

def fit_camera_to_objects(
    cam: bpy.types.Object,
    scene: bpy.types.Scene,
    objects,
    margin: float = 1.02,
    allow_xy_center: bool = True
):
    """
    Tight-fit camera to 'objects' by adjusting **position only**:
      - translate in camera-local X/Y (optional) to center the object on screen,
      - translate along camera-local -Z to fill the frame with minimal headroom,
      - no rotation or FOV changes.
    Works for perspective and orthographic cameras.

    'margin' is a multiplier (1.0 = mathematically tight; 1.02 = 2% headroom).
    """
    pts_world = _gather_world_bbox_points(objects)
    if not pts_world:
        return  # nothing to frame

    cam_pts = _project_to_cam_space(pts_world, cam)

    r = scene.render
    res_x = r.resolution_x * r.resolution_percentage / 100.0
    res_y = r.resolution_y * r.resolution_percentage / 100.0
    aspect = res_x / res_y if res_y else 1.0

    # ---------------- Ortho camera ----------------
    if cam.data.type == 'ORTHO':
        cx, cy, minx, maxx, miny, maxy = _center_in_camera_plane(cam_pts)

        # Move camera in local XY so the bbox center is in the view center
        if allow_xy_center:
            # Shifting camera by +tx in local X moves points by -tx (camera space)
            tx, ty = cx, cy
        else:
            tx, ty = 0.0, 0.0

        width = (maxx - minx)
        height = (maxy - miny)

        # Apply margin
        width *= margin
        height *= margin

        # Blender ortho camera uses 'ortho_scale' as width in world units.
        ortho_width_needed = max(width, height * aspect)
        cam.data.ortho_scale = max(ortho_width_needed, 1e-6)

        # Apply the XY shift to camera location in world space
        if allow_xy_center and (abs(tx) > 1e-9 or abs(ty) > 1e-9):
            q = cam.matrix_world.to_quaternion()
            right = q @ mathutils.Vector((1,0,0))
            up    = q @ mathutils.Vector((0,1,0))
            cam.location += right * tx + up * ty

        # Reasonable clip planes
        extent = max(width, height) * 0.5
        cam.data.clip_start = 0.001
        cam.data.clip_end   = max(cam.data.clip_end, extent * 20.0)
        return

    # ---------------- Perspective camera ----------------
    fy = fov_y_from_camera(cam, r) or math.radians(50.0)
    fx = fov_x_from_camera(cam, r)
    if fx is None:
        half_fy = fy * 0.5
        half_fx = math.atan(math.tan(half_fy) * aspect)
        fx = 2.0 * half_fx

    half_fx = max(fx * 0.5, 1e-6)
    half_fy = max(fy * 0.5, 1e-6)
    tan_fx = math.tan(half_fx)
    tan_fy = math.tan(half_fy)

    # Centering: choose tx, ty so that the bbox is centered in camera XY.
    cx, cy, minx, maxx, miny, maxy = _center_in_camera_plane(cam_pts)
    if allow_xy_center:
        tx, ty = cx, cy
    else:
        tx, ty = 0.0, 0.0

    # Determine required extra depth to fit all points with margin when centered.
    needed_delta = 0.0
    min_depth = float('inf')
    for p in cam_pts:
        # After shifting camera by (tx,ty), the relative point is (x' = x - tx, y' = y - ty, z' = z)
        x = p.x - tx
        y = p.y - ty
        d = -p.z  # positive depth
        min_depth = min(min_depth, d)
        # For this point to be inside frustum at depth D: |x| <= D * tan_fx, |y| <= D * tan_fy
        need_x = abs(x) / tan_fx if tan_fx > 1e-9 else 0.0
        need_y = abs(y) / tan_fy if tan_fy > 1e-9 else 0.0
        need = max(need_x, need_y)
        delta = need - d
        if delta > needed_delta:
            needed_delta = delta

    # Apply margin to the depth increase
    needed_delta = max(0.0, needed_delta) * margin

    # Move camera in world space: +right*tx + up*ty + forward*needed_delta
    q = cam.matrix_world.to_quaternion()
    right = q @ mathutils.Vector((1,0,0))
    up    = q @ mathutils.Vector((0,1,0))
    forward = -(q @ mathutils.Vector((0,0,1)))  # camera looks along -Z in its local space

    if allow_xy_center and (abs(tx) > 1e-9 or abs(ty) > 1e-9):
        cam.location += right * tx + up * ty

    if needed_delta > 1e-12:
        cam.location += forward * needed_delta

    # Clip planes (keep near small but safe, far comfortably large)
    new_near = max(0.001, (min_depth + needed_delta) * 0.1)
    cam.data.clip_start = min(cam.data.clip_start, new_near) if cam.data.clip_start > 0 else new_near
    cam.data.clip_end   = max(cam.data.clip_end, (min_depth + needed_delta) * 20.0)
