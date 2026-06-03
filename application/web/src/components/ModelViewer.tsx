import { useEffect, useRef, useState, useCallback } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js'
import * as GaussianSplats3D from '@mkkellogg/gaussian-splats-3d'
import { assetKind } from '../lib/assetKind'
import { colmapToScene, colmapToSplatScene, gltfHasBakedMeshRotation, nearestFrameLocal, panoramaViewToSceneDir, sceneToColmap, MESH_ROT } from '../lib/coords'
import { fetchManifest, fetchNearestPanorama } from '../api/client'
import type { PanoramaFrame, PanoramaManifest } from '../types/panorama'
import PanoramaViewer from './PanoramaViewer'
import './ModelViewer.css'

interface ModelViewerProps {
  taskId: string
  resultUrl: string | undefined
  fileName: string
}

function fixOpenMvsMaterials(root: THREE.Object3D) {
  root.traverse((o) => {
    const mesh = o as THREE.Mesh
    if (!mesh.isMesh || !mesh.material) return
    const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
    mats.forEach((mat) => {
      const m = mat as THREE.MeshStandardMaterial
      m.side = THREE.DoubleSide
      m.color?.setRGB(1, 1, 1)
      m.emissive?.setRGB(0, 0, 0)
      if (m.map) {
        m.map.flipY = false
        m.map.colorSpace = THREE.SRGBColorSpace
        m.map.needsUpdate = true
      }
    })
  })
}

function markerSizeFromFrames(frames: PanoramaFrame[]): number {
  if (!frames.length) return 0.1
  let minX = Infinity, minY = Infinity, minZ = Infinity
  let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity
  for (const f of frames) {
    minX = Math.min(minX, f.x); maxX = Math.max(maxX, f.x)
    minY = Math.min(minY, f.y); maxY = Math.max(maxY, f.y)
    minZ = Math.min(minZ, f.z); maxZ = Math.max(maxZ, f.z)
  }
  const span = Math.hypot(maxX - minX, maxY - minY, maxZ - minZ)
  return Math.max(span * 0.012, 0.05)
}


function frameIdFromUrl(url: string): number | null {
  const match = url.match(/erp_(\d+)/i)
  return match ? Number.parseInt(match[1], 10) : null
}

function addPanoramaMarkers(
  scene: THREE.Scene,
  frames: PanoramaFrame[],
  space: 'mesh' | 'splat',
  meshRoot?: THREE.Object3D,
): { group: THREE.Group; markers: Map<number, THREE.Mesh> } {
  const group = new THREE.Group()
  group.name = 'panorama-markers'
  const markers = new Map<number, THREE.Mesh>()

  const markerSize = meshRoot
    ? (() => {
        const box = new THREE.Box3().setFromObject(meshRoot)
        const size = box.getSize(new THREE.Vector3())
        return Math.max(size.length() * 0.012, 0.05)
      })()
    : markerSizeFromFrames(frames)

  const toScene = space === 'mesh' ? colmapToScene : colmapToSplatScene
  const pickRadius = markerSize * 4

  for (const frame of frames) {
    const pos = toScene(frame.x, frame.y, frame.z)
    const pick = new THREE.Mesh(
      new THREE.SphereGeometry(pickRadius, 8, 8),
      new THREE.MeshBasicMaterial({ visible: false }),
    )
    pick.position.copy(pos)
    pick.userData = { panoramaFrame: frame, frameId: frame.id, isMarker: true }

    group.add(pick)
    markers.set(frame.id, pick)
  }

  scene.add(group)
  return { group, markers }
}

