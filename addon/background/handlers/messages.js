/* Message handlers — the reference implementation for a handler module.
 *
 * Shape to copy:
 *   - one `tbxRegistry.define("<module>.<action>", async (params, ctx) => …)` per method
 *   - validate with `tbxUtil.need`, fail with `tbxError.usage` so the model can retry
 *   - return plain JSON; the Python side owns presentation
 *   - use `tbxUtil.mapLimited` for bulk work so a 500-message operation neither
 *     stalls the UI nor opens 500 IMAP requests at once
 *
 * Pagination note: Thunderbird hands out a `MessageList` — one page of messages
 * plus an `id` that continues *after* that page. Page size is the user's own
 * preference (`extensions.webextensions.messagesPerPage`), so a `limit` usually
 * runs out mid-page, and returning the list id there would skip everything
 * between the two. So a cursor is one of:
 *   - a raw Thunderbird list id, when the page ended exactly on the limit and
 *     nothing was left over;
 *   - `tbx:<load>:<n>`, ours, naming the tail we parked (with the id that
 *     continues after it) because the limit stopped us mid-page. `<load>` names
 *     this load of the script, so a cursor from before a background restart is
 *     refused rather than mistaken for one of ours.
 * Only the last 32 part-read pages are kept; the rest are dropped, and their
 * Thunderbird lists aborted, so an abandoned walk costs nothing.
 */

