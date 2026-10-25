/** 前端单测全局准备（B0）。 */
import "@testing-library/jest-dom/vitest";

// jsdom 没有实现 matchMedia：主题相关的组件会在启动时读它，
// 不打桩就是 "matchMedia is not a function"，与被测逻辑无关。
if (!window.matchMedia) {
  window.matchMedia = ((query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
}

// jsdom 也没有实现 scrollIntoView：引用跳转用得到它。
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = function scrollIntoView() {};
}

// localStorage 在 jsdom 里存在，但个别用例需要清干净
beforeEach(() => {
  localStorage.clear();
});
