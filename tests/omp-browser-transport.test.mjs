import assert from 'node:assert/strict';
import test from 'node:test';

import {
  OMP_BROWSER_TRANSPORT_ERROR_CODES,
  OMP_BROWSER_TRANSPORT_KEYS,
  OmpBrowserTransportError,
  createOmpBrowserTransport,
} from '../src/phase1/browser-transport.mjs';

const SECRET = 'super-secret-value';

/** Minimal fake DOM element mirroring the browser surface the transport reads. */
function element(tagName, attributes = {}, overrides = {}) {
  return {
    nodeType: 1,
    tagName,
    id: '',
    parentElement: null,
    children: [],
    attributes: { ...attributes },
    value: '',
    textContent: '',
    files: null,
    visible: true,
    getAttribute(name) {
      const value = this.attributes[name];
      return value === undefined ? null : String(value);
    },
    hasAttribute(name) {
      return Object.prototype.hasOwnProperty.call(this.attributes, name);
    },
    getBoundingClientRect() {
      return this.visible ? { width: 10, height: 10 } : { width: 0, height: 0 };
    },
    ...overrides,
  };
}

function tree(root) {
  const walk = (node, parent = null) => {
    node.parentElement = parent;
    for (const child of node.children) walk(child, node);
  };
  walk(root);
  return root;
}

/** Install minimal browser globals so evaluate bodies run under Node. */
function installDocument(root) {
  const all = [];
  const collect = (node) => {
    all.push(node);
    for (const child of node.children) collect(child);
  };
  collect(root);

  const attributeSelector = /^\[([a-z][a-z-]*)=("([^"]*)"|'([^']*)')\]$/u;
  const matches = (node, selector) => {
    const attr = attributeSelector.exec(selector);
    if (attr !== null) return node.getAttribute(attr[1]) === (attr[3] ?? attr[4]);
    if (selector.startsWith('#')) return node.id === selector.slice(1);
    return node.tagName.toLowerCase() === selector.toLowerCase();
  };

  globalThis.document = {
    querySelector(selector) {
      for (const node of all) {
        if (matches(node, selector)) return node;
      }
      return null;
    },
    querySelectorAll(selector) {
      return all.filter((node) => matches(node, selector));
    },
  };
  globalThis.window = {
    getComputedStyle() {
      return { display: 'block', visibility: 'visible' };
    },
  };
  globalThis.CSS = { escape: (value) => String(value) };
}

/** Fake OMP tab recording calls and simulating fill/press/upload mutations. */
function fakeTab(root, overrides = {}) {
  installDocument(root);
  const calls = [];
  const basenameOf = (filePath) => filePath.split(/[\\/]/u).at(-1);
  const tab = {
    calls,
    async fill(selector, value) {
      calls.push(['fill', selector, value]);
      const el = document.querySelector(selector);
      if (el !== null) el.value = value;
    },
    async click(selector) {
      calls.push(['click', selector]);
    },
    async press(selector, key) {
      calls.push(['press', selector, key]);
      const el = document.querySelector(selector);
      if (el === null) return;
      if (key === 'Space') el.value += ' ';
      if (key === 'Backspace') el.value = el.value.slice(0, -1);
    },
    async select(selector, optionValue) {
      calls.push(['select', selector, optionValue]);
    },
    async uploadFile(selector, filePath) {
      calls.push(['uploadFile', selector, filePath]);
      const el = document.querySelector(selector);
      if (el !== null) {
        el.files = [{ name: basenameOf(filePath) }];
        el.value = `C:\\fakepath\\${basenameOf(filePath)}`;
      }
    },
    async evaluate(fn, ...args) {
      calls.push(['evaluate']);
      return fn(...args);
    },
    ...overrides,
  };
  return tab;
}

function fakeClock(start = 0) {
  let value = start;
  return {
    now: () => value,
    advance: (milliseconds) => {
      value += milliseconds;
    },
    sleep: async (milliseconds) => {
      value += milliseconds;
    },
  };
}

function transportFor(root, options = {}, tabOverrides = {}) {
  const tab = fakeTab(root, tabOverrides);
  const transport = createOmpBrowserTransport(tab, {
    observe: async () => ({ observed: true }),
    ...options,
  });
  return { tab, transport };
}