{
  const DEFAULT_LIMIT = 25;
  const BULK_CONCURRENCY = 8;
  const PARKED_PAGES = 32;
  const CURSOR_PREFIX = "tbx:";

  /** Flatten a MessageHeader into the shape _common.message_summary expects. */
  function header(message) {
    if (!message) {
      return null;
    }
    return {
      id: message.id,
      headerMessageId: message.headerMessageId,
      subject: message.subject,
      author: message.author,
      recipients: message.recipients || [],
      ccList: message.ccList || [],
      bccList: message.bccList || [],
      date: message.date ? new Date(message.date).toISOString() : null,
      read: message.read,
      new: message.new,
      flagged: message.flagged,
      junk: message.junk,
      junkScore: message.junkScore,
      tags: message.tags || [],
      size: message.size,
      priority: message.priority,
      external: message.external,
      folderId: message.folder ? message.folder.id : undefined,
      folderPath: message.folder ? message.folder.path : undefined,
    };
  }

  function locator(message) {
    return { id: message.id, headerMessageId: message.headerMessageId,
      folderId: message.folder ? message.folder.id : undefined };
  }

  async function snapshot(ids) {
    const results = await tbxUtil.mapLimited(ids, BULK_CONCURRENCY, (id) => browser.messages.get(id));
    return tbxUtil.partition(results).values.map(locator);
  }

  async function landed(event, wanted, run) {
    // shortcut: some IMAP servers omit landing events; look up by Message-ID for automatic undo.
    if (!event) return { result: await run(), messages: [] };
    const messages = [];
    const seen = new Set();
    const key = (message) => JSON.stringify([message.id, message.folderId]);
    const requested = new Set(wanted.map(key));
    let finish;
    const complete = new Promise((resolve) => { finish = resolve; });
    const listener = (originals, moved) => {
      Promise.resolve().then(async () => {
        let page = Array.isArray(moved) ? { messages: moved } : moved;
        let offset = 0;
        while (page) {
          for (const [index, message] of (page.messages || []).entries()) {
            const original = originals && originals[offset + index];
            if (original && requested.has(key(locator(original)))) {
              messages.push(locator(message));
              seen.add(key(locator(original)));
            }
          }
          offset += (page.messages || []).length;
          page = page.id ? await browser.messages.continueList(page.id) : null;
        }
        if ([...requested].every((id) => seen.has(id))) finish();
      }).catch(() => {}); // Landing information is best effort.
    };
    event.addListener(listener);
    let timer;
    try {
      const result = await run();
      if (requested.size && ![...requested].every((id) => seen.has(id))) {
        await Promise.race([complete, new Promise((resolve) => {
          timer = setTimeout(resolve, 1000);
        })]);
      }
      return { result, messages };
    } finally {
      clearTimeout(timer);
      event.removeListener(listener);
    }
  }

  /* Pages we stopped part-way through, by the cursor we minted for them. Bounded,
   * because a caller that walks away from a search must not pin its messages —
   * and its Thunderbird list — for the rest of the session. */
  const parked = new Map();
  let parkSequence = 0;

  /* Every cursor we mint names this load of the script. The map above is empty
   * again after a background restart, and without this a cursor from before it
   * would quietly claim the new load's first parked page instead of being
   * refused. */
  const LOAD_ID = `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 6)}`;

  /** Let go of a Thunderbird list nobody can reach any more. */
  function abandonList(listId) {
    if (listId) {
      // Best effort: the list would expire on its own, this is just sooner.
      Promise.resolve(browser.messages.abortList(listId)).catch(() => {});
    }
  }

  /** Hold `rest` for the next call, and return the cursor that claims it back. */
  function park(listId, rest, next = null) {
    if (parked.size >= PARKED_PAGES) {
      const [oldest] = parked.keys(); // a Map iterates in insertion order
      abandonList(parked.get(oldest).listId);
      parked.delete(oldest);
    }
    parkSequence += 1;
    const cursor = `${CURSOR_PREFIX}${LOAD_ID}:${parkSequence}`;
    parked.set(cursor, { listId, rest, next });
    return cursor;
  }

  /**
   * Take back a page we parked, or say why the cursor is worthless.
   *
   * Anything carrying our prefix is ours to answer for: a cursor from another
   * load carries another load id, so it is simply not in the map, and it is
   * refused here rather than handed to Thunderbird as if it were a list id.
   */
  function unpark(cursor) {
    const held = parked.get(cursor);
    if (!held) {
      throw tbxError.usage(
        `that cursor is no longer valid: it was evicted (only ${PARKED_PAGES} part-read ` +
          "pages are kept) or belongs to an earlier Thunderbird session — re-run the " +
          "search without a cursor"
      );
    }
    parked.delete(cursor);
    return held;
  }

  /**
   * Collect up to `limit` headers, starting a walk or resuming one.
   *
   * @param {string|null} cursor  ours (`tbx:<load>:<n>`) or a raw Thunderbird list id.
   * @param {Function} start  opens the list, when there is no cursor to resume.
   */
  async function collectPage(cursor, start, limit) {
    const messages = [];
    let pending = []; // fetched but not yet returned, in order
    let listId = null; // the Thunderbird list that continues after `pending`
    let next = null;

    const absorb = (page) => {
      pending = page && page.messages ? [...page.messages] : [];
      listId = (page && page.id) || null;
      next = (page && page.next) || null;
    };

    if (!cursor) {
      absorb(await start());
    } else if (cursor.startsWith(CURSOR_PREFIX)) {
      const held = unpark(cursor);
      pending = held.rest;
      listId = held.listId;
      next = held.next;
    } else {
      try {
        absorb(await browser.messages.continueList(cursor));
      } catch (ex) {
        throw tbxError.usage(
          "that cursor has expired (Thunderbird drops message lists when it restarts " +
            "or after a timeout) — re-run the search without a cursor"
        );
      }
    }

    for (;;) {
      while (pending.length) {
        if (messages.length >= limit) {
          // Stopped mid-page: park the rest, because the list id continues after
          // the whole page and would skip every message still sitting here.
          return { messages, cursor: park(listId, pending, next) };
        }
        messages.push(header(pending.shift()));
      }
      if (messages.length >= limit) {
        return { messages, cursor: listId || (next ? park(null, [], next) : null) };
      }
      if (!pending.length && !listId && next) {
        const continuation = await next();
        absorb(continuation);
        continue;
      }
      if (messages.length >= limit || !listId) {
        // Nothing left over, so Thunderbird's own id is the cursor; when the list
        // is spent there is no id and the walk is over.
        return { messages, cursor: listId || (next ? park(null, [], next) : null) };
      }
      absorb(await browser.messages.continueList(listId));
      if (!pending.length) {
        // An empty page ends this call; its id, if any, can still be resumed.
        return { messages, cursor: listId };
      }
    }
  }

  // --------------------------------------------------------------------- query

  /**
   * Run a query and return its first page.
   *
   * Never ask for `returnMessageListId`: that flag makes Thunderbird answer with
   * the list id itself, a bare string with no messages on it, which is how every
   * search used to come back empty. A build that answers with one anyway is one
   * `continueList` away from the page we wanted.
   */
  async function startQuery(query) {
    const dates = { ...query };
    if (dates.fromDate) dates.fromDate = new Date(dates.fromDate);
    if (dates.toDate) dates.toDate = new Date(dates.toDate);
    const first = await browser.messages.query(dates);
    return typeof first === "string" ? browser.messages.continueList(first) : first;
  }

  const SORTED_PAGE_SIZE = 500;

  async function collectAll(query) {
    let page = await startQuery(Object.assign({}, query, { messagesPerPage: SORTED_PAGE_SIZE }));
    const all = [...((page && page.messages) || [])];
    while (page && page.id) {
      page = await browser.messages.continueList(page.id);
      all.push(...((page && page.messages) || []));
    }
    return all;
  }

  const time = (message) => (message.date ? new Date(message.date).getTime() : 0);
  const WINDOW_DAYS = [1, 7, 30, 365, null];

  async function sortedQuery(query, compare, limit, newestFirst = false, anchor = Date.now(),
    bounds = { lower: query.fromDate ? new Date(query.fromDate).getTime() : -Infinity,
      upper: query.toDate ? new Date(query.toDate).getTime() : Infinity }, ceiling = Infinity) {
    if (!newestFirst) {
      // ponytail: oldest and non-date sorts hold every match in memory; add
      // windowing for oldest if it ever times out.
      const messages = await collectAll(query);
      messages.sort(compare);
      return { messages };
    }
    const lower = query.fromDate ? new Date(query.fromDate).getTime() : -Infinity;
    const upper = query.toDate ? new Date(query.toDate).getTime() : Infinity;
    for (const days of WINDOW_DAYS) {
      const boundary = days === null ? lower : Math.max(lower, anchor - days * 86400000);
      if (boundary > upper) continue;
      const windowQuery = { ...query };
      if (Number.isFinite(boundary)) windowQuery.fromDate = new Date(boundary - 1).toISOString();
      const messages = (await collectAll(windowQuery)).filter((message) =>
        time(message) >= boundary && time(message) <= Math.min(upper, ceiling) &&
        time(message) > bounds.lower && time(message) < bounds.upper
      );
      if (messages.length >= limit || days === null || boundary === lower) {
        messages.sort(compare);
        const next = Number.isFinite(boundary) && boundary > lower
          ? () => sortedQuery({ ...query, toDate: new Date(Math.min(upper, boundary + 1)).toISOString() },
              compare, limit, true, boundary - 1, bounds, boundary - 1)
          : null;
        return { messages, next };
      }
    }
    return { messages: [] };
  }

  /** A folder or account id, or a list of them, as a list — or null for neither. */
  function idList(value) {
    if (!value) {
      return null;
    }
    return Array.isArray(value) ? [...value] : [value];
  }

  /**
   * What the search was aimed at, echoed back so the answer says what it covered
   * without anyone having to enumerate folders to find out.
   */
  function queryScope(query) {
    const folderIds = idList(query.folderId);
    const accountIds = idList(query.accountId);
    return {
      folderIds,
      accountIds,
      // There is nothing to recurse into unless a folder or account was named.
      includeSubFolders:
        typeof query.includeSubFolders === "boolean"
          ? query.includeSubFolders
          : Boolean(folderIds || accountIds),
    };
  }

  tbxRegistry.define("messages.query", async (params) => {
    const limit = params.limit || DEFAULT_LIMIT;
    const query = Object.assign({}, params.query || {});
    // Any positive page size is legal; asking for exactly what the caller wants
    // keeps the common case to one round trip. Thunderbird may still cut a page
    // short (autoPaginationTimeout), so the walk below loops regardless.
    query.messagesPerPage = limit;
    const sort = params.sort || "newest";
    const start =
      sort === "none" ? () => startQuery(query) : () => sortedQuery(
        query, (a, b) => sort === "newest" ? time(b) - time(a) : time(a) - time(b), limit,
        sort === "newest"
      );

    const paged = await collectPage(params.cursor, start, limit);
    const result = { messages: paged.messages, cursor: paged.cursor };
    if (!params.cursor) {
      // Only on the first page: a continuation is by definition the same search.
      result.scope = queryScope(query);
    }
    if (query.fullText && browser.tbx) {
      // Worth saying: fullText only sees what the global indexer has processed.
      const indexed = await browser.tbx.globalIndexEnabled().catch(() => null);
      if (indexed === false) {
        result.indexNote =
          "Thunderbird's global index is disabled, so full_text matched nothing. " +
          "Use subject/author/body filters, or enable indexing in Settings > General.";
      }
    }
    return result;
  });

  tbxRegistry.define("messages.list", async (params) => {
    const folderId = params.folderId;
    const specialUse = params.specialUse;
    if (Boolean(folderId) === Boolean(specialUse)) {
      throw tbxError.usage("give exactly one of folderId or specialUse");
    }
    if (folderId && typeof folderId !== "string") throw tbxError.usage("folderId must be a string");
    const types = ["inbox", "drafts", "sent", "trash", "templates", "archives", "junk"];
    if (specialUse && !types.includes(specialUse)) {
      throw tbxError.usage(`specialUse must be one of ${types.join(", ")}`);
    }
    const sortTypes = ["date", "subject", "author", "size", "read", "flagged"];
    const sortType = params.sortType || "date";
    if (!sortTypes.includes(sortType)) {
      throw tbxError.usage(`sortType must be one of ${sortTypes.join(", ")}`);
    }
    const sortOrder = params.sortOrder || "descending";
    if (sortOrder !== "descending" && sortOrder !== "ascending") {
      throw tbxError.usage("sortOrder must be descending or ascending");
    }
    const limit = params.limit || DEFAULT_LIMIT;
    let type = specialUse;
    if (folderId && !params.cursor) {
      const folder = await browser.folders.get(folderId);
      if (folder && folder.isUnified) type = folder.specialUse?.[0];
    }
    const folderIds = type
      ? (await browser.folders.query({ specialUse: [type], isUnified: false })).map((folder) => folder.id)
      : [folderId];
    const descending = sortOrder === "descending";
    const compare = (a, b) => {
      const value = (m) => sortType === "date" ? time(m)
        : sortType === "subject" || sortType === "author" ? String(m[sortType] || "")
        : sortType === "read" || sortType === "flagged" ? Number(Boolean(m[sortType]))
        : Number(m.size || 0);
      const left = value(a), right = value(b);
      const order = typeof left === "string" ? left.localeCompare(right) : left - right;
      return (descending ? -order : order) || time(b) - time(a);
    };
    const query = { folderId: folderIds.length === 1 ? folderIds[0] : folderIds,
      includeSubFolders: false };
    const paged = await collectPage(
      params.cursor,
      () => folderIds.length ? sortedQuery(query, compare, limit, sortType === "date" && descending)
        : { messages: [] },
      limit
    );
    return { messages: paged.messages, cursor: paged.cursor,
      ...(!params.cursor ? { folderId, folderIds } : {}) };
  });

  // ---------------------------------------------------------------------- read

  /** Depth-first walk of the MIME tree, collecting text bodies and attachments. */
  function walkParts(part, out) {
    if (!part) {
      return;
    }
    const contentType = (part.contentType || "").toLowerCase();
    if (part.body && (contentType.startsWith("text/") || !contentType)) {
      out.texts.push({ contentType: contentType || "text/plain", body: part.body });
    }
    if (part.name && part.partName) {
      out.attachments.push({
        partName: part.partName,
        name: part.name,
        contentType: part.contentType,
        size: part.size,
      });
    }
    for (const child of part.parts || []) {
      walkParts(child, out);
    }
  }

  function pickBody(texts, preferHtml) {
    const plain = texts.find((t) => t.contentType.startsWith("text/plain"));
    const html = texts.find((t) => t.contentType.startsWith("text/html"));
    if (preferHtml && html) {
      return { body: html.body, isHtml: true };
    }
    if (plain) {
      return { body: plain.body, isHtml: false };
    }
    if (html) {
      return { body: html.body, isHtml: true };
    }
    return { body: null, isHtml: false };
  }

  async function readOne(messageId, detail, decrypt) {
    const meta = await browser.messages.get(messageId);
    if (detail === "summary") {
      return { header: header(meta), attachments: [] };
    }
    const full = await browser.messages.getFull(messageId, {
      decrypt: decrypt !== false,
      decodeContent: true,
      decodeHeaders: true,
    });
    const collected = { texts: [], attachments: [] };
    walkParts(full, collected);
    const chosen = pickBody(collected.texts, false);
    const payload = {
      header: header(meta),
      body: chosen.body,
      bodyIsHtml: chosen.isHtml,
      attachments: collected.attachments,
      decryptionStatus: full.decryptionStatus,
    };
    if (detail === "full") {
      payload.headers = full.headers || {};
      payload.parts = collected.texts.map((t) => ({
        contentType: t.contentType,
        bytes: t.body ? t.body.length : 0,
      }));
      // An HTML alternative is genuinely useful at `full` detail.
      const html = collected.texts.find((t) => t.contentType.startsWith("text/html"));
      if (html && !chosen.isHtml) {
        payload.htmlBody = html.body;
      }
    }
    return payload;
  }

  tbxRegistry.define("messages.read", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    return readOne(messageId, params.detail || "text", params.decrypt);
  });

  tbxRegistry.define("messages.readMany", async (params, ctx) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const detail = params.detail || "summary";
    let done = 0;
    const results = await tbxUtil.mapLimited(ids, BULK_CONCURRENCY, async (id) => {
      const value = await readOne(id, detail, params.decrypt);
      done += 1;
      ctx.progress(done, ids.length, "reading messages");
      return value;
    });
    const { values, failures } = tbxUtil.partition(results);
    return { messages: values, failures };
  });

  tbxRegistry.define("messages.raw", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    let source;
    try {
      source = await browser.messages.getRaw(messageId, {
        decrypt: Boolean(params.decrypt),
        data_format: "BinaryString",
      });
    } catch (ex) {
      throw tbxError.unsupported(
        "the raw source is not available — on IMAP the message must be stored " +
          "offline first (right-click the folder > Properties > Synchronisation), " +
          `underlying error: ${ex.message || ex}`
      );
    }
    return { source, bytes: source ? source.length : 0 };
  });

  // --------------------------------------------------------------- attachments

  tbxRegistry.define("messages.listAttachments", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    const attachments = await browser.messages.listAttachments(messageId);
    return {
      attachments: (attachments || []).map((a) => ({
        partName: a.partName,
        name: a.name,
        contentType: a.contentType,
        size: a.size,
      })),
    };
  });

  tbxRegistry.define("messages.saveAttachment", async (params) => {
    const messageId = tbxUtil.need(params, "messageId", "int");
    const partName = tbxUtil.need(params, "partName", "string");
    const directory = tbxUtil.need(params, "directory", "string");
    const file = await browser.messages.getAttachmentFile(messageId, partName);
    if (!file) {
      throw tbxError.usage(`no attachment ${partName} on message ${messageId}`);
    }
    const buffer = await file.arrayBuffer();
    if (!browser.tbx) {
      throw tbxError.unsupported(
        "saving files needs the privileged half of the add-on, which did not load"
      );
    }
    let written;
    try {
      written = await browser.tbx.writeFile({
        directory,
        filename: params.filename || file.name,
        base64: tbxBase64.fromArrayBuffer(buffer),
        overwrite: Boolean(params.overwrite),
      });
    } catch (ex) {
      // "already exists — pass overwrite=true" is exactly the kind of refusal the
      // caller can act on, and it only survives the hop packed into a message.
      throw tbxError.fromWire(ex) || ex;
    }
    return { path: written.path, bytes: written.bytes, name: written.name };
  });

  // ------------------------------------------------------------------ mutation

  tbxRegistry.define("messages.mark", async (params, ctx) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const addTags = params.addTags || [];
    const removeTags = params.removeTags || [];
    let done = 0;
    const results = await tbxUtil.mapLimited(ids, BULK_CONCURRENCY, async (id) => {
      const current = await browser.messages.get(id);
      const properties = {};
      if (params.read !== null && params.read !== undefined) {
        properties.read = params.read;
      }
      if (params.flagged !== null && params.flagged !== undefined) {
        properties.flagged = params.flagged;
      }
      if (params.junk !== null && params.junk !== undefined) {
        properties.junk = params.junk;
      }
      if (addTags.length || removeTags.length) {
        const next = new Set(current.tags || []);
        for (const tag of addTags) {
          next.add(tag);
        }
        for (const tag of removeTags) {
          next.delete(tag);
        }
        properties.tags = [...next];
      }
      await browser.messages.update(id, properties);
      done += 1;
      ctx.progress(done, ids.length, "updating messages");
      return { ...locator(current), read: current.read, flagged: current.flagged,
        junk: current.junk, tags: current.tags || [] };
    });
    const { values, failures } = tbxUtil.partition(results);
    return { updated: values.length, failures, previous: values };
  });

  tbxRegistry.define("messages.move", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const destination = tbxUtil.need(params, "destinationFolderId", "string");
    const previous = await snapshot(ids);
    const sourceFolderIds = [
      ...new Set(previous.map((message) => message.folderId).filter(Boolean)),
    ];
    const { messages } = await landed(browser.messages.onMoved, previous,
      () => browser.messages.move(ids, destination));
    return { moved: ids.length, sourceFolderIds, previous, landed: messages };
  });

  tbxRegistry.define("messages.copy", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const destination = tbxUtil.need(params, "destinationFolderId", "string");
    const previous = await snapshot(ids);
    const { messages } = await landed(browser.messages.onCopied, previous,
      () => browser.messages.copy(ids, destination));
    return { copied: ids.length, previous, landed: messages };
  });

  tbxRegistry.define("messages.archive", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const previous = await snapshot(ids);
    const { messages } = await landed(browser.messages.onMoved, previous,
      () => browser.messages.archive(ids));
    return { archived: ids.length, previous, landed: messages };
  });

  tbxRegistry.define("messages.delete", async (params) => {
    const ids = tbxUtil.need(params, "messageIds", "array");
    const permanent = Boolean(params.permanent);
    await browser.messages.delete(ids, { deletePermanently: permanent });
    return { deleted: ids.length, permanent };
  });

  tbxRegistry.define("messages.import", async (params) => {
    const folderId = tbxUtil.need(params, "folderId", "string");
    const base64 = tbxUtil.need(params, "base64", "string");
    const blob = new Blob([tbxBase64.toUint8Array(base64)], { type: "message/rfc822" });
    const file = new File([blob], params.filename || "imported.eml", {
      type: "message/rfc822",
    });
    const message = await browser.messages.import(file, folderId, {
      read: params.read !== false,
      new: false,
      tags: params.tags || [],
    });
    return { message: header(message) };
  });
}
