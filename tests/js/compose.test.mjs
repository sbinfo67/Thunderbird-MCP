import assert from "node:assert/strict";
import { it } from "node:test";
import { fakeBrowser, fakeLog, loadScript } from "./harness.mjs";

function setup({ drafts = [], pageSize = 100 } = {}) {
  const registry = loadScript("background/registry.js");
  const browser = fakeBrowser({ folders: [{ id: "drafts", path: "/Drafts", specialUse: ["drafts"], isUnified: false }] });
  const calls = [];
  const handlers = new Map();
  let current = {};
  browser.tbx.invoke = async () => ({ name: "pic.jpg", contentType: "image/jpeg", base64: "YWJj" });
  browser.messages = {
    saveMessage: async () => { calls.push("headlessSave"); return { messages: [] }; },
    sendMessage: async () => { calls.push("headlessSend"); return { messages: [] }; },
    query: async () => ({ messages: drafts.slice(0, pageSize), id: drafts.length > pageSize ? "next" : undefined }),
    continueList: async () => ({ messages: drafts.slice(pageSize) }),
    abortList: async () => {},
  };
  browser.compose = {
    beginNew: async (details) => { calls.push("beginNew"); current = details; return { id: 1 }; },
    beginReply: async (_, __, details) => { calls.push("beginReply"); current = { ...details, body: "<body>quote</body>" }; return { id: 1 }; },
    setComposeDetails: async (_, patch) => { current = { ...current, ...patch }; },
    getComposeDetails: async () => current,
    saveMessage: async () => ({ messages: [] }),
    sendMessage: async () => ({ messages: [] }),
  };
  browser.tabs = { remove: async () => {} };
  loadScript("background/handlers/compose.js", {
    browser, tbxError: registry.tbxError, tbxUtil: registry.tbxUtil,
    tbxRegistry: { define: (name, fn) => handlers.set(name, fn) },
    tbxLog: fakeLog(), setTimeout: (fn) => fn(),
  });
  return { handlers, calls, browser, current: () => current };
}

it("embeds every named image reference and uses a compose window", async () => {
  const { handlers, calls, current } = setup();
  const result = await handlers.get("compose.save")({
    subject: "photo", isHtml: true,
    body: '<img src="cid:pic"><img src="cid:pic"><img src="cid:pic2">',
    inlineImages: [{ cid: "pic", path: "C:/pic.jpg" }, { cid: "pic2", path: "C:/pic2.jpg" }],
  });
  assert.deepEqual(calls, ["beginNew"]);
  assert.equal(result.transport, "composeWindow");
  assert.equal((current().body.match(/data:image\/jpeg/g) || []).length, 3);
  assert.doesNotMatch(current().body, /cid:/);
});

it("rejects invalid inline images before opening a window", async () => {
  for (const params of [
    { isHtml: false, body: '<img src="cid:pic">' },
    { isHtml: true, body: "no image" },
  ]) {
    const { handlers, calls } = setup();
    await assert.rejects(handlers.get("compose.save")({
      ...params, inlineImages: [{ cid: "pic", path: "C:/pic.jpg" }],
    }), (error) => error.tbxKind === "usage");
    assert.deepEqual(calls, []);
  }
});

it("finds a saved draft even when Thunderbird omits it from saveMessage", async () => {
  const now = Date.now();
  const { handlers } = setup({ drafts: [
    { id: 1, subject: "photo", date: now - 10000, folder: { id: "drafts", path: "/Drafts" } },
    { id: 2, subject: "photo", date: now, folder: { id: "drafts", path: "/Drafts" } },
  ], pageSize: 1 });
  const result = await handlers.get("compose.save")({ subject: "photo" });
  assert.equal(result.messageId, 2);
  assert.equal(result.folderPath, "/Drafts");
  assert.equal(result.transport, "headless");
});

it("routes raw data images through a window", async () => {
  const { handlers, calls } = setup();
  await handlers.get("compose.send")({ mode: "send", isHtml: true,
    body: '<img src="data:image/png;base64,YWJj">' });
  assert.deepEqual(calls, ["beginNew"]);
});

it("forces an inline image reply into HTML", async () => {
  const { handlers, calls, current } = setup();
  await handlers.get("compose.reply")({ messageId: 5, isHtml: true,
    body: '<img src="cid:pic">', inlineImages: [{ cid: "pic", path: "C:/pic.jpg" }] });
  assert.deepEqual(calls, ["beginReply"]);
  assert.equal(current().isPlainText, false);
  assert.match(current().body, /data:image\/jpeg/);
});