function fileInput() {
  return element('INPUT', { id: 'resume', type: 'file' });
}

function transportInvalid(error) {
  return error instanceof OmpBrowserTransportError
    && error.code === 'E_OMP_BROWSER_TRANSPORT_INVALID';
}

test('factory validates tab capabilities and options exactly', () => {
  const tab = fakeTab(tree(element('DIV')));
  const observe = async () => ({});
  assert.throws(() => createOmpBrowserTransport(null, { observe }), transportInvalid);
  const missingEvaluate = { ...tab };
  delete missingEvaluate.evaluate;
  assert.throws(() => createOmpBrowserTransport(missingEvaluate, { observe }), transportInvalid);
  const missingSelect = { ...tab };
  delete missingSelect.select;
  assert.throws(() => createOmpBrowserTransport(missingSelect, { observe }), transportInvalid);
  assert.throws(() => createOmpBrowserTransport(tab), transportInvalid);
  assert.throws(() => createOmpBrowserTransport(tab, {}), transportInvalid);
  assert.throws(() => createOmpBrowserTransport(tab, { observe: 'not-a-function' }), transportInvalid);
  assert.throws(() => createOmpBrowserTransport(tab, { observe, extra: 1 }), transportInvalid);
  assert.throws(() => createOmpBrowserTransport(tab, { observe, now: 'x' }), transportInvalid);
  assert.throws(() => createOmpBrowserTransport(tab, { observe, sleep: 5 }), transportInvalid);
  assert.ok(createOmpBrowserTransport(tab, { observe }));
  const inheritedTab = Object.assign(Object.create({ helper: true }), tab);
  assert.ok(createOmpBrowserTransport(inheritedTab, { observe }));
});

test('returns a frozen transport with exactly the executor keys', () => {
  const { transport } = transportFor(tree(element('DIV')));
  assert.deepEqual(
    new Set(Object.keys(transport)),
    new Set([
      'fill', 'click', 'press', 'select', 'uploadFile',
      'readOptions', 'clickOption', 'observe', 'now', 'sleep',
    ]),
  );
  assert.deepEqual([...OMP_BROWSER_TRANSPORT_KEYS].sort(), Object.keys(transport).sort());
  for (const key of OMP_BROWSER_TRANSPORT_KEYS) assert.equal(typeof transport[key], 'function');
  assert.equal(Object.isFrozen(transport), true);
  assert.ok(OMP_BROWSER_TRANSPORT_ERROR_CODES.length > 0);
  assert.equal(Object.isFrozen(OMP_BROWSER_TRANSPORT_ERROR_CODES), true);
});

test('exposes the injected now and sleep on the transport', () => {
  const clock = fakeClock();
  const { transport } = transportFor(tree(element('DIV')), {
    now: clock.now,
    sleep: clock.sleep,
  });
  assert.equal(transport.now, clock.now);
  assert.equal(transport.sleep, clock.sleep);
});

test('ordinary fill orders exact fill, commit keys, and exact-value verify', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'name' })],
  }));
  const { tab, transport } = transportFor(root);
  assert.equal(await transport.fill('[id="name"]', 'Ada Lovelace'), undefined);
  assert.deepEqual(tab.calls, [
    ['fill', '[id="name"]', 'Ada Lovelace'],
    ['evaluate'],
    ['press', '[id="name"]', 'End'],
    ['press', '[id="name"]', 'Space'],
    ['evaluate'],
    ['press', '[id="name"]', 'Backspace'],
    ['evaluate'],
    ['evaluate'],
  ]);
  assert.equal(document.querySelector('[id="name"]').value, 'Ada Lovelace');
});

test('fill skips Backspace when the Space sample does not append', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'name' }, { value: '' })],
  }));
  const { tab, transport } = transportFor(root, {}, {
    async press(selector, key) {
      this.calls.push(['press', selector, key]);
      if (key === 'Backspace') {
        const el = document.querySelector(selector);
        if (el !== null) el.value = el.value.slice(0, -1);
      }
    },
  });
  await transport.fill('[id="name"]', 'Ada');
  assert.ok(!tab.calls.some((call) => call[0] === 'press' && call[2] === 'Backspace'));
  assert.equal(document.querySelector('[id="name"]').value, 'Ada');
  assert.deepEqual(tab.calls, [
    ['fill', '[id="name"]', 'Ada'],
    ['evaluate'],
    ['press', '[id="name"]', 'End'],
    ['press', '[id="name"]', 'Space'],
    ['evaluate'],
    ['evaluate'],
    ['evaluate'],
  ]);
});

