import type { Task } from '../types'
import './TaskCard.css'

interface TaskCardProps {
  task: Task
  badge: { label: string; color: string }
  onSelect: (task: Task) => void
}

export default function TaskCard({ task, badge, onSelect }: TaskCardProps) {
  const getProgressColor = (progress: number) => {
    if (progress < 33) return '#ef4444'
    if (progress < 66) return '#f59e0b'
    return '#10b981'
  }

  const formatDate = (dateString: string) => {
    const date = new Date(dateString)
    return date.toLocaleDateString('ru-RU', {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit'
    })
  }

  return (
    <div className="task-card" onClick={() => onSelect(task)}>
      <div className="card-header">
        <h3 className="card-title">{task.original_filename}</h3>
        <span className={`status-badge status-${badge.color}`}>
          {badge.label}
        </span>
      </div>

      <div className="card-body">
        <div className="card-meta">
          <span className="meta-item">
            <span className="meta-label">Тип:</span>
            <span className="meta-value">{task.pipeline_type}</span>
          </span>
          <span className="meta-item">
            <span className="meta-label">Обновлено:</span>
            <span className="meta-value">{formatDate(task.updated_at)}</span>
          </span>
        </div>

        {task.status === 'processing' && (
          <div className="progress-container">
            <div className="progress-bar">
              <div
                className="progress-fill"
                style={{
                  width: `${task.progress}%`,
                  backgroundColor: getProgressColor(task.progress)
                }}
              ></div>
            </div>
            <span className="progress-text">{Math.round(task.progress)}%</span>
          </div>
        )}

        {task.error_message && (
          <div className="error-info">
            <span className="error-label">Ошибка:</span>
            <span className="error-text">{task.error_message}</span>
          </div>
        )}

        {task.status === 'completed' && task.result_url && (
          <div className="result-info">
            ✓ Результат готов
          </div>
        )}
      </div>

      <div className="card-footer">
        <button className="view-btn">Детали →</button>
      </div>
    </div>
  )
}
