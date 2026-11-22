const reviewResult = (() => {
  const $ = (id) => document.getElementById(id);
  const ui = {
    root: $("rl"),
    cv: $("rl-canvas"),
    count: $("rl-count"),
    draw: $("rl-draw"),
    undo: $("rl-undo"),
    clear: $("rl-clear"),
    pend: $("rl-pending"),
    note: $("rl-note"),
    start: $("rl-start"),
    end: $("rl-end"),
    sets: $("rl-sets"),
    sete: $("rl-sete"),
    add: $("rl-add"),
    status: $("rl-status"),
    list: $("rl-list"),
    send: $("rl-send"),
    store: $("rl-store"),
    share: $("rl-share"),
    open: $("rl-open"),
    sync: $("rl-sync"),
    label: $("rl-share-label"),
  };
  const cv = ui.cv,
    ctx = cv.getContext("2d"),
    wrap = document.createElement("div");
  wrap.className = "rl-wrap";
  player.before(wrap);
  wrap.append(player, cv);
  const ID = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/,
    JPEG = /^data:image\/jpeg;base64,[A-Za-z0-9+/]+=*$/;
  const time = (n) => typeof n === "number" && Number.isFinite(n) && n >= 0,
    unit = (n) => time(n) && n <= 1;
  // Same rules as the server's validMark; applied once to restored widget state and remote drafts.
  const valid = (m) =>
    !!m &&
    ID.test(m.id) &&
    time(m.start) &&
    time(m.frame) &&
    time(m.end) &&
    m.start <= m.frame &&
    m.frame <= m.end &&
    typeof m.note === "string" &&
    m.note.trim().length > 0 &&
    m.note.length <= 3000 &&
    typeof m.image === "string" &&
    m.image.length <= 85000 &&
    (m.image === "" || JPEG.test(m.image)) &&
    Array.isArray(m.shapes) &&
    m.shapes.length <= 60 &&
    m.shapes.every(
      (s) =>
        s &&
        ["rect", "circle", "arrow", "pen"].includes(s.kind) &&
        Array.isArray(s.points) &&
        s.points.length >= 2 &&
        s.points.length <= (s.kind === "pen" ? 512 : 2) &&
        s.points.every((p) => p && unit(p.x) && unit(p.y)),
    );
  const ids = (a) => (Array.isArray(a) ? a.filter((x) => typeof x === "string") : []);
  const ms = (t) => Math.round(t * 1000) / 1000,
    near = (f) => Math.abs(player.currentTime - f) < 0.05;
  const fmt = (t) => `${Math.floor(t / 60)}:${(t % 60).toFixed(2).padStart(5, "0")}`;
  const el = (t, s) => {
    const e = document.createElement(t);
    if (s != null) e.textContent = s;
    return e;
  };
  const say = (s) => {
    ui.status.textContent = s;
  };
  const outline = (ps) =>
    ps.length <= 16 ? ps : Array.from({ length: 16 }, (_, i) => ps[Math.round((i * (ps.length - 1)) / 15)]);
  const drafts = new Map();
  let cur,
    d,
    share,
    wstate,
    drag,
    viewing,
    sourceRevision = 0,
    busy = false,
    drawing = false,
    tainted = false,
    selected = new Set(),
    pending = { frame: 0, shapes: [] };
  const ws = () => wstate || window.openai?.widgetState || {};
  function draft(k) {
    if (!drafts.has(k)) {
      const s = ws().privateContent?.redline?.[k],
        sent = s?.sent && typeof s.sent === "object" ? s.sent : {};
      drafts.set(k, {
        marks: Array.isArray(s?.marks) ? s.marks.filter(valid).slice(0, 60) : [],
        sent: Object.fromEntries(Object.entries(sent).filter(([, v]) => v === "sent" || v === "unknown")),
        known: ids(s?.known),
        deleted: ids(s?.deleted),
        share: typeof s?.share === "string" ? s.share : "",
      });
    }
    return drafts.get(k);
  }
  // Widget state keeps text and shapes only; reference images stay in memory and are re-captured when needed.
  function save(extra) {
    const o = window.openai;
    if (!o?.setWidgetState) return;
    const s = ws(),
      redline = { ...s.privateContent?.redline };
    for (const [k, v] of drafts) redline[k] = { ...v, marks: v.marks.map((m) => ({ ...m, image: "" })) };
    wstate = { ...s, ...extra, privateContent: { ...s.privateContent, redline } };
    return Promise.resolve().then(() => o.setWidgetState(wstate)).catch(() => {
      if (extra) throw Error("参照画像をチャットに関連付けられませんでした。下書きはこの画面に残っています。");
      say("ウィジェット状態を保存できません。下書きはこの画面に残っています。");
    });
  }
  function area() {
    const vw = player.videoWidth,
      vh = player.videoHeight,
      w = cv.clientWidth,
      h = cv.clientHeight;
    if (player.hidden || !vw || !vh || !w || !h) return;
    const s = Math.min(w / vw, h / vh);
    return { x: (w - vw * s) / 2, y: (h - vh * s) / 2, w: vw * s, h: vh * s };
  }
  function strokes(c, shapes, b, color, lw) {
    c.save();
    c.strokeStyle = color;
    c.lineWidth = lw;
    c.lineCap = c.lineJoin = "round";
    for (const s of shapes) {
      const p = s.points.map((q) => [b.x + q.x * b.w, b.y + q.y * b.h]),
        a = p[0],
        z = p[p.length - 1];
      c.beginPath();
      if (s.kind === "rect") c.rect(a[0], a[1], z[0] - a[0], z[1] - a[1]);
      else if (s.kind === "circle")
        c.ellipse(
          (a[0] + z[0]) / 2,
          (a[1] + z[1]) / 2,
          Math.abs(z[0] - a[0]) / 2,
          Math.abs(z[1] - a[1]) / 2,
          0,
          0,
          Math.PI * 2,
        );
      else {
        p.forEach((q, i) => (i ? c.lineTo(q[0], q[1]) : c.moveTo(q[0], q[1])));
        if (s.kind === "arrow") {
          const g = Math.atan2(z[1] - a[1], z[0] - a[0]),
            k = lw * 5;
          for (const e of [-0.5, 0.5]) {
            c.moveTo(z[0] - k * Math.cos(g + e), z[1] - k * Math.sin(g + e));
            c.lineTo(z[0], z[1]);
          }
        }
      }
      c.stroke();
    }
    c.restore();
  }
  function paint() {
    const r = devicePixelRatio || 1,
      w = Math.round(cv.clientWidth * r),
      h = Math.round(cv.clientHeight * r);
    if (cv.width !== w || cv.height !== h) {
      cv.width = w;
      cv.height = h;
    }
    ctx.setTransform(r, 0, 0, r, 0, 0);
    ctx.clearRect(0, 0, cv.clientWidth, cv.clientHeight);
    const b = area();
    if (!b || !d || !ui.root.open) return;
    const t = player.currentTime;
    for (const m of d.marks)
      if (m.start - 0.02 <= t && t <= m.end + 0.02)
        strokes(ctx, m.shapes, b, m.id === viewing ? "#ffd400" : "rgba(255,212,0,.6)", 2);
    if (drag || near(pending.frame)) strokes(ctx, pending.shapes, b, "#ff3b30", 3);
  }
  const pend = () => {
    ui.pend.textContent = pending.shapes.length
      ? `未保存の描画 ${pending.shapes.length}個（${fmt(pending.frame)}）`
      : "";
  };
  function setDraw(on) {
    if (on && (player.readyState < 2 || !area())) {
      say("動画のフレームが表示されてから描画できます。");
      on = false;
    }
    drawing = on;
    cv.classList.toggle("on", on);
    ui.draw.setAttribute("aria-pressed", String(on));
    ui.draw.textContent = on ? "描画を終了" : "描画モード";
    if (on) {
      player.pause();
      if (pending.shapes.length && !near(pending.frame)) player.currentTime = pending.frame;
      say("一時停止中のフレームに描画できます。再生するには「描画を終了」を押してください。");
    }
    paint();
  }
  const pt = (e, b) => {
    const r = cv.getBoundingClientRect(),
      f = (v) => ms(Math.min(1, Math.max(0, v)));
    return { x: f((e.clientX - r.left - b.x) / b.w), y: f((e.clientY - r.top - b.y) / b.h) };
  };
  cv.addEventListener("pointerdown", (e) => {
    const b = area();
    if (!drawing || !b || e.button !== 0 || player.seeking || player.readyState < 2) return;
    if (pending.shapes.length >= 60) return say("1つの注釈に描ける図形は60個までです。");
    if (!pending.shapes.length) pending.frame = player.currentTime;
    const p = pt(e, b),
      kind = ui.root.querySelector("[name=rl-tool]:checked").value;
    drag = { kind, points: kind === "pen" ? [p] : [p, p] };
    pending.shapes.push(drag);
    cv.setPointerCapture(e.pointerId);
    paint();
  });
  cv.addEventListener("pointermove", (e) => {
    const b = area();
    if (!drag || !b) return;
    const p = pt(e, b),
      q = drag.points.at(-1);
    if (drag.kind !== "pen") drag.points[1] = p;
    else if (drag.points.length < 512 && Math.hypot(p.x - q.x, p.y - q.y) >= 0.004) drag.points.push(p);
    paint();
  });
  function finish() {
    if (!drag) return;
    const a = drag.points[0],
      z = drag.points.at(-1);
    if (drag.kind === "pen") {
      if (drag.points.length < 2) drag.points.push({ ...a });
    } else if (Math.hypot(z.x - a.x, z.y - a.y) < 0.01) pending.shapes.pop();
    drag = undefined;
    pend();
    paint();
  }
  cv.addEventListener("pointerup", finish);
  cv.addEventListener("pointercancel", finish);
  async function snapshot(m, revision) {
    if (player.hidden || !player.videoWidth) return "";
    if (!near(m.frame)) {
      player.pause();
      player.currentTime = m.frame;
      await Promise.race([
        new Promise((r) => player.addEventListener("seeked", r, { once: true })),
        new Promise((r) => setTimeout(r, 3000)),
      ]);
    }
    if (sourceRevision !== revision || player.seeking || player.readyState < 2 || !near(m.frame)) return "";
    for (const w of [640, 480, 320]) {
      const c = document.createElement("canvas"),
        s = Math.min(1, w / player.videoWidth),
        x = c.getContext("2d");
      c.width = Math.round(player.videoWidth * s);
      c.height = Math.round(player.videoHeight * s);
      x.drawImage(player, 0, 0, c.width, c.height);
      strokes(x, m.shapes, { x: 0, y: 0, w: c.width, h: c.height }, "#ff3b30", Math.max(2, c.width / 160));
      for (const q of [0.7, 0.5]) {
        let u;
        try {
          u = c.toDataURL("image/jpeg", q);
        } catch {
          tainted = true;
          return "";
        } // cross-origin frame: text and shapes only
        if (u.length < 85000 && JPEG.test(u)) return u;
      }
    }
    return "";
  }
  async function addMark() {
    if (drag) finish();
    const dd = d,
      revision = sourceRevision,
      note = ui.note.value.trim(),
      frame = ms(pending.shapes.length ? pending.frame : player.currentTime);
    const num = (i) => (i.value === "" ? frame : ms(Number(i.value))),
      start = num(ui.start),
      end = num(ui.end);
    if (!note) {
      ui.note.focus();
      return say("メモを入力してください。");
    }
    if (dd.marks.length >= 60) return say("注釈は60件までです。不要な注釈を削除してください。");
    if (!(start >= 0 && start <= frame && frame <= end))
      return say(`開始 ≤ 描画位置（${fmt(frame)}）≤ 終了 になるよう時間を設定してください。`);
    const m = { id: crypto.randomUUID(), start, end, frame, note, shapes: pending.shapes, image: "" };
    // Commit the annotation now. Capturing its optional image can take a seek.
    dd.marks.push(m);
    selected.add(m.id);
    save();
    pending = { frame: 0, shapes: [] };
    ui.note.value = ui.start.value = ui.end.value = "";
    pend();
    paint();
    setBusy(true);
    try {
      try {
        m.image = tainted ? "" : await snapshot(m, revision);
      } catch {
        m.image = "";
      }
      if (sourceRevision !== revision) return;
      paint();
      say("注釈を追加しました（未送信）。");
    } finally {
      setBusy(false);
    }
  }
  async function sendChat() {
    const dd = d,
      c = cur,
      revision = sourceRevision,
      o = window.openai,
      app = initialized && !(o?.sendFollowUpMessage && o.uploadFile && o.setWidgetState),
      list = dd.marks.filter((m) => selected.has(m.id));
    if (!list.length) return say("送信する注釈を選んでください。");
    if (!app && !o?.sendFollowUpMessage) return say("このホストではチャットに送信できません。");
    const withImages = app || !!(o.uploadFile && o.setWidgetState);
    let sending = false;
    setBusy(true);
    try {
      const imgs = [];
      for (const m of list)
        if (withImages && !m.image && !tainted) {
          const u = await snapshot(m, revision);
          // The video changed: never attach its frame or touch the new video's state.
          if (sourceRevision !== revision) return;
          m.image = u;
        }
      const rep = list.map((m) => ({
        id: m.id,
        start: m.start,
        end: m.end,
        frame: m.frame,
        note: m.note,
        reference_image: withImages && m.image ? imgs.push(m.image) : null,
        shapes: m.shapes.map((s) => ({ kind: s.kind, points: outline(s.points) })),
      }));
      const text =
        `動画「${c.info.filename}」（prompt_id: ${c.info.prompt_id}, index: ${c.info.index ?? 0}）へのレビュー注釈${list.length}件です。` +
        `時刻は秒、座標は映像の左上(0,0)〜右下(1,1)の正規化値です（図形は最大16点の概形）。` +
        (imgs.length ? "reference_imageは添付画像の番号で、赤線が描画箇所です。" : "参照画像はありません。") +
        `注釈ごとに修正内容を提案してください。\n${JSON.stringify(rep)}`;
      const fileIds = [];
      if (!app)
        for (const [i, u] of imgs.entries()) {
          const r = await o.uploadFile(
            new File([Uint8Array.from(atob(u.slice(23)), (ch) => ch.charCodeAt(0))], `redline-${i + 1}.jpg`, {
              type: "image/jpeg",
            }),
          );
          if (sourceRevision !== revision) return;
          if (typeof r?.fileId !== "string")
            throw Error("参照画像をアップロードできませんでした。何も送信していません。");
          fileIds.push(r.fileId);
        }
      // Marked before sending so an interrupted send is never retried automatically.
      for (const m of list) {
        dd.sent[m.id] = "unknown";
        selected.delete(m.id);
      }
      await save(app ? undefined : { modelContent: text, imageIds: fileIds });
      if (sourceRevision !== revision) {
        for (const m of list) delete dd.sent[m.id];
        if (cur?.key === c.key) for (const m of list) selected.add(m.id);
        save();
        return;
      }
      sending = true;
      if (app) {
        const r = await request("ui/message", {
          role: "user",
          content: [
            { type: "text", text },
            ...imgs.map((u) => ({ type: "image", data: u.slice(23), mimeType: "image/jpeg" })),
          ],
        });
        if (r?.isError) {
          for (const m of list) {
            delete dd.sent[m.id];
            if (cur?.key === c.key) selected.add(m.id);
          }
          save();
          if (sourceRevision === revision) say("チャットへの送信が拒否されました。何も送信していません。");
          return;
        }
      } else await o.sendFollowUpMessage({ prompt: text });
      for (const m of list) dd.sent[m.id] = "sent";
      save();
      if (sourceRevision === revision) say(`${list.length}件の注釈をチャットに送信しました。`);
    } catch (e) {
      if (!sending) {
        for (const m of list) {
          delete dd.sent[m.id];
          if (cur?.key === c.key) selected.add(m.id);
        }
        save();
      }
      if (sourceRevision !== revision) return;
      say(
        sending
          ? "送信できたか確認できません。チャット欄を確認し、届いていない注釈だけ選び直して送信してください。"
          : e.name === "Error"
            ? e.message
            : "送信の準備に失敗しました。",
      );
    } finally {
      setBusy(false);
    }
  }
  async function openShare() {
    const c = cur,
      revision = sourceRevision,
      args = {
        prompt_id: c.info.prompt_id,
        index: c.info.index ?? 0,
        ...(c.version ? { version: c.version } : {}),
      };
    setBusy(true);
    try {
      const r = await (serverTools || !window.openai?.callTool
        ? request("tools/call", { name: "open_split_review_share", arguments: args })
        : window.openai.callTool("open_split_review_share", args));
      if (r?.isError)
        throw Error(r.content?.find((x) => x.type === "text")?.text || "共有下書きを開けませんでした。");
      const s = r?._meta?.["pmd/review"],
        u = URL.canParse(s?.base) ? new URL(s.base) : null;
      if (
        !u ||
        u.protocol !== "https:" ||
        u.username ||
        u.password ||
        typeof s.label !== "string" ||
        typeof s.version !== "string" ||
        s.ref?.prompt_id !== args.prompt_id ||
        !Number.isSafeInteger(s.ref.index) ||
        (Number.isSafeInteger(c.info.output_index) && s.ref.index !== c.info.output_index)
      )
        throw Error("共有下書きの応答を確認できません。");
      if (c.version && s.version !== c.version)
        throw Error("動画の版が変わりました。動画を再取得してから開いてください。");
      if (sourceRevision !== revision) return;
      share = { base: u.href, version: s.version, label: s.label };
      if (d.share !== s.version) {
        Object.assign(d, { known: [], deleted: [], share: s.version });
        save();
      }
      say("共有下書きを開きました。「共有下書きと同期」で取得と保存を行います。");
    } catch (e) {
      if (sourceRevision === revision) say(e.message || "共有下書きを開けませんでした。");
    } finally {
      setBusy(false);
    }
  }
  async function sync() {
    const dd = d,
      s = share,
      revision = sourceRevision;
    setBusy(true);
    try {
      const res = await fetch(s.base, {
        credentials: "omit",
        redirect: "error",
        cache: "no-store",
        signal: AbortSignal.timeout(10000),
      });
      if (!res.ok)
        throw Error(`共有下書きを読み込めません（HTTP ${res.status}）。ローカルの注釈は残っています。`);
      const remote = await res.json();
      if (sourceRevision !== revision) return;
      if (
        !Number.isSafeInteger(remote?.revision) ||
        !Array.isArray(remote.marks) ||
        remote.marks.length > 60 ||
        !remote.marks.every(valid)
      )
        throw Error("共有下書きの形式が正しくありません。ローカルの注釈は残っています。");
      // Keep remote marks except ones deleted here; add marks created here; marks synced earlier but gone remotely stay deleted.
      const there = new Set(remote.marks.map((m) => m.id)),
        known = new Set(dd.known),
        gone = new Set(dd.deleted);
      const local = dd.marks.filter((m) => !there.has(m.id) && !known.has(m.id)),
        marks = [...remote.marks.filter((m) => !gone.has(m.id)), ...local];
      if (marks.length > 60)
        throw Error("統合すると注釈が60件を超えます。不要な注釈を削除してから同期してください。");
      if (local.length || marks.length !== remote.marks.length) {
        const body = JSON.stringify({ revision: remote.revision, marks });
        if (body.length > 1600000)
          throw Error("参照画像の合計が大きすぎます。注釈を減らしてから同期してください。");
        const post = await fetch(s.base, {
          method: "POST",
          credentials: "omit",
          redirect: "error",
          cache: "no-store",
          signal: AbortSignal.timeout(10000),
          headers: { "content-type": "application/json" },
          body,
        });
        if (post.status === 409)
          throw Error(
            "他の編集者が先に保存しました。ローカルの注釈は残っています。もう一度「共有下書きと同期」を押すと最新版と統合します。",
          );
        if (!post.ok)
          throw Error(`共有下書きを保存できません（HTTP ${post.status}）。ローカルの注釈は残っています。`);
        if (!Number.isSafeInteger((await post.json())?.revision))
          throw Error("保存結果を確認できません。もう一度同期してください。");
      }
      if (sourceRevision !== revision) return;
      Object.assign(dd, { marks, known: marks.map((m) => m.id), deleted: [] });
      save();
      if (viewing && !marks.some((m) => m.id === viewing)) viewing = undefined;
      say(`共有下書きと同期しました（${marks.length}件）。`);
    } catch (e) {
      if (sourceRevision !== revision) return;
      say(
        e.name === "Error"
          ? e.message
          : "共有サービスに接続できません。ローカルの注釈とチャット送信はそのまま使えます。",
      );
    } finally {
      setBusy(false);
      paint();
    }
  }
  function setBusy(b) {
    busy = b;
    if (cur) renderUI();
  }
  function buttons() {
    for (const b of [ui.add, ui.open, ui.sync]) b.disabled = busy;
    ui.send.disabled = busy || !d.marks.some((m) => selected.has(m.id));
  }
  function renderUI() {
    const known = new Set(d.known),
      synced = !!share || d.known.length > 0;
    ui.list.replaceChildren(
      ...d.marks.map((m, i) => {
        const li = el("li"),
          row = el("div"),
          lab = el("label"),
          cb = el("input"),
          view = el("button", "表示"),
          del = el("button", "削除"),
          note = el("div", m.note);
        const tag = [
          { sent: "送信済み", unknown: "送信状態不明" }[d.sent[m.id]] || "未送信",
          synced ? (known.has(m.id) ? "共有済み" : "未同期") : "",
        ]
          .filter(Boolean)
          .join("・");
        row.className = "rl-row";
        note.className = "rl-note";
        cb.type = "checkbox";
        cb.checked = selected.has(m.id);
        view.type = del.type = "button";
        cb.setAttribute("aria-label", `注釈${i + 1}を送信対象にする`);
        view.setAttribute("aria-label", `注釈${i + 1}のフレームを表示`);
        del.setAttribute("aria-label", `注釈${i + 1}を削除`);
        del.disabled = busy;
        if (m.id === viewing) li.setAttribute("aria-current", "true");
        cb.onchange = () => {
          cb.checked ? selected.add(m.id) : selected.delete(m.id);
          buttons();
        };
        view.onclick = () => {
          setDraw(false);
          player.pause();
          player.currentTime = m.frame;
          viewing = m.id;
          renderUI();
          paint();
        };
        del.onclick = () => {
          d.marks = d.marks.filter((x) => x !== m);
          if (known.has(m.id)) d.deleted.push(m.id);
          selected.delete(m.id);
          if (viewing === m.id) viewing = undefined;
          save();
          renderUI();
          paint();
          ui.list.focus();
          say(`注釈${i + 1}を削除しました。`);
        };
        lab.append(cb, ` #${i + 1} ${fmt(m.start)}–${fmt(m.end)}`);
        row.append(lab, el("small", `${tag}${m.shapes.length ? `・図形${m.shapes.length}` : ""}`), view, del);
        li.append(row, note);
        return li;
      }),
    );
    const unsynced = d.marks.filter((m) => !known.has(m.id)).length + d.deleted.length;
    ui.count.textContent = d.marks.length ? `（${d.marks.length}件）` : "";
    ui.share.hidden = !cur.sharing;
    ui.sync.hidden = !share;
    ui.label.textContent = share
      ? `共有: ${share.label}／未同期 ${unsynced}件`
      : "共有下書きは未接続です（外部と通信していません）。";
    ui.store.textContent =
      (window.openai?.setWidgetState
        ? "下書きはこのウィジェットに保存されます（参照画像は送信時に再作成）。"
        : "下書きはこの画面を開いている間だけ保持されます。") +
      (tainted ? " この動画はフレーム画像を取り出せないため、テキストと図形のみ送信します。" : "");
    buttons();
  }
  ui.draw.onclick = () => setDraw(!drawing);
  ui.undo.onclick = () => {
    pending.shapes.pop();
    pend();
    paint();
  };
  ui.clear.onclick = () => {
    pending.shapes = [];
    pend();
    paint();
  };
  ui.sets.onclick = () => {
    ui.start.value = ms(player.currentTime);
  };
  ui.sete.onclick = () => {
    ui.end.value = ms(player.currentTime);
  };
  ui.add.onclick = addMark;
  ui.send.onclick = sendChat;
  ui.open.onclick = openShare;
  ui.sync.onclick = sync;
  ui.root.addEventListener("toggle", () => {
    if (!ui.root.open) setDraw(false);
    paint();
  });
  for (const t of ["timeupdate", "seeked", "loadeddata"]) player.addEventListener(t, paint);
  player.addEventListener("play", () => {
    if (drawing) setDraw(false);
  });
  player.addEventListener("emptied", () => setDraw(false));
  addEventListener("keydown", (e) => {
    if (e.key === "Escape" && drawing) setDraw(false);
  });
  new ResizeObserver(paint).observe(wrap);
  return function (info, meta) {
    if (!info?.prompt_id) {
      sourceRevision++;
      cur = undefined;
      ui.root.hidden = true;
      setDraw(false);
      return;
    }
    const r = meta?.["split/review"],
      prev = cur?.key;
    const k = JSON.stringify([
      info.prompt_id,
      info.index ?? 0,
      info.url,
      info.etag ?? r?.version ?? info.size,
    ]);
    cur = {
      key: k,
      info,
      sharing: r?.sharing === true,
      version: typeof r?.version === "string" ? r.version : "",
    };
    if (prev !== k) {
      sourceRevision++;
      share = viewing = drag = undefined;
      tainted = false;
      pending = { frame: 0, shapes: [] };
      d = draft(k);
      ui.note.value = ui.start.value = ui.end.value = "";
      selected = new Set(d.marks.filter((m) => !d.sent[m.id]).map((m) => m.id));
      setDraw(false);
      pend();
      say("");
    }
    if (share && (!cur.sharing || (cur.version && cur.version !== share.version))) share = undefined;
    ui.root.hidden = false;
    renderUI();
    paint();
  };
})();
