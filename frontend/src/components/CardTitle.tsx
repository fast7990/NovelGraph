import { Typography } from '@douyinfe/semi-ui'

/**
 * Card 的标题。
 *
 * 为什么不直接写 `<Card title="xxx">`：Semi 对**字符串**标题会硬编码渲染成
 * `<Typography.Title heading={6} ellipsis={{ showTooltip: true }}>`，而
 * Typography 的省略号内部用 ReactResizeObserver 定位，它靠 `findDOMNode`
 * 拿 DOM —— React 18 开 StrictMode 时每次挂载都会刷两条 "findDOMNode is
 * deprecated" 告警，控制台一片红。
 *
 * 传成非字符串元素就绕过这段逻辑（Semi 只对 string 特判），这里保持同样的
 * heading=6，视觉一致，只是不再带 tooltip 式省略。
 */
export default function CardTitle({ text }: { text: string }) {
  return <Typography.Title heading={6}>{text}</Typography.Title>
}
