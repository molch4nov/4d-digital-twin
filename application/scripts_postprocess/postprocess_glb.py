#!/usr/bin/env python3
"""
Post-process an OpenMVS-exported GLB so it opens correctly in browsers.

OpenMVS TextureMesh --export-type glb produces files with three issues:

1. Materials use ``baseColorFactor = [0.4, 0.4, 0.4, 1.0]``, which multiplies the
   atlas by 40%. The mesh ends up looking ~2.5x darker than it should.
2. Materials are ``doubleSided: false``. With photogrammetry meshes the back
   faces are often visible (the mesh is often not closed / normals are wrong),
   so a lot of the model shows up as black.
3. The scene root node has no transform. OpenMVS keeps geometry in the SfM
   world frame, where the "up" axis is usually -Y or -Z (CV convention), so in
   a Y-up viewer (three.js / model-viewer) the model is upside-down.

This script rewrites the GLB:

* sets all material ``baseColorFactor`` to [1,1,1,1] and ``doubleSided=true``;
* writes a 4x4 ``matrix`` on the scene root node so the model appears upright
  in a Y-up viewer (rotation is auto-detected from vertex stats unless
  ``--rotation`` is set explicitly);
* optionally downscales every embedded PNG/JPEG texture to ``--max-texture``
  pixels (default 4096) to keep the file size browser-friendly;
* packs OpenMVS ``images[].uri`` sidecar files into the GLB buffer so a single
  file opens in drag-and-drop viewers (``--skip-embed-external`` keeps URIs);
* optionally clips the ceiling (``--clip-ceiling``) in OpenMVS local coords
  (Y-down: removes mesh below ``p4(Y) + margin``, keeps floor at high Y).

Only the JSON manifest is rewritten in-place when textures are not resized;
when textures ARE resized, the binary blob is rebuilt and offsets fixed up.

Usage:
    python postprocess_glb.py input.glb [output.glb] \
        [--rotation auto|x180|x90|x-90|y180|none] \
        [--max-texture 4096] \
        [--basecolor 1.0] \
        [--clip-ceiling] [--clip-y-max METRES] [--clip-ceiling-margin 0.12]
"""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def human_bytes(n: int) -> str:
    """Human-readable size (avoid ``0.0 MB`` for ~30 KiB GLBs)."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.2f} MiB"


# ----------------------------- GLB raw I/O ------------------------------------

def _pad4(n: int) -> int:
    return (4 - (n % 4)) % 4


def read_glb(path: Path) -> tuple[dict, bytes]:
    data = path.read_bytes()
    magic, version, total = struct.unpack("<4sII", data[:12])
    if magic != b"glTF":
        raise ValueError(f"Not a GLB file: {path}")
    if version != 2:
        raise ValueError(f"Unsupported glTF version {version}")

    json_len, json_type = struct.unpack("<I4s", data[12:20])
    if json_type != b"JSON":
        raise ValueError("First chunk is not JSON")
    gltf = json.loads(data[20 : 20 + json_len])

    off = 20 + json_len
    if off >= len(data):
        return gltf, b""
    bin_len, bin_type = struct.unpack("<I4s", data[off : off + 8])
    if bin_type != b"BIN\x00":
        raise ValueError("Second chunk is not BIN")
    blob = bytes(data[off + 8 : off + 8 + bin_len])
    return gltf, blob


def write_glb(path: Path, gltf: dict, blob: bytes) -> None:
    j = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    j_pad = _pad4(len(j))
    j_padded = j + (b" " * j_pad)
    b_pad = _pad4(len(blob))
    blob_padded = blob + (b"\x00" * b_pad)
    total = 12 + 8 + len(j_padded) + 8 + len(blob_padded)
    with path.open("wb") as f:
        f.write(b"glTF")
        f.write(struct.pack("<II", 2, total))
        f.write(struct.pack("<I", len(j_padded)))
        f.write(b"JSON")
        f.write(j_padded)
        f.write(struct.pack("<I", len(blob_padded)))
        f.write(b"BIN\x00")
        f.write(blob_padded)


# ----------------------------- Vertex stats -----------------------------------

_COMP_SIZE = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_COMP_DTYPE = {5120: "i1", 5121: "u1", 5122: "i2", 5123: "u2", 5125: "u4", 5126: "f4"}
_TYPE_COUNT = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def _accessor_view(gltf: dict, blob: bytes, accessor_index: int) -> np.ndarray:
    acc = gltf["accessors"][accessor_index]
    bv = gltf["bufferViews"][acc["bufferView"]]
    offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = acc["count"] * _TYPE_COUNT[acc["type"]]
    dtype = np.dtype(_COMP_DTYPE[acc["componentType"]])
    return np.frombuffer(blob, dtype=dtype, count=count, offset=offset).reshape(
        acc["count"], _TYPE_COUNT[acc["type"]]
    )


def _rotation_matrix4(rotation_name: str) -> np.ndarray:
    """Column-major 4x4 (glTF layout)."""
    m = _ROTATIONS[rotation_name]
    return np.array(m, dtype=np.float64).reshape(4, 4, order="F")


def positions_in_display_space(positions: np.ndarray, rotation_name: str) -> np.ndarray:
    """Map local vertex positions to Y-up display coordinates (root rotation only)."""
    if positions.shape[0] == 0:
        return positions
    m = _rotation_matrix4(rotation_name)
    r = m[:3, :3]
    t = m[:3, 3]
    return (positions @ r.T) + t


def auto_ceiling_y_min_local(y_local: np.ndarray, margin: float) -> float:
    """Min local-Y to keep when removing the ceiling (OpenMVS/COLMAP Y-down).

    Ceiling sits at the *low* Y extreme; floor at high Y.  Cut everything
    below ``p4 + margin`` in mesh-local coordinates (independent of GLB root
    rotation applied later for viewing).
    """
    if y_local.shape[0] == 0:
        return 0.0
    return float(np.percentile(y_local[:, 1], 4.0)) + margin


def _accessor_component_dtype(component_type: int) -> np.dtype:
    return np.dtype(_COMP_DTYPE[component_type])


def _read_indices(gltf: dict, blob: bytes, accessor_index: int) -> np.ndarray:
    acc = gltf["accessors"][accessor_index]
    bv = gltf["bufferViews"][acc["bufferView"]]
    offset = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    count = acc["count"]
    dtype = _accessor_component_dtype(acc["componentType"])
    return np.frombuffer(blob, dtype=dtype, count=count, offset=offset).astype(np.int64)


def clip_meshes_ceiling(
    gltf: dict,
    blob: bytes,
    y_min_local: float,
) -> tuple[dict, bytes, int]:
    """Drop ceiling triangles (OpenMVS local Y-down: remove verts with Y < y_min)."""
    removed_faces = 0
    new_blob = bytearray()
    new_accessors: list[dict] = []
    new_buffer_views: list[dict] = []
    new_meshes: list[dict] = []

    def append_array(data: bytes, target: int = 34962) -> int:
        pad = _pad4(len(new_blob))
        new_blob.extend(b"\x00" * pad)
        off = len(new_blob)
        new_blob.extend(data)
        bv_idx = len(new_buffer_views)
        new_buffer_views.append(
            {"buffer": 0, "byteOffset": off, "byteLength": len(data), "target": target}
        )
        return bv_idx

    for mesh in gltf.get("meshes", []):
        new_primitives = []
        for prim in mesh["primitives"]:
            pos_idx = prim["attributes"].get("POSITION")
            if pos_idx is None:
                new_primitives.append(prim)
                continue
            pos = _accessor_view(gltf, blob, pos_idx).astype(np.float64)

            if "indices" in prim:
                idx = _read_indices(gltf, blob, prim["indices"])
                tris = idx.reshape(-1, 3)
            else:
                tris = np.arange(len(pos), dtype=np.int64).reshape(-1, 3)

            keep = []
            for tri in tris:
                ys = pos[tri, 1]
                if float(ys.min()) >= y_min_local:
                    keep.append(tri)
                else:
                    removed_faces += 1

            if not keep:
                continue
            keep_arr = np.stack(keep, axis=0)
            used = np.unique(keep_arr.ravel())
            remap = {int(old): i for i, old in enumerate(used)}
            new_pos = pos[used].astype(np.float32)
            new_tris = np.vectorize(remap.get)(keep_arr).astype(np.uint32)

            new_attrs: dict[str, int] = {}
            pos_bv = append_array(new_pos.tobytes())
            new_accessors.append(
                {
                    "bufferView": pos_bv,
                    "componentType": 5126,
                    "count": len(new_pos),
                    "type": "VEC3",
                    "min": new_pos.min(axis=0).tolist(),
                    "max": new_pos.max(axis=0).tolist(),
                }
            )
            new_attrs["POSITION"] = len(new_accessors) - 1

            if "TEXCOORD_0" in prim["attributes"]:
                uv = _accessor_view(gltf, blob, prim["attributes"]["TEXCOORD_0"]).astype(np.float32)
                new_uv = uv[used]
                uv_bv = append_array(new_uv.tobytes())
                new_accessors.append(
                    {
                        "bufferView": uv_bv,
                        "componentType": 5126,
                        "count": len(new_uv),
                        "type": "VEC2",
                    }
                )
                new_attrs["TEXCOORD_0"] = len(new_accessors) - 1

            idx_bv = append_array(new_tris.astype(np.uint32).tobytes(), target=34963)
            new_accessors.append(
                {
                    "bufferView": idx_bv,
                    "componentType": 5125,
                    "count": int(new_tris.size),
                    "type": "SCALAR",
                }
            )
            new_prim = {
                "mode": prim.get("mode", 4),
                "attributes": new_attrs,
                "indices": len(new_accessors) - 1,
                "material": prim.get("material", 0),
            }
            new_primitives.append(new_prim)
        new_meshes.append({**mesh, "primitives": new_primitives})

    # Preserve image bufferViews from original (textures unchanged).
    image_bv_start = len(new_buffer_views)
    old_bvs = gltf.get("bufferViews", [])
    old_images = gltf.get("images") or []
    image_bv_map: dict[int, int] = {}
    for img in old_images:
        old_bv = img.get("bufferView")
        if old_bv is None:
            continue
        bv = old_bvs[old_bv]
        pad = _pad4(len(new_blob))
        new_blob.extend(b"\x00" * pad)
        off = len(new_blob)
        start = bv.get("byteOffset", 0)
        data = blob[start : start + bv["byteLength"]]
        new_blob.extend(data)
        new_idx = len(new_buffer_views)
        new_buffer_views.append(
            {"buffer": 0, "byteOffset": off, "byteLength": len(data)}
        )
        image_bv_map[old_bv] = new_idx

    new_gltf = {**gltf, "meshes": new_meshes, "accessors": new_accessors, "bufferViews": new_buffer_views}
    if new_gltf.get("buffers"):
        new_gltf["buffers"] = [{"byteLength": len(new_blob)}]
    for img in new_gltf.get("images") or []:
        old_bv = img.get("bufferView")
        if old_bv is not None and old_bv in image_bv_map:
            img["bufferView"] = image_bv_map[old_bv]

    return new_gltf, bytes(new_blob), removed_faces


def collect_positions(gltf: dict, blob: bytes) -> np.ndarray:
    chunks = []
    for mesh in gltf.get("meshes", []):
        for prim in mesh["primitives"]:
            pos_idx = prim["attributes"].get("POSITION")
            if pos_idx is None:
                continue
            chunks.append(_accessor_view(gltf, blob, pos_idx))
    if not chunks:
        return np.zeros((0, 3), dtype=np.float32)
    return np.concatenate(chunks, axis=0)


# ----------------------------- Rotation logic ---------------------------------

# Column-major 4x4 (glTF stores matrices column-major).
_ROTATIONS = {
    "none": [1, 0, 0, 0,  0, 1, 0, 0,  0, 0, 1, 0,  0, 0, 0, 1],
    "x180": [1, 0, 0, 0,  0, -1, 0, 0,  0, 0, -1, 0,  0, 0, 0, 1],
    # +90° around X (Y-down OpenMVS -> Y-up glTF), keeps the scene right-side-up
    # when the OpenMVS "vertical" is the +Z axis.
    "x90":  [1, 0, 0, 0,  0, 0, 1, 0,  0, -1, 0, 0,  0, 0, 0, 1],
    "x-90": [1, 0, 0, 0,  0, 0, -1, 0,  0, 1, 0, 0,  0, 0, 0, 1],
    "y180": [-1, 0, 0, 0,  0, 1, 0, 0,  0, 0, -1, 0,  0, 0, 0, 1],
}


def auto_rotation(positions: np.ndarray) -> tuple[str, str]:
    """Heuristically choose a root rotation for a Y-up viewer.

    A typical 360-photogrammetry capture has the camera moving in a roughly
    horizontal plane, so the axis of the scene with the smallest spread is the
    vertical axis. Combined with the sign of the centroid we can guess the
    OpenMVS up direction and rotate accordingly.
    """
    if positions.shape[0] == 0:
        return "none", "no vertices"
    stds = positions.std(axis=0)
    centroid = positions.mean(axis=0)
    vertical = int(np.argmin(stds))
    sign = -1.0 if centroid[vertical] < 0 else 1.0
    reason = f"vertical=axis{vertical} (std={stds.tolist()}); centroid={centroid.tolist()}"
    # We want vertical = Y(+). Determine the rotation that maps the chosen axis
    # to +Y.
    if vertical == 1:
        # Already Y; only flip if camera is "above" the model (centroid Y > 0).
        return ("x180" if sign > 0 else "none"), reason
    if vertical == 2:
        # Z is the OpenMVS "up"; rotate to +Y.
        return ("x-90" if sign > 0 else "x90"), reason
    # vertical == 0 (X is up) is extremely unusual; fall back to x180 which is
    # safe for OpenMVS's most common Y-down output.
    return "x180", reason + " (fallback)"


# ----------------------------- Material fixes ---------------------------------

def patch_materials(gltf: dict, base_color: float, double_sided: bool) -> int:
    n = 0
    for mat in gltf.get("materials", []):
        pbr = mat.setdefault("pbrMetallicRoughness", {})
        if "baseColorTexture" in pbr:
            pbr["baseColorFactor"] = [base_color, base_color, base_color, 1.0]
        else:
            existing = pbr.get("baseColorFactor", [base_color, base_color, base_color, 1.0])
            pbr["baseColorFactor"] = [
                base_color, base_color, base_color, existing[3] if len(existing) > 3 else 1.0
            ]
        mat["doubleSided"] = double_sided
        # roughness 0.9 from OpenMVS looks washed out; 0.7 is a better default.
        if "roughnessFactor" in pbr and pbr["roughnessFactor"] > 0.85:
            pbr["roughnessFactor"] = 0.7
        n += 1
    return n


def embed_external_images(
    gltf: dict, blob: bytes, glb_path: Path
) -> tuple[dict, bytes, int]:
    """OpenMVS often writes GLB geometry with ``images[].uri`` pointing to a
    sibling ``.png``. Browsers / drag-drop need a single self-contained GLB.
    Append each external file into buffer 0 and replace ``uri`` with ``bufferView``.
    """
    n_embedded = 0
    out_blob = bytearray(blob)
    bvs = gltf.setdefault("bufferViews", [])
    glb_dir = glb_path.parent

    for img in gltf.get("images") or []:
        if "bufferView" in img:
            continue
        uri = img.get("uri")
        if not uri:
            continue
        path = (glb_dir / uri).resolve()
        if not path.is_file():
            print(
                f"[postprocess_glb] WARN: external image not found (model may look untextured in browser): {path}",
                file=sys.stderr,
            )
            continue
        raw = path.read_bytes()
        pad = _pad4(len(out_blob))
        out_blob.extend(b"\x00" * pad)
        byte_offset = len(out_blob)
        out_blob.extend(raw)
        bv_index = len(bvs)
        bvs.append(
            {
                "buffer": 0,
                "byteOffset": byte_offset,
                "byteLength": len(raw),
            }
        )
        ext = path.suffix.lower()
        if ext == ".png":
            mime = "image/png"
        elif ext in (".jpg", ".jpeg"):
            mime = "image/jpeg"
        else:
            mime = "application/octet-stream"
        img.pop("uri", None)
        img.pop("name", None)
        img["bufferView"] = bv_index
        img["mimeType"] = mime
        n_embedded += 1

    if gltf.get("buffers"):
        gltf["buffers"][0]["byteLength"] = len(out_blob)
    return gltf, bytes(out_blob), n_embedded


def patch_root_node(gltf: dict, rotation_name: str) -> None:
    matrix = _ROTATIONS[rotation_name]
    scenes = gltf.get("scenes")
    if not scenes:
        return
    root_index = scenes[0]["nodes"][0]
    root = gltf["nodes"][root_index]
    # If the existing root already has a transform, wrap it under a new parent
    # carrying the rotation rather than overwriting user transforms.
    if any(k in root for k in ("matrix", "rotation", "translation", "scale")):
        new_root = {"name": "y_up_root", "children": [root_index]}
        if matrix != _ROTATIONS["none"]:
            new_root["matrix"] = matrix
        gltf["nodes"].append(new_root)
        scenes[0]["nodes"][0] = len(gltf["nodes"]) - 1
        return
    if matrix != _ROTATIONS["none"]:
        root["matrix"] = matrix


# ----------------------------- Texture downscale ------------------------------

def _decode_image_from_bufferview(blob: bytes, bv: dict) -> tuple[Image.Image, str]:
    start = bv.get("byteOffset", 0)
    length = bv["byteLength"]
    raw = blob[start : start + length]
    img = Image.open(io.BytesIO(raw))
    img.load()
    fmt = (img.format or "PNG").upper()
    return img, fmt


def _encode_image(img: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    fmt = fmt.upper()
    if fmt == "JPEG":
        img.convert("RGB").save(buf, format="JPEG", quality=88, optimize=True)
    else:
        img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def downscale_textures(
    gltf: dict, blob: bytes, max_size: int, force_jpeg: bool
) -> bytes:
    """Re-encode every embedded image to a smaller size and rebuild the binary
    blob with updated buffer-view offsets.
    """

    images = gltf.get("images") or []
    if not images:
        return blob

    # Collect ranges of all bufferViews currently in the buffer so we can keep
    # the non-image data intact and only swap the image ranges.
    buffer_views = gltf["bufferViews"]
    image_bv_indices = {
        img["bufferView"]: i for i, img in enumerate(images) if "bufferView" in img
    }

    new_blob = bytearray()
    new_bufferviews = []
    # bvs are usually ordered by offset, but be safe.
    order = sorted(range(len(buffer_views)), key=lambda k: buffer_views[k].get("byteOffset", 0))

    image_new_bytes: dict[int, bytes] = {}
    image_new_mime: dict[int, str] = {}
    for bv_idx, img_idx in image_bv_indices.items():
        bv = buffer_views[bv_idx]
        img, fmt = _decode_image_from_bufferview(blob, bv)
        if max(img.size) > max_size:
            scale = max_size / float(max(img.size))
            new_size = (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale)))
            print(
                f"  texture[{img_idx}] {img.size} -> {new_size}  ({fmt}, "
                f"{human_bytes(bv['byteLength'])} raw)",
                flush=True,
            )
            img = img.resize(new_size, Image.LANCZOS)
        target_fmt = "JPEG" if force_jpeg else fmt
        image_new_bytes[bv_idx] = _encode_image(img, target_fmt)
        image_new_mime[bv_idx] = "image/jpeg" if target_fmt == "JPEG" else (
            "image/png" if fmt == "PNG" else f"image/{fmt.lower()}"
        )

    # rebuild
    new_bvs = [None] * len(buffer_views)
    for k in order:
        bv = buffer_views[k]
        align_pad = _pad4(len(new_blob))
        new_blob.extend(b"\x00" * align_pad)
        new_offset = len(new_blob)
        if k in image_new_bytes:
            data = image_new_bytes[k]
        else:
            old_off = bv.get("byteOffset", 0)
            data = blob[old_off : old_off + bv["byteLength"]]
        new_blob.extend(data)
        new_bv = {**bv, "byteOffset": new_offset, "byteLength": len(data)}
        # drop byteStride if it would now be meaningless (it stays the same).
        new_bvs[k] = new_bv

    gltf["bufferViews"] = new_bvs
    if gltf["buffers"]:
        gltf["buffers"][0]["byteLength"] = len(new_blob)

    # Update mime types on images if we converted format.
    for bv_idx, mime in image_new_mime.items():
        img_idx = image_bv_indices[bv_idx]
        gltf["images"][img_idx]["mimeType"] = mime

    return bytes(new_blob)


# ------------------------------- CLI ------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("input", type=Path)
    ap.add_argument("output", nargs="?", type=Path)
    ap.add_argument(
        "--rotation",
        choices=list(_ROTATIONS.keys()) + ["auto"],
        default="auto",
        help="Root-node rotation to apply (default: auto-detect)",
    )
    ap.add_argument(
        "--max-texture",
        type=int,
        default=0,
        help="If >0, downscale every embedded image to this max side length.",
    )
    ap.add_argument(
        "--basecolor",
        type=float,
        default=1.0,
        help="Material baseColorFactor multiplier (default 1.0 - fixes OpenMVS 0.4 dimming).",
    )
    ap.add_argument(
        "--single-sided",
        action="store_true",
        help="Keep materials single-sided (default flips to doubleSided=true).",
    )
    ap.add_argument(
        "--jpeg",
        action="store_true",
        help="When downscaling, force JPEG output (smaller for photo-textures).",
    )
    ap.add_argument(
        "--skip-embed-external",
        action="store_true",
        help="Do not pack sibling PNG/JPEG (images[].uri) into the GLB buffer.",
    )
    ap.add_argument(
        "--clip-ceiling",
        action="store_true",
        help="Remove mesh above the ceiling (Y-up display frame, after rotation).",
    )
    ap.add_argument(
        "--clip-y-min",
        type=float,
        default=None,
        metavar="METRES",
        help="Min local-Y to keep (OpenMVS/COLMAP: ceiling is below this; default: auto p4+margin).",
    )
    ap.add_argument(
        "--clip-y-max",
        type=float,
        default=None,
        metavar="METRES",
        help=argparse.SUPPRESS,
    )
    ap.add_argument(
        "--clip-ceiling-margin",
        type=float,
        default=0.12,
        metavar="METRES",
        help="With --clip-ceiling: keep this much room below the auto-detected ceiling (default 0.12).",
    )
    args = ap.parse_args()

    inp = args.input
    out = args.output or inp.with_name(inp.stem + "_web.glb")
    if not inp.exists():
        print(f"input not found: {inp}", file=sys.stderr)
        return 2

    in_sz = inp.stat().st_size
    print(f"[postprocess_glb] input  : {inp}  ({human_bytes(in_sz)})")
    print(f"[postprocess_glb] output : {out}")

    gltf, blob = read_glb(inp)

    if not args.skip_embed_external:
        gltf, blob, n_ext = embed_external_images(gltf, blob, inp)
        if n_ext:
            print(
                f"[postprocess_glb] embedded {n_ext} external texture file(s) "
                f"(GLB buffer now {human_bytes(len(blob))})"
            )

    positions = collect_positions(gltf, blob)
    print(
        f"[postprocess_glb] geometry: {positions.shape[0]} verts, "
        f"min={positions.min(axis=0).tolist() if len(positions) else []}, "
        f"max={positions.max(axis=0).tolist() if len(positions) else []}"
    )

    if args.rotation == "auto":
        rot, why = auto_rotation(positions)
        print(f"[postprocess_glb] rotation: auto -> {rot}  ({why})")
    else:
        rot = args.rotation
        print(f"[postprocess_glb] rotation: {rot}")

    if args.clip_ceiling or args.clip_y_max is not None or args.clip_y_min is not None:
        if args.clip_y_min is not None:
            y_min = args.clip_y_min
            print(f"[postprocess_glb] clip ceiling: local keep Y≥{y_min:.4f} (manual)")
        elif args.clip_y_max is not None:
            # Legacy alias: treat clip-y-max as “cut high Y” (floor at top) — invert to local min.
            y_min = args.clip_y_max
            print(
                f"[postprocess_glb] WARN: --clip-y-max is deprecated for ceiling; "
                f"use --clip-y-min (local min Y to keep). Using {y_min:.4f}",
                file=sys.stderr,
            )
        else:
            y_min = auto_ceiling_y_min_local(positions, args.clip_ceiling_margin)
            p4 = float(np.percentile(positions[:, 1], 4))
            p96 = float(np.percentile(positions[:, 1], 96))
            print(
                f"[postprocess_glb] clip ceiling: local keep Y≥{y_min:.4f} m "
                f"(COLMAP Y-down, p4={p4:.3f} p96={p96:.3f}, margin={args.clip_ceiling_margin:.3f})"
            )
        gltf, blob, n_rm = clip_meshes_ceiling(gltf, blob, y_min)
        positions = collect_positions(gltf, blob)
        print(
            f"[postprocess_glb] ceiling clip: removed {n_rm} triangles, "
            f"{positions.shape[0]} verts remain, "
            f"y=[{positions.min(axis=0)[1]:.3f}, {positions.max(axis=0)[1]:.3f}] local"
        )

    patch_root_node(gltf, rot)
    n_mats = patch_materials(gltf, args.basecolor, double_sided=not args.single_sided)
    print(
        f"[postprocess_glb] materials patched: {n_mats} "
        f"(baseColorFactor={args.basecolor}, doubleSided={not args.single_sided})"
    )

    if args.max_texture > 0:
        print(f"[postprocess_glb] downscaling textures to <= {args.max_texture}px ...")
        blob = downscale_textures(gltf, blob, args.max_texture, force_jpeg=args.jpeg)

    write_glb(out, gltf, blob)
    out_sz = out.stat().st_size
    print(f"[postprocess_glb] done -> {out}  ({human_bytes(out_sz)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