test('combobox query input receives no commit keys', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'search', role: 'combobox' })],
  }));
  const { tab, transport } = transportFor(root);
  await transport.fill('[id="search"]', 'Engineering');
  assert.deepEqual(tab.calls, [
    ['fill', '[id="search"]', 'Engineering'],
    ['evaluate'],
  ]);
  assert.equal(document.querySelector('[id="search"]').value, 'Engineering');
});

test('clear fill removes the value through the same exact path', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'optional' }, { value: 'existing' })],
  }));
  const { tab, transport } = transportFor(root);
  await transport.fill('[id="optional"]', '');
  assert.equal(document.querySelector('[id="optional"]').value, '');
  assert.ok(tab.calls.some((call) => call[0] === 'press' && call[2] === 'Backspace'));
});

test('ordinary fill fails closed when React clears the value during settle', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'name' })],
  }));
  const { transport } = transportFor(root, {
    sleep: async () => {
      document.querySelector('[id="name"]').value = '';
    },
  });
  await assert.rejects(
    transport.fill('[id="name"]', 'Ada'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_FILL_MISMATCH',
  );
});

test('rejected text fails closed without exposing values', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'name' }, { value: 'Old' })],
  }));
  const { transport } = transportFor(root, {}, {
    async fill(selector, value) {
      this.calls.push(['fill', selector, value]);
    },
  });
  await assert.rejects(
    transport.fill('[id="name"]', 'Ada'),
    (error) => {
      assert.ok(error instanceof OmpBrowserTransportError);
      assert.equal(error.code, 'E_OMP_BROWSER_FILL_MISMATCH');
      assert.equal(error.message, 'E_OMP_BROWSER_FILL_MISMATCH');
      const serialized = JSON.stringify(error);
      assert.ok(!serialized.includes('Ada'));
      assert.ok(!serialized.includes('Old'));
      return true;
    },
  );
});

test('commit changing the text fails closed', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'name' })],
  }));
  const { transport } = transportFor(root, {}, {
    async press(selector, key) {
      this.calls.push(['press', selector, key]);
      const el = document.querySelector(selector);
      if (el === null) return;
      if (key === 'Space') el.value += ' ';
      if (key === 'Backspace') el.value = el.value.slice(0, -2);
    },
  });
  await assert.rejects(
    transport.fill('[id="name"]', 'Ada'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_FILL_MISMATCH',
  );
});

test('fill on a missing element fails closed as unverifiable', async () => {
  const { transport } = transportFor(tree(element('DIV')));
  await assert.rejects(
    transport.fill('[id="missing"]', 'Ada'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_FILL_UNVERIFIABLE',
  );
});

test('tab helper failures sanitize without leaking values', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'name' })],
  }));
  const { transport } = transportFor(root, {}, {
    async fill() {
      throw new Error(SECRET);
    },
  });
  await assert.rejects(
    transport.fill('[id="name"]', 'Ada'),
    (error) => {
      assert.equal(error.code, 'E_OMP_BROWSER_TAB_FAILED');
      assert.ok(!JSON.stringify(error).includes(SECRET));
      return true;
    },
  );
});

test('press passes allowed keys through and rejects others', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'x' })],
  }));
  const { tab, transport } = transportFor(root);
  assert.equal(await transport.press('[id="x"]', 'ArrowDown'), undefined);
  assert.deepEqual(tab.calls, [['press', '[id="x"]', 'ArrowDown']]);
  await assert.rejects(transport.press('[id="x"]', 'Enter'), transportInvalid);
  await assert.rejects(transport.press('[id="x"]', 7), transportInvalid);
  await assert.rejects(transport.press('', 'ArrowDown'), transportInvalid);
});

