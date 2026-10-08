/* messages.query and messages.list, driven the way the daemon drives them.
 *
 * Both walk Thunderbird's MessageList, and both had a defect that only a real
 * mailbox showed: a query that answered with a list id instead of messages, and
 * a cursor that resumed on the page *after* the one it stopped in, silently
 * dropping everything it had not returned. The tests here are those two walks.
 */

import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { fakeBrowser, fakeClock, fakeMessages, loadScript } from "./harness.mjs";

const FOLDER = "account1://Inbox";

/** `count` headers, newest first, shaped the way Thunderbird hands them over. */
function sample(count, folderId = FOLDER) {
  return Array.from({ length: count }, (_, index) => ({
    id: 100 + index,
    headerMessageId: `<m${index}@example.invalid>`,
    subject: `Re: item ${index}`,
    author: "Ada <ada@example.invalid>",
    recipients: ["bob@example.invalid"],
    date: 1700000000000 - index * 60000,
    read: false,
    flagged: false,
    junk: false,
    tags: [],
    size: 1024,
    folder: { id: folderId, path: "/Inbox" },
  }));
}

/**
 * Load the handler module and hand back what it defined.
 *
 * The real registry.js supplies tbxError and tbxUtil so failures carry the same
 * shape the daemon sees; tbxRegistry is faked only to catch the definitions.
 */
function loadHandlers(messages, folders = [], clock = globalThis) {
  const registry = loadScript("background/registry.js");
  const browser = fakeBrowser({ folders });
  const handlers = new Map();
  browser.messages = messages;
  loadScript("background/handlers/messages.js", {
    browser,
    tbxError: registry.tbxError,
    tbxUtil: registry.tbxUtil,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    tbxRegistry: {
      define(method, fn) {
        handlers.set(method, fn);
      },
    },
  });
  return handlers;
}

function event() {
  const listeners = new Set();
  return {
    addListener: (listener) => listeners.add(listener),
    removeListener: (listener) => listeners.delete(listener),
    fire: (originals, messages) => { for (const listener of listeners) listener(originals, messages); },
    get size() { return listeners.size; },
  };
}

function writableMessages(headers = sample(2)) {
  const byId = new Map(headers.map((message) => [message.id, message]));
  return {
    onMoved: event(), onCopied: event(),
    get: async (id) => ({ ...byId.get(id), tags: [...byId.get(id).tags] }),
    update: async (id, properties) => Object.assign(byId.get(id), properties),
    move: async () => {}, copy: async () => {}, archive: async () => {},
  };
}

