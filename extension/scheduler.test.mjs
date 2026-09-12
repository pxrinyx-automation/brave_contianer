import assert from "node:assert/strict";
import test from "node:test";

import { parseMarker, planMoves, startScheduler } from "./scheduler.mjs";

const marker = (fragment) => `https://brave-container.invalid/#${fragment}`;
const record = (slot, sequence, nonce = `nonce-${sequence}`) => ({ slot, sequence, nonce });
const records = (values) => Object.fromEntries(
  Object.entries(values).map(([id, value]) => [`managed-tab:${id}`, value]),
);
const tab = (id, index, pinned = false) => ({ id, index, pinned });

function applyMoves(tabs, moves) {
  const result = [...tabs].sort((a, b) => a.index - b.index);
  for (const { tabId, index } of moves) {
    const source = result.findIndex((item) => item.id === tabId);
    const [moved] = result.splice(source, 1);
    result.splice(index, 0, moved);
  }
  return result.map((item) => item.id);
}

class FakeEvent {
  listeners = [];

  addListener(listener) {
    this.listeners.push(listener);
  }

  emit(...args) {
    for (const listener of this.listeners) listener(...args);
  }
}

function fakeChrome(initialTabs = []) {
  const tabState = initialTabs.map((item) => ({ pinned: false, ...item }));
  const stored = {};
  const calls = { move: [], update: [], query: [] };
  const queryGates = new Map();
  const events = {
    onCreated: new FakeEvent(),
    onUpdated: new FakeEvent(),
    onMoved: new FakeEvent(),
    onRemoved: new FakeEvent(),
    onReplaced: new FakeEvent(),
    onAttached: new FakeEvent(),
    onDetached: new FakeEvent(),
  };
  const api = {
    _calls: calls,
    _events: events,
    _stored: stored,
    _tabs: tabState,
    _failMoves: new Set(),
    _failGets: new Set(),
    _failQueries: new Set(),
    _failUpdates: new Set(),
    _failRemoves: new Set(),
    _failSet: false,
    _queryGates: queryGates,
    _beforeSet: undefined,
    tabs: {
      ...events,
      async get(tabId) {
        if (api._failGets.has(tabId)) throw new Error("get failed");
        const found = tabState.find((item) => item.id === tabId);
        if (!found) throw new Error("No tab");
        return { ...found };
      },
      async query(query) {
        calls.query.push(query);
        if (api._failQueries.has(query.windowId) || (query.windowId === undefined && api._failQueries.has("all"))) {
          throw new Error("query failed");
        }
        if (queryGates.has(query.windowId)) await queryGates.get(query.windowId);
        return tabState
          .filter((item) => query.windowId === undefined || item.windowId === query.windowId)
          .filter((item) => query.windowType === undefined || (item.windowType ?? "normal") === query.windowType)
          .map((item) => ({ ...item }));
      },
      async move(tabId, { index }) {
        calls.move.push({ tabId, index });
        if (calls.move.length > 50) throw new Error("move feedback loop");
        if (this._owner._failMoves.has(tabId)) throw new Error("move failed");
        const moved = tabState.find((item) => item.id === tabId);
        const positions = tabState.flatMap((item, position) => (
          item.windowId === moved.windowId ? [position] : []
        ));
        const peers = tabState.filter((item) => item.windowId === moved.windowId && item.id !== tabId)
          .sort((a, b) => a.index - b.index);
        peers.splice(index, 0, moved);
        for (let position = positions.length - 1; position >= 0; position -= 1) {
          tabState.splice(positions[position], 1);
        }
        tabState.splice(positions[0], 0, ...peers);
        peers.forEach((item, position) => { item.index = position; });
        queueMicrotask(() => events.onMoved.emit(tabId, { windowId: moved.windowId }));
        return { ...moved };
      },
      async update(tabId, changes) {
        calls.update.push({ tabId, ...changes });
        if (api._failUpdates.has(tabId)) throw new Error("update failed");
        const current = tabState.find((item) => item.id === tabId);
        if (!current) throw new Error("No tab");
        Object.assign(current, changes);
        if ("url" in changes) delete current.pendingUrl;
        queueMicrotask(() => events.onUpdated.emit(tabId, changes, { ...current }));
        return this.get(tabId);
      },
    },
    storage: {
      session: {
        async get(keys) {
          if ((keys === null && api._failGets.has("storage:all"))
            || (keys !== null && [...(Array.isArray(keys) ? keys : [keys])]
              .some((key) => api._failGets.has(`storage:${key}`)))) {
            throw new Error("storage get failed");
          }
          if (keys === null) return { ...stored };
          const wanted = Array.isArray(keys) ? keys : [keys];
          return Object.fromEntries(wanted.filter((key) => key in stored).map((key) => [key, stored[key]]));
        },
        async set(values) {
          if (api._failSet) throw new Error("storage set failed");
          const beforeSet = api._beforeSet;
          api._beforeSet = undefined;
          if (beforeSet) await beforeSet();
          Object.assign(stored, values);
        },
        async remove(keys) {
          if ([...(Array.isArray(keys) ? keys : [keys])]
            .some((key) => api._failRemoves.has(key))) throw new Error("storage remove failed");
          for (const key of Array.isArray(keys) ? keys : [keys]) delete stored[key];
        },
      },
    },
    commands: { onCommand: new FakeEvent() },
  };
  return api;
}