test('click and select pass through exactly', async () => {
  const root = tree(element('DIV', {}, {
    children: [element('INPUT', { id: 'x' })],
  }));
  const { tab, transport } = transportFor(root);
  assert.equal(await transport.click('[id="x"]'), undefined);
  assert.equal(await transport.select('[id="x"]', 'us'), undefined);
  assert.deepEqual(tab.calls, [
    ['click', '[id="x"]'],
    ['select', '[id="x"]', 'us'],
  ]);
  await assert.rejects(transport.select('[id="x"]', ''), transportInvalid);
});

test('upload performs the exact helper call and succeeds after a stable settle', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  const { tab, transport } = transportFor(root, { now: clock.now, sleep: clock.sleep });
  assert.equal(await transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'), undefined);
  assert.deepEqual(tab.calls[0], ['uploadFile', '[id="resume"]', '/tmp/resume.pdf']);
  const evaluateCalls = tab.calls.filter((call) => call[0] === 'evaluate').length;
  assert.ok(evaluateCalls >= 2, 'polls beyond the first match through the settle');
  assert.ok(evaluateCalls <= 25, 'settle polling stays bounded');
  const el = document.querySelector('[id="resume"]');
  assert.deepEqual(el.files.map((file) => file.name), ['resume.pdf']);
  assert.equal(el.value, 'C:\\fakepath\\resume.pdf');
});

test('windows-style upload paths derive the basename locally', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  const { tab, transport } = transportFor(root, { now: clock.now, sleep: clock.sleep });
  await transport.uploadFile('[id="resume"]', 'C:\\Users\\ian\\resume.pdf');
  assert.deepEqual(tab.calls[0], ['uploadFile', '[id="resume"]', 'C:\\Users\\ian\\resume.pdf']);
  assert.deepEqual(document.querySelector('[id="resume"]').files.map((file) => file.name), ['resume.pdf']);
});

test('transient upload selection fails closed', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  let cleared = false;
  const { transport } = transportFor(root, {
    now: clock.now,
    sleep: async (milliseconds) => {
      clock.advance(milliseconds);
      if (!cleared) {
        cleared = true;
        const el = document.querySelector('[id="resume"]');
        el.files = [];
        el.value = '';
      }
    },
  });
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_TRANSIENT'
      && error.message === 'E_OMP_BROWSER_UPLOAD_TRANSIENT',
  );
});

test('missing upload selection fails closed after the bounded settle', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  const { transport } = transportFor(root, { now: clock.now, sleep: clock.sleep }, {
    async uploadFile(selector, filePath) {
      this.calls.push(['uploadFile', selector, filePath]);
    },
  });
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_MISSING',
  );
});

test('generic role group filename is never accepted as upload evidence', async () => {
  const filename = element('SPAN', {}, { textContent: 'resume.pdf' });
  const genericGroup = element('DIV', { role: 'group' }, {
    querySelector() {
      return filename;
    },
  });
  const input = fileInput();
  input.closest = (selector) => (selector.includes('[role="group"]') ? genericGroup : null);
  const root = tree(element('DIV', {}, { children: [genericGroup, input] }));
  const clock = fakeClock();
  const { transport } = transportFor(root, { now: clock.now, sleep: clock.sleep }, {
    async uploadFile(selector, filePath) {
      this.calls.push(['uploadFile', selector, filePath]);
    },
  });
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_MISSING',
  );
});

test('ambiguous upload selection fails closed without polling forever', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  const { transport } = transportFor(root, { now: clock.now, sleep: clock.sleep }, {
    async uploadFile(selector, filePath) {
      this.calls.push(['uploadFile', selector, filePath]);
      const el = document.querySelector(selector);
      el.files = [{ name: 'a.pdf' }, { name: 'b.pdf' }];
      el.value = 'C:\\fakepath\\a.pdf';
    },
  });
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_AMBIGUOUS',
  );
});

test('upload fails closed when the rendered filename disagrees', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  const { transport } = transportFor(root, { now: clock.now, sleep: clock.sleep }, {
    async uploadFile(selector, filePath) {
      this.calls.push(['uploadFile', selector, filePath]);
      const el = document.querySelector(selector);
      el.files = [{ name: 'resume.pdf' }];
      el.value = 'C:\\fakepath\\other.pdf';
    },
  });
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_MISSING',
  );
});