describe("message write history", () => {
  it("mark returns the state before updating each message, including tags", async () => {
    const messages = writableMessages();
    const mark = loadHandlers(messages).get("messages.mark");
    const result = plain(await mark({ messageIds: [100, 101], read: true,
      addTags: ["new"] }, { progress() {} }));
    assert.equal(result.updated, 2);
    assert.deepEqual(result.failures, []);
    assert.deepEqual(result.previous, [100, 101].map((id, index) => ({
      id, headerMessageId: `<m${index}@example.invalid>`, folderId: FOLDER,
      read: false, flagged: false, junk: false, tags: [],
    })));
    assert.equal((await messages.get(100)).read, true);
    assert.deepEqual((await messages.get(100)).tags, ["new"]);
  });

  it("move reports each source and landing message from an array event", async () => {
    const messages = writableMessages([sample(1)[0],
      { ...sample(1, "account1://Other")[0], id: 101,
        headerMessageId: "<other@example.invalid>" }]);
    const originals = [sample(1)[0],
      { id: 999, headerMessageId: "<unrelated@example.invalid>", folder: { id: "elsewhere" } },
      { ...sample(1, "account1://Other")[0], id: 101,
        headerMessageId: "<other@example.invalid>" }];
    messages.move = async () => messages.onMoved.fire(originals, [
      { ...sample(1, "account1://Archive")[0], id: 201 },
      { id: 999, headerMessageId: "<unrelated@example.invalid>", folder: { id: "elsewhere" } },
      { id: 202, headerMessageId: "<other@example.invalid>", folder: { id: "account1://Archive" } },
    ]);
    const move = loadHandlers(messages).get("messages.move");
    const result = plain(await move({ messageIds: [100, 101],
      destinationFolderId: "account1://Archive" }));
    assert.equal(result.moved, 2);
    assert.deepEqual(result.sourceFolderIds, [FOLDER, "account1://Other"]);
    assert.deepEqual(result.previous.map((message) => message.folderId),
      [FOLDER, "account1://Other"]);
    assert.deepEqual(result.landed.map((message) => message.id), [201, 202]);
    assert.deepEqual(result.landed.map((message) => message.folderId),
      ["account1://Archive", "account1://Archive"]);
    assert.equal(messages.onMoved.size, 0);
  });

  it("move reads every page of a MessageList event", async () => {
    const messages = writableMessages();
    messages.continueList = async (id) => {
      assert.equal(id, "next");
      return { messages: [{ ...sample(1)[0], id: 202,
        headerMessageId: "<m1@example.invalid>", folder: { id: "account1://Archive" } }] };
    };
    messages.move = async () => messages.onMoved.fire(sample(2), { id: "next", messages: [
      { ...sample(1)[0], id: 201, folder: { id: "account1://Archive" } },
    ] });
    const result = plain(await loadHandlers(messages).get("messages.move")({
      messageIds: [100, 101], destinationFolderId: "account1://Archive",
    }));
    assert.deepEqual(result.landed.map((message) => message.id), [201, 202]);
  });

  it("copy and archive report their source and landing folders", async () => {
    const messages = writableMessages();
    messages.copy = async () => messages.onCopied.fire(sample(1), [{ ...sample(1)[0], id: 301,
      folder: { id: "account1://Copies" } }]);
    messages.archive = async () => messages.onMoved.fire(sample(1), [{ ...sample(1)[0], id: 401,
      folder: { id: "account1://Archives" } }]);
    const handlers = loadHandlers(messages);
    const copied = plain(await handlers.get("messages.copy")({ messageIds: [100],
      destinationFolderId: "account1://Copies" }));
    const archived = plain(await handlers.get("messages.archive")({ messageIds: [100] }));
    assert.equal(copied.copied, 1);
    assert.equal(archived.archived, 1);
    assert.equal(copied.previous[0].folderId, FOLDER);
    assert.equal(archived.previous[0].folderId, FOLDER);
    assert.equal(copied.landed[0].folderId, "account1://Copies");
    assert.equal(archived.landed[0].folderId, "account1://Archives");
  });

  it("succeeds and removes the listener when Thunderbird sends no event", async () => {
    const clock = fakeClock();
    const messages = writableMessages();
    const pending = loadHandlers(messages, [], clock).get("messages.copy")({
      messageIds: [100], destinationFolderId: "account1://Copies",
    });
    await clock.advance(0);
    await clock.advance(1000);
    assert.deepEqual(plain((await pending).landed), []);
    assert.equal(messages.onCopied.size, 0);
  });

  it("succeeds when Thunderbird has no copy event", async () => {
    const messages = writableMessages();
    delete messages.onCopied;
    const result = plain(await loadHandlers(messages).get("messages.copy")({
      messageIds: [100], destinationFolderId: "account1://Copies",
    }));
    assert.equal(result.copied, 1);
    assert.deepEqual(result.landed, []);
  });

  it("matches landing events to source id and folder, including duplicate Message-IDs", async () => {
    const headers = sample(2);
    headers[1].headerMessageId = headers[0].headerMessageId;
    const messages = writableMessages(headers);
    messages.copy = async () => {
      messages.onCopied.fire([
        { ...headers[0], id: 999, folder: { id: "account1://Other" } },
      ], [{ ...headers[0], id: 300, folder: { id: "account1://Wrong" } }]);
      messages.onCopied.fire([headers[0]], [
        { ...headers[0], id: 301, folder: { id: "account1://Copies" } },
      ]);
      messages.onCopied.fire([headers[1]], [
        { ...headers[1], id: 302, folder: { id: "account1://Copies" } },
      ]);
    };
    const result = plain(await loadHandlers(messages).get("messages.copy")({
      messageIds: [100, 101], destinationFolderId: "account1://Copies",
    }));
    assert.deepEqual(result.landed.map((message) => message.id), [301, 302]);
  });
});

/** Values built inside the vm carry that realm's prototypes; strip them. */
function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

/** The message ids of one page. */
function ids(result) {
  return plain(result.messages).map((message) => message.id);
}

/** Errors cross the vm realm boundary, so match on the wire fields, not instanceof. */
function usageError(pattern) {
  return (ex) => {
    assert.equal(ex.tbxKind, "usage", `wrong error kind: ${ex.message}`);
    assert.match(ex.message, pattern);
    return true;
  };
}

