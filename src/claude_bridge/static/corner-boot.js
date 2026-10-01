// 门户 SSO 下的右上角工具：@szyyw/design 的应用切换器 + 账户菜单（版本见 vendor/szyyw-design/VENDORED.md，
// 用 scripts/update-design.sh 同步）。只由 `serve --multi-user` 在 SZYYW_SSO=1 时注入页面（<html data-sso="1"
// data-portal="…">）；standalone 单密码模式与嵌入式组件不加载它。
//
// 样式隔离：bridge 页面用自己的 bridge-*.css，设计包的 components.css 带全局规则（*、body、a、label、.btn…），
// 直接引入会改掉 bridge 的排版。所以这里不 <link> 它，而是取回原文、整份包进
// @scope（见下方 SCOPE）再作为 adopted stylesheet 挂上——只作用于这三棵 DOM（工具位 / 切换器面板 / 账户面板），
// vendor 文件保持上游原样。tokens.css 只定义 :root 上的 --bg/--text… 变量（bridge 用的是 --bridge-*，不冲突），
// 由服务端直接 <link>；<html data-scheme="auto"> 让它的 color-scheme 跟随系统，与 bridge 的明暗一致。
import { mountAppSwitcher, mountAccountMenu } from './vendor/szyyw-design/switcher.js';

// 作用域根是 <body>，下界把 body 下除这三个容器以外的直接子元素（#app、bridge 的弹层…）连同子树排除掉。
// 不能直接 @scope (.corner-tools, …)：@scope 里的选择器隐含「:scope 的后代」，根元素自己（.corner-tools {position:fixed…}）
// 会匹配不上；body 是根，所以 components.css 的 body {…} 也同样不生效。
const SCOPE = '@scope (body) to (:scope > :not(.corner-tools, .app-switcher, .account-menu))';

async function adoptScopedComponents() {
  const res = await fetch(new URL('./vendor/szyyw-design/components.css', import.meta.url));
  if (!res.ok) throw new Error(`components.css HTTP ${res.status}`);
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(`${SCOPE} {\n${await res.text()}\n}`);
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
}

const root = document.documentElement;
if (root.dataset.sso === '1') {
  const portal = root.dataset.portal || 'https://szyyw.xyz';
  if (typeof CSSScopeRule === 'undefined') {
    // 不支持 @scope 的浏览器：不挂（没有隔离就只能全局引入 components.css）；登出仍可在门户完成
    console.warn('claude-bridge: CSS @scope unsupported; corner tools not mounted');
  } else {
    try {
      await adoptScopedComponents();
      mountAppSwitcher({ portal });
      mountAccountMenu({ portal });
    } catch (e) {
      console.warn('claude-bridge: corner tools not mounted:', e);
    }
  }
}
