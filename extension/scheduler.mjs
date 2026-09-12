const MARKER_ORIGIN = "https://brave-container.invalid";
const RECORD_PREFIX = "managed-tab:";

function validTarget(value) {
  if (typeof value !== "string"
    || !value
    || value !== value.trim()
    || value.startsWith("-")
    || /[\\\s\u0000-\u001f\u007f]/.test(value)
    || /[;|$`<>]/.test(value)
    || /%(?:0[0-9a-f]|1[0-9a-f]|7f)/i.test(value)) return false;
  if (value === "about:blank") return true;
  if (/^https?:/i.test(value) && !/^https?:\/\/[^/]/i.test(value)) return false;
  try {
    const url = new URL(value);
    return (
      (url.protocol === "http:" || url.protocol === "https:")
      || (url.protocol === "file:" && url.pathname && value.toLowerCase() !== "file:")
    ) && url.hostname.replace(/\.+$/, "").toLowerCase() !== "brave-container.invalid";
  } catch {
    return false;
  }
}

export function parseMarker(rawUrl) {
  if (typeof rawUrl !== "string") return null;
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return null;
  }
  if (
    url.origin !== MARKER_ORIGIN
    || url.pathname !== "/"
    || url.search
    || url.username
    || url.password
  ) return null;

  const params = new URLSearchParams(url.hash.slice(1));
  const expected = ["v", "action", "slot", "target", "nonce"];
  if (params.size !== expected.length || expected.some((key) => params.getAll(key).length !== 1)) {
    return null;
  }

  const slotValue = params.get("slot");
  const slot = Number(slotValue);
  const target = params.get("target");
  const nonce = params.get("nonce");
  if (
    params.get("v") !== "1"
    || params.get("action") !== "open"
    || !/^[1-9]$/.test(slotValue)
    || !validTarget(target)
    || !/^[A-Za-z0-9_-]+$/.test(nonce)
  ) return null;
  return { slot, target, nonce };
}

function longestIncreasingSubsequence(values) {
  const tails = [];
  const previous = Array(values.length).fill(-1);
  for (let index = 0; index < values.length; index += 1) {
    let low = 0;
    let high = tails.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (values[tails[middle]] < values[index]) low = middle + 1;
      else high = middle;
    }
    if (low) previous[index] = tails[low - 1];
    tails[low] = index;
  }

  const kept = new Set();
  for (let index = tails.at(-1); index !== undefined && index >= 0; index = previous[index]) {
    kept.add(index);
  }
  return kept;
}

export function planMoves(tabs, records) {
  const orderedTabs = [...tabs].sort((a, b) => a.index - b.index);
  const managed = orderedTabs.filter((tab) => {
    const value = records[`${RECORD_PREFIX}${tab.id}`];
    return !tab.pinned
      && Number.isInteger(value?.slot) && value.slot >= 1 && value.slot <= 9
      && Number.isSafeInteger(value?.sequence) && value.sequence >= 0;
  });
  if (managed.length < 2) return [];

  const desired = [...managed].sort((left, right) => {
    const a = records[`${RECORD_PREFIX}${left.id}`];
    const b = records[`${RECORD_PREFIX}${right.id}`];
    return a.slot - b.slot || a.sequence - b.sequence;
  });
  const desiredPosition = new Map(desired.map((tab, index) => [tab.id, index]));
  const keptCurrentIndexes = longestIncreasingSubsequence(
    managed.map((tab) => desiredPosition.get(tab.id)),
  );
  const keptIds = new Set([...keptCurrentIndexes].map((index) => managed[index].id));
  const model = [...orderedTabs];
  const managedIds = new Set(managed.map((tab) => tab.id));
  const moves = [];
  for (let index = desired.length - 1; index >= 0; index -= 1) {
    if (!keptIds.has(desired[index].id)) {
      const tabId = desired[index].id;
      const source = model.findIndex((tab) => tab.id === tabId);
      let target;
      if (index + 1 < desired.length) {
        const anchor = model.findIndex((tab) => tab.id === desired[index + 1].id);
        target = source < anchor ? anchor - 1 : anchor;
      } else {
        const lastOther = model.reduce(
          (last, tab, position) => managedIds.has(tab.id) && tab.id !== tabId ? position : last,
          -1,
        );
        target = lastOther;
      }
      moves.push({ tabId, index: target });
      model.splice(target, 0, ...model.splice(source, 1));
    }
  }
  return moves;
}

export function startScheduler(chromeApi) {
  const { tabs, storage: { session }, commands } = chromeApi;
  const queues = new Map();
  const pending = new Set();
  const detachedWindows = new Map();
  let sequence;
  let sequenceQueue = Promise.resolve();

  const track = (promise) => {
    const tracked = Promise.resolve(promise)
      .catch(() => {})
      .finally(() => pending.delete(tracked));
    pending.add(tracked);
    return tracked;
  };

  const enqueue = (windowId, work) => {
    const queued = (queues.get(windowId) ?? Promise.resolve())
      .catch(() => {})
      .then(work);
    queues.set(windowId, queued);
    return queued.finally(() => {
      if (queues.get(windowId) === queued) queues.delete(windowId);
    });
  };

  const allocateSequence = () => {
    const allocated = sequenceQueue.then(async () => {
      if (sequence === undefined) {
        const values = await session.get(null);
        sequence = Math.max(0, ...Object.values(values)
          .map((value) => value?.sequence)
          .filter((value) => Number.isSafeInteger(value) && value >= 0));
      }
      if (sequence >= Number.MAX_SAFE_INTEGER) throw new Error("sequence exhausted");
      sequence += 1;
      return sequence;
    });
    sequenceQueue = allocated.then(() => undefined, () => undefined);
    return allocated;
  };

  const normalize = async (windowId) => {
    const [windowTabs, records] = await Promise.all([
      tabs.query({ windowId, windowType: "normal" }),
      session.get(null),
    ]);
    for (const move of planMoves(windowTabs, records)) {
      try {
        await tabs.move(move.tabId, { index: move.index });
      } catch {
        // A closed or temporarily immovable tab will be repaired by the next event/command.
      }
    }
  };

  const processTab = async (tabId, windowId, normalizeUnpinned = false) => {
    let current;
    try {
      current = await tabs.get(tabId);
    } catch {
      return;
    }
    if (current.windowId !== windowId) {
      track(enqueue(current.windowId, () => processTab(tabId, current.windowId, normalizeUnpinned)));
      return;
    }
    const normalTabs = await tabs.query({ windowId, windowType: "normal" });
    if (!normalTabs.some((tab) => tab.id === tabId)) return;
    const marker = parseMarker(current.pendingUrl ?? current.url);
    if (!marker) {
      if (normalizeUnpinned) {
        const key = `${RECORD_PREFIX}${tabId}`;
        if ((await session.get(key))[key]) await normalize(windowId);
      }
      return;
    }

    const key = `${RECORD_PREFIX}${tabId}`;
    const existing = (await session.get(key))[key];
    if (existing?.nonce !== marker.nonce || existing?.slot !== marker.slot
      || !Number.isSafeInteger(existing?.sequence) || existing.sequence < 0) {
      await session.set({
        [key]: { slot: marker.slot, sequence: await allocateSequence(), nonce: marker.nonce },
      });
      await normalize(windowId);
    }
    await tabs.update(tabId, { url: marker.target });
  };

  tabs.onCreated.addListener((tab) => {
    track(enqueue(tab.windowId, () => processTab(tab.id, tab.windowId)));
  });
  tabs.onUpdated.addListener((tabId, changeInfo, tab) => {
    track(enqueue(tab.windowId, () => processTab(tabId, tab.windowId, changeInfo.pinned === false)));
  });
  tabs.onMoved.addListener((tabId, moveInfo) => {
    track(enqueue(moveInfo.windowId, async () => {
      const key = `${RECORD_PREFIX}${tabId}`;
      if ((await session.get(key))[key]) await normalize(moveInfo.windowId);
    }));
  });
  tabs.onRemoved.addListener((tabId, removeInfo) => {
    track(enqueue(removeInfo.windowId, async () => {
      await session.remove(`${RECORD_PREFIX}${tabId}`);
      await normalize(removeInfo.windowId);
    }));
  });
  tabs.onReplaced.addListener((addedTabId, removedTabId) => {
    track((async () => {
      const oldKey = `${RECORD_PREFIX}${removedTabId}`;
      let tab;
      try {
        tab = await tabs.get(addedTabId);
      } catch {
        await Promise.allSettled([...queues.values()]);
        await session.remove(oldKey);
        return;
      }
      await enqueue(tab.windowId, async () => {
        const record = (await session.get(oldKey))[oldKey];
        if (!record) {
          await processTab(addedTabId, tab.windowId);
          return;
        }
        await session.set({ [`${RECORD_PREFIX}${addedTabId}`]: record });
        await session.remove(oldKey);
        await normalize(tab.windowId);
      });
    })());
  });
  tabs.onDetached.addListener((tabId, detachInfo) => {
    detachedWindows.set(tabId, detachInfo.oldWindowId);
    track(enqueue(detachInfo.oldWindowId, async () => {
      const key = `${RECORD_PREFIX}${tabId}`;
      if ((await session.get(key))[key]) await normalize(detachInfo.oldWindowId);
    }));
  });
  tabs.onAttached.addListener((tabId, attachInfo) => {
    const oldWindowId = detachedWindows.get(tabId);
    detachedWindows.delete(tabId);
    if (oldWindowId !== undefined) {
      track(enqueue(oldWindowId, () => normalize(oldWindowId)));
    }
    track(enqueue(attachInfo.newWindowId, async () => {
      const key = `${RECORD_PREFIX}${tabId}`;
      if ((await session.get(key))[key]) await normalize(attachInfo.newWindowId);
    }));
  });
  commands.onCommand.addListener((command) => {
    if (command !== "sort-all-managed") return;
    track((async () => {
      const allTabs = await tabs.query({ windowType: "normal" });
      await Promise.all([...new Set(allTabs.map((tab) => tab.windowId))]
        .map((windowId) => enqueue(windowId, () => normalize(windowId))));
    })());
  });

  return {
    async drain() {
      while (pending.size) await Promise.all([...pending]);
    },
  };
}