function wireFake(chromeApi) {
  chromeApi.tabs._owner = chromeApi;
  chromeApi._scheduler = startScheduler(chromeApi);
  return chromeApi;
}

const drain = (chromeApi) => chromeApi._scheduler.drain();
const waitFor = async (predicate) => {
  for (let tries = 0; tries < 50 && !predicate(); tries += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.ok(predicate(), "timed out waiting for scheduler work");
};

const openMarker = (slot, nonce, target = `https://example.com/${nonce}`) => marker(
  `v=1&action=open&slot=${slot}&target=${encodeURIComponent(target)}&nonce=${nonce}`,
);

test("parseMarker decodes a valid marker", () => {
  const raw = marker(
    "v=1&action=open&slot=3&target=https%3A%2F%2Fexample.com%2Fa%3Fx%3D1&nonce=abc_123-XYZ",
  );
  assert.deepEqual(parseMarker(raw), {
    slot: 3,
    target: "https://example.com/a?x=1",
    nonce: "abc_123-XYZ",
  });
});

test("parseMarker accepts the supported final target schemes", () => {
  for (const target of ["http://example.com/", "file:///tmp/readme.txt", "about:blank"]) {
    const raw = marker(`v=1&action=open&slot=9&target=${encodeURIComponent(target)}&nonce=n`);
    assert.equal(parseMarker(raw)?.target, target);
  }
});

test("parseMarker rejects malformed, privileged, recursive, and ambiguous markers", () => {
  const invalid = [
    "not a URL",
    "http://brave-container.invalid/#v=1&action=open&slot=1&target=https%3A%2F%2Fexample.com&nonce=n",
    marker("v=2&action=open&slot=1&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=sort&slot=1&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=0&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=10&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=01&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=1.0&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=1e0&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=%2B1&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=0x1&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=%201&target=https%3A%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=1&target=brave%3A%2F%2Fsettings&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3Aexample.com&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2F%2F%2Fexample.com&nonce=n"),
    marker("v=1&action=open&slot=1&target=http%3A%2F%2F%2Fexample.com%2Fa&nonce=n"),
    marker("v=1&action=open&slot=1&target=&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2Fbrave-container.invalid%2F&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2Fbrave-container.invalid.%2F&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2Fbrave-container.invalid..%2F&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2F%2562rave-container.invalid%2F&nonce=n"),
    marker("v=1&action=open&slot=1&target=https%3A%2F%2Fexample.com&nonce="),
    marker("v=1&action=open&slot=1&slot=2&target=https%3A%2F%2Fexample.com&nonce=n"),
    "https://brave-container.invalid/?v=1#v=1&action=open&slot=1&target=https%3A%2F%2Fexample.com&nonce=n",
  ];
  for (const raw of invalid) assert.equal(parseMarker(raw), null, raw);
});

test("parseMarker rejects backslashes and decoded control bytes in targets", () => {
  for (const target of [
    "https://example.com\\path",
    "https://example.com/\u0000",
    "https://example.com/\u001f",
    "https://example.com/\u007f",
  ]) {
    const raw = marker(`v=1&action=open&slot=1&target=${encodeURIComponent(target)}&nonce=n`);
    assert.equal(parseMarker(raw), null, JSON.stringify(target));
  }
});

test("parseMarker validates URL structure, credentials, ports, and boundaries", () => {
  assert.equal(parseMarker(null), null);
  assert.equal(parseMarker("https://[not-an-ip]/"), null);
  for (const target of [
    "https://user:pass@127.0.0.1:8443/path?x=1&y=2#part",
    "http://[::1]:8080/#loopback",
    "https://例え.テスト/",
    "file://localhost/tmp/readme.txt",
    "about:blank",
  ]) {
    const raw = openMarker(1, "valid", target);
    assert.equal(parseMarker(raw)?.target, target);
  }
  for (const target of [
    "https://example.com/%00", "https://example.com/$(id)",
    "https://example.com/a;b", "--flag", "https://example.com:65536/",
    "https://[::1", "file:", " https://example.com/",
  ]) {
    assert.equal(parseMarker(openMarker(9, "valid", target)), null, target);
  }
  assert.deepEqual(parseMarker(openMarker(9, "last-slot")), {
    slot: 9, target: "https://example.com/last-slot", nonce: "last-slot",
  });
});

test("parseMarker rejects duplicate, missing, unknown, and credentialed markers", () => {
  const validTarget = encodeURIComponent("https://example.com/");
  const cases = [
    marker("v=1&action=open&slot=1&target=" + validTarget + "&nonce=n&nonce=m"),
    marker("v=1&action=open&slot=1&target=" + validTarget + "&nonce=n&extra=x"),
    marker("v=1&action=open&slot=1&target=" + validTarget),
    "https://user:pass@brave-container.invalid/#v=1&action=open&slot=1&target=" + validTarget + "&nonce=n",
    "https://brave-container.invalid:444/#v=1&action=open&slot=1&target=" + validTarget + "&nonce=n",
    "https://brave-container.invalid/?x=1#v=1&action=open&slot=1&target=" + validTarget + "&nonce=n",
    "https://brave-container.invalid/path#v=1&action=open&slot=1&target=" + validTarget + "&nonce=n",
  ];
  for (const raw of cases) assert.equal(parseMarker(raw), null, raw);
});

test("planMoves returns no work for empty state", () => {
  assert.deepEqual(planMoves([], {}), []);
});

test("planMoves preserves stable sequence order within a repeated slot", () => {
  const tabs = [tab(2, 0), tab(1, 1), tab(3, 2)];
  const state = records({ 1: record(1, 1), 2: record(1, 2), 3: record(1, 3) });
  const moves = planMoves(tabs, state);
  assert.equal(moves.length, 1);
  assert.deepEqual(applyMoves(tabs, moves), [1, 2, 3]);
});

test("planMoves sorts across a missing middle slot", () => {
  const tabs = [tab(3, 0), tab(1, 1)];
  const moves = planMoves(tabs, records({ 1: record(1, 1), 3: record(3, 2) }));
  assert.deepEqual(moves, [{ tabId: 3, index: 1 }]);
  assert.deepEqual(applyMoves(tabs, moves), [1, 3]);
});

test("planMoves does nothing when managed tabs are already sorted", () => {
  const tabs = [tab(1, 0), tab(2, 1), tab(3, 2)];
  assert.deepEqual(planMoves(tabs, records({
    1: record(1, 1),
    2: record(2, 2),
    3: record(3, 3),
  })), []);
});

test("planMoves moves only managed tabs around manual interleaving", () => {
  const tabs = [tab(2, 0), tab(99, 1), tab(1, 2)];
  const moves = planMoves(tabs, records({ 1: record(1, 1), 2: record(2, 2) }));
  assert.deepEqual(moves, [{ tabId: 2, index: 2 }]);
  assert.deepEqual(applyMoves(tabs, moves).filter((id) => id !== 99), [1, 2]);
  assert.ok(moves.every(({ tabId }) => tabId !== 99));
});

test("planMoves keeps later destinations correct across multiple moves and a manual tab", () => {
  const tabs = [tab(1, 0), tab(4, 1), tab(3, 2), tab(99, 3), tab(2, 4)];
  const moves = planMoves(tabs, records({
    1: record(1, 1),
    2: record(2, 2),
    3: record(3, 3),
    4: record(4, 4),
  }));
  assert.equal(moves.length, 2);
  assert.deepEqual(applyMoves(tabs, moves).filter((id) => id !== 99), [1, 2, 3, 4]);
});

test("planMoves excludes pinned tabs even when they have records", () => {
  const tabs = [tab(9, 0, true), tab(2, 1), tab(1, 2)];
  const moves = planMoves(tabs, records({
    9: record(9, 1),
    1: record(1, 2),
    2: record(2, 3),
  }));
  assert.deepEqual(moves, [{ tabId: 2, index: 2 }]);
  assert.ok(moves.every(({ tabId }) => tabId !== 9));
});

test("planMoves repairs a dragged managed tab with one move", () => {
  const tabs = [tab(3, 0), tab(1, 1), tab(2, 2)];
  const moves = planMoves(tabs, records({
    1: record(1, 1),
    2: record(2, 2),
    3: record(3, 3),
  }));
  assert.deepEqual(moves, [{ tabId: 3, index: 2 }]);
  assert.deepEqual(applyMoves(tabs, moves), [1, 2, 3]);
});

test("planMoves ignores invalid records, missing tabs, pinned tabs, and unsafe sequences", () => {
  const tabs = [tab(1, 5, true), tab(2, 3), tab(3, 1), tab(4, 2), tab(99, 0)];
  const state = {
    ...records({ 1: record(1, 1), 2: record(2, 2), 3: record(1, 3), 4: record(2, 4) }),
    "managed-tab:5": record(1, Number.MAX_SAFE_INTEGER + 1),
    "managed-tab:6": record(0, 1),
    "managed-tab:7": { slot: 1, sequence: -1 },
    "managed-tab:8": { slot: 1, sequence: "1" },
  };
  const moves = planMoves(tabs, state);
  assert.deepEqual(applyMoves(tabs, moves).filter((id) => [2, 3, 4].includes(id)), [3, 2, 4]);
  assert.ok(moves.every(({ tabId }) => [2, 3, 4].includes(tabId)));
  assert.equal(moves.some(({ tabId }) => tabId === 1 || tabId === 99), false);
});

test("planMoves handles unsorted indexes and sequence boundaries", () => {
  const tabs = [tab(1, 40), tab(2, 10), tab(3, 30), tab(4, 20)];
  const state = records({
    1: record(9, Number.MAX_SAFE_INTEGER), 2: record(1, 0),
    3: record(9, Number.MAX_SAFE_INTEGER - 1), 4: record(1, 1),
  });
  const moves = planMoves(tabs, state);
  assert.deepEqual(applyMoves(tabs, moves), [2, 4, 3, 1]);
  assert.ok(moves.every(({ index }) => Number.isInteger(index) && index >= 0));
});

test("planMoves moves an out-of-order last tab to the end", () => {
  const tabs = [tab(2, 0), tab(1, 1)];
  const moves = planMoves(tabs, records({
    1: record(1, 1), 2: record(2, 2),
  }));
  assert.deepEqual(moves, [{ tabId: 2, index: 1 }]);
  assert.deepEqual(applyMoves(tabs, moves), [1, 2]);
});

test("planMoves satisfies the ordering invariant for every four-tab permutation", () => {
  const permutations = (items) => items.length === 0
    ? [[]]
    : items.flatMap((item, index) => permutations([
      ...items.slice(0, index), ...items.slice(index + 1),
    ]).map((rest) => [item, ...rest]));
  for (const ids of permutations([1, 2, 3, 4])) {
    const tabs = ids.map((id, index) => tab(id, index));
    const state = records({
      1: record(2, 1), 2: record(1, 2), 3: record(2, 3), 4: record(1, 4),
    });
    const result = applyMoves(tabs, planMoves(tabs, state));
    assert.deepEqual(result, [2, 4, 1, 3], ids.join(","));
  }
});

test("scheduler waits for an updated marker, records it, moves, then navigates", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 10, index: 0, url: "https://manual.example/" },
    { id: 2, windowId: 10, index: 1, url: openMarker(1, "first") },
    { id: 3, windowId: 10, index: 2, url: "about:blank" },
  ]));

  chromeApi._events.onCreated.emit({ id: 3, windowId: 10, url: "about:blank" });
  await drain(chromeApi);
  chromeApi._tabs.find((item) => item.id === 3).url = openMarker(2, "later");
  chromeApi._events.onUpdated.emit(3, { url: openMarker(2, "later") }, { id: 3, windowId: 10 });
  await drain(chromeApi);

  assert.deepEqual(chromeApi._stored["managed-tab:3"], { slot: 2, sequence: 1, nonce: "later" });
  assert.deepEqual(chromeApi._calls.update.at(-1), { tabId: 3, url: "https://example.com/later" });
});

