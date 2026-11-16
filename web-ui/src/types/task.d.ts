// Based on the Pydantic model in the backend

export interface Task {
  id: number;
  task_name: string;
  enabled: boolean;
  keyword: string;
  description: string;
  analyze_images: boolean;
  max_pages: number;
  personal_only: boolean;
  min_price: string | null;
  max_price: string | null;
  cron: string | null;
  next_run_at?: string | null;
  ai_prompt_base_file: string;
  ai_prompt_criteria_file: string;
  account_state_file?: string | null;
  account_strategy: 'auto' | 'fixed' | 'rotate';
  free_shipping?: boolean;
  new_publish_option?: string | null;
  region?: string | null;
  decision_mode: 'ai' | 'keyword';
  keyword_rules: string[];
  is_running: boolean;
}

export type TaskGenerationStatus = 'queued' | 'running' | 'completed' | 'failed';
export type TaskGenerationStepStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface TaskGenerationStep {
  key: string;
  label: string;
  status: TaskGenerationStepStatus;
  message: string;
}

export interface TaskGenerationJob {
  job_id: string;
  task_name: string;
  status: TaskGenerationStatus;
  message: string;
  current_step: string | null;
  steps: TaskGenerationStep[];
  task: Task | null;
  error: string | null;
}

export interface TaskCreateResponse {
  message: string;
  task?: Task;
  job?: TaskGenerationJob;
}

// For PATCH requests, all fields are optional
export type TaskUpdate = Partial<Omit<Task, 'id' | 'next_run_at'>>;

// For task creation
export interface TaskGenerateRequest {
  task_name: string;
  keyword: string;
  description?: string;
  analyze_images?: boolean;
  personal_only?: boolean;
  min_price?: string | null;
  max_price?: string | null;
  max_pages?: number;
  cron?: string | null;
  account_state_file?: string | null;
  account_strategy?: 'auto' | 'fixed' | 'rotate';
  free_shipping?: boolean;
  new_publish_option?: string | null;
  region?: string | null;
  decision_mode?: 'ai' | 'keyword';
  keyword_rules?: string[];
}

// AI 生成避开其它任务执行时间的定时规则
export interface ScheduleSuggestRequest {
  // 编辑已有任务时传入，用于把该任务自身排除在冲突检查之外
  task_id?: number | null;
  task_name?: string;
  keyword?: string;
  preferred?: string;
}

export interface ScheduleSuggestResponse {
  cron: string;
  reason: string;
  // ai = AI 生成；fallback = AI 不可用时的确定性兜底
  source: 'ai' | 'fallback';
  avoided: string[];
  considered_tasks: number;
}
