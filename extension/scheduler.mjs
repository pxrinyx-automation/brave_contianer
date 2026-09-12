const MARKER_ORIGIN = "https://brave-container.invalid";
const RECORD_PREFIX = "managed-tab:";
const LOG_PREFIX = "[brave-container-scheduler]";

// Self-healing catches (a tab closing mid-operation, a transient storage
// failure) intentionally keep running rather than propagate -- the next
// event or command repairs the state. Logging here doesn't change that
// recovery behavior, it just stops the failure from being invisible.
function logError(context, error) {
  console.error(LOG_PREFIX, context, error);
}

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
  const { tabs, storage: { local }, commands, action } = chromeApi;
  const queues = new Map();
  const pending = new Set();
  const detachedWindows = new Map();
  let sequence;
  let sequenceQueue = Promise.resolve();

  const track = (promise) => {
    const tracked = Promise.resolve(promise)
      .catch((error) => logError("unhandled", error))
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
        const values = await local.get(null);
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
    let windowTabs;
    let records;
    try {
      [windowTabs, records] = await Promise.all([
        tabs.query({ windowId, windowType: "normal" }),
        local.get(null),
      ]);
    } catch (error) {
      // Best-effort: a failed sort is repaired by the next event/command,
      // and must never block the tab that triggered it from navigating.
      logError("normalize:query", error);
      return;
    }
    for (const move of planMoves(windowTabs, records)) {
      try {
        await tabs.move(move.tabId, { index: move.index });
      } catch (error) {
        // A closed or temporarily immovable tab will be repaired by the next event/command.
        logError("move", error);
      }
    }
  };

  // Ctrl+Shift+0 otherwise gives no feedback: "command never fired",
  // "extension not running", and "fired, nothing to sort" all look
  // identical. The badge distinguishes them; it self-clears so it never
  // looks like a permanent extension error count.
  const reportManagedCount = async () => {
    if (!action) return;
    const [allTabs, records] = await Promise.all([
      tabs.query({ windowType: "normal" }),
      local.get(null),
    ]);
    const count = allTabs.filter((tab) => {
      const value = records[`${RECORD_PREFIX}${tab.id}`];
      return !tab.pinned && Number.isInteger(value?.slot) && value.slot >= 1 && value.slot <= 9;
    }).length;
    await action.setBadgeText({ text: String(count) });
    // unref: a plain browser setTimeout handle has no .unref, so this is a
    // no-op there; under node:test it stops the dangling timer from holding
    // the process open for the full 2s per test.
    setTimeout(() => action.setBadgeText({ text: "" }), 2000).unref?.();
  };

  const processTab = async (tabId, windowId, normalizeUnpinned = false) => {
    let current;
    try {
      current = await tabs.get(tabId);
    } catch (error) {
      logError("processTab:get", error);
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
        if ((await local.get(key))[key]) await normalize(windowId);
      }
      return;
    }

    const key = `${RECORD_PREFIX}${tabId}`;
    const existing = (await local.get(key))[key];
    const isNew = existing?.nonce !== marker.nonce || existing?.slot !== marker.slot
      || !Number.isSafeInteger(existing?.sequence) || existing.sequence < 0;
    if (isNew) {
      await local.set({
        [key]: { slot: marker.slot, sequence: await allocateSequence(), nonce: marker.nonce },
      });
    }
    // Navigation must never wait on the sort: a stranded marker tab (stuck on
    // the brave-container.invalid error page) is worse than a brief mis-order.
    await tabs.update(tabId, { url: marker.target });
    if (isNew) await normalize(windowId);
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
      if ((await local.get(key))[key]) await normalize(moveInfo.windowId);
    }));
  });
  tabs.onRemoved.addListener((tabId, removeInfo) => {
    track(enqueue(removeInfo.windowId, async () => {
      await local.remove(`${RECORD_PREFIX}${tabId}`);
      await normalize(removeInfo.windowId);
    }));
  });
  tabs.onReplaced.addListener((addedTabId, removedTabId) => {
    track((async () => {
      const oldKey = `${RECORD_PREFIX}${removedTabId}`;
      let tab;
      try {
        tab = await tabs.get(addedTabId);
      } catch (error) {
        logError("onReplaced:get", error);
        await Promise.allSettled([...queues.values()]);
        await local.remove(oldKey);
        return;
      }
      await enqueue(tab.windowId, async () => {
        const record = (await local.get(oldKey))[oldKey];
        if (!record) {
          await processTab(addedTabId, tab.windowId);
          return;
        }
        await local.set({ [`${RECORD_PREFIX}${addedTabId}`]: record });
        await local.remove(oldKey);
        await normalize(tab.windowId);
      });
    })());
  });
  tabs.onDetached.addListener((tabId, detachInfo) => {
    detachedWindows.set(tabId, detachInfo.oldWindowId);
    track(enqueue(detachInfo.oldWindowId, async () => {
      const key = `${RECORD_PREFIX}${tabId}`;
      if ((await local.get(key))[key]) await normalize(detachInfo.oldWindowId);
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
      if ((await local.get(key))[key]) await normalize(attachInfo.newWindowId);
    }));
  });
  commands.onCommand.addListener((command) => {
    if (command !== "sort-all-managed") return;
    track((async () => {
      const allTabs = await tabs.query({ windowType: "normal" });
      await Promise.all([...new Set(allTabs.map((tab) => tab.windowId))]
        .map((windowId) => enqueue(windowId, () => normalize(windowId))));
      await reportManagedCount();
    })());
  });

  // Records live in storage.local so they survive an extension reload (tab
  // ids are stable across that), but tab ids do NOT survive a browser
  // restart -- a record for a tab that no longer exists would otherwise sit
  // there forever. Prune once, at worker start.
  track((async () => {
    const [allTabs, storedRecords] = await Promise.all([tabs.query({}), local.get(null)]);
    const liveIds = new Set(allTabs.map((tab) => tab.id));
    const stale = Object.keys(storedRecords).filter((key) => key.startsWith(RECORD_PREFIX)
      && !liveIds.has(Number(key.slice(RECORD_PREFIX.length))));
    if (stale.length) await local.remove(stale);
  })());

  return {
    async drain() {
      while (pending.size) await Promise.all([...pending]);
    },
  };
}