test("duplicate events are idempotent and rapid markers keep processing order", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(3, "a") },
    { id: 2, windowId: 1, index: 1, url: openMarker(1, "b") },
    { id: 3, windowId: 1, index: 2, url: openMarker(1, "c") },
  ]));

  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  chromeApi._events.onCreated.emit({ id: 2, windowId: 1 });
  chromeApi._events.onCreated.emit({ id: 3, windowId: 1 });
  chromeApi._events.onUpdated.emit(3, { url: openMarker(1, "c") }, { id: 3, windowId: 1 });
  await drain(chromeApi);

  assert.deepEqual([
    chromeApi._stored["managed-tab:1"].sequence,
    chromeApi._stored["managed-tab:2"].sequence,
    chromeApi._stored["managed-tab:3"].sequence,
  ], [1, 2, 3]);
  assert.equal(chromeApi._calls.update.filter(({ tabId }) => tabId === 3).length, 1);
  assert.deepEqual(chromeApi._tabs.filter(({ windowId }) => windowId === 1).map(({ id }) => id), [2, 3, 1]);
});

test("move failure retains state and still opens the target", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: "https://example.com/old" },
    { id: 2, windowId: 1, index: 1, url: openMarker(1, "new") },
  ]));
  chromeApi._stored["managed-tab:1"] = record(2, 1);
  chromeApi._failMoves.add(1);

  chromeApi._events.onCreated.emit({ id: 2, windowId: 1 });
  await drain(chromeApi);

  assert.deepEqual(chromeApi._stored["managed-tab:2"], { slot: 1, sequence: 2, nonce: "new" });
  assert.deepEqual(chromeApi._calls.update.at(-1), { tabId: 2, url: "https://example.com/new" });
});

