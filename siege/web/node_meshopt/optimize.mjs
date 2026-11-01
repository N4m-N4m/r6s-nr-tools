// optimize.mjs
// Stage 2: web packaging for cleaned GLBs from the Blender stage.
//
//   npm i @gltf-transform/core @gltf-transform/extensions @gltf-transform/functions meshoptimizer
//   node optimize.mjs input_clean.glb output_web.glb [--error 0.0005] [--keep-normals]
//
// Pipeline: strip leftover attributes -> dedup -> GPU instancing ->
//           weld -> error-bounded simplify (border-locked) -> prune ->
//           meshopt quantize+compress (EXT_meshopt_compression)
//
// Viewer side needs MeshoptDecoder registered on GLTFLoader:
//   import { MeshoptDecoder } from 'meshoptimizer';
//   gltfLoader.setMeshoptDecoder(MeshoptDecoder);

import { NodeIO } from '@gltf-transform/core';
import { ALL_EXTENSIONS } from '@gltf-transform/extensions';
import {
  dedup, instance, weld, simplify, prune, meshopt,
} from '@gltf-transform/functions';
import { MeshoptSimplifier, MeshoptEncoder } from 'meshoptimizer';
import { statSync } from 'node:fs';

const args = process.argv.slice(2);
const [input, output] = args.filter((a) => !a.startsWith('--'));
const keepNormals = args.includes('--keep-normals');
const errIdx = args.indexOf('--error');
const ERROR = errIdx !== -1 ? parseFloat(args[errIdx + 1]) : 0.0005; // fraction of mesh radius

if (!input || !output) {
  console.error('usage: node optimize.mjs in.glb out.glb [--error 0.0005] [--keep-normals]');
  process.exit(1);
}

const KEEP_SEMANTICS = new Set(['POSITION', ...(keepNormals ? ['NORMAL'] : [])]);

await MeshoptEncoder.ready;
await MeshoptSimplifier.ready;

const io = new NodeIO().registerExtensions(ALL_EXTENSIONS);
const doc = await io.read(input);

// --- strip every vertex attribute we don't need for depth rendering
for (const mesh of doc.getRoot().listMeshes()) {
  for (const prim of mesh.listPrimitives()) {
    for (const semantic of prim.listSemantics()) {
      if (!KEEP_SEMANTICS.has(semantic)) {
        const acc = prim.getAttribute(semantic);
        prim.setAttribute(semantic, null);
        if (acc && acc.listParents().length <= 1) acc.dispose();
      }
    }
    prim.setMaterial(null);
  }
}

await doc.transform(
  // collapse identical mesh data -> one mesh, many nodes
  dedup(),
  // nodes sharing a mesh -> EXT_mesh_gpu_instancing (huge win on entitized maps)
  instance({ min: 2 }),
  // weld BEFORE simplify; split verts cripple the simplifier
  weld(),
  // error-bounded, not ratio-chasing: ratio 0 = "remove everything you can
  // within the error budget". lockBorder keeps open boundaries pinned.
  simplify({
    simplifier: MeshoptSimplifier,
    ratio: 0.0,
    error: ERROR,
    lockBorder: true,
  }),
  prune(),
  // quantize + EXT_meshopt_compression in one step
  meshopt({ encoder: MeshoptEncoder, level: 'medium' }),
);

await io.write(output, doc);

const mb = (p) => (statSync(p).size / 1024 / 1024).toFixed(2);
console.log(`${input} (${mb(input)} MB) -> ${output} (${mb(output)} MB)`);