test('invalid upload paths fail before any helper call', async () => {
  const root = tree(element('DIV', {}, { children: [fileInput()] }));
  const clock = fakeClock();
  const { tab, transport } = transportFor(root, { now: clock.now, sleep: clock.sleep });
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/..'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_INVALID_PATH',
  );
  await assert.rejects(
    transport.uploadFile('[id="resume"]', '/tmp/'),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_UPLOAD_INVALID_PATH',
  );
  assert.equal(tab.calls.length, 0);
});

test('readOptions emits bounded frozen records preserving disabled', async () => {
  const options = Array.from({ length: 300 }, (_, index) => (
    element('DIV', {}, {
      textContent: `Option ${index}`,
      attributes: {
        role: 'option',
        ...(index === 7 ? { 'aria-disabled': 'true' } : {}),
      },
    })
  ));
  const root = tree(element('DIV', {}, { children: options }));
  const { transport } = transportFor(root);
  const records = await transport.readOptions();
  assert.equal(records.length, 256);
  for (const record of records) {
    assert.deepEqual(Object.keys(record).sort(), ['disabled', 'key', 'text', 'value']);
    assert.equal(Object.isFrozen(record), true);
    assert.equal(typeof record.key, 'string');
    assert.equal(typeof record.text, 'string');
    assert.equal(typeof record.value, 'string');
    assert.equal(typeof record.disabled, 'boolean');
  }
  assert.equal(records.find((record) => record.text === 'Option 7').disabled, true);
  assert.equal(records.find((record) => record.text === 'Option 8').disabled, false);
});

test('readOptions excludes hidden options and uses data-value records', async () => {
  const root = tree(element('DIV', {}, {
    children: [
      element('DIV', {}, {
        textContent: 'Engineering',
        attributes: { role: 'option', 'data-value': 'eng' },
      }),
      element('DIV', {}, {
        textContent: 'Hidden',
        attributes: { role: 'option' },
        visible: false,
      }),
    ],
  }));
  const { transport } = transportFor(root);
  const records = await transport.readOptions();
  assert.deepEqual(records, [
    Object.freeze({ key: 'eng', text: 'Engineering', value: 'eng', disabled: false }),
  ]);
});

test('clickOption clicks the exact unique visible candidate', async () => {
  const root = tree(element('DIV', {}, {
    children: [
      element('DIV', {}, {
        id: 'opt-eng',
        textContent: 'Engineering',
        attributes: { role: 'option', 'data-value': 'eng' },
      }),
      element('DIV', {}, {
        id: 'opt-mgr',
        textContent: 'Manager',
        attributes: { role: 'option', 'data-value': 'mgr' },
      }),
    ],
  }));
  const { tab, transport } = transportFor(root);
  assert.equal(await transport.clickOption({
    key: 'eng', text: 'Engineering', value: 'eng', strategy: 'exact_text',
  }), undefined);
  assert.deepEqual(tab.calls.filter((call) => call[0] === 'click'), [['click', '#opt-eng']]);
});

test('clickOption derives a unique path when the option has no id', async () => {
  const root = tree(element('DIV', {}, {
    children: [
      element('DIV', {}, {
        children: [
          element('DIV', {}, {
            textContent: 'Engineering',
            attributes: { role: 'option' },
          }),
          element('DIV', {}, {
            textContent: 'Manager',
            attributes: { role: 'option' },
          }),
        ],
      }),
    ],
  }));
  const { tab, transport } = transportFor(root);
  await transport.clickOption({
    key: '0', text: 'Engineering', value: 'Engineering', strategy: 'exact_text',
  });
  assert.deepEqual(tab.calls.filter((call) => call[0] === 'click'), [
    ['click', 'div > div > div:nth-of-type(1)'],
  ]);
});

test('clickOption fails closed when the candidate is missing, ambiguous, or disabled', async () => {
  const root = tree(element('DIV', {}, {
    children: [
      element('DIV', {}, {
        id: 'opt-one',
        textContent: 'Engineering',
        attributes: { role: 'option', 'data-value': 'eng' },
      }),
      element('DIV', {}, {
        id: 'opt-two',
        textContent: 'Engineering',
        attributes: { role: 'option', 'data-value': 'eng' },
      }),
    ],
  }));
  const { tab, transport } = transportFor(root);
  await assert.rejects(
    transport.clickOption({
      key: 'x', text: 'Nope', value: 'nope', strategy: 'exact_text',
    }),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_OPTION_NOT_FOUND',
  );
  await assert.rejects(
    transport.clickOption({
      key: 'eng', text: 'Engineering', value: 'eng', strategy: 'exact_text',
    }),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_OPTION_AMBIGUOUS',
  );
  assert.deepEqual(tab.calls.filter((call) => call[0] === 'click'), []);
});