describe("messages.query", () => {
  it("limits a newest search to a recent date window", async () => {
    const now = Date.now();
    const recent = sample(3).map((m, i) => ({ ...m, date: now - i * 1000 }));
    const old = sample(30).map((m, i) => ({ ...m, id: 200 + i }));
    const messages = fakeMessages({ folders: { [FOLDER]: [...old, ...recent] } });
    const query = loadHandlers(messages).get("messages.query");
    assert.deepEqual(ids(await query({ query: { subject: "Re" }, limit: 2 })), [100, 101]);
    assert.ok(messages.calls.some((call) => call.method === "query" && call.args[0].fromDate));
  });

  it("pages past date windows without skipping their boundaries", async () => {
    const now = Date.now();
    const day = 86400000;
    const dates = [now, now - day, now - day - 1, now - 7 * day, now - 7 * day - 1,
      now - 30 * day, now - 30 * day - 1, now - 365 * day, now - 365 * day - 1];
    const data = dates.map((date, i) => ({ ...sample(1)[0], id: i + 1, date }));
    const query = loadHandlers(fakeMessages({ folders: { [FOLDER]: data } })).get("messages.query");
    const seen = [];
    let cursor = null;
    do {
      const result = await query({ query: { folderId: FOLDER }, limit: 2, cursor });
      seen.push(...ids(result));
      cursor = result.cursor;
    } while (cursor);
    assert.deepEqual(seen, dates.map((_, i) => i + 1));
    const bounded = await query({ query: { folderId: FOLDER,
      fromDate: new Date(now - 7 * day - 1).toISOString(),
      toDate: new Date(now - day).toISOString() }, limit: 9 });
    assert.deepEqual(ids(bounded), [3, 4]);
  });
  it("answers with the matching headers, not with a list id", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({ query: { subject: "Re" }, limit: 5 });

    assert.equal(result.messages.length, 5);
    assert.deepEqual(ids(result), [100, 101, 102, 103, 104]);
    assert.equal(result.messages[0].subject, "Re: item 0");
    assert.ok(result.cursor, "25 matches are left, so there has to be a cursor");
  });

  it("asks for a page of its own size rather than a bare list id", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    await query({ query: { subject: "Re" }, limit: 5, sort: "none" });

    const queryInfo = messages.calls[0].args[0];
    assert.equal(queryInfo.returnMessageListId, undefined);
    assert.equal(queryInfo.messagesPerPage, 5);
  });

  it("puts the newest match first across folders, and pages in that order", async () => {
    // The old folder comes first in storage order, as Sent did on a real mailbox.
    const old = sample(12, "account1://Old").map((m) => ({ ...m, id: m.id + 1000, date: m.date - 1e9 }));
    const folders = { "account1://Old": old, [FOLDER]: sample(12) };
    const query = loadHandlers(fakeMessages({ folders, queryPageSize: 5 })).get("messages.query");

    const first = await query({ query: { subject: "Re" }, limit: 5 });
    const next = await query({ query: { subject: "Re" }, limit: 5, cursor: first.cursor });
    const oldest = await query({ query: { subject: "Re" }, limit: 1, sort: "oldest" });

    assert.deepEqual(ids(first), [100, 101, 102, 103, 104]);
    assert.deepEqual(ids(next), [105, 106, 107, 108, 109]);
    assert.deepEqual(ids(oldest), [1111]);
  });

  it("resolves a bare list id, for a build that insists on answering with one", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    // Same fake, forced into the shape returnMessageListId produces.
    const stubborn = {
      ...messages,
      query: (queryInfo) => messages.query({ ...queryInfo, returnMessageListId: true }),
    };
    const query = loadHandlers(stubborn).get("messages.query");

    const result = await query({ query: { subject: "Re" }, limit: 5 });

    assert.deepEqual(ids(result), [100, 101, 102, 103, 104]);
  });

  it("echoes the scope the query named, defaulting to sub-folders included", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({ query: { subject: "Re", folderId: FOLDER }, limit: 5 });

    assert.ok(result.scope, "a first page says what it searched");
    assert.deepEqual(plain(result.scope), {
      folderIds: [FOLDER],
      accountIds: null,
      includeSubFolders: true,
    });
  });

  it("keeps the account scope and an explicit includeSubFolders", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({
      query: { subject: "Re", accountId: "account1", includeSubFolders: false },
      limit: 5,
    });

    assert.equal(result.messages.length, 5);
    assert.ok(result.scope, "a first page says what it searched");
    assert.deepEqual(plain(result.scope), {
      folderIds: null,
      accountIds: ["account1"],
      includeSubFolders: false,
    });
  });

  it("reports a query that named no folder as unscoped", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");

    const result = await query({ query: { subject: "Re" }, limit: 5 });

    assert.ok(result.scope, "a first page says what it searched");
    assert.deepEqual(plain(result.scope), {
      folderIds: null,
      accountIds: null,
      includeSubFolders: false,
    });
  });

  it("leaves the scope off a continuation page", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
    const query = loadHandlers(messages).get("messages.query");
    const params = { query: { subject: "Re", folderId: FOLDER }, limit: 5 };

    const first = await query(params);
    const next = await query({ ...params, cursor: first.cursor });

    assert.equal(next.messages.length, 5, "the walk carried on");
    assert.equal(next.scope, undefined, "the scope belongs to the first page only");
  });
});

