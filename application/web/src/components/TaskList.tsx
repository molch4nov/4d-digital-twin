import type { Task } from '../types'
import TaskCard from './TaskCard'
import './TaskList.css'

interface TaskListProps {
  tasks: Task[]
  loading: boolean
  onTaskSelected: (task: Task) => void
  onRefresh: () => void
}

export default function TaskList({ tasks, loading, onTaskSelected, onRefresh }: TaskListProps) {
  const getStatusBadge = (status: string) => {
    const badges: Record<string, { label: string; color: string }> = {
      pending: { label: 'В ожидании', color: 'gray' },
      processing: { label: 'Обработка', color: 'blue' },
      completed: { label: 'Завершено', color: 'green' },
      failed: { label: 'Ошибка', color: 'red' },
    }
    return badges[status] || badges.pending
  }

  return (
    <div className="task-list-container">
      <div className="task-list-header">
        <h2>История задач</h2>
        <button onClick={onRefresh} className="refresh-btn" disabled={loading}>
          ⟳ Обновить
        </button>
      </div>

      {loading && tasks.length === 0 ? (
        <div className="loading-state">
          <div className="spinner"></div>
          <p>Загрузка задач...</p>
        </div>
      ) : tasks.length === 0 ? (
        <div className="empty-state">
          <p>Нет задач. Загрузите видеофайл, чтобы начать.</p>
        </div>
      ) : (
        <div className="task-grid">
          {tasks.map(task => (
            <TaskCard
              key={task.task_id}
              task={task}
              badge={getStatusBadge(task.status)}
              onSelect={onTaskSelected}
            />
          ))}
        </div>
      )}
    </div>
  )
}
