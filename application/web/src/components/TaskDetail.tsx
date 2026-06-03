import { useState, useEffect } from 'react'
import type { Task, TaskDetailResponse } from '../types'
import { fetchTaskDetail, downloadResult, downloadLog, restartTask, subscribeToTask } from '../api/client'
import ModelViewer from './ModelViewer'
import './TaskDetail.css'

interface TaskDetailProps {
  task: Task
  onBack: () => void
  onTaskUpdated: (task: Task) => void
}

export default function TaskDetail({ task: initialTask, onBack, onTaskUpdated }: TaskDetailProps) {
  const [task, setTask] = useState<TaskDetailResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [downloading, setDownloading] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [showInfo, setShowInfo] = useState(false)

  useEffect(() => {
    const loadDetail = async () => {
      try {
        const detail = await fetchTaskDetail(initialTask.task_id)
        setTask(detail)
        setLoading(false)
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Ошибка загрузки деталей')
        setLoading(false)
      }
    }

    loadDetail()

    const unsubscribe = subscribeToTask(initialTask.task_id, (updated) => {
      setTask(prev => prev ? { ...prev, ...updated } : null)
      onTaskUpdated(updated)
    })

    return () => unsubscribe()
  }, [initialTask.task_id, onTaskUpdated])

  const handleDownloadResult = async () => {
    try {
      setDownloading(true)
      await downloadResult(initialTask.task_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка загрузки')
    } finally {
      setDownloading(false)
    }
  }

  const handleDownloadLog = async () => {
    try {
      setDownloading(true)
      await downloadLog(initialTask.task_id)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка загрузки логов')
    } finally {
      setDownloading(false)
    }
  }

  const handleRestart = async () => {
    try {
      setRestarting(true)
      await restartTask(initialTask.task_id)
      const updated = await fetchTaskDetail(initialTask.task_id)
      setTask(updated)
      onTaskUpdated(updated)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка перезапуска')
    } finally {
      setRestarting(false)
    }
  }

  if (loading) {
    return (
      <div className="detail-container">
        <button className="back-btn" onClick={onBack}>← Назад</button>
        <div className="loading-state">
          <div className="spinner"></div>
          <p>Загрузка деталей...</p>
        </div>
      </div>
    )
  }

  if (!task) {
    return (
      <div className="detail-container">
        <button className="back-btn" onClick={onBack}>← Назад</button>
        <div className="error-state">
          <p>Задача не найдена</p>
        </div>
      </div>
    )
  }

  const getStatusIcon = (status: string) => {
    const icons: Record<string, string> = {
      pending: '⏱️',
      processing: '⚙️',
      completed: '✓',
      failed: '✕',
    }
    return icons[status] || '?'
  }

  const formatDate = (dateString: string) => {
    const date = new Date(dateString)
    return date.toLocaleString('ru-RU', {
      year: 'numeric',
      month: 'long',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    })
  }

  const isFullscreenViewer = task.status === 'completed' && Boolean(task.result_url)

  if (isFullscreenViewer) {
    return (
      <div className="detail-fullscreen">
        <div className="detail-fullscreen-bar">
          <button className="back-btn back-btn-compact" onClick={onBack}>← Назад</button>
          <div className="detail-fullscreen-title">
            <span className="detail-fullscreen-name">{task.original_filename}</span>
            <span className="detail-fullscreen-id">{task.task_id}</span>
          </div>
          <div className="detail-fullscreen-actions">
            <button
              className="action-btn action-primary action-btn-compact"
              onClick={handleDownloadResult}
              disabled={downloading}
            >
              {downloading ? '⏳' : '⬇️ Скачать'}
            </button>
            <button
              className="action-btn action-secondary action-btn-compact"
              onClick={handleDownloadLog}
              disabled={downloading}
            >
              📋 Логи
            </button>
            <button
              className="action-btn action-secondary action-btn-compact"
              onClick={() => setShowInfo(v => !v)}
            >
              {showInfo ? 'Скрыть инфо' : 'Инфо'}
            </button>
          </div>
        </div>

        {error && (
          <div className="error-banner error-banner-overlay">
            <span>{error}</span>
            <button onClick={() => setError(null)}>✕</button>
          </div>
        )}

        <ModelViewer
          taskId={task.task_id}
          resultUrl={task.result_url}
          fileName={task.original_filename}
        />

        {showInfo && (
          <div className="detail-info-panel">
            <div className="info-grid">
              <div className="info-item">
                <span className="info-label">Статус</span>
                <span className="info-value">{getStatusIcon(task.status)} Завершено</span>
              </div>
              <div className="info-item">
                <span className="info-label">Тип</span>
                <span className="info-value">{task.pipeline_type}</span>
              </div>
              <div className="info-item">
                <span className="info-label">Кадров</span>
                <span className="info-value">{task.frames_count || '—'}</span>
              </div>
              <div className="info-item">
                <span className="info-label">Обновлено</span>
                <span className="info-value">{formatDate(task.updated_at)}</span>
              </div>
            </div>
            <button
              className="action-btn action-warning action-btn-compact"
              onClick={handleRestart}
              disabled={restarting}
            >
              {restarting ? '⏳' : '🔄 Перезапустить'}
            </button>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="detail-container">
      <button className="back-btn" onClick={onBack}>← Назад</button>

      {error && (
        <div className="error-banner">
          <span>{error}</span>
          <button onClick={() => setError(null)}>✕</button>
        </div>
      )}

      <div className="detail-card">
        <div className="detail-header">
          <div className="header-info">
            <h1 className="detail-title">{task.original_filename}</h1>
            <p className="detail-subtitle">{task.task_id}</p>
          </div>
          <div className={`detail-status status-${task.status}`}>
            <span className="status-icon">{getStatusIcon(task.status)}</span>
            <span className="status-text">
              {task.status === 'pending' && 'В ожидании'}
              {task.status === 'processing' && 'Обработка'}
              {task.status === 'completed' && 'Завершено'}
              {task.status === 'failed' && 'Ошибка'}
            </span>
          </div>
        </div>

        {task.status === 'processing' && (
          <div className="progress-section">
            <div className="progress-info">
              <span className="progress-label">Прогресс</span>
              <span className="progress-value">{Math.round(task.progress)}%</span>
            </div>
            <div className="progress-bar-large">
              <div
                className="progress-fill"
                style={{ width: `${task.progress}%` }}
              ></div>
            </div>
          </div>
        )}

        <div className="detail-info">
          <div className="info-section">
            <h3>Информация</h3>
            <div className="info-grid">
              <div className="info-item">
                <span className="info-label">Тип обработки:</span>
                <span className="info-value">{task.pipeline_type}</span>
              </div>
              <div className="info-item">
                <span className="info-label">Кадров:</span>
                <span className="info-value">{task.frames_count || '—'}</span>
              </div>
              <div className="info-item">
                <span className="info-label">Обновлено:</span>
                <span className="info-value">{formatDate(task.updated_at)}</span>
              </div>
              <div className="info-item">
                <span className="info-label">ID задачи:</span>
                <span className="info-value monospace">{task.task_id}</span>
              </div>
            </div>
          </div>

          {task.error_message && (
            <div className="info-section error-section">
              <h3>Сообщение об ошибке</h3>
              <p className="error-message-text">{task.error_message}</p>
            </div>
          )}
        </div>

        <div className="detail-actions">
          <button
            className="action-btn action-secondary"
            onClick={handleDownloadLog}
            disabled={downloading}
          >
            {downloading ? '⏳ Загрузка...' : '📋 Логи'}
          </button>
          {task.status !== 'processing' && (
            <button
              className="action-btn action-warning"
              onClick={handleRestart}
              disabled={restarting}
            >
              {restarting ? '⏳ Перезапуск...' : '🔄 Перезапустить'}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
