import { useState, useEffect } from 'react'
import './App.css'
import TaskUploader from './components/TaskUploader'
import TaskList from './components/TaskList'
import TaskDetail from './components/TaskDetail'
import type { Task, TaskListResponse } from './types'
import { fetchTasks } from './api/client'

type AppView = 'list' | 'detail'

function App() {
  const [view, setView] = useState<AppView>('list')
  const [tasks, setTasks] = useState<Task[]>([])
  const [selectedTask, setSelectedTask] = useState<Task | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const loadTasks = async () => {
    try {
      setLoading(true)
      setError(null)
      const response: TaskListResponse = await fetchTasks(30)
      setTasks(response.tasks)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Ошибка загрузки задач')
    } finally {
      setLoading(false)
    }
  }

  // Загружаем задачи один раз при монтировании
  useEffect(() => {
    loadTasks()
  }, [])

  const handleTaskSelected = (task: Task) => {
    setSelectedTask(task)
    setView('detail')
  }

  const handleTaskUpdated = (updatedTask: Task) => {
    setTasks(tasks.map(t => t.task_id === updatedTask.task_id ? updatedTask : t))
    setSelectedTask(updatedTask)
  }

  const handleBackToList = () => {
    setView('list')
    loadTasks()
  }

  const handleTaskCreated = () => {
    loadTasks()
  }

  return (
    <div className="app-container">
      <main className="app-content">
        {view === 'list' ? (
          <div className="list-view">
            <TaskUploader onTaskCreated={handleTaskCreated} />
            
            {error && (
              <div className="error-banner">
                <span>{error}</span>
                <button onClick={() => setError(null)}>✕</button>
              </div>
            )}
            
            <TaskList
              tasks={tasks}
              loading={loading}
              onTaskSelected={handleTaskSelected}
              onRefresh={loadTasks}
            />
          </div>
        ) : (
          selectedTask && (
            <TaskDetail
              task={selectedTask}
              onBack={handleBackToList}
              onTaskUpdated={handleTaskUpdated}
            />
          )
        )}
      </main>
    </div>
  )
}

export default App
