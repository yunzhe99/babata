export function resolve(specifier, context, nextResolve) {
  if (specifier === 'wx') {
    return { url: new URL('./mocks/wx.mjs', import.meta.url).href, shortCircuit: true };
  }
  return nextResolve(specifier, context);
}
