/** token 数量的显示格式。
 *
 * 侧栏那一格宽度有限，"12928 tok" 会把它挤爆，上万直接折成 "13k"；
 * 一千到一万之间保留一位小数（"1.4k"），因为 1234 和 9876 在这个量级上
 * 差别是用户真正关心的。四个地方（侧栏徽标 / 答案下方用量条）共用这一份，
 * 免得两处各写一套、显示不一致。
 */
export function fmtTok(n: number) {
  const v = Number(n) || 0
  if (v >= 10000) return `${(v / 1000).toFixed(0)}k`
  if (v >= 1000) return `${(v / 1000).toFixed(1)}k`
  return String(v)
}
