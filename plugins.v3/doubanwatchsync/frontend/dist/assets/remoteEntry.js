const modules = ['Config', 'Page', 'Dashboard'];
let stylesheet;
export function init(shared) {
  globalThis.__federation_shared__ ||= {};
  for (const [name, versions] of Object.entries(shared)) {
    for (const [version, entry] of Object.entries(versions)) {
      const scope = entry.scope || 'default';
      globalThis.__federation_shared__[scope] ||= {};
      globalThis.__federation_shared__[scope][name] ||= {};
      globalThis.__federation_shared__[scope][name][version] = entry;
    }
  }
}
export async function share(name) {
  const versions = globalThis.__federation_shared__?.default?.[name];
  if (!versions) throw new Error('MoviePilot 未提供 ' + name);
  const entry = versions[Object.keys(versions)[0]];
  const module = (await entry.get())();
  const value = await module;
  return value.default ? Object.assign({}, value.default, value) : value;
}
export async function get(name) {
  const component = name.replace(/^\.\//, '');
  if (!modules.includes(component)) throw new Error('Unknown component: ' + component);
  if (!stylesheet && typeof document !== 'undefined') {
    stylesheet = document.createElement('link');
    stylesheet.rel = 'stylesheet';
    stylesheet.href = new URL('watch.css?v=2.3.5', import.meta.url).href;
    document.head.appendChild(stylesheet);
  }
  const module = await import('./watch.js?v=2.3.5');
  return () => module[component];
}