test("close cleanup stays behind in-flight marker storage", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(1, "closing") },
  ]));
  let entered;
  let release;
  const started = new Promise((resolve) => { entered = resolve; });
  const blocked = new Promise((resolve) => { release = resolve; });
  chromeApi._beforeSet = async () => { entered(); await blocked; };

  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await started;
  chromeApi._tabs.splice(0, 1);
  chromeApi._events.onRemoved.emit(1, { windowId: 1 });
  release();
  await drain(chromeApi);

  assert.equal("managed-tab:1" in chromeApi._stored, false);
});

test("replacement transfer stays behind in-flight marker storage", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(2, "replaced") },
    { id: 2, windowId: 1, index: 1, url: "about:blank" },
  ]));
  let entered;
  let release;
  const started = new Promise((resolve) => { entered = resolve; });
  const blocked = new Promise((resolve) => { release = resolve; });
  chromeApi._beforeSet = async () => { entered(); await blocked; };

  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await started;
  chromeApi._tabs.splice(chromeApi._tabs.findIndex(({ id }) => id === 1), 1);
  chromeApi._events.onReplaced.emit(2, 1);
  release();
  await drain(chromeApi);

  assert.equal("managed-tab:1" in chromeApi._stored, false);
  assert.deepEqual(chromeApi._stored["managed-tab:2"], { slot: 2, sequence: 1, nonce: "replaced" });
});

