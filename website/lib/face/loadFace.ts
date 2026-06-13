export interface FaceData {
  /** normalized, centered vertex positions (count*3) */
  position: Float32Array;
  /** triangle indices */
  index: Uint16Array;
  /** unique edge indices for wireframe (pairs) */
  edges: Uint16Array;
  count: number;
}

let cache: FaceData | null = null;

/** Parse the MediaPipe canonical face model OBJ into geometry buffers. */
export async function loadFace(url = "/models/canonical_face_model.obj"): Promise<FaceData> {
  if (cache) return cache;
  const res = await fetch(url);
  const text = await res.text();

  const verts: number[] = [];
  const tris: number[] = [];

  for (const line of text.split("\n")) {
    if (line.startsWith("v ")) {
      const p = line.split(/\s+/);
      verts.push(parseFloat(p[1]), parseFloat(p[2]), parseFloat(p[3]));
    } else if (line.startsWith("f ")) {
      const p = line.trim().split(/\s+/).slice(1);
      const idx = p.map((tok) => parseInt(tok.split("/")[0], 10) - 1);
      // fan-triangulate any polygon
      for (let i = 1; i < idx.length - 1; i++) {
        tris.push(idx[0], idx[i], idx[i + 1]);
      }
    }
  }

  const count = verts.length / 3;
  const position = new Float32Array(verts);

  // center + scale to ~2.2 units tall
  let minX = Infinity, minY = Infinity, minZ = Infinity;
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
  for (let i = 0; i < count; i++) {
    const x = position[i * 3], y = position[i * 3 + 1], z = position[i * 3 + 2];
    minX = Math.min(minX, x); maxX = Math.max(maxX, x);
    minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
  }
  const cx = (minX + maxX) / 2, cy = (minY + maxY) / 2, cz = (minZ + maxZ) / 2;
  const maxDim = Math.max(maxX - minX, maxY - minY, maxZ - minZ);
  const scale = 2.4 / maxDim;
  for (let i = 0; i < count; i++) {
    position[i * 3] = (position[i * 3] - cx) * scale;
    position[i * 3 + 1] = (position[i * 3 + 1] - cy) * scale;
    position[i * 3 + 2] = (position[i * 3 + 2] - cz) * scale;
  }

  const index = new Uint16Array(tris);

  // unique edges from triangles
  const seen = new Set<number>();
  const edgeList: number[] = [];
  const addEdge = (a: number, b: number) => {
    const key = a < b ? a * 100000 + b : b * 100000 + a;
    if (!seen.has(key)) {
      seen.add(key);
      edgeList.push(a, b);
    }
  };
  for (let i = 0; i < tris.length; i += 3) {
    addEdge(tris[i], tris[i + 1]);
    addEdge(tris[i + 1], tris[i + 2]);
    addEdge(tris[i + 2], tris[i]);
  }

  cache = { position, index, edges: new Uint16Array(edgeList), count };
  return cache;
}
