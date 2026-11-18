/**
 * shell 特性公开出口：只导出对外暴露的部分。
 *
 * 约定：features 之间禁止互相 import，跨 feature 引用一律走各自的 index.ts，
 * 这样依赖方向是单向的（routes → shell → lib），不会因为互相 import 悄悄长出环。
 */
export { AppLayout } from "./AppLayout";