test("replacement removes the old record when the added tab already disappeared", async () => {
  const chromeApi = wireFake(fakeChrome([]));
  chromeApi._stored["managed-tab:1"] = record(2, 1);

  chromeApi._events.onReplaced.emit(2, 1);
  await drain(chromeApi);

  assert.equal("managed-tab:1" in chromeApi._stored, false);
});

test("replacement classifies an added marker when no old record exists", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 2, windowId: 1, index: 0, url: openMarker(3, "replacement-marker") },
  ]));

  chromeApi._events.onReplaced.emit(2, 1);
  await drain(chromeApi);

  assert.deepEqual(chromeApi._stored["managed-tab:2"], {
    slot: 3, sequence: 1, nonce: "replacement-marker",
  });
  assert.deepEqual(chromeApi._calls.update.at(-1), {
    tabId: 2, url: "https://example.com/replacement-marker",
  });
});

test("stale reciprocal window hints reroute without deadlocking", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 2, index: 0, url: openMarker(1, "one") },
    { id: 2, windowId: 1, index: 0, url: openMarker(2, "two") },
  ]));

  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  chromeApi._events.onCreated.emit({ id: 2, windowId: 2 });
  await Promise.race([
    drain(chromeApi),
    new Promise((_, reject) => setTimeout(() => reject(new Error("scheduler deadlocked")), 100)),
  ]);

  assert.deepEqual(Object.keys(chromeApi._stored).sort(), ["managed-tab:1", "managed-tab:2"]);
});

test("independent-window sequence allocation is unique and deterministic", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(1, "one") },
    { id: 2, windowId: 2, index: 0, url: openMarker(1, "two") },
  ]));

  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  chromeApi._events.onCreated.emit({ id: 2, windowId: 2 });
  await drain(chromeApi);

  assert.deepEqual([
    chromeApi._stored["managed-tab:1"].sequence,
    chromeApi._stored["managed-tab:2"].sequence,
  ], [1, 2]);
});

test("window queues are independent and pinned markers are not moved", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(2, "slow") },
    { id: 2, windowId: 2, index: 0, url: openMarker(1, "fast"), pinned: true },
  ]));
  let release;
  chromeApi._queryGates.set(1, new Promise((resolve) => { release = resolve; }));

  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  chromeApi._events.onCreated.emit({ id: 2, windowId: 2 });
  await waitFor(() => chromeApi._calls.update.some(({ tabId }) => tabId === 2));

  assert.deepEqual(chromeApi._calls.update.at(-1), { tabId: 2, url: "https://example.com/fast" });
  assert.equal(chromeApi._calls.move.some(({ tabId }) => tabId === 2), false);
  release();
  await drain(chromeApi);
});

