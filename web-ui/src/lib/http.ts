import { useAuth } from '@/composables/useAuth'

interface FetchOptions extends RequestInit {
  params?: Record<string, string | number | boolean | undefined>;
}

/**
 * 提取后端返回的错误信息。
 *
 * 后端 detail 可能是普通字符串，也可能是结构化对象（例如任务因熔断被拒绝启动时
 * 返回的 409：{ message, reason, paused_until, ... }）。直接 new Error(obj) 会得到
 * "[object Object]"，所以这里统一转成可读文本。
 */
function extractErrorMessage(errorData: unknown, status: number): string {
  const fallback = `HTTP error! status: ${status}`
  if (!errorData || typeof errorData !== 'object') {
    return typeof errorData === 'string' && errorData ? errorData : fallback
  }

  const detail = (errorData as { detail?: unknown }).detail
  if (typeof detail === 'string' && detail) {
    return detail
  }
  if (detail && typeof detail === 'object') {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) {
      return message
    }
  }
  return fallback
}

export async function http(url: string, options: FetchOptions = {}) {
  const { logout } = useAuth()
  
  const headers = new Headers(options.headers)

  // Handle Query Params
  let fullUrl = url
  if (options.params) {
    const searchParams = new URLSearchParams()
    Object.entries(options.params).forEach(([key, value]) => {
      if (value !== undefined && value !== null) {
        searchParams.append(key, String(value))
      }
    })
    const queryString = searchParams.toString()
    if (queryString) {
      fullUrl += (url.includes('?') ? '&' : '?') + queryString
    }
  }

  const config: RequestInit = {
    ...options,
    headers,
  }

  const response = await fetch(fullUrl, config)

  if (response.status === 401) {
    // Basic Auth failed or session expired
    logout()
    // Optional: Redirect to login handled by router or state change
    throw new Error('Unauthorized')
  }

  if (!response.ok) {
    const errorData = await response.json().catch(() => ({}))
    throw new Error(extractErrorMessage(errorData, response.status))
  }

  // Handle 204 No Content
  if (response.status === 204) {
    return null
  }

  return response.json()
}