function createCameraFrustum(depth: number, fovDeg = 55, aspect = 1.35): THREE.Group {
  const group = new THREE.Group()
  group.name = 'camera-frustum'

  const halfH = depth * Math.tan((fovDeg * Math.PI) / 360)
  const halfW = halfH * aspect
  const z = -depth

  const corners = [
    new THREE.Vector3(-halfW, -halfH, z),
    new THREE.Vector3(halfW, -halfH, z),
    new THREE.Vector3(halfW, halfH, z),
    new THREE.Vector3(-halfW, halfH, z),
  ]

  const linePoints: THREE.Vector3[] = []
  for (const corner of corners) {
    linePoints.push(new THREE.Vector3(0, 0, 0), corner)
  }
  for (let i = 0; i < corners.length; i += 1) {
    linePoints.push(corners[i], corners[(i + 1) % corners.length])
  }

  const lineGeom = new THREE.BufferGeometry().setFromPoints(linePoints)
  const lines = new THREE.LineSegments(
    lineGeom,
    new THREE.LineBasicMaterial({ color: 0xf97316, transparent: true, opacity: 0.95 }),
  )
  group.add(lines)

  const faceVerts: number[] = []
  const pushTri = (a: THREE.Vector3, b: THREE.Vector3, c: THREE.Vector3) => {
    faceVerts.push(a.x, a.y, a.z, b.x, b.y, b.z, c.x, c.y, c.z)
  }
  pushTri(new THREE.Vector3(), corners[0], corners[1])
  pushTri(new THREE.Vector3(), corners[1], corners[2])
  pushTri(new THREE.Vector3(), corners[2], corners[3])
  pushTri(new THREE.Vector3(), corners[3], corners[0])

  const faceGeom = new THREE.BufferGeometry()
  faceGeom.setAttribute('position', new THREE.Float32BufferAttribute(faceVerts, 3))
  const faces = new THREE.Mesh(
    faceGeom,
    new THREE.MeshBasicMaterial({
      color: 0xf97316,
      transparent: true,
      opacity: 0.14,
      side: THREE.DoubleSide,
      depthWrite: false,
    }),
  )
  group.add(faces)

  return group
}

function disposeCameraFrustum(group: THREE.Group) {
  group.traverse((o) => {
    if (!(o instanceof THREE.Mesh) && !(o instanceof THREE.LineSegments)) return
    o.geometry?.dispose()
    const mat = o.material
    if (Array.isArray(mat)) mat.forEach((m) => m.dispose())
    else mat?.dispose()
  })
}

function orientCameraFrustum(
  group: THREE.Group,
  position: THREE.Vector3,
  forward: THREE.Vector3,
  up: THREE.Vector3,
) {
  const eye = position
  const target = eye.clone().add(forward)
  const mat = new THREE.Matrix4().lookAt(eye, target, up)
  group.quaternion.setFromRotationMatrix(mat)
  group.position.copy(eye)
}

interface SplatHit {
  origin: THREE.Vector3
}

interface SplatViewerHandle {
  renderer: THREE.WebGLRenderer
  camera: THREE.PerspectiveCamera
  splatMesh: object
  raycaster: {
    setFromCameraAndScreenPosition: (
      camera: THREE.Camera,
      screenPosition: THREE.Vector2,
      screenDimensions: THREE.Vector2,
    ) => void
    intersectSplatMesh: (splatMesh: object, outHits: SplatHit[]) => SplatHit[]
  }
  threeScene: THREE.Scene
}