test("only managed moves normalize, and unpinning a managed tab normalizes", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 2, windowId: 1, index: 0, url: "https://example.com/2" },
    { id: 1, windowId: 1, index: 1, url: "https://example.com/1" },
    { id: 9, windowId: 1, index: 2, url: "https://manual.example/" },
  ]));
  Object.assign(chromeApi._stored, records({ 1: record(1, 1), 2: record(2, 2) }));

  chromeApi._events.onMoved.emit(9, { windowId: 1 });
  await drain(chromeApi);
  assert.equal(chromeApi._calls.move.length, 0);
  chromeApi._events.onMoved.emit(2, { windowId: 1 });
  await drain(chromeApi);
  assert.equal(chromeApi._calls.move.length, 1);

  chromeApi._tabs.find(({ id }) => id === 2).pinned = false;
  chromeApi._tabs.find(({ id }) => id === 2).index = 0;
  chromeApi._tabs.find(({ id }) => id === 1).index = 1;
  chromeApi._events.onUpdated.emit(2, { pinned: false }, { id: 2, windowId: 1, pinned: false });
  await drain(chromeApi);
  assert.equal(chromeApi._calls.move.length, 2);
});

test("close, replacement, and cross-window attachment maintain session records", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: "https://example.com/1" },
    { id: 2, windowId: 2, index: 0, url: "https://example.com/2" },
    { id: 3, windowId: 2, index: 1, url: "https://example.com/3" },
  ]));
  Object.assign(chromeApi._stored, records({ 1: record(1, 1), 2: record(2, 2) }));

  chromeApi._events.onReplaced.emit(3, 2);
  await drain(chromeApi);
  assert.deepEqual(chromeApi._stored["managed-tab:3"], record(2, 2));
  assert.equal("managed-tab:2" in chromeApi._stored, false);
  chromeApi._events.onDetached.emit(3, { oldWindowId: 2 });
  await drain(chromeApi);
  chromeApi._tabs.find(({ id }) => id === 3).windowId = 1;
  chromeApi._events.onAttached.emit(3, { newWindowId: 1 });
  chromeApi._events.onRemoved.emit(1, { windowId: 1 });
  await drain(chromeApi);
  assert.equal("managed-tab:1" in chromeApi._stored, false);
  assert.deepEqual(chromeApi._stored["managed-tab:3"], record(2, 2));
  assert.ok(chromeApi._calls.query.some(({ windowId }) => windowId === 2));
  assert.ok(chromeApi._calls.query.some(({ windowId }) => windowId === 1));
});

test("sort-all-managed normalizes every normal window", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 2, windowId: 1, index: 0, url: "https://example.com/2" },
    { id: 1, windowId: 1, index: 1, url: "https://example.com/1" },
    { id: 4, windowId: 2, index: 0, url: "https://example.com/4" },
    { id: 3, windowId: 2, index: 1, url: "https://example.com/3" },
    { id: 6, windowId: 3, windowType: "popup", index: 0, url: "https://example.com/6" },
    { id: 5, windowId: 3, windowType: "popup", index: 1, url: "https://example.com/5" },
  ]));
  Object.assign(chromeApi._stored, records({
    1: record(1, 1), 2: record(2, 2), 3: record(1, 3), 4: record(2, 4),
    5: record(1, 5), 6: record(2, 6),
  }));

  chromeApi.commands.onCommand.emit("sort-all-managed");
  await drain(chromeApi);

  assert.deepEqual(chromeApi._tabs.filter(({ windowId }) => windowId === 1).map(({ id }) => id), [1, 2]);
  assert.deepEqual(chromeApi._tabs.filter(({ windowId }) => windowId === 2).map(({ id }) => id), [3, 4]);
  assert.deepEqual(chromeApi._tabs.filter(({ windowId }) => windowId === 3).map(({ id }) => id), [6, 5]);
  assert.ok(chromeApi._calls.query.some(({ windowType }) => windowType === "normal"));
});

test("created tabs use pendingUrl before the current URL", async () => {
  const pending = openMarker(4, "pending");
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: "about:blank", pendingUrl: pending },
  ]));
  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await drain(chromeApi);
  assert.deepEqual(chromeApi._stored["managed-tab:1"], {
    slot: 4, sequence: 1, nonce: "pending",
  });
  assert.deepEqual(chromeApi._calls.update.at(-1), {
    tabId: 1, url: "https://example.com/pending",
  });
});

test("popup markers are ignored and empty normal windows are harmless", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 2, windowType: "popup", index: 0, url: openMarker(1, "popup") },
  ]));
  chromeApi._events.onCreated.emit({ id: 1, windowId: 2 });
  chromeApi.commands.onCommand.emit("sort-all-managed");
  await drain(chromeApi);
  assert.deepEqual(chromeApi._stored, {});
  assert.equal(chromeApi._calls.update.length, 0);
  assert.deepEqual(chromeApi._tabs.map(({ id }) => id), [1]);
});