describe("paging", () => {
  /** Walk a handler with its own cursors until it says there is nothing left. */
  async function walk(handler, params) {
    const seen = [];
    const pages = [];
    const cursors = [];
    for (let guard = 0, cursor = null; guard < 200; guard += 1) {
      const result = await handler({ ...params, cursor });
      pages.push(result.messages.length);
      seen.push(...ids(result));
      cursors.push(result.cursor);
      cursor = result.cursor;
      if (!cursor) {
        return { seen, pages, cursors };
      }
    }
    throw new Error("the walk never ran out of cursors");
  }

  it("walks a folder without gaps or duplicates, whatever the limit", async () => {
    for (const limit of [1, 3, 7, 25]) {
      const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
      const list = loadHandlers(messages).get("messages.list");
      const whole = await list({ folderId: FOLDER, limit: 30 });

      const walked = await walk(list, { folderId: FOLDER, limit });

      assert.equal(walked.seen.length, 30, `limit ${limit} lost messages`);
      assert.deepEqual(walked.seen, ids(whole), `limit ${limit} walked out of order`);
    }
  });

  it("walks a query without gaps or duplicates, whatever the limit", async () => {
    for (const limit of [1, 3, 7, 25]) {
      const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, queryPageSize: 10 });
      const query = loadHandlers(messages).get("messages.query");
      const params = { query: { subject: "Re: item" } };
      const whole = await query({ ...params, limit: 30 });

      const walked = await walk(query, { ...params, limit });

      assert.equal(walked.seen.length, 30, `limit ${limit} lost messages`);
      assert.deepEqual(walked.seen, ids(whole), `limit ${limit} walked out of order`);
    }
  });

  it("returns the short last page and then stops", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(12) }, pageSize: 10 });
    const list = loadHandlers(messages).get("messages.list");

    const walked = await walk(list, { folderId: FOLDER, limit: 5 });

    assert.deepEqual(walked.pages, [5, 5, 2]);
    assert.equal(walked.cursors.at(-1), null);
    assert.match(
      walked.cursors[0],
      /^tbx:[^:]+:\d+$/,
      "a part-read page needs a cursor of ours, naming this load"
    );
    assert.match(walked.cursors[1], /^tbx:/);
  });

  it("evicts the oldest raw-query page and aborts its Thunderbird list", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
    const list = loadHandlers({ ...messages,
      query: (info) => messages.query({ ...info, messagesPerPage: 10 }),
    }).get("messages.query");
    const params = { query: { folderId: FOLDER }, sort: "none", limit: 3 };
    const cursors = [];
    for (let walks = 0; walks < 33; walks += 1) {
      // Each one stops 3 messages into a 10-message page and is then abandoned.
      cursors.push((await list(params)).cursor);
    }

    assert.deepEqual(
      messages.calls.filter((call) => call.method === "abortList").map((call) => call.args[0]),
      ["list-1"]
    );
    await assert.rejects(
      () => list({ ...params, cursor: cursors[0] }),
      usageError(/cursor/)
    );
    const newest = await list({ ...params, cursor: cursors.at(-1) });
    assert.equal(newest.messages.length, 3, "the newest walk survived the eviction");
  });

  it("refuses a cursor minted by an earlier load of the script", async () => {
    const folders = { [FOLDER]: sample(30) };
    const first = loadHandlers(fakeMessages({ folders, pageSize: 10 }));
    const second = loadHandlers(fakeMessages({ folders, pageSize: 10 }));
    const stale = (await first.get("messages.list")({ folderId: FOLDER, limit: 3 })).cursor;
    const fresh = (await second.get("messages.list")({ folderId: FOLDER, limit: 3 })).cursor;

    assert.notEqual(stale, fresh, "two loads must not mint the same cursor");
    await assert.rejects(
      () => second.get("messages.list")({ folderId: FOLDER, limit: 3, cursor: stale }),
      usageError(/cursor/)
    );
  });

  it("refuses a cursor of ours from a foreign load instead of trying it", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
    const list = loadHandlers(messages).get("messages.query");

    await assert.rejects(
      () => list({ folderId: FOLDER, limit: 3, cursor: "tbx:zzzz:1" }),
      usageError(/cursor/)
    );
    assert.deepEqual(
      messages.calls.filter((call) => call.method === "continueList"),
      [],
      "a cursor of ours must never be handed to Thunderbird"
    );
  });

  it("says a raw Thunderbird cursor has expired", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(30) }, pageSize: 10 });
    const list = loadHandlers(messages).get("messages.query");

    await assert.rejects(
      () => list({ query: { folderId: FOLDER }, sort: "none", limit: 3, cursor: "list-404" }),
      usageError(/expired/)
    );
  });
});

