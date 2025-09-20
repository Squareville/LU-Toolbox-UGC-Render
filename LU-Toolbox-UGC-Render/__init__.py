bl_info = {
    "name": "LU Toolbox UGC Render",
    "author": "Christopher Fantauzzo",
    "version": (1, 0, 0),  # keep at 1.0.0 until release
    "blender": (3, 1, 0),
    "location": "View3D > N-panel > LU UGC Render",
    "description": "Append UGC preset scene, remap materials for render, frame camera, render, and restore.",
    "category": "Render",
}

import bpy
import os
from bpy.types import Operator, Panel
from bpy.props import EnumProperty, IntProperty, StringProperty, FloatProperty

# --- imports: meshes-only & UI fitter ---
from .cam_fit_ui import (
    collect_render_objects_from_collection as collect_meshes_from_collection,
    fit_camera_ui as fit_ui,
)

# ---------- utilities (paths / append scene & materials) ----------

def addon_dir() -> str:
    return os.path.dirname(os.path.realpath(__file__))

def ugc_blend_path() -> str:
    return os.path.join(addon_dir(), "UGC_Renders.blend")

def _append_from_blend(block_type: str, name: str):
    path = ugc_blend_path()
    if not os.path.exists(path):
        return False
    directory = os.path.join(path, block_type)
    if not directory.endswith(os.sep):
        directory += os.sep
    try:
        bpy.ops.wm.append(
            filepath=os.path.join(directory, name),
            directory=directory,
            filename=name,
            link=False,
            autoselect=False
        )
        return True
    except Exception:
        return False

def get_or_append_material(name: str):
    mat = bpy.data.materials.get(name)
    if mat:
        return mat
    ok = _append_from_blend("Material", name)
    return bpy.data.materials.get(name) if ok else None

def append_scene(scene_name: str) -> bpy.types.Scene:
    if scene_name in bpy.data.scenes:
        return bpy.data.scenes[scene_name]
    path = ugc_blend_path()
    if not os.path.exists(path):
        raise FileNotFoundError(f"UGC_Renders.blend not found at: {path}")

    directory = os.path.join(path, "Scene")
    if not directory.endswith(os.sep):
        directory = directory + os.sep

    bpy.ops.wm.append(
        filepath=os.path.join(directory, scene_name),
        directory=directory,
        filename=scene_name,
        link=False,
        autoselect=False
    )
    if scene_name not in bpy.data.scenes:
        raise RuntimeError(f"Failed to append Scene '{scene_name}' from {path}")
    return bpy.data.scenes[scene_name]

# ---------- LOD selection / linking ----------

def find_best_lod_collection(scene: bpy.types.Scene):
    candidates = [f"_LOD_{i}" for i in range(0, 5)]
    root = scene.collection
    def all_cols(r):
        stack = [r]; seen=set()
        while stack:
            c = stack.pop()
            if c.name in seen: continue
            seen.add(c.name)
            yield c
            for cc in c.children: stack.append(cc)
    cols = list(all_cols(root))
    for suffix in candidates:
        for col in cols:
            if col.name.endswith(suffix):
                return col
    return None

def link_collection_into_scene(collection: bpy.types.Collection, target_scene: bpy.types.Scene):
    root = target_scene.collection
    def is_linked(root_col, col_to_find):
        stack=[root_col]
        while stack:
            c=stack.pop()
            if c==col_to_find: return True
            stack.extend(c.children)
        return False
    if not is_linked(root, collection):
        try:
            root.children.link(collection)
        except RuntimeError:
            pass
    return collection

# ---------- material remap (render-only) ----------

def _collect_mesh_objects_in_collection(col: bpy.types.Collection):
    objs = set()
    def walk(c):
        for o in c.objects:
            if o.type == 'MESH' and not getattr(o, "hide_render", False):
                objs.add(o)
        for cc in c.children:
            walk(cc)
    walk(col)
    return list(objs)

def _remap_materials_for_render(mesh_objs, mapping):
    record = {}
    for obj in mesh_objs:
        slots = obj.material_slots
        if not slots: continue
        restore = []
        for i, slot in enumerate(slots):
            mat = slot.material
            if not mat: continue
            new_mat = mapping.get(mat.name)
            if isinstance(new_mat, str):
                new_mat = bpy.data.materials.get(new_mat)
            if new_mat and new_mat != mat:
                restore.append((i, mat.name))
                slot.material = new_mat
        if restore:
            record[obj.name] = restore
    return record

