import * as THREE from 'three'

export const MESH_ROT = new THREE.Matrix4().makeRotationX(Math.PI)
export const MESH_ROT_INV = MESH_ROT.clone().invert()

export function matrixHasRotation(m: THREE.Matrix4): boolean {
  const e = m.elements
  return (
    Math.abs(e[0] - 1) > 1e-4 ||
    Math.abs(e[5] - 1) > 1e-4 ||
    Math.abs(e[10] - 1) > 1e-4 ||
    Math.abs(e[1]) > 1e-4 ||
    Math.abs(e[2]) > 1e-4 ||
    Math.abs(e[4]) > 1e-4 ||
    Math.abs(e[6]) > 1e-4 ||
    Math.abs(e[8]) > 1e-4 ||
    Math.abs(e[9]) > 1e-4
  )
}

/** postprocess_glb bakes COLMAP→Y-up rotation into the GLB root node. */
export function gltfHasBakedMeshRotation(root: THREE.Object3D): boolean {
  let baked = false
  root.traverse((node) => {
    if (baked || node === root) return
    node.updateMatrix()
    if (matrixHasRotation(node.matrix)) baked = true
  })
  return baked
}

export function colmapToScene(x: number, y: number, z: number): THREE.Vector3 {
  const v = new THREE.Vector3(x, y, z)
  v.applyMatrix4(MESH_ROT)
  return v
}

export function sceneToColmap(v: THREE.Vector3): THREE.Vector3 {
  const p = v.clone()
  p.applyMatrix4(MESH_ROT_INV)
  return p
}

export function colmapToSplatScene(x: number, y: number, z: number): THREE.Vector3 {
  return new THREE.Vector3(x, y, z)
}

/** ERP image center in COLMAP when pano look dir is (0, 0, -1) in Three.js Y-up. */
const DEFAULT_ERP_CENTER_COLMAP = new THREE.Vector3(0, 0, 1)
const _alignQuat = new THREE.Quaternion()
const _tmpForward = new THREE.Vector3()
const _tmpColmap = new THREE.Vector3()
const Y_AXIS = new THREE.Vector3(0, 1, 0)

/** Panorama viewer (Three.js Y-up) → COLMAP direction (Y-down, Z-forward). */
export function panoDirToColmap(dir: THREE.Vector3): THREE.Vector3 {
  return new THREE.Vector3(dir.x, -dir.y, -dir.z)
}

/** ffmpeg v360 ERP center vs COLMAP v0 forward: constant yaw offset. */
const ERP_FORWARD_YAW = Math.PI / 2

/** ERP texture is offset vs COLMAP world; fallback rotate when forward is missing. */
const PANORAMA_YAW_OFFSET = -Math.PI / 2

function applyFallbackYawOffset(dirColmap: THREE.Vector3): THREE.Vector3 {
  return dirColmap.clone().applyAxisAngle(Y_AXIS, PANORAMA_YAW_OFFSET).normalize()
}

/** Panorama viewer look direction → scene direction for markers/arrows. */
export function panoramaViewToSceneDir(
  dir: THREE.Vector3,
  space: 'mesh' | 'splat',
  frameForward?: { x: number; y: number; z: number },
): THREE.Vector3 {
  _tmpColmap.copy(panoDirToColmap(dir))

  if (frameForward) {
    _tmpForward.set(frameForward.x, frameForward.y, frameForward.z).normalize()
    _alignQuat.setFromUnitVectors(DEFAULT_ERP_CENTER_COLMAP, _tmpForward)
    _tmpColmap.applyQuaternion(_alignQuat).normalize()
    _tmpColmap.applyAxisAngle(Y_AXIS, ERP_FORWARD_YAW).normalize()
  } else {
    _tmpColmap.copy(applyFallbackYawOffset(_tmpColmap))
  }

  if (space === 'mesh') {
    return colmapToScene(_tmpColmap.x, _tmpColmap.y, _tmpColmap.z).normalize()
  }
  return _tmpColmap.clone().normalize()
}

/** Horizontal heading in degrees (0° = default forward, clockwise). */
export function panoramaHeadingDeg(dir: THREE.Vector3): number {
  return (Math.atan2(dir.x, -dir.z) * 180) / Math.PI
}

export function nearestFrameLocal(
  frames: Array<{ id: number; x: number; y: number; z: number; url: string }>,
  x: number,
  y: number,
  z: number,
) {
  let best: (typeof frames)[0] | null = null
  let bestD = Infinity
  for (const fr of frames) {
    const dx = fr.x - x
    const dy = fr.y - y
    const dz = fr.z - z
    const d = Math.sqrt(dx * dx + dy * dy + dz * dz)
    if (d < bestD) {
      bestD = d
      best = fr
    }
  }
  return best ? { frame: best, distance: bestD } : null
}
