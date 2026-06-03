import type { Task, TaskListResponse, TaskDetailResponse } from '../types'

import type { PanoramaManifest, NearestPanoramaResponse } from '../types/panorama'

const API_BASE = '/api/v1'

export async function fetchTasks(limit: number = 30): Promise<TaskListResponse> {
  const response = await fetch(`${API_BASE}/tasks/?limit=${limit}`)
  if (!response.ok) throw new Error('Не удалось загрузить задачи')
  return response.json()
}

export async function fetchTaskDetail(taskId: string): Promise<TaskDetailResponse> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}`)
  if (!response.ok) throw new Error('Задача не найдена')
  return response.json()
}

export async function uploadTask(file: File, pipeline: string): Promise<{ task_id: string; status: string }> {
  const formData = new FormData()
  formData.append('file', file)
  formData.append('pipeline', pipeline)

  const response = await fetch(`${API_BASE}/tasks/`, {
    method: 'POST',
    body: formData
  })
  
  if (!response.ok) {
    const error = await response.json()
    throw new Error(error.detail || 'Ошибка загрузки видео')
  }
  
  return response.json()
}

export async function downloadResult(taskId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/result`)
  if (!response.ok) throw new Error('Ошибка загрузки результата')
  
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `result-${taskId}.tar.gz`
  a.click()
  URL.revokeObjectURL(url)
}

export async function downloadLog(taskId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/log`)
  if (!response.ok) throw new Error('Ошибка загрузки логов')
  
  const blob = await response.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `log-${taskId}.txt`
  a.click()
  URL.revokeObjectURL(url)
}

export async function restartTask(taskId: string): Promise<Task> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/restart`, {
    method: 'POST'
  })
  
  if (!response.ok) throw new Error('Ошибка перезапуска задачи')
  return response.json()
}

export function subscribeToTask(taskId: string, callback: (task: Task) => void): () => void {
  const eventSource = new EventSource(`${API_BASE}/tasks/${taskId}/stream`)
  
  eventSource.addEventListener('task', (event) => {
    try {
      const data = JSON.parse(event.data)
      callback(data)
    } catch (e) {
      console.error('Failed to parse task update:', e)
    }
  })

  return () => eventSource.close()
}

export async function fetchManifest(taskId: string): Promise<PanoramaManifest | null> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}/manifest`)
  if (response.status === 404) return null
  if (!response.ok) throw new Error('Не удалось загрузить манифест панорам')
  return response.json()
}

export async function fetchNearestPanorama(
  taskId: string,
  x: number,
  y: number,
  z: number,
): Promise<NearestPanoramaResponse> {
  const params = new URLSearchParams({
    x: String(x),
    y: String(y),
    z: String(z),
  })
  const response = await fetch(`${API_BASE}/tasks/${taskId}/panorama/nearest?${params}`)
  if (!response.ok) throw new Error('Панорама не найдена')
  return response.json()
}
