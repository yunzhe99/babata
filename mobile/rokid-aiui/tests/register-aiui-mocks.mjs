import { register } from 'node:module';

// AIUI provides bare runtime modules; Node tests resolve only that explicit
// host boundary to a shared mock, never a coincidental globalThis.wx object.
register('./resolve-aiui-mocks.mjs', import.meta.url);
