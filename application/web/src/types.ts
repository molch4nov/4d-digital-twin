export interface Task {
  task_id: string
  status: 'pending' | 'processing' | 'completed' | 'failed'
  progress: number
  pipeline_type: string
  original_filename: string
  error_message: string | null
  updated_at: string
  result_url?: string
  result_path?: string
  frames_count?: number
  extra_data?: Record<string, unknown>
}

export interface TaskListResponse {
  tasks: Task[]
}

export interface TaskDetailResponse extends Task {
  colmap_dir?: string
}
