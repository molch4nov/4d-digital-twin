export interface PanoramaFrame {
  id: number
  x: number
  y: number
  z: number
  url: string
  path?: string
  /** COLMAP camera forward (+Z) for ERP center (view v0). */
  forward?: { x: number; y: number; z: number }
}

export interface PanoramaManifest {
  task_id: string
  coordinate: string
  viewer_type: string
  mesh_rotation?: string
  asset_url?: string
  frames: PanoramaFrame[]
}

export interface NearestPanoramaResponse {
  frame_id: number
  distance: number
  erp_url: string
  position: { x: number; y: number; z: number }
}