def _restore_materials(record):
    for obj_name, slots_info in record.items():
        obj = bpy.data.objects.get(obj_name)
        if not obj or not obj.material_slots:
            continue
        for idx, mat_name in slots_info:
            mat = bpy.data.materials.get(mat_name)
            if mat and idx < len(obj.material_slots):
                try:
                    obj.material_slots[idx].material = mat
                except Exception:
                    pass

# ---------------------------- UI / Operator ----------------------------

UGC_TYPES = [
    ("BRICKBUILD", "BrickBuild", "Use the BrickBuild preset scene"),
    ("ROCKET",     "Rocket",     "Use the Rocket preset scene"),
    ("CAR",        "Car",        "Use the Car preset scene"),
]

class LUUGC_OT_RenderIcon(Operator):
    bl_idname = "luugc.render_icon"
    bl_label = "Render Icon"
    bl_options = {'REGISTER', 'UNDO'}

    ugc_type: EnumProperty(name="UGC Type", items=UGC_TYPES, default="BRICKBUILD")
    resolution: IntProperty(name="Resolution", default=128, min=32, soft_max=8192)
    margin: FloatProperty(
        name="Framing Scale",
        default=1.03,
        min=0.10,
        max=10.00,
        description="1.00=as framed; <1 tighter; >1 looser (Z-dolly only)"
    )
    save_path: StringProperty(name="Output Path", default="", subtype='FILE_PATH')

    def invoke(self, context, event):
        sc = context.scene
        self.ugc_type   = getattr(sc, "luugc_type", self.ugc_type)
        self.resolution = getattr(sc, "luugc_resolution", self.resolution)
        self.margin     = getattr(sc, "luugc_margin", self.margin)
        self.save_path  = getattr(sc, "luugc_save_path", self.save_path)
        return self.execute(context)

    def _resolve_output_path(self, context, src_scene):
        if self.save_path.strip():
            return bpy.path.abspath(self.save_path)

        # No explicit path → save next to .blend using scene name
        blend_path = bpy.data.filepath
        if not blend_path:
            self.report({'ERROR'}, "Output Path is empty and the .blend is not saved. Save the .blend or set Output Path.")
            return None
        out_dir = os.path.dirname(blend_path)
        out_name = f"{src_scene.name}.png"
        return os.path.join(out_dir, out_name)

    def execute(self, context):
        prev_scene = context.window.scene
        src_scene = context.scene
        scene_name = {"BRICKBUILD":"BrickBuild","ROCKET":"Rocket","CAR":"Car"}[self.ugc_type]

        # Append preset scene
        try:
            target_scene = append_scene(scene_name)
        except Exception as e:
            self.report({'ERROR'}, f"Append failed: {e}")
            return {'CANCELLED'}

        context.window.scene = target_scene

        # Locate LOD collection in source; link it into target scene.
        lod_col = find_best_lod_collection(src_scene)
        if lod_col is None:
            lod_col = bpy.data.collections.new("UGC_Linked_All")
            try: target_scene.collection.children.link(lod_col)
            except RuntimeError: pass
            for obj in src_scene.objects:
                if obj.parent is None and obj.type in {'MESH','CURVE','SURFACE','FONT','META','VOLUME','EMPTY'}:
                    try: lod_col.objects.link(obj)
                    except RuntimeError: pass
        else:
            link_collection_into_scene(lod_col, target_scene)

        # Meshes-only for framing & material remap.
        objs_to_frame = collect_meshes_from_collection(lod_col)
        mesh_objs     = _collect_mesh_objects_in_collection(lod_col)

        # Ensure render materials exist
        vc_r  = get_or_append_material("VertexColor_Render")
        vct_r = get_or_append_material("VertexColorTransparent_Render")
        mapping = {}
        if vc_r:  mapping["VertexColor"] = vc_r
        if vct_r: mapping["VertexColorTransparent"] = vct_r

        # Set square resolution
        target_scene.render.resolution_x = int(self.resolution)
        target_scene.render.resolution_y = int(self.resolution)

        # Camera
        cam = target_scene.camera or next((o for o in target_scene.objects if o.type == 'CAMERA'), None)
        if not cam:
            self.report({'ERROR'}, f"No camera found in target scene '{scene_name}'.")
            context.window.scene = prev_scene
            return {'CANCELLED'}

        # Resolve output path now; fail gracefully if impossible
        outpath = self._resolve_output_path(context, src_scene)
        if not outpath:
            context.window.scene = prev_scene
            return {'CANCELLED'}

        # Prepare to render
        restore_record = {}
        prev_fmt  = target_scene.render.image_settings.file_format
        prev_path = target_scene.render.filepath
        try:
            # remap materials for render
            if mapping:
                restore_record = _remap_materials_for_render(mesh_objs, mapping)

            # --- FIT (UI operator path) ---
            # If there's truly no 3D View, this will print and do nothing—user is in UI flow.
            fit_ui(context, cam, objs_to_frame, framing_scale=self.margin, debug=True)

            # --- RENDER (always save) ---
            os.makedirs(os.path.dirname(outpath), exist_ok=True)
            ext = os.path.splitext(outpath)[1].lower()
            fmt_map = {
                ".png": 'PNG', ".jpg": 'JPEG', ".jpeg": 'JPEG', ".tga": 'TARGA',
                ".tif": 'TIFF', ".tiff": 'TIFF', ".exr": 'OPEN_EXR', ".hdr": 'HDR', ".bmp": 'BMP',
            }
            fmt = fmt_map.get(ext, 'PNG')
            target_scene.render.image_settings.file_format = fmt
            target_scene.render.filepath = outpath
            bpy.ops.render.render(write_still=True)
            self.report({'INFO'}, f"Saved render: {outpath}")

        finally:
            # Restore materials and previous scene/filepath settings
            if restore_record:
                _restore_materials(restore_record)
            target_scene.render.image_settings.file_format = prev_fmt
            target_scene.render.filepath = prev_path
            context.window.scene = prev_scene

        return {'FINISHED'}

