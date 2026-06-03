export type AssetKind = 'mesh' | 'splat' | 'openmvs-ply' | 'unknown'

export function fileExtension(nameOrUrl: string): string {
  const raw = (nameOrUrl || '').split('?')[0].split('#')[0]
  const base = raw.split('/').pop() || raw
  const dot = base.lastIndexOf('.')
  return dot >= 0 ? base.slice(dot + 1).toLowerCase() : ''
}

export function assetKind(nameOrUrl: string): AssetKind {
  const ext = fileExtension(nameOrUrl)
  const low = (nameOrUrl || '').toLowerCase()

  if (ext === 'glb' || ext === 'gltf') return 'mesh'
  if (ext === 'splat' || ext === 'ksplat') return 'splat'
  if (ext === 'ply') {
    if (/mesh|dense|scene|texture|refine|reconstruct/i.test(low)) return 'openmvs-ply'
    return 'splat'
  }
  return 'unknown'
}
