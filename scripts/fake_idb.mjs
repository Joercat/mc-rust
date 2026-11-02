// An in-memory IndexedDB for `scripts/asset_cache_check.mjs`.
//
// Only what `public/asset-cache.js` uses: `open`/`deleteDatabase`, one store
// per database, `get`/`put`/`delete`/`getAllKeys`, and the request/tx events.
// A write is applied when it is issued and its transaction completes on the
// next macrotask, which is the part that matters: a browser's events are never
// delivered inside the call that queued them, so the cache cannot depend on
// seeing its own writes synchronously. Records are kept by reference; a real
// structured clone would copy a Blob, and the harness only compares bytes.

const later = (fn) => setTimeout(fn, 0);

export class FakeIndexedDB {
  constructor() {
    this.databases = new Map();
    /** Throw from `open` with this message: a browser refusing the cache. */
    this.failOpen = null;
    this.opened = 0;
  }

  open(name) {
    const request = {
      result: null,
      error: null,
      onsuccess: null,
      onerror: null,
      onupgradeneeded: null,
      onblocked: null,
    };
    later(() => {
      if (this.failOpen) {
        request.error = new Error(this.failOpen);
        request.onerror?.();
        return;
      }
      let db = this.databases.get(name);
      const created = db === undefined;
      if (created) {
        db = new FakeDatabase(name);
        this.databases.set(name, db);
      }
      request.result = db;
      this.opened += 1;
      if (created) request.onupgradeneeded?.();
      request.onsuccess?.();
    });
    return request;
  }

  deleteDatabase(name) {
    const request = { onsuccess: null, onerror: null, onblocked: null };
    later(() => {
      this.databases.delete(name);
      request.onsuccess?.();
    });
    return request;
  }
}

class FakeDatabase {
  constructor(name) {
    this.name = name;
    this.stores = new Map();
    this.objectStoreNames = { contains: (store) => this.stores.has(store) };
  }

  createObjectStore(name) {
    this.stores.set(name, new Map());
    return { name };
  }

  transaction(store) {
    const rows = this.stores.get(store);
    if (!rows) throw new Error(`no store ${store}`);
    // `onerror` being assigned is not an abort: the cache installs a handler on
    // every write. Only an error of ours would skip the completion event.
    const tx = { oncomplete: null, onerror: null, error: null, aborted: false };
    tx.objectStore = () => ({
      get: (key) => {
        const request = { result: undefined, error: null, onsuccess: null, onerror: null };
        later(() => {
          request.result = rows.get(key);
          request.onsuccess?.();
        });
        return request;
      },
      put: (record) => {
        rows.set(record.key, record);
        return { result: record.key, onsuccess: null, onerror: null };
      },
      delete: (key) => {
        rows.delete(key);
        return { result: undefined, onsuccess: null, onerror: null };
      },
      getAllKeys: () => {
        const request = { result: [], error: null, onsuccess: null, onerror: null };
        later(() => {
          request.result = [...rows.keys()];
          request.onsuccess?.();
        });
        return request;
      },
    });
    // A read-only transaction commits too; a browser fires `complete` for it.
    later(() => {
      if (tx.aborted) return;
      tx.oncomplete?.();
    });
    return tx;
  }
}