class LUUGC_PT_Panel(Panel):
    bl_label = "LU UGC Render"
    bl_idname = "LUUGC_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "LU UGC Render"

    def draw(self, context):
        layout = self.layout
        sc = context.scene

        layout.prop(sc, "luugc_type", text="UGC Type")
        layout.prop(sc, "luugc_resolution", text="Resolution")

        box = layout.box()
        box.label(text="Camera Fit")
        box.prop(sc, "luugc_margin", text="Framing Scale")

        layout.prop(sc, "luugc_save_path", text="Output Path")

        op = layout.operator("luugc.render_icon", text="Render Icon", icon='RENDER_STILL')
        op.ugc_type   = sc.luugc_type
        op.resolution = sc.luugc_resolution
        op.margin     = sc.luugc_margin
        op.save_path  = sc.luugc_save_path

# ---------------------------- Scene props ----------------------------

def _register_scene_props():
    bpy.types.Scene.luugc_type = EnumProperty(name="UGC Type", items=UGC_TYPES, default="BRICKBUILD")
    bpy.types.Scene.luugc_resolution = IntProperty(name="Resolution", default=128, min=32, soft_max=8192)
    bpy.types.Scene.luugc_margin = FloatProperty(
        name="Framing Scale",
        default=1.03,
        min=0.10,
        max=10.00,
        description="1.00=as framed; <1 tighter; >1 looser (Z-dolly only)"
    )
    bpy.types.Scene.luugc_save_path = StringProperty(name="Output Path", default="", subtype='FILE_PATH')

def _unregister_scene_props():
    for p in ("luugc_type","luugc_resolution","luugc_margin","luugc_save_path"):
        if hasattr(bpy.types.Scene, p): delattr(bpy.types.Scene, p)

classes = (LUUGC_OT_RenderIcon, LUUGC_PT_Panel)

def register():
    for c in classes: bpy.utils.register_class(c)
    _register_scene_props()

def unregister():
    _unregister_scene_props()
    for c in reversed(classes): bpy.utils.unregister_class(c)

if __name__ == "__main__":
    register()