test('clickOption never clicks a disabled candidate', async () => {
  const root = tree(element('DIV', {}, {
    children: [
      element('DIV', {}, {
        id: 'opt-disabled',
        textContent: 'Engineering',
        attributes: { role: 'option', 'data-value': 'eng', 'aria-disabled': 'true' },
      }),
    ],
  }));
  const { tab, transport } = transportFor(root);
  await assert.rejects(
    transport.clickOption({
      key: 'eng', text: 'Engineering', value: 'eng', strategy: 'exact_text',
    }),
    (error) => error instanceof OmpBrowserTransportError
      && error.code === 'E_OMP_BROWSER_OPTION_NOT_FOUND',
  );
  assert.deepEqual(tab.calls.filter((call) => call[0] === 'click'), []);
});

test('clickOption validates the candidate shape', async () => {
  const { transport } = transportFor(tree(element('DIV')));
  await assert.rejects(transport.clickOption(null), transportInvalid);
  await assert.rejects(transport.clickOption({ text: 7, value: 'x' }), transportInvalid);
  await assert.rejects(transport.clickOption({ text: 'x' }), transportInvalid);
});

test('observe passes through the injected producer and sanitizes failures', async () => {
  const tab = fakeTab(tree(element('DIV')));
  let observeCalls = 0;
  const transport = createOmpBrowserTransport(tab, {
    observe: async () => {
      observeCalls += 1;
      return { observation: observeCalls };
    },
  });
  assert.deepEqual(await transport.observe(), { observation: 1 });
  assert.equal(observeCalls, 1);

  const failing = createOmpBrowserTransport(tab, {
    observe: async () => {
      throw new Error(SECRET);
    },
  });
  await assert.rejects(
    failing.observe(),
    (error) => {
      assert.equal(error.code, 'E_OMP_BROWSER_TAB_FAILED');
      assert.ok(!JSON.stringify(error).includes(SECRET));
      return true;
    },
  );
});

test('transport methods never leak raw tab return values', async () => {
  const root = tree(element('DIV', {}, {
    children: [
      element('INPUT', { id: 'name' }),
      element('INPUT', { id: 'x' }),
      fileInput(),
      element('DIV', {}, {
        id: 'opt-eng',
        textContent: 'Engineering',
        attributes: { role: 'option', 'data-value': 'eng' },
      }),
    ],
  }));
  const clock = fakeClock();
  const leaked = { sensitive: 'tab-return' };
  const tab = fakeTab(root, {
    fill: async (selector, value) => {
      const el = document.querySelector(selector);
      if (el !== null) el.value = value;
      return leaked;
    },
    click: async () => leaked,
    press: async () => leaked,
    select: async () => leaked,
    uploadFile: async (selector, filePath) => {
      const el = document.querySelector(selector);
      const basename = filePath.split(/[\\/]/u).at(-1);
      el.files = [{ name: basename }];
      el.value = `C:\\fakepath\\${basename}`;
      return leaked;
    },
    evaluate: async (fn, ...args) => fn(...args),
  });
  const transport = createOmpBrowserTransport(tab, {
    observe: async () => ({ observed: true }),
    now: clock.now,
    sleep: clock.sleep,
  });
  assert.equal(await transport.fill('[id="name"]', 'Ada'), undefined);
  assert.equal(await transport.click('[id="x"]'), undefined);
  assert.equal(await transport.press('[id="x"]', 'ArrowDown'), undefined);
  assert.equal(await transport.select('[id="x"]', 'us'), undefined);
  assert.equal(await transport.uploadFile('[id="resume"]', '/tmp/resume.pdf'), undefined);
  assert.equal(await transport.clickOption({
    key: 'eng', text: 'Engineering', value: 'eng', strategy: 'exact_text',
  }), undefined);
});
