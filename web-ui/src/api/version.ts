import { http } from '@/lib/http'

export interface AppVersionInfo {
  /** 版本号，例如 "2.5-18-gcb0e4cb"；拿不到时是 "unknown" */
  version: string
  /** 提交短哈希，例如 "cb0e4cb"；拿不到时是空串 */
  commit: string
}

/**
 * 读取当前运行的版本。
 *
 * 顶部徽标靠它确认「这次升级到底生效没有」：后端返回的是**正在跑的**那一份代码
 * 的版本，接口不通（后端太旧）时返回 null，徽标就不显示。
 */
export async function fetchAppVersion(): Promise<AppVersionInfo | null> {
  try {
    const data = await http('/api/version')
    if (!data || typeof data.version !== 'string') return null
    return {
      version: data.version,
      commit: typeof data.commit === 'string' ? data.commit : '',
    }
  } catch {
    return null
  }
}
