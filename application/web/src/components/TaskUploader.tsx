import { useState, useRef } from 'react'
import { uploadTask } from '../api/client'
import './TaskUploader.css'

interface TaskUploaderProps {
  onTaskCreated: () => void
}

export default function TaskUploader({ onTaskCreated }: TaskUploaderProps) {
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [errorExpanded, setErrorExpanded] = useState(false)
  const [pipeline, setPipeline] = useState('colmap360_openmvs')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const handleFileSelect = async (file: File) => {
    if (!file.type.startsWith('video/')) {
      setError('Пожалуйста, выберите видеофайл (MP4, AVI, MOV)')
      setErrorExpanded(true)
      return
    }

    if (file.size > 5 * 1024 * 1024 * 1024) {
      setError('Файл слишком большой (максимум 5 GB)')
      setErrorExpanded(true)
      return
    }

    try {
      setUploading(true)
      setError(null)
      await uploadTask(file, pipeline)
      onTaskCreated()
      if (fileInputRef.current) {
        fileInputRef.current.value = ''
      }
    } catch (err) {
      const errorMsg = err instanceof Error ? err.message : 'Ошибка загрузки'
      setError(errorMsg)
      setErrorExpanded(true)
    } finally {
      setUploading(false)
    }
  }

  const handleInputChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0]
    if (file) {
      handleFileSelect(file)
    }
  }

  const handleDragOver = (e: React.DragEvent) => {
    e.preventDefault()
    e.currentTarget.classList.add('drag-over')
  }

  const handleDragLeave = (e: React.DragEvent) => {
    e.currentTarget.classList.remove('drag-over')
  }

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault()
    e.currentTarget.classList.remove('drag-over')
    const file = e.dataTransfer.files?.[0]
    if (file) {
      handleFileSelect(file)
    }
  }

  const pipelines = [
    { id: 'colmap360_openmvs', label: 'COLMAP 360 + OpenMVS' },
    { id: 'colmap360_3dgs', label: 'COLMAP 360 + 3D Gaussian Splatting' },
    { id: 'openmvg_openmvs', label: 'OpenMVG + OpenMVS' },
    { id: 'sphere_colmap_openmvs', label: 'SphereSfM + COLMAP + OpenMVS' },
    { id: 'gaussian_splatting', label: 'Gaussian Splatting' },
  ]

  // Получить короткое описание ошибки
  const getErrorSummary = () => {
    if (!error) return ''
    const lines = error.split('\n')
    return lines[0].substring(0, 100)
  }

  return (
    <div className="uploader-container">
      <div className="uploader-card">
        <div
          className="upload-zone"
          onDragOver={handleDragOver}
          onDragLeave={handleDragLeave}
          onDrop={handleDrop}
        >
          <div className="upload-icon">📹</div>
          <h2>Загрузите видеофайл</h2>
          <p>Перетащите видео или нажмите для выбора</p>
          <button
            className="upload-btn"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
          >
            {uploading ? 'Загрузка...' : 'Выбрать файл'}
          </button>
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*"
            onChange={handleInputChange}
            disabled={uploading}
            style={{ display: 'none' }}
          />
        </div>

        <div className="uploader-options">
          <div className="option-group">
            <label>Выберите способ обработки:</label>
            <select
              value={pipeline}
              onChange={(e) => setPipeline(e.target.value)}
              disabled={uploading}
            >
              {pipelines.map(p => (
                <option key={p.id} value={p.id}>{p.label}</option>
              ))}
            </select>
          </div>
        </div>

        {error && (
          <div className="error-section">
            <div 
              className="error-header"
              onClick={() => setErrorExpanded(!errorExpanded)}
            >
              <span className="error-icon">⚠️</span>
              <span className="error-summary">
                {errorExpanded ? 'Скрыть ошибку' : getErrorSummary()}
              </span>
              <span className="error-toggle">
                {errorExpanded ? '▼' : '▶'}
              </span>
            </div>
            {errorExpanded && (
              <div className="error-details">
                {error}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
