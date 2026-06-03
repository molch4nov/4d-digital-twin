import { useEffect, useRef, useState } from 'react'
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
import { panoramaHeadingDeg } from '../lib/coords'
import './PanoramaViewer.css'

export interface PanoramaViewOrientation {
  direction: THREE.Vector3
  up: THREE.Vector3
}

interface PanoramaViewerProps {
  imageUrl: string
  title?: string
  onClose: () => void
  onViewChange?: (orientation: PanoramaViewOrientation) => void
}

export default function PanoramaViewer({
  imageUrl,
  title,
  onClose,
  onViewChange,
}: PanoramaViewerProps) {
  const viewportRef = useRef<HTMLDivElement>(null)
  const compassNeedleRef = useRef<HTMLDivElement>(null)
  const compassHeadingRef = useRef<HTMLSpanElement>(null)
  const onCloseRef = useRef(onClose)
  const onViewChangeRef = useRef(onViewChange)
  const [loadState, setLoadState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [loadError, setLoadError] = useState<string | null>(null)

  onCloseRef.current = onClose
  onViewChangeRef.current = onViewChange

  useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return

    let disposed = false
    setLoadState('loading')
    setLoadError(null)

    const renderer = new THREE.WebGLRenderer({ antialias: true })
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2))
    renderer.outputColorSpace = THREE.SRGBColorSpace
    viewport.appendChild(renderer.domElement)

    const scene = new THREE.Scene()
    scene.background = new THREE.Color(0x111111)
    const camera = new THREE.PerspectiveCamera(75, 1, 0.1, 1100)
    camera.position.set(0, 0, 0.1)

    const controls = new OrbitControls(camera, renderer.domElement)
    controls.enableZoom = true
    controls.enablePan = false
    controls.rotateSpeed = -0.35

    let sphere: THREE.Mesh | null = null
    let animId = 0
    const lookDir = new THREE.Vector3()
    const lookUp = new THREE.Vector3()

    const resize = () => {
      const w = viewport.clientWidth
      const h = viewport.clientHeight
      if (w < 1 || h < 1) return
      camera.aspect = w / h
      camera.updateProjectionMatrix()
      renderer.setSize(w, h, false)
    }

    const ro = new ResizeObserver(resize)
    ro.observe(viewport)
    resize()

    const loop = () => {
      if (disposed) return
      animId = requestAnimationFrame(loop)
      controls.update()
      camera.getWorldDirection(lookDir)
      lookUp.setFromMatrixColumn(camera.matrixWorld, 1).normalize()
      const heading = panoramaHeadingDeg(lookDir)
      if (compassNeedleRef.current) {
        compassNeedleRef.current.style.transform = `translate(-50%, -100%) rotate(${heading}deg)`
      }
      if (compassHeadingRef.current) {
        compassHeadingRef.current.textContent = `${Math.round(heading)}°`
      }
      onViewChangeRef.current?.({ direction: lookDir.clone(), up: lookUp.clone() })
      renderer.render(scene, camera)
    }
    loop()

    const absoluteUrl = imageUrl.startsWith('http')
      ? imageUrl
      : `${window.location.origin}${imageUrl.startsWith('/') ? '' : '/'}${imageUrl}`

    const loader = new THREE.TextureLoader()
    if (!absoluteUrl.startsWith(window.location.origin)) {
      loader.setCrossOrigin('anonymous')
    }
    loader.load(
      absoluteUrl,
      (texture) => {
        if (disposed) {
          texture.dispose()
          return
        }
        texture.colorSpace = THREE.SRGBColorSpace
        const geometry = new THREE.SphereGeometry(500, 64, 32)
        geometry.scale(-1, 1, 1)
        const material = new THREE.MeshBasicMaterial({ map: texture })
        sphere = new THREE.Mesh(geometry, material)
        scene.add(sphere)
        setLoadState('ready')
      },
      undefined,
      (err) => {
        if (disposed) return
        console.error('Ошибка загрузки панорамы:', absoluteUrl, err)
        setLoadState('error')
        setLoadError(`Не удалось загрузить ${absoluteUrl}`)
      },
    )

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    window.addEventListener('keydown', onKey)

    return () => {
      disposed = true
      ro.disconnect()
      window.removeEventListener('keydown', onKey)
      if (animId) cancelAnimationFrame(animId)
      controls.dispose()
      if (sphere) {
        sphere.geometry.dispose()
        const mat = sphere.material as THREE.MeshBasicMaterial
        mat.map?.dispose()
        mat.dispose()
      }
      renderer.dispose()
      if (renderer.domElement.parentElement === viewport) {
        viewport.removeChild(renderer.domElement)
      }
    }
  }, [imageUrl])

  return (
    <div className="panorama-panel">
      <div className="panorama-header">
        <span className="panorama-title">{title || 'Панорама'}</span>
        <button type="button" className="panorama-close" onClick={() => onCloseRef.current()}>✕ Закрыть</button>
      </div>
      <div ref={viewportRef} className="panorama-viewport">
        <div className="panorama-reticle" aria-hidden="true">
          <span className="panorama-reticle-v" />
          <span className="panorama-reticle-h" />
        </div>
      </div>
      {loadState === 'loading' && (
        <div className="panorama-status">Загрузка панорамы…</div>
      )}
      {loadState === 'error' && (
        <div className="panorama-status panorama-status-error">{loadError}</div>
      )}
    </div>
  )
}