test("persisted sequence maxima are continued and malformed maxima are ignored", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(1, "next") },
  ]));
  Object.assign(chromeApi._stored, {
    "managed-tab:old": record(9, 41),
    "managed-tab:string": { sequence: "999" },
    "other": { sequence: Number.MAX_SAFE_INTEGER + 1 },
  });
  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await drain(chromeApi);
  assert.equal(chromeApi._stored["managed-tab:1"].sequence, 42);
});

test("missing tabs, failed storage, query, and update calls recover on later events", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(1, "recover") },
    { id: 2, windowId: 1, index: 1, url: openMarker(2, "storage") },
    { id: 3, windowId: 1, index: 2, url: openMarker(3, "query") },
    { id: 4, windowId: 1, index: 3, url: openMarker(4, "update") },
  ]));
  chromeApi._failGets.add(99);
  chromeApi._events.onCreated.emit({ id: 99, windowId: 1 });
  await drain(chromeApi);

  chromeApi._failGets.add("storage:managed-tab:1");
  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await drain(chromeApi);
  chromeApi._failGets.delete("storage:managed-tab:1");
  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await drain(chromeApi);
  assert.ok(chromeApi._stored["managed-tab:1"]);

  chromeApi._failSet = true;
  chromeApi._events.onCreated.emit({ id: 2, windowId: 1 });
  await drain(chromeApi);
  chromeApi._failSet = false;
  chromeApi._events.onCreated.emit({ id: 2, windowId: 1 });
  await drain(chromeApi);
  assert.ok(chromeApi._stored["managed-tab:2"]);

  chromeApi._failQueries.add(1);
  chromeApi._events.onCreated.emit({ id: 3, windowId: 1 });
  await drain(chromeApi);
  chromeApi._failQueries.delete(1);
  chromeApi._events.onCreated.emit({ id: 3, windowId: 1 });
  await drain(chromeApi);
  assert.ok(chromeApi._stored["managed-tab:3"]);

  const allGetsFail = wireFake(fakeChrome([
    { id: 5, windowId: 5, index: 0, url: openMarker(5, "all-get") },
  ]));
  allGetsFail._failGets.add("storage:all");
  allGetsFail._events.onCreated.emit({ id: 5, windowId: 5 });
  await drain(allGetsFail);
  allGetsFail._failGets.delete("storage:all");
  allGetsFail._events.onCreated.emit({ id: 5, windowId: 5 });
  await drain(allGetsFail);
  assert.ok(allGetsFail._stored["managed-tab:5"]);

  chromeApi._failUpdates.add(4);
  chromeApi._events.onCreated.emit({ id: 4, windowId: 1 });
  await drain(chromeApi);
  assert.ok(chromeApi._stored["managed-tab:4"]);
  chromeApi._failUpdates.delete(4);
  chromeApi._events.onUpdated.emit(4, { url: openMarker(4, "update") },
    { id: 4, windowId: 1 });
  await drain(chromeApi);
  assert.equal(chromeApi._calls.update.filter(({ tabId }) => tabId === 4).length, 2);
});

test("invalid existing records are repaired and unpinning without a marker normalizes", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: "https://example.com/one", pinned: false },
    { id: 2, windowId: 1, index: 1, url: openMarker(1, "repair") },
  ]));
  chromeApi._stored["managed-tab:1"] = { slot: 99, sequence: "bad", nonce: "repair" };
  chromeApi._events.onCreated.emit({ id: 2, windowId: 1 });
  await drain(chromeApi);
  assert.equal(chromeApi._stored["managed-tab:2"].slot, 1);
  chromeApi._stored["managed-tab:1"] = record(1, 1);
  chromeApi._events.onUpdated.emit(1, { pinned: false }, {
    id: 1, windowId: 1, pinned: false, url: "https://example.com/one",
  });
  await drain(chromeApi);
  assert.ok(chromeApi._calls.query.some(({ windowId }) => windowId === 1));
});

test("same-nonce records with invalid sequence values are repaired", async () => {
  for (const sequence of ["bad", -1]) {
    const chromeApi = wireFake(fakeChrome([
      { id: 1, windowId: 1, index: 0, url: openMarker(1, "same") },
    ]));
    chromeApi._stored["managed-tab:1"] = { slot: 1, sequence, nonce: "same" };
    chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
    await drain(chromeApi);
    assert.equal(chromeApi._stored["managed-tab:1"].sequence, 1);
  }
});