it("rejects a non-image file", async () => {
  const { handlers, browser, calls } = setup();
  browser.tbx.invoke = async () => ({ name: "notes.txt", contentType: "text/plain", base64: "YWJj" });
  await assert.rejects(handlers.get("compose.save")({ isHtml: true,
    body: '<img src="cid:pic">', inlineImages: [{ cid: "pic", path: "C:/notes.txt" }],
  }), (error) => error.tbxKind === "usage" && /image file/.test(error.message));
  assert.deepEqual(calls, []);
});

it("leaves ambiguous draft lookup unidentified", async () => {
  const now = Date.now();
  const draft = (id) => ({ id, subject: "photo", date: now,
    folder: { id: "drafts", path: "/Drafts" } });
  const { handlers, browser } = setup();
  // Unpaginated: a later query offering a single match must never be reached.
  const pages = [[draft(1), draft(2)], [draft(2)]];
  let queries = 0;
  browser.messages.query = async () => ({ messages: pages[Math.min(queries++, 1)] });
  const result = await handlers.get("compose.save")({ subject: "photo" });
  assert.equal(result.messageId, null);
  assert.equal(queries, 1);
});

it("saves and closes the window when the subject cannot be read", async () => {
  const { handlers, browser, calls } = setup();
  browser.compose.getComposeDetails = async () => { throw new Error("no details"); };
  browser.compose.saveMessage = async () => { calls.push("save"); return { messages: [] }; };
  browser.tabs.remove = async () => { calls.push("close"); };
  browser.messages.query = async () => { calls.push("query"); return { messages: [] }; };
  const result = await handlers.get("compose.save")({ subject: "photo", isHtml: true,
    body: '<img src="cid:pic">', inlineImages: [{ cid: "pic", path: "C:/pic.jpg" }] });
  assert.deepEqual(calls, ["beginNew", "save", "close"]);
  assert.equal(result.messageId, null);
  assert.equal(result.transport, "composeWindow");
});

it("locates a draft saved through a compose window", async () => {
  const { handlers } = setup({ drafts: [{ id: 7, subject: "photo", date: Date.now(),
    folder: { id: "drafts", path: "/Drafts" } }] });
  const result = await handlers.get("compose.save")({ subject: "photo", isHtml: true,
    body: '<img src="cid:pic">', inlineImages: [{ cid: "pic", path: "C:/pic.jpg" }] });
  assert.equal(result.messageId, 7);
  assert.equal(result.transport, "composeWindow");
});

it("leaves an absent draft unidentified without failing the save", async () => {
  const { handlers } = setup();
  const result = await handlers.get("compose.save")({ subject: "photo" });
  assert.equal(result.messageId, null);
});

it("passes empty bodies explicitly to headless save and send", async () => {
  for (const [handler, params, expected] of [
    ["compose.save", { subject: "empty" }, { body: "", plainTextBody: "", isPlainText: true }],
    ["compose.save", { subject: "empty", body: "" }, { body: "", plainTextBody: "", isPlainText: true }],
    ["compose.send", { mode: "send", body: "" }, { body: "", plainTextBody: "", isPlainText: true }],
    ["compose.save", { isHtml: true }, { body: "", isPlainText: false }],
  ]) {
    const { handlers, browser } = setup();
    let details;
    browser.messages.saveMessage = browser.messages.sendMessage = async (value) => {
      details = value;
      return { messages: [] };
    };
    const result = await handlers.get(handler)(params);
    assert.equal(result.transport, "headless");
    for (const [key, value] of Object.entries(expected)) {
      assert.equal(details[key], value, `${handler}: ${key}`);
    }
  }
});

it("preserves nonempty headless text and absent window bodies", async () => {
  const { handlers, browser, current } = setup();
  let details;
  browser.messages.saveMessage = async (value) => {
    details = value;
    return { messages: [] };
  };
  await handlers.get("compose.save")({ body: "hello" });
  assert.equal(details.plainTextBody, "hello");
  assert.equal(details.isPlainText, true);
  assert.equal(Object.hasOwn(details, "body"), false);

  browser.compose.getComposeState = async () => ({});
  await handlers.get("compose.open")({ subject: "window" });
  for (const key of ["body", "plainTextBody", "isPlainText"]) {
    assert.equal(Object.hasOwn(current(), key), false);
  }
});