export default function ModelViewer({ taskId, resultUrl, fileName: _fileName }: ModelViewerProps) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const markersOverlayRef = useRef<HTMLDivElement>(null)
  const [, setStatus] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle')
  const [error, setError] = useState<string | null>(null)

  const [panoramaUrl, setPanoramaUrl] = useState<string | null>(null)
  const [panoramaTitle, setPanoramaTitle] = useState('')
  const [hasPanoramas, setHasPanoramas] = useState(false)
  const activeFrameIdRef = useRef<number | null>(null)
  const viewOrientationRef = useRef<{ direction: THREE.Vector3; up: THREE.Vector3 } | null>(null)

  const openPanorama = useCallback((url: string, title: string, frameId?: number) => {
    const id = frameId ?? frameIdFromUrl(url)
    setPanoramaUrl(url)
    setPanoramaTitle(title)
    activeFrameIdRef.current = id
  }, [])

  const closePanorama = useCallback(() => {
    setPanoramaUrl(null)
    setPanoramaTitle('')
    activeFrameIdRef.current = null
    viewOrientationRef.current = null
  }, [])

  const handlePanoramaViewChange = useCallback((orientation: { direction: THREE.Vector3; up: THREE.Vector3 }) => {
    viewOrientationRef.current = orientation
  }, [])

  useEffect(() => {
    const viewport = viewportRef.current
    if (!resultUrl || !viewport) return

    const kind = assetKind(resultUrl)
    let disposed = false

    let renderer: THREE.WebGLRenderer | null = null
    let scene: THREE.Scene | null = null
    let camera: THREE.PerspectiveCamera | null = null
    let controls: OrbitControls | null = null
    let animId = 0
    let meshRoot: THREE.Object3D | null = null
    let markersGroup: THREE.Group | null = null
    let markerMeshes: Map<number, THREE.Mesh> = new Map()
    let viewFrustum: THREE.Group | null = null
    let markerSize = 0.1
    let raycaster: THREE.Raycaster | null = null
    let pointer = new THREE.Vector2()
    let splatViewer: SplatViewerHandle | null = null
    let meshResizeObserver: ResizeObserver | null = null
    let markerOverlayAnimId = 0
    let markerOverlayButtons: HTMLButtonElement[] = []
    let markerOverlayFrameIds: number[] = []
    let markerOverlayFrames: PanoramaFrame[] = []
    let markerOverlaySpace: 'mesh' | 'splat' = 'mesh'

    let manifest: PanoramaManifest | null = null
    const space: 'mesh' | 'splat' = kind === 'splat' ? 'splat' : 'mesh'

    const frameForward = () => {
      const frameId = activeFrameIdRef.current
      if (frameId == null || !manifest?.frames) return undefined
      return manifest.frames.find((f) => f.id === frameId)?.forward
    }

    const updateViewCameraFrustum = () => {
      if (!viewFrustum || !markersGroup) return
      const frameId = activeFrameIdRef.current
      const orientation = viewOrientationRef.current
      const marker = frameId !== null ? markerMeshes.get(frameId) : undefined
      if (!marker || !orientation) {
        viewFrustum.visible = false
        return
      }
      const fwd = frameForward()
      const forward = panoramaViewToSceneDir(orientation.direction, space, fwd)
      const up = panoramaViewToSceneDir(orientation.up, space, fwd)
      orientCameraFrustum(viewFrustum, marker.position, forward, up)
      viewFrustum.visible = true
    }

    const syncMarkerVisuals = () => {
      updateMarkerOverlayPositions()
      updateViewCameraFrustum()
      markerOverlayFrameIds.forEach((frameId, i) => {
        const btn = markerOverlayButtons[i]
        if (!btn) return
        btn.classList.toggle('panorama-dot-active', activeFrameIdRef.current === frameId)
      })
    }

    const pickPanoramaAtColmap = async (cx: number, cy: number, cz: number) => {
      const local = manifest?.frames
        ? nearestFrameLocal(manifest.frames, cx, cy, cz)
        : null
      if (local) {
        openPanorama(local.frame.url, `Кадр ${local.frame.id}`, local.frame.id)
        return
      }
      try {
        const hit = await fetchNearestPanorama(taskId, cx, cy, cz)
        openPanorama(hit.erp_url, `Кадр ${hit.frame_id}`, hit.frame_id)
      } catch {
        /* no panorama */
      }
    }

    let clickDownX = 0
    let clickDownY = 0
    let mouseDownOnCanvas = false

    const onCanvasMouseDown = (event: MouseEvent) => {
      mouseDownOnCanvas = true
      clickDownX = event.clientX
      clickDownY = event.clientY
    }

    const onSplatClick = async (event: MouseEvent) => {
      if (!splatViewer || kind !== 'splat') return
      if (
        mouseDownOnCanvas &&
        Math.hypot(event.clientX - clickDownX, event.clientY - clickDownY) > 6
      ) {
        mouseDownOnCanvas = false
        return
      }
      mouseDownOnCanvas = false

      const canvas = splatViewer.renderer.domElement
      const rect = canvas.getBoundingClientRect()
      const renderDimensions = new THREE.Vector2(rect.width, rect.height)
      const mousePos = new THREE.Vector2(event.clientX - rect.left, event.clientY - rect.top)

      pointer.x = (mousePos.x / renderDimensions.x) * 2 - 1
      pointer.y = -(mousePos.y / renderDimensions.y) * 2 + 1
      if (!raycaster) raycaster = new THREE.Raycaster()
      raycaster.setFromCamera(pointer, splatViewer.camera)

      const markerHits = raycaster.intersectObjects(markersGroup?.children ?? [], false)
      if (markerHits.length > 0) {
        const frame = markerHits[0].object.userData.panoramaFrame as PanoramaFrame
        if (frame?.url) {
          openPanorama(frame.url, `Кадр ${frame.id}`, frame.id)
          return
        }
      }

      const outHits: SplatHit[] = []
      splatViewer.raycaster.setFromCameraAndScreenPosition(
        splatViewer.camera,
        mousePos,
        renderDimensions,
      )
      splatViewer.raycaster.intersectSplatMesh(splatViewer.splatMesh, outHits)
      if (outHits.length > 0 && manifest?.frames?.length) {
        const p = outHits[0].origin
        await pickPanoramaAtColmap(p.x, p.y, p.z)
      }
    }

    const disposeMarkerOverlay = () => {
      if (markerOverlayAnimId) cancelAnimationFrame(markerOverlayAnimId)
      markerOverlayAnimId = 0
      markerOverlayButtons.forEach((btn) => btn.remove())
      markerOverlayButtons = []
      markerOverlayFrameIds = []
      markerOverlayFrames = []
      if (markersOverlayRef.current) markersOverlayRef.current.replaceChildren()
    }

    const updateMarkerOverlayPositions = () => {
      const overlay = markersOverlayRef.current
      if (!overlay || markerOverlayButtons.length === 0) return
      const cam = markerOverlaySpace === 'mesh' ? camera : splatViewer?.camera
      if (!cam) return

      const w = viewport.clientWidth
      const h = viewport.clientHeight
      if (w < 1 || h < 1) return

      const toScene = markerOverlaySpace === 'mesh' ? colmapToScene : colmapToSplatScene
      const temp = new THREE.Vector3()

      markerOverlayFrames.forEach((frame, i) => {
        const btn = markerOverlayButtons[i]
        if (!btn) return
        temp.copy(toScene(frame.x, frame.y, frame.z))
        temp.project(cam)
        if (temp.z >= 1) {
          btn.style.display = 'none'
          return
        }
        btn.style.display = 'block'
        btn.style.left = `${(temp.x * 0.5 + 0.5) * w}px`
        btn.style.top = `${(-temp.y * 0.5 + 0.5) * h}px`
      })
    }

    const setupMarkerOverlay = (frames: PanoramaFrame[], space: 'mesh' | 'splat') => {
      const overlay = markersOverlayRef.current
      if (!overlay || frames.length === 0) return

      disposeMarkerOverlay()
      markerOverlayFrames = frames
      markerOverlaySpace = space

      for (const frame of frames) {
        const btn = document.createElement('button')
        btn.type = 'button'
        btn.className = 'panorama-dot'
        btn.title = `Кадр ${frame.id}`
        btn.addEventListener('click', (e) => {
          e.stopPropagation()
          openPanorama(frame.url, `Кадр ${frame.id}`, frame.id)
        })
        overlay.appendChild(btn)
        markerOverlayButtons.push(btn)
        markerOverlayFrameIds.push(frame.id)
      }

      if (space === 'splat') {
        const tick = () => {
          if (disposed || !splatViewer) return
          syncMarkerVisuals()
          markerOverlayAnimId = requestAnimationFrame(tick)
        }
        markerOverlayAnimId = requestAnimationFrame(tick)
      }
    }

    const onCanvasClick = async (event: MouseEvent) => {
      if (!renderer || !camera || !scene || kind !== 'mesh') return
      if (
        mouseDownOnCanvas &&
        Math.hypot(event.clientX - clickDownX, event.clientY - clickDownY) > 6
      ) {
        mouseDownOnCanvas = false
        return
      }
      mouseDownOnCanvas = false

      const rect = renderer.domElement.getBoundingClientRect()
      pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1
      pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1
      if (!raycaster) raycaster = new THREE.Raycaster()
      raycaster.setFromCamera(pointer, camera)

      const markerHits = raycaster.intersectObjects(markersGroup?.children ?? [], false)
      if (markerHits.length > 0) {
        const frame = markerHits[0].object.userData.panoramaFrame as PanoramaFrame
        if (frame?.url) {
          openPanorama(frame.url, `Кадр ${frame.id}`, frame.id)
          return
        }
      }

      const meshHits = raycaster.intersectObject(meshRoot!, true)
      if (meshHits.length > 0 && manifest?.frames?.length) {
        const colmap = sceneToColmap(meshHits[0].point)
        await pickPanoramaAtColmap(colmap.x, colmap.y, colmap.z)
      }
    }

    const disposeMesh = () => {
      disposeMarkerOverlay()
      meshResizeObserver?.disconnect()
      meshResizeObserver = null
      if (renderer) {
        renderer.domElement.removeEventListener('mousedown', onCanvasMouseDown)
        renderer.domElement.removeEventListener('click', onCanvasClick)
      }
      if (animId) cancelAnimationFrame(animId)
      animId = 0
      controls?.dispose()
      controls = null
      if (markersGroup && scene) {
        scene.remove(markersGroup)
        markersGroup.traverse((o) => {
          const mesh = o as THREE.Mesh
          if (mesh.isMesh) {
            mesh.geometry?.dispose()
            ;(mesh.material as THREE.Material)?.dispose()
          }
        })
        markersGroup = null
      }
      if (viewFrustum && scene) {
        scene.remove(viewFrustum)
        disposeCameraFrustum(viewFrustum)
        viewFrustum = null
      }
      markerMeshes = new Map()
      if (meshRoot && scene) {
        meshRoot.traverse((o) => {
          const mesh = o as THREE.Mesh
          if (mesh.isMesh) {
            mesh.geometry?.dispose()
            const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material]
            mats.forEach((m) => {
              if (!m) return
              for (const k of ['map', 'normalMap', 'roughnessMap', 'metalnessMap', 'aoMap', 'emissiveMap'] as const) {
                const tex = m[k]
                if (tex && 'dispose' in tex) tex.dispose()
              }
              m.dispose()
            })
          }
        })
        scene.remove(meshRoot)
        meshRoot = null
      }
      if (renderer) {
        renderer.dispose()
        if (renderer.domElement.parentElement === viewport) {
          viewport.removeChild(renderer.domElement)
        }
        renderer = null
      }
      scene = null
      camera = null
    }

    const disposeSplat = async () => {
      disposeMarkerOverlay()
      if (splatViewer?.renderer) {
        splatViewer.renderer.domElement.removeEventListener('mousedown', onCanvasMouseDown)
        splatViewer.renderer.domElement.removeEventListener('click', onSplatClick)
      }
      if (!splatViewer) return
      const viewer = splatViewer as unknown as InstanceType<typeof GaussianSplats3D.Viewer>
      const n = viewer.getSceneCount()
      if (n > 0) {
        await viewer.removeSplatScenes(Array.from({ length: n }, (_, i) => i), false)
      }
      viewer.dispose?.()
      splatViewer = null
    }

    const frameMesh = (obj: THREE.Object3D) => {
      if (!camera || !controls) return
      const box = new THREE.Box3().setFromObject(obj)
      if (box.isEmpty()) return
      const size = box.getSize(new THREE.Vector3())
      const center = box.getCenter(new THREE.Vector3())
      const maxDim = Math.max(size.x, size.y, size.z, 1e-3)
      const dist = maxDim * 1.8 / Math.tan((camera.fov * Math.PI) / 360)
      camera.position.copy(center).add(new THREE.Vector3(dist * 0.6, dist * 0.45, dist))
      controls.target.copy(center)
      camera.near = Math.max(dist / 200, 0.01)
      camera.far = dist * 50
      camera.updateProjectionMatrix()
      controls.update()
    }

    const loadMesh = async (url: string) => {
      setStatus('loading')
      setError(null)

      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false })
      renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
      renderer.outputColorSpace = THREE.SRGBColorSpace
      renderer.toneMapping = THREE.ACESFilmicToneMapping
      renderer.toneMappingExposure = 1.0
      viewport.appendChild(renderer.domElement)

      scene = new THREE.Scene()
      scene.background = new THREE.Color(0xe8e8e8)
      camera = new THREE.PerspectiveCamera(50, 1, 0.01, 5000)
      camera.position.set(2, 2, 4)

      scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 0.6))
      const dir = new THREE.DirectionalLight(0xffffff, 0.8)
      dir.position.set(5, 10, 7)
      scene.add(dir)

      controls = new OrbitControls(camera, renderer.domElement)
      controls.enableDamping = true
      controls.dampingFactor = 0.08

      const resize = () => {
        if (!renderer || !camera) return
        const w = viewport.clientWidth
        const h = viewport.clientHeight
        if (w < 1 || h < 1) return
        camera.aspect = w / h
        camera.updateProjectionMatrix()
        renderer.setSize(w, h, false)
      }
      resize()
      meshResizeObserver = new ResizeObserver(resize)
      meshResizeObserver.observe(viewport)
      window.addEventListener('resize', resize)

      const loader = new GLTFLoader()
      const gltf = await new Promise<import('three/addons/loaders/GLTFLoader.js').GLTF>((resolve, reject) => {
        loader.load(url, resolve, undefined, reject)
      })

      if (disposed) return

      const orient = new THREE.Group()
      orient.add(gltf.scene)
      const bakedRotation = gltfHasBakedMeshRotation(gltf.scene)
      if (!bakedRotation) {
        orient.applyMatrix4(MESH_ROT)
      }
      orient.updateMatrixWorld(true)
      fixOpenMvsMaterials(gltf.scene)
      meshRoot = orient
      scene.add(meshRoot)
      frameMesh(meshRoot)

      const frames = manifest?.frames ?? []
      if (frames.length > 0) {
        const markerData = addPanoramaMarkers(scene, frames, 'mesh', meshRoot)
        markersGroup = markerData.group
        markerMeshes = markerData.markers
        markerSize = markerSizeFromFrames(frames)
        viewFrustum = createCameraFrustum(Math.max(markerSize * 8, 0.55))
        viewFrustum.visible = false
        scene.add(viewFrustum)
      }

      renderer.domElement.addEventListener('mousedown', onCanvasMouseDown)
      renderer.domElement.addEventListener('click', onCanvasClick)

      if (frames.length > 0) {
        setupMarkerOverlay(frames, 'mesh')
      }

      const loop = () => {
        if (disposed) return
        animId = requestAnimationFrame(loop)
        controls?.update()
        syncMarkerVisuals()
        if (renderer && scene && camera) renderer.render(scene, camera)
      }
      loop()

      setStatus('ready')
    }

    const loadSplat = async (url: string) => {
      setStatus('loading')
      setError(null)

      const frames = manifest?.frames ?? []
      const splatOverlayScene = new THREE.Scene()
      if (frames.length > 0) {
        const markerData = addPanoramaMarkers(splatOverlayScene, frames, 'splat')
        markersGroup = markerData.group
        markerMeshes = markerData.markers
        markerSize = markerSizeFromFrames(frames)
        viewFrustum = createCameraFrustum(Math.max(markerSize * 8, 0.55))
        viewFrustum.visible = false
        splatOverlayScene.add(viewFrustum)
      }

      const isolated = typeof crossOriginIsolated !== 'undefined' && crossOriginIsolated
      const viewer = new GaussianSplats3D.Viewer({
        rootElement: viewport,
        threeScene: splatOverlayScene,
        cameraUp: [0, -1, 0],
        initialCameraPosition: [0, 2, 8],
        initialCameraLookAt: [0, 0, 0],
        sharedMemoryForWorkers: isolated,
        gpuAcceleratedSort: isolated,
        sphericalHarmonicsDegree: 2,
        logLevel: GaussianSplats3D.LogLevel.None,
      })
      splatViewer = viewer as unknown as SplatViewerHandle

      await viewer.addSplatScene(url, {
        splatAlphaRemovalThreshold: 5,
        showLoadingUI: false,
        progressiveLoad: false,
      })

      if (disposed) return
      viewer.start()

      setupMarkerOverlay(frames, 'splat')

      splatViewer.renderer.domElement.addEventListener('mousedown', onCanvasMouseDown)
      splatViewer.renderer.domElement.addEventListener('click', onSplatClick)

      setStatus('ready')
    }

    const run = async () => {
      try {
        try {
          manifest = await fetchManifest(taskId)
          if (!disposed) setHasPanoramas(Boolean(manifest?.frames?.length))
        } catch {
          manifest = null
          if (!disposed) setHasPanoramas(false)
        }

        if (kind === 'mesh') {
          await loadMesh(resultUrl)
        } else if (kind === 'splat') await loadSplat(resultUrl)
        else if (kind === 'openmvs-ply') {
          setStatus('error')
          setError('Это mesh OpenMVS (.ply). Нужен .glb после TextureMesh.')
        } else {
          setStatus('error')
          setError(`Формат не поддерживается: ${resultUrl}`)
        }
      } catch (err) {
        if (disposed) return
        console.error('Ошибка загрузки 3D:', err)
        setStatus('error')
        setError(err instanceof Error ? err.message : 'Ошибка загрузки 3D')
      }
    }

    run()

    return () => {
      disposed = true
      disposeMesh()
      void disposeSplat()
      setStatus('idle')
    }
  }, [resultUrl, taskId, openPanorama])

  if (!resultUrl) {
    return (
      <div className="viewer-placeholder">
        <p>Результат ещё не готов</p>
      </div>
    )
  }

  return (
    <div className={`viewer-root${panoramaUrl ? ' viewer-root-split' : ''}`}>
      <div className="viewer-shell">
        <div ref={viewportRef} className="viewer-viewport">
          {hasPanoramas && (
            <div ref={markersOverlayRef} className="panorama-dots-overlay" aria-hidden="false" />
          )}
        </div>
        {error && <div className="viewer-error">{error}</div>}
      </div>

      {panoramaUrl && (
        <PanoramaViewer
          imageUrl={panoramaUrl}
          title={panoramaTitle}
          onClose={closePanorama}
          onViewChange={handlePanoramaViewChange}
        />
      )}
    </div>
  )
}