describe("messages.list", () => {
  const inboxes = ["account1://Inbox", "account2://Inbox", "account3://Inbox"];
  const folders = [
    ...inboxes.map((id) => ({ id, specialUse: ["inbox"], isUnified: false })),
    { id: "account1://Sent", specialUse: ["sent"], isUnified: false },
    { id: "account1://Inbox/Child", specialUse: [], isUnified: false },
    { id: "unified://inbox", specialUse: ["inbox"], isUnified: true },
  ];

  it("lists a folder newest first without invalid list arguments", async () => {
    const messages = fakeMessages({ folders: { [FOLDER]: sample(3).reverse() } });
    const list = loadHandlers(messages, folders).get("messages.list");
    assert.deepEqual(ids(await list({ folderId: FOLDER, limit: 3 })), [100, 101, 102]);
    assert.equal(messages.calls.some((call) => call.method === "list" && call.args.length > 1), false);
    assert.ok(messages.calls.filter((call) => call.method === "query" && call.args[0].fromDate)
      .every((call) => Object.prototype.toString.call(call.args[0].fromDate) === "[object Date]"));
  });

  it("combines inboxes and resolves a unified inbox", async () => {
    const now = Date.now();
    const data = Object.fromEntries(inboxes.map((id, i) => [id, [{ ...sample(1, id)[0], id: i + 1, date: now - i * 1000 }] ]));
    data["account1://Sent"] = [{ ...sample(1)[0], id: 90, date: now + 1000 }];
    data["account1://Inbox/Child"] = [{ ...sample(1)[0], id: 91, date: now + 1000 }];
    const list = loadHandlers(fakeMessages({ folders: data }), folders).get("messages.list");
    assert.deepEqual(ids(await list({ specialUse: "inbox", limit: 2 })), [1, 2]);
    assert.deepEqual(ids(await list({ folderId: "unified://inbox", limit: 2 })), [1, 2]);
    await assert.rejects(() => list({}), usageError(/folderId|specialUse/));
    await assert.rejects(() => list({ folderId: FOLDER, specialUse: "inbox" }), usageError(/folderId|specialUse/));
  });

  it("sorts subjects and ascending dates", async () => {
    const data = sample(3).map((m, i) => ({ ...m, subject: ["Z", "A", "M"][i] }));
    const list = loadHandlers(fakeMessages({ folders: { [FOLDER]: data } }), folders).get("messages.list");
    assert.deepEqual(ids(await list({ folderId: FOLDER, sortType: "subject", limit: 3 })), [100, 102, 101]);
    assert.deepEqual(ids(await list({ folderId: FOLDER, sortOrder: "ascending", limit: 3 })), [102, 101, 100]);
  });

  it("rejects unknown sort and special-use values with the accepted options", async () => {
    const list = loadHandlers(fakeMessages({ folders: { [FOLDER]: sample(1) } }), folders).get("messages.list");
    await assert.rejects(() => list({ folderId: FOLDER, sortType: "sizes" }), usageError(/date, subject, author/));
    await assert.rejects(() => list({ folderId: FOLDER, sortOrder: "DESC" }), usageError(/descending or ascending/));
    await assert.rejects(() => list({ specialUse: "outbox" }), usageError(/inbox, drafts/));
  });
});
