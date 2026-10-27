import React from 'react'
import ReactDOM from 'react-dom/client'
import { ConfigProvider, Toast } from '@douyinfe/semi-ui'
import zhCN from '@douyinfe/semi-ui/lib/es/locale/source/zh_CN'
// Semi 各组件的样式随组件自身引入，这里补上基础 token / 排版重置
import '@douyinfe/semi-ui/lib/es/_base/base.css'
import './styles.css'
import App from './App'

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <ConfigProvider locale={zhCN} getPopupContainer={() => document.body}>
      <App />
      <Toast />
    </ConfigProvider>
  </React.StrictMode>,
)