test("cleanup tolerates missing records and failing removals", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: "https://example.com/one" },
  ]));
  chromeApi._events.onRemoved.emit(1, { windowId: 1 });
  await drain(chromeApi);
  chromeApi._tabs.push({ id: 2, windowId: 1, index: 0, url: "https://example.com/two" });
  chromeApi._stored["managed-tab:2"] = record(1, 1);
  chromeApi._failRemoves.add("managed-tab:2");
  chromeApi._events.onRemoved.emit(2, { windowId: 1 });
  await drain(chromeApi);
  chromeApi._failRemoves.delete("managed-tab:2");
  chromeApi._events.onRemoved.emit(2, { windowId: 1 });
  await drain(chromeApi);
  assert.equal("managed-tab:2" in chromeApi._stored, false);
});

test("replacement, detach, attach, and unknown commands recover from failures", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: "https://example.com/one" },
    { id: 2, windowId: 2, index: 0, url: "https://example.com/two" },
  ]));
  chromeApi._stored["managed-tab:1"] = record(2, 4);
  chromeApi._failGets.add(2);
  chromeApi._events.onReplaced.emit(2, 1);
  await drain(chromeApi);
  assert.equal("managed-tab:1" in chromeApi._stored, false);
  chromeApi._failGets.delete(2);
  chromeApi._tabs.find(({ id }) => id === 2).url = openMarker(2, "replacement");
  chromeApi._events.onReplaced.emit(2, 1);
  await drain(chromeApi);
  assert.deepEqual(chromeApi._stored["managed-tab:2"], record(2, 1, "replacement"));

  chromeApi._events.onDetached.emit(2, { oldWindowId: 2 });
  await drain(chromeApi);
  chromeApi._tabs.find(({ id }) => id === 2).windowId = 1;
  chromeApi._events.onAttached.emit(2, { newWindowId: 1 });
  chromeApi.commands.onCommand.emit("not-a-command");
  await drain(chromeApi);
  assert.equal("managed-tab:2" in chromeApi._stored, true);
});

test("sequence exhaustion fails closed without updating the tab", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 1, index: 0, url: openMarker(1, "overflow") },
  ]));
  chromeApi._stored["managed-tab:old"] = record(1, Number.MAX_SAFE_INTEGER);
  chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
  await drain(chromeApi);
  assert.equal("managed-tab:1" in chromeApi._stored, false);
  assert.equal(chromeApi._calls.update.length, 0);
});

test("swallowed failures are still observable through structured logging", async () => {
  const logs = [];
  const originalError = console.error;
  console.error = (...args) => logs.push(args);
  try {
    const chromeApi = wireFake(fakeChrome([
      { id: 10, windowId: 1, index: 0, url: "https://example.com/existing" },
      { id: 1, windowId: 1, index: 1, url: openMarker(1, "log-one") },
    ]));
    chromeApi._stored["managed-tab:10"] = record(2, 1);
    chromeApi._failMoves.add(10); // tab 10 (slot 2) must shift for slot 1 -- this move fails.
    chromeApi._events.onCreated.emit({ id: 1, windowId: 1 });
    await drain(chromeApi);
    assert.ok(logs.length > 0, "expected at least one structured log entry");
    assert.ok(logs.every(([prefix]) => prefix === "[brave-container-scheduler]"),
      "log entries should carry a stable, greppable prefix");
  } finally {
    console.error = originalError;
  }
});

test("closing every tab in a window removes all of that window's managed-tab state", async () => {
  const chromeApi = wireFake(fakeChrome([
    { id: 1, windowId: 9, index: 0, url: openMarker(1, "w9-a") },
    { id: 2, windowId: 9, index: 1, url: openMarker(2, "w9-b") },
    { id: 3, windowId: 1, index: 0, url: openMarker(1, "w1-a") },
  ]));
  chromeApi._events.onCreated.emit({ id: 1, windowId: 9 });
  chromeApi._events.onCreated.emit({ id: 2, windowId: 9 });
  chromeApi._events.onCreated.emit({ id: 3, windowId: 1 });
  await drain(chromeApi);
  assert.ok(chromeApi._stored["managed-tab:1"]);
  assert.ok(chromeApi._stored["managed-tab:2"]);

  // Chrome fires tabs.onRemoved for every tab a closing window contained
  // (removeInfo.isWindowClosing: true) before windows.onRemoved fires for
  // the window itself, so per-tab cleanup already clears all of a closed
  // window's state -- no separate windows.onRemoved handler is needed.
  chromeApi._events.onRemoved.emit(1, { windowId: 9, isWindowClosing: true });
  chromeApi._events.onRemoved.emit(2, { windowId: 9, isWindowClosing: true });
  await drain(chromeApi);
  assert.equal("managed-tab:1" in chromeApi._stored, false);
  assert.equal("managed-tab:2" in chromeApi._stored, false);
  assert.ok(chromeApi._stored["managed-tab:3"], "other windows must be untouched");
});
