#!/usr/bin/env node
/**
 * 让仓库根 tests/ 下的前端测试能解析到依赖。
 *
 * 背景：前端单测物理位置在 `tests/frontend/`（全部测试集中在根 tests/），
 * 但依赖只装在 `frontend/node_modules`。Node 的模块解析是从**导入方所在目录**
 * 向上逐级找 node_modules，所以 tests/frontend/*.tsx 里 `import ... from "react"`
 * 默认找不到。
 *
 * 解法：在 `tests/node_modules` 建一个指向 `frontend/node_modules` 的**相对**符号链接。
 * 为什么放在 `tests/` 而不是 `tests/frontend/`：放 frontend 下会被 vitest 的
 * `include: ["../tests/frontend/**"]` 扫到，把第三方包自带的 __tests__ 当用例收集。
 *
 * 为什么不入库这个链接：悬空链接（clone 后未 install）会让工具误判；改为在
 * `npm install` 的 postinstall 里自动创建，幂等且可重复执行。
 *
 * 失败不阻塞 install：Windows 无权限建符号链接时只打印提示，前端测试仍可在
 * frontend/ 目录下用 `npx vitest run --root .` 之外的方式处理（见 wiki/05）。
 */
import { existsSync, lstatSync, readlinkSync, symlinkSync, unlinkSync } from "node:fs";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const linkPath = resolve(repoRoot, "tests", "node_modules");
const targetPath = resolve(repoRoot, "frontend", "node_modules");

function relTarget() {
  return relative(dirname(linkPath), targetPath);
}

if (!existsSync(targetPath)) {
  console.log("[link-test-deps] 跳过：frontend/node_modules 不存在（先跑 npm install）");
  process.exit(0);
}

try {
  if (existsSync(linkPath) || isDanglingLink(linkPath)) {
    const stat = safeLstat(linkPath);
    if (stat?.isSymbolicLink() && readlinkSync(linkPath) === relTarget()) {
      console.log(`[link-test-deps] 已就绪：tests/node_modules -> ${relTarget()}`);
      process.exit(0);
    }
    if (stat?.isSymbolicLink()) {
      unlinkSync(linkPath);
    } else {
      console.log("[link-test-deps] tests/node_modules 已存在且不是符号链接，跳过以免覆盖");
      process.exit(0);
    }
  }
  symlinkSync(relTarget(), linkPath, "dir");
  console.log(`[link-test-deps] 已创建：tests/node_modules -> ${relTarget()}`);
} catch (err) {
  console.warn(
    `[link-test-deps] 创建符号链接失败（${err.code || err.message}）。` +
      "前端测试仍可从 frontend/ 目录运行，但 tests/frontend 下的用例会找不到依赖。",
  );
}

function safeLstat(p) {
  try {
    return lstatSync(p);
  } catch {
    return null;
  }
}

function isDanglingLink(p) {
  const stat = safeLstat(p);
  return Boolean(stat?.isSymbolicLink() && !existsSync(p));
}
