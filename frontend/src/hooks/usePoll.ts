import { useEffect, useRef } from 'react'

/**
 * 页面可见时才跑的定时器。
 *
 * 用 ref 存 fn，所以调用方传进来的函数每次渲染换个身份也不会重建定时器；
 * 依赖只有 [ms, enabled] 两个"真·参数"。这一点是硬要求：
 * 只要把回调和 fs 状态放进依赖，就会出现
 * "load() → setState → 依赖变 → 副作用重跑 → 立刻 load()" 的自激循环，
 * 一秒能打上百次接口。
 */
export function usePoll(fn: () => void | Promise<void>, ms: number, enabled = true) {
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    if (!enabled) return
    const tick = () => {
      if (document.visibilityState !== 'visible') return
      void fnRef.current()
    }
    const t = window.setInterval(tick, ms)
    // 从后台切回来立刻补一次，别让用户盯着过期数字等下一个周期
    const onVisible = () => {
      if (document.visibilityState === 'visible') void fnRef.current()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      window.clearInterval(t)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [ms, enabled])
}
