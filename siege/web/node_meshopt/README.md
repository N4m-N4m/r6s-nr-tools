# Optional Node meshopt post-step

The addon's **Export Web GLB** operator (Siege Map / Web Export panel) runs the
whole pipeline inside Blender and compresses with **Draco** (decoded by three.js
`DRACOLoader`). That's enough for most web viewers.

These scripts are the original two-stage pipeline, kept for when you specifically
want **`EXT_meshopt_compression`** + true **GPU instancing** (`EXT_mesh_gpu_instancing`)
+ error-bounded simplification — things gltf-transform / meshoptimizer do better
than Blender's exporter. They require Node.js.

| file | role |
|------|------|
| `r6_map_clean.py`   | headless Blender cleanup (same logic as the in-addon **Clean Map for Web**), one fresh Blender process per map |
| `optimize.mjs`      | gltf-transform stage: strip attrs → dedup → GPU instancing → weld → error-bounded simplify → prune → meshopt quantize+compress |
| `process_all.ps1`   | batch driver: runs both stages over a folder of maps |

## Use as a post-step on an addon export

1. In Blender, export with **Export Web GLB** (Draco off if you'll meshopt instead).
2. Then run just the Node stage on that GLB:

```
npm i @gltf-transform/core @gltf-transform/extensions @gltf-transform/functions meshoptimizer
node optimize.mjs map.glb map_web.glb --error 0.0005
```

## Or run the whole batch pipeline headless

```
.\process_all.ps1 -InDir "D:\maps\raw" -OutDir "D:\maps\web" -Blender "C:\Program Files\Blender Foundation\Blender 5.0\blender.exe"
```

The viewer must register the decoder it needs:

```js
import { MeshoptDecoder } from 'meshoptimizer';
gltfLoader.setMeshoptDecoder(MeshoptDecoder);   // for optimize.mjs output
// or DRACOLoader for the in-addon Draco export
```
