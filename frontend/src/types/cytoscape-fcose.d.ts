// cytoscape-fcose 是 UMD 包，自身不带类型声明。
// 它导出的是一个「扩展注册函数」：cytoscape.use(fcose)。
declare module 'cytoscape-fcose' {
  import type cytoscape from 'cytoscape'
  const ext: cytoscape.Ext
  export default ext
}
