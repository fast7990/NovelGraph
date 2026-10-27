import { ReactNode } from 'react'
import { Typography } from '@douyinfe/semi-ui'

/** 统一的页面标题区：左侧标题 + 说明，右侧放页面级操作。 */
export default function PageHead({
  title,
  desc,
  extra,
}: {
  title: string
  desc?: string
  extra?: ReactNode
}) {
  return (
    <div className="page-head">
      <div>
        <h1>{title}</h1>
        {desc && <p>{desc}</p>}
      </div>
      {extra && <div className="row">{extra}</div>}
    </div>
  )
}

/** 指标卡里的数字，需要等宽字形的场合用它。 */
export function Mono({ children }: { children: ReactNode }) {
  return <Typography.Text className="mono">{children}</Typography.Text>
}
