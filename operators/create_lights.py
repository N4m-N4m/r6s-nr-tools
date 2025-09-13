import bpy # type: ignore

class NODE_OT_CreateLightsFromMaterial(bpy.types.Operator):
    """
    Operator to create lights based on the active object's material.
    """
    bl_idname = "object.create_lights_from_material"
    bl_label = "Create Lights From Material"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        # Ensure there's an active object
        active_object = bpy.context.active_object
        if not active_object:
            self.report({'WARNING'}, "No active object selected.")
            return {'CANCELLED'}

        # Ensure the active object has a material
        if not active_object.material_slots or not active_object.material_slots[0].material:
            self.report({'WARNING'}, "Active object does not have a material.")
            return {'CANCELLED'}

        # Get the material from the active object
        material = active_object.material_slots[0].material
        material_name = material.name

        # Find or create the collections for master and instanced lights
        master_lights_collection = bpy.data.collections.get("Master Lights")
        if not master_lights_collection:
            master_lights_collection = bpy.data.collections.new("Master Lights")
            bpy.context.scene.collection.children.link(master_lights_collection)

        instanced_lights_collection = bpy.data.collections.get("Instanced Lights")
        if not instanced_lights_collection:
            instanced_lights_collection = bpy.data.collections.new("Instanced Lights")
            bpy.context.scene.collection.children.link(instanced_lights_collection)

        # Find all objects that use the same material
        objects_with_material = [obj for obj in bpy.data.objects if obj.type == 'MESH' and material in [slot.material for slot in obj.material_slots]]

        if not objects_with_material:
            self.report({'INFO'}, f"No objects found using material {material_name}.")
            return {'CANCELLED'}

        # Create or reuse a master light
        master_light_name = f"_lightsetup_{material_name}"
        master_light_object = bpy.data.objects.get(master_light_name)
        if not master_light_object:
            master_light_data = bpy.data.lights.new(name=master_light_name, type='POINT')
            master_light_object = bpy.data.objects.new(name=master_light_name, object_data=master_light_data)
            master_lights_collection.objects.link(master_light_object)
        else:
            self.report({'INFO'}, f"Master light '{master_light_name}' already exists.")

        # Clear existing instance lights in the Instanced Lights collection for this material
        for obj in list(instanced_lights_collection.objects):
            if obj.name.startswith(f"{master_light_name}_instance_"):
                bpy.data.objects.remove(obj, do_unlink=True)

        # Set up light instances for each object with the material
        for obj in objects_with_material:
            instance_light = master_light_object.copy()
            instance_light.location = obj.location
            instance_light.name = f"{master_light_name}_instance_{obj.name}"
            instanced_lights_collection.objects.link(instance_light)

        self.report({'INFO'}, f"Light setup completed for material: {material_name}.")
        return {'FINISHED'}

def register():
    bpy.utils.register_class(NODE_OT_CreateLightsFromMaterial)

def unregister():
    bpy.utils.unregister_class(NODE_OT_CreateLightsFromMaterial)
