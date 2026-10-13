/* The viewer is one HTML page, so its behaviour lives in browser JavaScript
 * that pytest cannot reach — which is how a broken row click and a stuck
 * "Converting…" both shipped. This loads the real page script against a stub
 * DOM and exercises the parts with logic in them.
 *
 * Run: node tests/page.test.js   (pytest runs it too, via test_page.py)
 */
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");

// ---- the page itself ---------------------------------------------------------
const page = readFileSync(
  join(__dirname, "..", "src", "transcript_viewer", "page.html"),
  "utf8",
);
const script = page.slice(
  page.indexOf("<script>") + 8,
  page.lastIndexOf("</script>"),
);

// ---- just enough DOM for the script to load ----------------------------------
const el = () => ({
  innerHTML: "",
  textContent: "",
  value: "",
  style: {},
  classList: { toggle() {}, add() {}, remove() {} },
  setAttribute() {},
  getAttribute: () => "paper",
  focus() {},
  setSelectionRange() {},
  querySelectorAll: () => [],
});
global.document = {
  documentElement: el(),
  body: el(),
  addEventListener() {},
  createElement: el,
  getElementById: () => null,
};
global.addEventListener = () => {};
global.localStorage = { getItem: () => null, setItem() {} };
global.fetch = () =>
  Promise.resolve({ ok: true, json: () => Promise.resolve({}) });
for (const id of [
  "q",
  "list",
  "main",
  "count",
  "toggle",
  "picker",
  "drop",
  "themes",
  "tagbar",
  "theme",
  "crumb",
  "modal",
  "urlmodal",
])
  global[id] = el();

/* The page script boots on every eval — it fetches the index and draws. Tests
   stub globals inside their own body, so reset the ones the boot touches first,
   or one test's stub crashes the next test's boot. */
const SAFE_FETCH = () =>
  Promise.resolve({ ok: true, json: () => Promise.resolve({}) });

/* Lookup by id, as the page expects. Reset with fetch on every run: a test that
   stubs it to watch one element used to leave the next test's render() holding
   an object with no focus(), which fails somewhere else entirely. */
const SAFE_BY_ID = () => null;

/* The page reads and writes an address so a transcript can be linked to. The
   stub keeps one, so tests can watch it without a browser. */
global.location = { hash: "", pathname: "/" };
global.history = {
  pushState: (_s, _t, to) => {
    global.location.hash = String(to).startsWith("#") ? to : "";
  },
  replaceState: (_s, _t, to) => {
    global.location.hash = String(to).startsWith("#") ? to : "";
  },
};

/* Looking for an element by selector, against what was actually painted. The
   stub DOM holds markup as a string rather than a tree, so this answers the one
   question the page asks — is this box on the page — from that string. */
const SAFE_QUERY = (sel) => {
  const m = /\[data-lane="([^"]+)"\]/.exec(sel);
  if (!m) return null;
  const html = (global.main && global.main.innerHTML) || "";
  return html.includes(`data-lane="${m[1]}"`)
    ? { open: false, classList: { add() {}, remove() {} }, scrollIntoView() {} }
    : null;
};

/* The page escapes an id before putting it in a selector; the stub DOM has no
   CSS object, so provide the one method that is used. */
global.CSS = { escape: (v) => String(v).replace(/["\\]/g, "\\$&") };

const run = (body) => {
  global.fetch = SAFE_FETCH;
  global.document.getElementById = SAFE_BY_ID;
  global.document.querySelector = SAFE_QUERY;
  /* The expand test replaces this to watch what gets opened, and left its own
     stub behind for every test after it — which failed somewhere else entirely
     the moment another part of the page started asking main what it holds. */
  global.main.querySelectorAll = () => [];
  global.location.hash = "";
  return eval(script + "\n;(() => {" + body + "})()");
};

const tests = [];
const test = (name, body) => tests.push([name, body]);

// ---- fixtures ----------------------------------------------------------------
const TRAJ = {
  schema_version: "ATIF-v1.7",
  session_id: "s1",
  agent: { name: "claude-code", version: "2.1", model_name: "claude-opus-5" },
  steps: [
    {
      step_id: 1,
      source: "user",
      message: "do the thing",
      timestamp: "2026-01-01T00:00:00Z",
    },
    {
      step_id: 2,
      source: "agent",
      message: "on it",
      timestamp: "2026-01-01T00:05:00Z",
      tool_calls: [
        {
          tool_call_id: "c1",
          function_name: "Bash",
          arguments: { command: "ls" },
        },
      ],
      observation: {
        results: [{ source_call_id: "c1", content: "README.md" }],
      },
    },
    {
      step_id: 3,
      source: "agent",
      message: "done",
      reasoning_content: "thinking",
      timestamp: "2026-01-01T02:00:00Z",
    },
  ],
  subagent_trajectories: [
    {
      trajectory_id: "sub-a",
      agent: { name: "claude-code", version: "2.1" },
      steps: [{ step_id: 2, source: "agent", message: "inner" }],
    },
  ],
};
const ROW = {
  key: "K1",
  origin: "scanned",
  agent: "claude-code",
  format: "claude-code-transcript",
  path: "/tmp/a.jsonl",
  project: "proj",
  session_id: "s1",
  modified: "2026-01-01T00:00:00Z",
  size_bytes: 1048576,
  subagents: 0,
  title: "",
  folder: "",
  tags: [],
  starred: false,
  starred_steps: [],
};

const setup = (row = ROW, traj = TRAJ) => `
  INDEX=[${JSON.stringify(row)}]; FOLDERS=[]; TAGS=[];
  traj=${JSON.stringify(traj)}; cur="${row.key}";
`;

// ---- step keys ---------------------------------------------------------------
test("a subagent step key is scoped, so it cannot collide with the parent's", () => {
  const out = run(
    setup() +
      `
    const parent = step(traj.steps[1], traj, 0, 1, "");
    const inner  = step(traj.subagent_trajectories[0].steps[0], traj.subagent_trajectories[0],
                        1, null, "sub-a-");
    return { parent, inner };
  `,
  );
  // Both are "step 2"; only the scoping keeps them apart.
  assert.match(out.parent, /id="step-2"/);
  assert.match(out.inner, /id="step-sub-a-2"/);
  assert.ok(
    !out.inner.includes('id="step-2"'),
    "subagent step reused the parent's id",
  );
});

test("starring a subagent step does not star the parent's step of the same number", () => {
  const row = { ...ROW, starred_steps: ["sub-a-2"] };
  const out = run(
    setup(row) +
      `
    return { parent: step(traj.steps[1], traj, 0, 1, ""),
             inner:  step(traj.subagent_trajectories[0].steps[0],
                          traj.subagent_trajectories[0], 1, null, "sub-a-") };
  `,
  );
  assert.ok(
    /sstar on/.test(out.inner),
    "the starred subagent step is not marked",
  );
  assert.ok(
    !/sstar on/.test(out.parent),
    "starring a subagent step marked the parent's",
  );
});

test("a top-level star marks only the top-level step", () => {
  const row = { ...ROW, starred_steps: ["2"] };
  const out = run(
    setup(row) +
      `
    return { parent: step(traj.steps[1], traj, 0, 1, ""),
             inner:  step(traj.subagent_trajectories[0].steps[0],
                          traj.subagent_trajectories[0], 1, null, "sub-a-") };
  `,
  );
  assert.ok(/sstar on/.test(out.parent));
  assert.ok(!/sstar on/.test(out.inner));
});

test("the star is rendered on every step, not only on hover", () => {
  // A hover-only affordance is one nobody discovers.
  const out = run(setup() + `return step(traj.steps[0], traj, 0, 0, "");`);
  assert.match(out, /class="sstar"/);
  assert.match(out, /Add to favourites/);
});

// ---- opening a row -----------------------------------------------------------
test("a single click on the title opens the session", () => {
  const opened = run(
    setup() +
      `
    const log=[]; pick=(k)=>log.push(k); showLibrary=()=>{};
    titleClick({stopPropagation(){},target:{closest:()=>null}}, "K1");
    return new Promise(r=>setTimeout(()=>r(log), 350));
  `,
  );
  return opened.then((log) => assert.deepEqual(log, ["K1"]));
});

test("a double click renames instead of opening", () => {
  const result = run(
    setup() +
      `
    const log=[]; pick=(k)=>log.push(k); showLibrary=()=>{};
    const ev={stopPropagation(){},target:{closest:()=>null}};
    titleClick(ev,"K1"); startEdit("K1","title");
    return new Promise(r=>setTimeout(()=>r({log, editing:EDITING}), 350));
  `,
  );
  return result.then(({ log, editing }) => {
    assert.deepEqual(log, [], "a double click also opened the session");
    assert.equal(editing.field, "title");
  });
});

test("clicking a row action does not open the session", () => {
  const log = run(
    setup() +
      `
    const log=[]; pick=(k)=>log.push(k);
    openRow({target:{closest:(s)=>s==="input,button"?{}:null}}, "K1");
    return log;
  `,
  );
  assert.deepEqual(log, []);
});

// ---- the trajectory view -----------------------------------------------------
test("render finds its session by key, not by list position", () => {
  // cur is a content key; indexing INDEX by it once left the pane on "Converting…".
  const painted = run(setup() + `render(); return main.innerHTML;`);
  assert.ok(painted.length > 200, "nothing was painted");
  assert.match(painted, /claude-opus-5/);
});

test("duration reports the span from the first step to the last", () => {
  const shown = run(setup() + `render(); return main.innerHTML;`);
  assert.match(shown, /<b>2h<\/b><span>duration<\/span>/);
});

test("the Favourited lens counts and filters starred steps", () => {
  const row = { ...ROW, starred_steps: ["1", "3"] };
  const out = run(
    setup(row) +
      `
    lens="all"; render();
    const count=(main.innerHTML.match(/Favourited<b>(\\d+)/)||[])[1];
    lens="favourited"; render();
    const shown=(main.innerHTML.match(/class="step /g)||[]).length;
    return {count, shown};
  `,
  );
  assert.equal(out.count, "2");
  assert.equal(out.shown, 2);
});

test("starring a step keeps the reader where they were", async () => {
  // A full repaint used to drop the reader at the top of a long transcript.
  const at = await run(
    setup({ ...ROW, starred_steps: [] }) +
      `
    main.scrollTop = 4200;
    annotate = async () => {};
    return toggleStep({ stopPropagation() {} }, "2").then(() => main.scrollTop);
  `,
  );
  assert.equal(at, 4200);
});

test("expand all opens every tool call and branch when painted", () => {
  const out = run(
    setup() +
      `
    OPEN_ALL = true;
    return { tool: step(traj.steps[1], traj, 0, 1, ""),
             branch: branch({trajectory_id:"sub-a"}, traj, 0) };
  `,
  );
  assert.match(out.tool, /<details class="tool" open>/);
  // Attribute order is not the behaviour; being open is.
  assert.match(out.branch, /<details class="branch"[^>]*\sopen>/);
});

test("collapsed is the default, so a long transcript opens readable", () => {
  const out = run(setup() + `return step(traj.steps[1], traj, 0, 1, "");`);
  assert.match(out, /<details class="tool">/);
});

test("toggling expand does not repaint, so the reader keeps their place", () => {
  const out = run(
    setup() +
      `
    const opened=[];
    main.scrollTop = 900;
    main.querySelectorAll=()=>[{set open(v){opened.push(v)}}];
    toggleExpand();
    return {opened, at: main.scrollTop, state: OPEN_ALL};
  `,
  );
  assert.deepEqual(out.opened, [true]);
  assert.equal(out.at, 900);
  assert.equal(out.state, true);
});

// ---- rendering -----------------------------------------------------------------
test("markup in a message is escaped, not rendered", () => {
  const html = run(`return md('<script>alert(1)</script> **bold**');`);
  assert.ok(!html.includes("<script"), "script tag survived");
  assert.match(html, /<strong>bold<\/strong>/);
});

test("shell output colours diffs without reading '---' as a removal", () => {
  const html = run(`return shell("--- a/x.py\\n+++ b/x.py\\n-gone\\n+added");`);
  assert.match(html, /s-meta">--- a\/x\.py/);
  assert.match(html, /s-del">-gone/);
  assert.match(html, /s-add">\+added/);
});

test("a plain listing is left uncoloured", () => {
  assert.ok(
    !/class="s-/.test(run(`return shell("README.md\\nsrc\\npackage.json");`)),
  );
});

test("only http(s) links become anchors", () => {
  const html = run(
    `return md("[x](javascript:alert(1)) and https://example.com");`,
  );
  // The text stays as written; what matters is that nothing links to it.
  const hrefs = [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);
  assert.ok(
    !hrefs.some((h) => h.startsWith("javascript:")),
    "a javascript: URL was linked",
  );
  assert.match(html, /href="https:\/\/example\.com"/);
});

test("a path links to the reveal endpoint, in prose and in the files panel", () => {
  const prose = run(`return md("see /Users/me/a.txt");`);
  const files = run(`
    extra={files:[{name:"a & b.jsonl",path:"/Users/me/a.jsonl",role:"source",size:2048}]};
    tab="files"; return panelTab();
  `);
  assert.match(prose, /\/api\/reveal/);
  assert.match(files, /\/api\/reveal/);
  assert.ok(files.includes("a &amp; b.jsonl"), "the file name was not escaped");
});

test("prose that looks like a path is left alone", () => {
  for (const text of ["use and/or as needed", "a 3/4 split", "on 2026/08/20"]) {
    assert.ok(
      !run(`return md(${JSON.stringify(text)});`).includes("/api/reveal"),
      `linked a path in: ${text}`,
    );
  }
});

test("raw JSONL is coloured through the same renderer as tool arguments", () => {
  const out = run(`return rawSource('{"a":1}\\n{"b":"two"}\\n');`);
  assert.match(out.html, /class="j-key"/);
  assert.match(out.html, /class="j-num"/);
  assert.ok(out.html.includes("\n  "), "not pretty-printed");
});

test("raw text that is not JSON is left as text", () => {
  const out = run(`return rawSource("just some notes\\nnot json");`);
  assert.ok(!/class="j-/.test(out.html));
});

test("the sheet has a field for every credential the server can hold", () => {
  const html = run(`
    SETTINGS={secrets:{
      github:{label:"GitHub token",placeholder:"ghp_…",env:"GITHUB_TOKEN",source:"",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  for (const label of ["GitHub token"]) {
    assert.ok(html.includes(label), `no field for ${label}`);
  }
  // One token plus the AWS profile, which is a name rather than a secret.
  assert.strictEqual(html.match(/class="secret"/g).length, 2);
  assert.match(html, /AWS profile/);
});

test("a saved token shows as dots with its tail, never as a value", () => {
  const html = run(`
    SETTINGS={secrets:{github:{label:"GitHub token",placeholder:"ghp_…",
      env:"GITHUB_TOKEN",source:"settings",hint:"\u2026GQAA"}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.match(html, /placeholder="•+ \u2026GQAA"/);
  const tokenField = html.slice(0, html.indexOf("AWS profile"));
  assert.ok(
    !/value=/.test(tokenField),
    "dots as a value could be submitted and stored",
  );
});

test("a token from the environment names the variable rather than showing dots", () => {
  const html = run(`
    SETTINGS={secrets:{github:{label:"GitHub token",placeholder:"ghp_…",
      env:"GITHUB_TOKEN",source:"environment",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.match(html, /Set by GITHUB_TOKEN/);
  assert.ok(
    !html.includes("•"),
    "dots imply it was saved here and can be removed here",
  );
});

test("an unset token prompts for one", () => {
  const html = run(`
    SETTINGS={secrets:{github:{label:"GitHub token",placeholder:"ghp_…",
      env:"GITHUB_TOKEN",source:"",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.match(html, /placeholder="ghp_…"/);
});

test("a label from the server is escaped, not rendered as markup", () => {
  const html = run(`
    SETTINGS={secrets:{x:{label:"<img src=x onerror=alert(1)>",placeholder:"p",
      env:"E",source:"",hint:""}}};
    const host={innerHTML:""};
    document.getElementById=id=>id==="secrets"?host:null;
    drawSecrets();
    return host.innerHTML;`);
  assert.ok(!html.includes("<img"), "a label was rendered as HTML");
});

test("the download reports progress as files land", () => {
  const bar = { hidden: true },
    fill = { style: {} },
    state = { textContent: "" },
    button = { textContent: "" };
  run(
    `
    document.getElementById=id=>({urlbar:globalThis.__bar,urlfill:globalThis.__fill,
      urlstate:globalThis.__state,urlgo:globalThis.__button})[id]||null;
    showProgress(3,12,"transcript.jsonl");`,
    (globalThis.__bar = bar),
    (globalThis.__fill = fill),
    (globalThis.__state = state),
    (globalThis.__button = button),
  );
  assert.strictEqual(bar.hidden, false, "the bar stayed hidden");
  assert.strictEqual(fill.style.width, "25%");
  assert.match(state.textContent, /3 of 12/);
  assert.match(state.textContent, /transcript\.jsonl/);
});

test("the bar hides again when there is nothing running", () => {
  const bar = { hidden: false },
    fill = { style: {} };
  run(
    `
    document.getElementById=id=>({urlbar:globalThis.__bar,urlfill:globalThis.__fill})[id]||null;
    showProgress(0,0);`,
    (globalThis.__bar = bar),
    (globalThis.__fill = fill),
  );
  assert.strictEqual(bar.hidden, true);
});

test("a finished download refreshes the index before anything else can fail", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "https://github.com/o/r" },
    urlinto: { value: "/tmp/x" },
    urlerr: { hidden: true },
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
    urlbar: { hidden: true },
    urlfill: { style: {} },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__calls.push(url);
      if(url==="/api/browse")return {ok:true,json:async()=>({nodes:[
        {name:"a.jsonl",path:"a.jsonl",kind:"file",size:2048}]})};
      if(url==="/api/fetch"){
        const body=JSON.parse(opts.body);
        if(!body.confirm)return {ok:true,json:async()=>({plan:true,label:"o--r",
          count:2,bytes:2048,into:"/tmp/x/o--r",names:["a.jsonl"]})};
        const lines=['{"t":"file","done":1,"total":2,"name":"a.jsonl"}',
                     '{"t":"file","done":2,"total":2,"name":"b.jsonl"}',
                     '{"t":"added","added":2,"into":"/tmp/x/o--r"}'];
        const chunk=new TextEncoder().encode(lines.join(String.fromCharCode(10)));
        let sent=false;
        return {ok:true,body:{getReader:()=>({read:async()=>
          sent?{done:true}:((sent=true),{done:false,value:chunk})})}};
      }
      return {ok:true,json:async()=>({sessions:[{key:"a"},{key:"b"}],folders:[],
        tags:[],settings:{},downloads:"/tmp/x"})};
    };
    // Break what the toast depends on, rather than replacing the toast: the
    // point is that the real note() cannot abort the handler.
    document.createElement=()=>{throw new Error("no DOM here")};
    // run() wraps this in a plain arrow, so chain rather than await.
    return planUrl().then(()=>planUrl());`);
  assert.ok(calls.includes("/api/index"), "the index was never refreshed");
  assert.strictEqual(calls.filter((c) => c === "/api/fetch").length, 1);
  assert.strictEqual(
    fields.urlerr.hidden,
    true,
    "a working download showed an error",
  );
});

test("collapsing a node hides everything beneath it, not just its children", () => {
  const rows = run(`
    GROUPS=["Remote","Remote/GitHub","Remote/GitHub/owner",
            "Remote/GitHub/owner/repo","Remote/GitHub/owner/repo/attacks",
            "Remote/GitHub/owner/repo/attacks/one"];
    INDEX=[{key:"a",group:"Remote/GitHub/owner/repo/attacks/one"}];
    // Everything open, then one node in the middle closed.
    EXPANDED={};
    for(const g of GROUPS)EXPANDED[g]=true;
    EXPANDED["Remote/GitHub/owner/repo"]=false;
    return visibleRails().map(r=>r.path);`);
  assert.ok(
    rows.includes("Remote/GitHub/owner/repo"),
    "the closed node itself vanished",
  );
  const beneath = rows.filter((p) =>
    p.startsWith("Remote/GitHub/owner/repo/"),
  );
  assert.deepStrictEqual(beneath, [], `still showing: ${beneath.join(", ")}`);
});

test("a node stays visible while every ancestor is open", () => {
  const rows = run(`
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/transcript-viewer"];
    INDEX=[{key:"a",group:"Local/Claude Code/transcript-viewer"}];
    EXPANDED={"Local":true,"Local/Claude Code":true};
    return visibleRails().map(r=>r.path);`);
  assert.ok(rows.includes("Local/Claude Code/transcript-viewer"));
});

test("closing the top node leaves only the top nodes", () => {
  const rows = run(`
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/transcript-viewer","Remote"];
    INDEX=[{key:"a",group:"Local/Claude Code/transcript-viewer"}];
    EXPANDED={"Local":false,"Local/Claude Code":true};
    return visibleRails().map(r=>r.path);`);
  assert.deepStrictEqual(rows, ["__all", "Local", "Remote"]);
});

test("counts still roll up through a closed node", () => {
  const rows = run(`
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/transcript-viewer"];
    INDEX=[{key:"a",group:"Local/Claude Code/transcript-viewer"},
           {key:"b",group:"Local/Claude Code/transcript-viewer"}];
    EXPANDED={};
    return visibleRails();`);
  const local = rows.find((r) => r.path === "Local");
  assert.strictEqual(
    local.count,
    2,
    "a closed node should still count what is inside",
  );
});

test("the clear control appears only when there is something to clear", () => {
  const empty = run(`
    INDEX=[];GROUPS=[];EXPANDED={};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.ok(!empty.includes("railtop"), "offered to clear an empty library");

  const full = run(`
    INDEX=[{key:"a",group:"Local"}];GROUPS=["Local"];EXPANDED={};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.match(full, /railtop/);
  assert.match(full, /Clear library/);
});

test("clearing asks first, and does nothing when refused", async () => {
  const calls = [];
  globalThis.__cleared = calls;
  await run(`
    INDEX=[{key:"a",origin:"scanned"}];
    globalThis.confirm=()=>false;
    globalThis.fetch=(url,opts)=>{globalThis.__cleared.push(url);
      return Promise.resolve({ok:true,json:async()=>({})})};
    return clearLibrary();`);
  assert.deepStrictEqual(calls, [], "cleared without being confirmed");
});

test("the warning names how many copies the viewer would delete", async () => {
  const asked = [];
  globalThis.__asked = asked;
  await run(`
    INDEX=[{key:"a",origin:"scanned"},{key:"b",origin:"opened"},
           {key:"c",origin:"fetched"}];
    globalThis.confirm=m=>{globalThis.__asked.push(m);return false};
    return clearLibrary();`);
  assert.strictEqual(asked.length, 1);
  assert.match(asked[0], /all 3 sessions/);
  assert.match(asked[0], /1 copy the viewer made itself/);
  assert.match(asked[0], /Titles, tags and stars/);
});

test("with nothing of ours, the warning says files simply stay", async () => {
  const asked = [];
  globalThis.__asked = asked;
  await run(`
    INDEX=[{key:"a",origin:"scanned"},{key:"c",origin:"fetched"}];
    globalThis.confirm=m=>{globalThis.__asked.push(m);return false};
    return clearLibrary();`);
  assert.match(asked[0], /Files stay where they are\./);
  assert.ok(!asked[0].includes("viewer made"));
});

test("Local is offered even when the library is empty", () => {
  const rows = run(`
    INDEX=[];GROUPS=[];EXPANDED={};
    return visibleRails().map(r=>r.path);`);
  assert.deepStrictEqual(rows, ["__all", "Local"]);
});

test("Local is not doubled once it has sessions", () => {
  const rows = run(`
    INDEX=[{key:"a",group:"Local/Claude Code/x"}];
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/x"];
    EXPANDED={};
    return visibleRails().map(r=>r.path);`);
  assert.deepStrictEqual(rows, ["__all", "Local"]);
});

test("an s3 source is shown but not linked, having no page to open", () => {
  const html = run(`
    INDEX=[{key:"b",group:"Remote/S3/bucket",source:"s3://bucket/runs"}];
    GROUPS=["Remote","Remote/S3","Remote/S3/bucket"];
    EXPANDED={"Remote":true,"Remote/S3":true};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.ok(!html.includes('href="s3://'), "an s3 URI was made into a link");
});

test("a single file is confirmed, not browsed", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "https://example.com/a.jsonl" },
    urlerr: { hidden: true },
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
    tree: { hidden: true, innerHTML: "" },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__calls.length=0;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__calls.push(url);
      if(url==="/api/browse")return {ok:false,status:400,
        json:async()=>({error:"That is a single file, so there is nothing to look inside."})};
      return {ok:true,json:async()=>({plan:true,label:"example.com",count:1,
        bytes:2048,into:"/tmp/x",names:["a.jsonl"]})};
    };
    return planUrl();`);
  // Browsing is tried first; a lone file has no inside, so it falls through.
  assert.deepStrictEqual(calls, ["/api/browse", "/api/fetch"]);
  assert.match(fields.urlstate.textContent, /1 file/);
});

test("more than one file opens a picker instead of asking for a number", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "s3://bucket" },
    urlerr: { hidden: true },
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
    tree: { hidden: true, innerHTML: "" },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__calls.length=0;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__calls.push(url);
      if(url==="/api/browse")return {ok:true,json:async()=>({nodes:[
        {name:"chippy",path:"chippy",kind:"folder",size:null},
        {name:"one.jsonl",path:"one.jsonl",kind:"file",size:4096}]})};
      return {ok:true,json:async()=>({plan:true,label:"bucket",count:9,bytes:99,
        into:"/tmp/x",names:[]})};
    };
    return planUrl();`);
  // Somewhere with an inside is never weighed against the download limits
  // first — that is what refused a 118,801-object bucket before the picker.
  assert.deepStrictEqual(calls, ["/api/browse"]);
  assert.strictEqual(fields.tree.hidden, false, "the picker stayed hidden");
  assert.match(fields.tree.innerHTML, /chippy/);
  assert.match(fields.tree.innerHTML, /one\.jsonl/);
});

test("ticking a folder covers what is inside it", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(),busy:new Set(),sized:{},sizing:0};
    globalThis.fetch=async()=>({ok:true,json:async()=>({files:0,bytes:0})});
    document.getElementById=()=>null;
    pickNode("chippy");
    return [isPicked("chippy"),isPicked("chippy/abc/x.zip"),isPicked("other")];`);
  assert.deepStrictEqual(out, [true, true, false]);
});

test("ticking a folder drops the redundant ticks inside it", () => {
  const out = run(`
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(),busy:new Set(),sized:{},sizing:0};
    globalThis.fetch=async()=>({ok:true,json:async()=>({files:0,bytes:0})});
    document.getElementById=()=>null;
    pickNode("chippy/abc");
    pickNode("chippy");
    return [...TREE.picked];`);
  assert.deepStrictEqual(out, ["chippy"], "a nested tick outlived its parent");
});

test("the profile is a chooser only when there is a choice", () => {
  const one = run(
    `SETTINGS={aws_profiles:["rw-eng"],aws_profile:""};return awsField();`,
  );
  assert.ok(!one.includes("<select"), "asked to choose between one thing");
  assert.match(one, /nothing to set here/);

  const many = run(
    `SETTINGS={aws_profiles:["a","b"],aws_profile:"b"};return awsField();`,
  );
  assert.match(many, /<select/);
  assert.match(many, /value="b" selected/);
  assert.match(many, /pick the one to use/);
});

test("the examples fill the field and look straight away", async () => {
  const calls = [];
  globalThis.__calls = calls;
  const fields = {
    urlin: { value: "" },
    urlerr: { hidden: true, textContent: "" },
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
    tree: { hidden: true, innerHTML: "" },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__calls.length=0;
    globalThis.fetch=async(url)=>{
      globalThis.__calls.push(url);
      return {ok:true,json:async()=>({nodes:[]})};
    };
    const i = EXAMPLES.findIndex(e=>e.url.startsWith("s3://"));
    globalThis.__url = EXAMPLES[i].url;
    return useExample(i);`);
  assert.strictEqual(fields.urlin.value, globalThis.__url);
  assert.ok(
    calls.includes("/api/browse"),
    "it filled the field but did not look",
  );
});

test("every example names a place the fetcher can actually reach", () => {
  const egs = run(`return EXAMPLES;`);
  assert.ok(egs.length >= 2, "there should be more than one thing to try");
  for (const e of egs) {
    assert.ok(e.name, "an example without a name is a blank button");
    assert.match(
      e.url,
      /^(https:\/\/github\.com\/|s3:\/\/)/,
      `${e.name}: not a scheme the fetcher handles`,
    );
  }
  assert.ok(egs.every((e) => e.url.startsWith("s3://")));
});

test("an example that needs credentials says so", () => {
  const egs = run(`return EXAMPLES;`);
  for (const e of egs) {
    if (e.url.startsWith("s3://")) {
      assert.ok(e.note, `${e.name} needs something first and does not say so`);
    }
  }
});

test("a selection is measured rather than guessed at", async () => {
  const asked = [];
  globalThis.__asked = asked;
  const fields = {
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
    tree: { hidden: false, innerHTML: "" },
  };
  globalThis.__fields = fields;
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__asked.length=0;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__asked.push(JSON.parse(opts.body));
      return {ok:true,json:async()=>({files:180,bytes:3641000000,capped:false})};
    };
    TREE={url:"s3://b",open:{},nodes:{"":[]},picked:new Set(["chippy"]),
          busy:new Set(),sized:{},sizing:0};
    return measure();`);
  assert.deepStrictEqual(asked, [{ url: "s3://b", paths: ["chippy"] }]);
  assert.match(fields.urlstate.textContent, /180 files · 3\.4 GB/);
  assert.match(fields.urlgo.textContent, /Download 180/);
});

test("a measured folder is not measured twice", async () => {
  const asked = [];
  globalThis.__asked = asked;
  globalThis.__fields = {
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
  };
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.__asked.length=0;
    globalThis.fetch=async(url,opts)=>{
      globalThis.__asked.push(1);
      return {ok:true,json:async()=>({files:2,bytes:100,capped:false})};
    };
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(["a"]),
          busy:new Set(),sized:{},sizing:0};
    return measure().then(()=>measure());`);
  assert.strictEqual(asked.length, 1, "the same folder was listed twice");
});

test("a selection over the limit says so", async () => {
  globalThis.__fields = {
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
  };
  await run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    globalThis.fetch=async()=>({ok:true,
      json:async()=>({files:2001,bytes:99,capped:true})});
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(["big"]),
          busy:new Set(),sized:{},sizing:0};
    return measure();`);
  assert.match(globalThis.__fields.urlstate.textContent, /narrow it down/);
});

test("nothing ticked asks for a tick", () => {
  globalThis.__fields = {
    urlstate: { textContent: "" },
    urlgo: { textContent: "" },
  };
  run(`
    document.getElementById=id=>globalThis.__fields[id]||null;
    TREE={url:"s3://b",open:{},nodes:{},picked:new Set(),busy:new Set(),
          sized:{},sizing:0};
    drawTotal();`);
  assert.match(globalThis.__fields.urlstate.textContent, /Tick what you want/);
});

test("select all ticks the top level, select none clears it", () => {
  const out = run(`
    globalThis.fetch=async()=>({ok:true,json:async()=>({files:0,bytes:0})});
    document.getElementById=()=>null;
    TREE={url:"s3://b",open:{},nodes:{"":[
      {name:"a",path:"a",kind:"folder",size:null},
      {name:"b",path:"b",kind:"folder",size:null}]},
      picked:new Set(),busy:new Set(),sized:{},sizing:0};
    pickAll();
    const all=[...TREE.picked];
    pickNone();
    return [all,[...TREE.picked]];`);
  assert.deepStrictEqual(out, [["a", "b"], []]);
});

test("a size is shown in a unit that suits it", () => {
  const got =
    run(`return [0,1,900,1023,1024,1536,687642,3641000000,154618822656]
    .map(bytes);`);
  assert.deepStrictEqual(got, [
    "0 B",
    "1 B",
    "900 B",
    "1023 B",
    "1.0 KB",
    "1.5 KB",
    "672 KB",
    "3.4 GB",
    "144 GB",
  ]);
});

test("one formatter is used, not a division scattered about", () => {
  const html = run(`
    return libraryRow({key:"k",title:"t",agent:"a",modified:"2026-01-01",
      size_bytes:900,tags:[],group:"Local"});`);
  assert.match(html, /900 B/, "a small file was rounded away as MB");
});

test("the examples are rendered, not printed as template source", () => {
  const host = { innerHTML: "" };
  globalThis.__host = host;
  run(`
    document.getElementById=id=>id==="egs"?globalThis.__host:null;
    drawExamples();`);
  assert.match(host.innerHTML, /Redwood agent transcripts/);
  assert.match(host.innerHTML, /Redwood agent transcripts/);
  assert.match(host.innerHTML, /useExample\(0\)/);
  assert.ok(
    !host.innerHTML.includes("${"),
    "a template expression reached the page",
  );
});

test("a session is called what it was named, not where it ran", () => {
  const named =
    run(`return titleOf({session_title:"transcript-viewer-library-system",
    project:"/Users/girish/Documents/AIResearch/atif/transcript-viewer",path:"/x/abc.jsonl"});`);
  assert.strictEqual(named, "transcript-viewer-library-system");
});

test("what the reader named it wins over what the agent named it", () => {
  const got = run(
    `return titleOf({title:"mine",session_title:"theirs",project:"/w"});`,
  );
  assert.strictEqual(got, "mine");
});

test("with no name at all it falls back to where it ran", () => {
  const got = run(
    `return titleOf({project:"/Users/g/Documents/thing",path:"/x/abc.jsonl"});`,
  );
  assert.match(got, /thing/);
});

test("the tree still groups by directory, not by name", () => {
  const html = run(`
    INDEX=[{key:"a",group:"Local/Claude Code/transcript-viewer",
            session_title:"transcript-viewer-library-system"}];
    GROUPS=["Local","Local/Claude Code","Local/Claude Code/transcript-viewer"];
    EXPANDED={"Local":true,"Local/Claude Code":true};
    const el={innerHTML:""};
    list=el;tagbar={innerHTML:""};
    drawList();
    return el.innerHTML;`);
  assert.match(
    html,
    /transcript-viewer</,
    "the folder name should label the tree",
  );
  assert.ok(
    !html.includes("library-system"),
    "a session name leaked into the tree",
  );
});

test("a deep path reads as a trail, not a wall", () => {
  const html = run(`return crumbPath(
    "Remote/GitHub/owner/repo/attacks/plausible_naming");`);
  const parts = [...html.matchAll(/<span>([^<]+)<\/span>/g)].map((m) => m[1]);
  assert.deepStrictEqual(parts, [
    "Remote",
    "GitHub",
    "owner",
    "repo",
    "attacks",
    "plausible_naming",
  ]);
  assert.strictEqual(html.match(/class="sep"/g).length, parts.length - 1);
  // Tags carry slashes of their own; what matters is the text between them.
  const text = html.replace(/<[^>]*>/g, "");
  assert.ok(!text.includes("/"), "a bare slash survived: " + text);
});

test("a part is escaped, not rendered", () => {
  const html = run(`return crumbPath("a/<img src=x>/b");`);
  assert.ok(!html.includes("<img"));
  assert.match(html, /&lt;img/);
});

test("the header and the library heading say the same thing", () => {
  const both = run(`
    collection="Local/Claude Code/atif-make";
    cur=null;INDEX=[];
    const bar={innerHTML:""};
    crumb=bar;
    drawCrumb();
    return [bar.innerHTML, whereYouAre()];`);
  assert.strictEqual(
    both[0].trim(),
    both[1].trim(),
    "the two places disagree about where you are",
  );
  assert.match(both[0], /Claude Code/);
});

test("everything reads as All sessions", () => {
  const html = run(`collection="__all";return whereYouAre();`);
  assert.match(html, /All sessions/);
  assert.ok(!html.includes("sep"), "nothing to separate");
});

// ---- runner --------------------------------------------------------------------
(async () => {
  let failed = 0;
  for (const [name, body] of tests) {
    try {
      await body();
      console.log(`  ok   ${name}`);
    } catch (error) {
      failed++;
      console.log(`  FAIL ${name}\n       ${error.message}`);
    }
  }
  console.log(`\n${tests.length - failed} passed, ${failed} failed`);
  process.exit(failed ? 1 : 0);
})();

test("opening the library reports each stage it goes through", async () => {
  const said = [];
  const el = { hidden: true };
  const bar = {
    classList: { add() {}, remove() {} },
    firstElementChild: { style: {} },
  };
  const text = { textContent: "" };
  globalThis.__boot = { boot: el, bootbar: bar, boottext: text, said };

  const seen = run(`
    document.getElementById = id => globalThis.__boot[id] || {textContent:"",style:{},classList:{add(){},remove(){}}};
    globalThis.requestAnimationFrame = fn => fn();
    globalThis.fetch = async () => ({
      ok: true,
      headers: { get: () => "0" },
      body: null,
      json: async () => ({sessions: [], groups: [], tags: [], ai: {}}),
    });
    const stages = [];
    const realSay = bootSay;
    bootSay = (what) => { stages.push(what); realSay(what, 0, 0); };
    return openLibrary().then(() => stages);
  `);

  return seen.then((stages) => {
    assert.ok(
      stages.some((s) => /Looking for your sessions/.test(s)),
      "it should say it is looking before it has anything",
    );
    assert.ok(
      stages.some((s) => /Drawing/.test(s)),
      "it should say it is drawing once it has the data",
    );
  });
});

test("a failure to open leaves the reason on screen", async () => {
  const text = { textContent: "" };
  const el = { hidden: false };
  globalThis.__boot = {
    boot: el,
    bootbar: {
      classList: { add() {}, remove() {} },
      firstElementChild: { style: {} },
    },
    boottext: text,
  };

  await run(`
    document.getElementById = id => globalThis.__boot[id] || {textContent:"",style:{},classList:{add(){},remove(){}}};
    globalThis.fetch = async () => ({ ok: false, status: 500 });
    return openLibrary();
  `);

  assert.match(text.textContent, /Could not open your library/);
  assert.strictEqual(
    el.hidden,
    false,
    "a blank window would say nothing at all",
  );
});

// ---- how a tool call reads ---------------------------------------------------

test("a command is coloured as a command, not printed as JSON", () => {
  const html = run(`return shBlock('ls -la /tmp')`);
  assert.match(html, /class="sh"/, "it should be a shell block");
  assert.match(
    html,
    /class="sh-cmd">ls</,
    "the thing being run is the command",
  );
  assert.match(html, /class="sh-flag">-la</, "a flag reads as a flag");
});

test("what follows a pipe is another command", () => {
  const html = run(`return sh('grep -rn foo src/ | head -5')`);
  const commands = [...html.matchAll(/class="sh-cmd">([^<]+)</g)].map(
    (m) => m[1],
  );
  assert.deepStrictEqual(commands, ["grep", "head"]);
});

test("a quoted string is one piece, whatever is inside it", () => {
  const html = run(`return sh('grep "no assistant turns" src/')`);
  assert.match(html, /class="sh-str">&quot;no assistant turns&quot;</);
  // The words inside must not be mistaken for commands of their own.
  const commands = [...html.matchAll(/class="sh-cmd">([^<]+)</g)].map(
    (m) => m[1],
  );
  assert.deepStrictEqual(commands, ["grep"]);
});

test("a comment is a comment only where a word could start", () => {
  const commented = run(`return sh('# count them\\nwc -l x')`);
  assert.match(commented, /class="sh-com"># count them</);
  const inside = run(`return sh('curl https://x/a#b')`);
  assert.ok(!/sh-com/.test(inside), "a # inside a word is part of the word");
});

test("a variable is picked out in either spelling", () => {
  const html = run(`return sh('echo $HOME and \${PATH}')`);
  const vars = [...html.matchAll(/class="sh-var">([^<]+)</g)].map((m) => m[1]);
  assert.deepStrictEqual(vars, ["$HOME", "${PATH}"]);
});

test("markup in a command cannot reach the page as markup", () => {
  const html = run(`return sh('echo "<img src=x onerror=alert(1)>"')`);
  assert.ok(!html.includes("<img"), "it must be escaped");
  assert.match(html, /&lt;img/);
});

test("a path in a command still opens in Finder, and a URL still opens", () => {
  const withPath = run(`return shBlock('ls /Users/someone/Documents')`);
  assert.match(withPath, /api\/reveal\?path=/, "a path should still reveal");
  const withUrl = run(`return shBlock('curl -s https://pypi.org/simple/')`);
  assert.match(withUrl, /<a href="https:\/\/pypi\.org\/simple\/"/);
});

test("a call says what it was for, beside what it was called", () => {
  const title = run(
    `return TOOLVIEW.Bash.title({command:"ls",description:"List vault root"})`,
  );
  assert.strictEqual(title, "List vault root");
  const fetched = run(
    `return TOOLVIEW.WebFetch.title({url:"https://help.obsidian.md/plugins/templates"})`,
  );
  assert.strictEqual(fetched, "help.obsidian.md");
});

test("a fetch shows its address as a link and its question as prose", () => {
  const html = run(
    `return TOOLVIEW.WebFetch.body({url:"https://example.com/a",prompt:"What does it say?"})`,
  );
  assert.match(html, /<a href="https:\/\/example\.com\/a"/);
  assert.match(html, /What does it say\?/);
});

test("a malformed address does not stop the box drawing", () => {
  const title = run(`return TOOLVIEW.WebFetch.title({url:"not a url"})`);
  assert.strictEqual(title, "not a url");
});

test("one box can be shown raw without the rest following", () => {
  const out = run(`
    raw = false;
    CALLS["a"] = {c:{function_name:"Bash",tool_call_id:"a",arguments:{command:"ls",description:"d"}}, r:null};
    CALLS["b"] = {c:{function_name:"Bash",tool_call_id:"b",arguments:{command:"pwd",description:"d"}}, r:null};
    TOOLRAW["a"] = true;
    return [toolBody("a"), toolBody("b")];
  `);
  assert.match(
    out[0],
    /class="json"/,
    "the flipped one shows what was recorded",
  );
  assert.match(out[1], /class="sh"/, "its neighbour is untouched");
});

test("the page-wide raw setting is what a box does until it is told otherwise", () => {
  const out = run(`
    raw = true;
    CALLS["c"] = {c:{function_name:"Bash",tool_call_id:"c",arguments:{command:"ls"}}, r:null};
    const following = toolBody("c");
    TOOLRAW["c"] = false;
    const flipped = toolBody("c");
    raw = false;
    return [following, flipped];
  `);
  assert.match(out[0], /class="json"/, "it follows the page by default");
  assert.match(out[1], /class="sh"/, "and can disagree once flipped");
});

test("a tool with no view of its own still gets its JSON", () => {
  const html = run(`
    CALLS["z"] = {c:{function_name:"SomethingNew",tool_call_id:"z",arguments:{a:1}}, r:null};
    return toolBody("z");
  `);
  assert.match(html, /class="json"/);
});

test("a redirect target is a file, not a command", () => {
  const html = run(`return sh('wc -l a.txt > /tmp/out.txt')`);
  const commands = [...html.matchAll(/class="sh-cmd">([^<]+)</g)].map(
    (m) => m[1],
  );
  assert.deepStrictEqual(commands, ["wc"], "only wc is being run");
});

test("a subshell still starts a command", () => {
  const html = run(`return sh('(cd /tmp && ls)')`);
  const commands = [...html.matchAll(/class="sh-cmd">([^<]+)</g)].map(
    (m) => m[1],
  );
  assert.deepStrictEqual(commands, ["cd", "ls"]);
});

test("changing the page-wide setting means it, even for boxes flipped by hand", () => {
  const out = run(`
    raw = false;
    CALLS["g"] = {c:{function_name:"Bash",tool_call_id:"g",arguments:{command:"ls"}}, r:null};
    TOOLRAW["g"] = true;               // this box was asked for the recording
    const before = toolBody("g");
    render = () => {};                 // the repaint is not what is under test
    toggleRaw();                        // now the whole page is
    const after = toolBody("g");
    return [before, after, raw];
  `);
  assert.match(out[0], /class="json"/, "it was showing the recording");
  assert.match(
    out[1],
    /class="json"/,
    "and still does, because the page now says so",
  );
  assert.strictEqual(out[2], true);
});

test("a file path is titled by its end, where the name is", () => {
  const title = run(
    `return TOOLVIEW.Read.title({file_path:"/private/tmp/very/long/way/down/ARCHITECTURE.md"})`,
  );
  assert.strictEqual(title, "down/ARCHITECTURE.md");
});

test("a command substitution does not stop the scanner", () => {
  // $( matched no branch and the word reader could not move past $, so the page
  // hung for ever on any command using one. 58 of them in one real session.
  const html = run(`return sh('echo $(date) and $(basename /a/b)')`);
  assert.match(html, /sh-var/);
  assert.ok(html.includes("date"), "what is inside must still be there");
});

test("a lone dollar is just a character", () => {
  const html = run(`return sh('echo 5$ and $')`);
  assert.ok(html.includes("5$"), "it should survive");
});

test("every character survives the scanner", () => {
  // The strongest check there is: what goes in comes out, tags aside. A branch
  // that stops matching shows up here as mangled text rather than as a hang.
  const samples = [
    "echo $(date)",
    'for f in $HOME/*; do echo "$f"; done',
    "awk '{print $1}' file | sort -u > /tmp/o",
    "x=$((1+2)); echo $x $$ $? $",
    "cat <<'EOF'\nliteral $(not a call)\nEOF",
    "curl -s https://x/a#b -o /tmp/x && echo done # a comment",
  ];
  for (const src of samples) {
    const out = run(`return sh(${JSON.stringify(src)})`);
    const back = out
      .replace(/<[^>]+>/g, "")
      .replace(/&quot;/g, '"')
      .replace(/&#39;/g, "'")
      .replace(/&lt;/g, "<")
      .replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&");
    assert.strictEqual(back, src, `mangled: ${JSON.stringify(src)}`);
  }
});

test("a real 20,000-character command draws, and quickly", () => {
  // This exact command hung the page: 448 lines of heredoc with 58 command
  // substitutions in it. Kept as a fixture because no invented sample had the
  // shape that broke it.
  const command = JSON.parse(
    readFileSync(join(__dirname, "fixtures", "big-command.json"), "utf8"),
  );
  const started = Date.now();
  const html = run(`return shBlock(${JSON.stringify(command)})`);
  const took = Date.now() - started;
  assert.ok(html.includes('class="sh"'), "it should render");
  assert.ok(took < 2000, `took ${took}ms, which is a page nobody waits for`);
});

// ---- a file read, written or changed -----------------------------------------

test("a file read back keeps its line numbers in a gutter", () => {
  const html = run(`return TOOLVIEW.Read.output('1\\tfirst\\n2\\tsecond')`);
  assert.match(html, /class="filelines"/);
  assert.match(html, /<span class="ln">1<\/span>first/);
  assert.match(html, /<span class="ln">2<\/span>second/);
  assert.ok(!html.includes("\t"), "the tab becomes the gutter, not whitespace");
});

test("a line that is not numbered still shows", () => {
  const html = run(`return TOOLVIEW.Read.output('no number here')`);
  assert.match(html, /no number here/);
});

test("a read of part of a file says which part", () => {
  const html = run(
    `return TOOLVIEW.Read.body({file_path:"/a/b.py",offset:40,limit:120})`,
  );
  assert.match(html, /from line 40/);
  assert.match(html, /120 lines/);
});

test("a whole-file read says nothing about ranges", () => {
  const html = run(`return TOOLVIEW.Read.body({file_path:"/a/b.py"})`);
  assert.ok(!/from line/.test(html));
});

test("a path in a file view still opens in Finder", () => {
  const html = run(
    `return TOOLVIEW.Read.body({file_path:"/Users/someone/a.py"})`,
  );
  assert.match(html, /api\/reveal\?path=/);
});

test("a write shows what it wrote, and how much", () => {
  const html = run(
    `return TOOLVIEW.Write.body({file_path:"/a/b.js",content:"one\\ntwo\\nthree"})`,
  );
  assert.match(html, /class="filebody"/);
  assert.match(html, /three/);
  assert.match(html, /3 lines/);
});

test("an edit shows what it replaced and what it put there", () => {
  const html = run(
    `return TOOLVIEW.Edit.body({file_path:"/a/b.js",old_string:"before",new_string:"after",replace_all:true})`,
  );
  assert.match(html, /class="eside out"[^>]*>.*replaced.*before/s);
  assert.match(html, /class="eside in"[^>]*>.*with.*after/s);
  assert.match(html, /every occurrence/);
});

test("an edit that replaces once does not claim otherwise", () => {
  const html = run(
    `return TOOLVIEW.Edit.body({file_path:"/a/b.js",old_string:"x",new_string:"y",replace_all:false})`,
  );
  assert.ok(!/every occurrence/.test(html));
});

test("file contents cannot reach the page as markup", () => {
  for (const expr of [
    `TOOLVIEW.Write.body({file_path:"/a",content:"<img src=x onerror=alert(1)>"})`,
    `TOOLVIEW.Edit.body({file_path:"/a",old_string:"<script>",new_string:"<b>"})`,
    `TOOLVIEW.Read.output("1\\t<img src=x onerror=alert(1)>")`,
  ]) {
    const html = run(`return ${expr}`);
    assert.ok(!/<img|<script>/.test(html), `escaped badly: ${expr}`);
  }
});

test("asking for the recording gives the recording, not the file view", () => {
  const html = run(`
    raw = false;
    CALLS["r1"] = {
      c:{function_name:"Read",tool_call_id:"r1",arguments:{file_path:"/a/b.py"}},
      r:{source_call_id:"r1",content:"1\\thello"},
    };
    TOOLRAW["r1"] = true;
    return toolBody("r1");
  `);
  assert.match(html, /class="json"/, "the call shows as recorded");
  assert.ok(!/class="filelines"/.test(html), "and so does the output");
});

for (const content of [
  "Nested answer <script>alert(1)</script>",
  [{ type: "text", text: "Nested answer <script>alert(1)</script>" }],
]) {
  test(`a nested tool answer is rendered and searchable (${typeof content})`, () => {
    const nested = {
      trajectory_id: "child",
      steps: [{
        step_id: 1,
        source: "agent",
        tool_calls: [{ tool_call_id: "nested", function_name: "Task", arguments: {} }],
        observation: { results: [{
          source_call_id: "nested",
          content: null,
          extra: { tool_result: { data: { content } } },
        }] },
      }],
    };
    for (const raw of [false, true]) {
      const result = run(`
        raw = ${raw};
        const child = ${JSON.stringify(nested)};
        return {
          html: branch({trajectory_id: "child"}, {subagent_trajectories: [child]}, 1),
          search: haystack(child.steps[0]),
        };
      `);
      assert.match(result.html, /class="io out"/);
      assert.match(result.html, /Nested answer/);
      assert.ok(!result.html.includes("<script>"), "the answer must be escaped");
      assert.match(result.search, /nested answer/);
    }
  });
}

test("normal result content wins over the nested fallback, and missing results are safe", () => {
  const contents = run(`return [
    resultContent({content: "primary", extra: {tool_result: {data: {content: "fallback"}}}}),
    resultContent({content: "", extra: {tool_result: {data: {content: "fallback"}}}}),
    resultContent({extra: {tool_result: {data: {content: "fallback"}}}}),
    resultContent({extra: {tool_result: {}}}),
    resultContent(undefined),
  ];`);
  assert.deepStrictEqual(contents, ["primary", "", "fallback", undefined, undefined]);
});

test("a path with a space in it links to the whole path", () => {
  // A real one: "/Users/girish/Documents/Redwood Research/…" linked only as far
  // as "Redwood", so the link opened a directory that was not the file.
  const html = run(
    `return TOOLVIEW.Read.body({file_path:"/Users/girish/Documents/Redwood Research/app/mentions.py"})`,
  );
  assert.match(html, /Redwood%20Research/, "the space must be inside the link");
  assert.ok(
    !/<\/a>\s*Research/.test(html),
    "the link must not stop at the space",
  );
});

test("a relative path is shown but not offered as a link", () => {
  const html = run(`return TOOLVIEW.Read.body({file_path:"src/thing.py"})`);
  assert.match(html, /src\/thing\.py/);
  assert.ok(!/api\/reveal/.test(html), "revealing a relative path would guess");
});

// ---- the delegation timeline -------------------------------------------------

const TWO_AT_ONCE = {
  steps: [
    {
      step_id: 1,
      source: "agent",
      timestamp: "2026-01-01T10:00:00Z",
      tool_calls: [{ tool_call_id: "c1", function_name: "Task" }],
      observation: {
        results: [
          {
            source_call_id: "c1",
            subagent_trajectory_ref: [{ trajectory_id: "a" }],
          },
        ],
      },
    },
    {
      step_id: 2,
      source: "agent",
      timestamp: "2026-01-01T10:01:00Z",
      tool_calls: [{ tool_call_id: "c2", function_name: "Task" }],
      observation: {
        results: [
          {
            source_call_id: "c2",
            subagent_trajectory_ref: [{ trajectory_id: "b" }],
          },
        ],
      },
    },
    { step_id: 3, source: "agent", timestamp: "2026-01-01T10:20:00Z" },
  ],
  subagent_trajectories: [
    {
      trajectory_id: "a",
      agent: { name: "claude-code" },
      extra: { agentType: "Explore", description: "find the theme" },
      steps: [
        {
          step_id: 1,
          source: "agent",
          timestamp: "2026-01-01T10:00:10Z",
          tool_calls: [{ tool_call_id: "x", function_name: "Grep" }],
        },
        { step_id: 2, source: "agent", timestamp: "2026-01-01T10:08:00Z" },
      ],
    },
    {
      trajectory_id: "b",
      agent: { name: "claude-code" },
      extra: { agentType: "general-purpose" },
      steps: [
        { step_id: 1, source: "agent", timestamp: "2026-01-01T10:05:00Z" },
        { step_id: 2, source: "agent", timestamp: "2026-01-01T10:12:00Z" },
      ],
    },
  ],
};

test("a subagent becomes a lane, found through the call that spawned it", () => {
  const lanes = run(`return laneIndex(${JSON.stringify(TWO_AT_ONCE)})`);
  assert.strictEqual(lanes.length, 2);
  assert.deepStrictEqual(
    lanes.map((l) => l.kind),
    ["Explore", "general-purpose"],
  );
  assert.strictEqual(lanes[0].said, "find the theme");
  assert.strictEqual(
    lanes[0].fromStep,
    1,
    "it remembers the step it came from",
  );
  assert.deepStrictEqual(lanes[0].tools, ["Grep"]);
});

test("two subagents running at once is found, and is the point", () => {
  // a runs 10:00:10–10:08, b runs 10:05–10:12, so 10:05–10:08 has both.
  const bands = run(`
    const lanes = laneIndex(${JSON.stringify(TWO_AT_ONCE)});
    return together(lanes).map(([a,b]) => [new Date(a).toISOString(), new Date(b).toISOString()]);
  `);
  assert.strictEqual(bands.length, 1);
  assert.strictEqual(bands[0][0], "2026-01-01T10:05:00.000Z");
  assert.strictEqual(bands[0][1], "2026-01-01T10:08:00.000Z");
});

test("subagents that never overlap produce no band", () => {
  const apart = JSON.parse(JSON.stringify(TWO_AT_ONCE));
  apart.subagent_trajectories[1].steps = [
    { step_id: 1, source: "agent", timestamp: "2026-01-01T10:09:00Z" },
    { step_id: 2, source: "agent", timestamp: "2026-01-01T10:12:00Z" },
  ];
  const bands = run(`return together(laneIndex(${JSON.stringify(apart)}))`);
  assert.deepStrictEqual(bands, [], "nothing ran alongside anything");
});

test("the timeline says how many ran at once", () => {
  const html = run(`return drawLanes(${JSON.stringify(TWO_AT_ONCE)})`);
  assert.match(html, /id="lanes"/);
  assert.match(html, /2 subagents/);
  assert.match(
    html,
    /overlapped once/,
    "one overlap reads as once, not '1 times'",
  );
  // Overlap is not given a colour of its own: two bars in the same column of
  // the chart already say it, and a second encoding of the same fact is noise.
  assert.ok(!/class="band"/.test(html), "the lanes are the encoding");
});

test("the header counts, and counts in English", () => {
  const one = run(
    `return laneHeader({count: 1, shown: "5m", overlaps: 0}).join(" · ")`,
  );
  assert.strictEqual(one, "1 subagent · 5m");

  const some = run(
    `return laneHeader({count: 12, shown: "2h", overlaps: 4}).join(" · ")`,
  );
  assert.match(some, /^12 subagents · 2h · /);
  assert.match(some, /overlapped 4 times/);
});

test("the header never passes off the drawn time as the whole run", () => {
  // The axis skips the waiting, so a run of ten days can show thirty-one hours.
  // Saying "31h" alone would be false about the run.
  const elided = run(
    `return laneHeader({count: 3, shown: "31h", whole: "10d"}).join(" · ")`,
  );
  assert.match(elided, /31h shown of 10d/);

  // Where nothing was skipped the two are the same and it says it once.
  const straight = run(
    `return laneHeader({count: 3, shown: "20m", whole: "20m"}).join(" · ")`,
  );
  assert.strictEqual(straight, "3 subagents · 20m");
});

test("a transcript that delegated nothing gets no timeline at all", () => {
  const html = run(
    `return drawLanes({steps:[{step_id:1,source:"user",message:"hi"}]})`,
  );
  assert.strictEqual(html, "", "an empty chart says less than no chart");
});

test("without timestamps the lanes fall back to step position and say so", () => {
  const untimed = JSON.parse(JSON.stringify(TWO_AT_ONCE));
  for (const s of untimed.steps) delete s.timestamp;
  for (const sub of untimed.subagent_trajectories) {
    for (const s of sub.steps) delete s.timestamp;
  }
  const html = run(`return drawLanes(${JSON.stringify(untimed)})`);
  assert.match(
    html,
    /carry no times/,
    "it must not imply a duration it does not have",
  );
  assert.ok(
    !/class="band"/.test(html),
    "and must not claim overlap it cannot know",
  );
  assert.ok(!/overlapped/.test(html), "nor say two things ran together");
  assert.ok(!/hidden/.test(html), "nor that anything was left out");
});

test("the axis is read in whole units, not seven of them", () => {
  const marks = run(`return axisMarks(0, 20*60*1000)`);
  assert.ok(
    marks.length >= 2 && marks.length <= 7,
    `got ${marks.length} marks`,
  );
  // Labelled with the app's own duration formatter, so "0s" not a bare 0.
  assert.match(marks[0].label, /^0/);
  assert.match(
    marks[marks.length - 1].label,
    /m$/,
    "the last mark reads in minutes",
  );
});

test("a lane opens the box the transcript already has for it", () => {
  const html = run(`
    return branch({trajectory_id:"a"},
      {subagent_trajectories:[{trajectory_id:"a",agent:{name:"claude-code"},steps:[],extra:{}}]}, 0);
  `);
  assert.match(html, /data-lane="a"/, "the lane needs something to find");
});

test("a subagent that delegates further is a lane too", () => {
  const deep = {
    steps: [
      {
        step_id: 1,
        source: "agent",
        tool_calls: [{ tool_call_id: "c1", function_name: "Task" }],
        observation: {
          results: [
            {
              source_call_id: "c1",
              subagent_trajectory_ref: [{ trajectory_id: "outer" }],
            },
          ],
        },
      },
    ],
    subagent_trajectories: [
      {
        trajectory_id: "outer",
        agent: { name: "claude-code" },
        extra: { agentType: "Explore" },
        steps: [
          {
            step_id: 1,
            source: "agent",
            tool_calls: [{ tool_call_id: "c2", function_name: "Task" }],
            observation: {
              results: [
                {
                  source_call_id: "c2",
                  subagent_trajectory_ref: [{ trajectory_id: "inner" }],
                },
              ],
            },
          },
        ],
        subagent_trajectories: [
          {
            trajectory_id: "inner",
            agent: { name: "claude-code" },
            extra: { agentType: "general-purpose" },
            steps: [],
          },
        ],
      },
    ],
  };
  const lanes = run(`return laneIndex(${JSON.stringify(deep)})`);
  assert.deepStrictEqual(
    lanes.map((l) => l.depth),
    [0, 1],
    "nesting is not flattened away",
  );
});

test("a quiet stretch is skipped, and the work either side keeps its length", () => {
  // Two minutes of work, a two-hour gap, two more minutes of work.
  const t0 = Date.parse("2026-01-01T10:00:00Z");
  const min = 60000;
  const scale = run(`return timeScale(${t0}, ${t0 + 124 * min},
    [[${t0}, ${t0 + 2 * min}], [${t0 + 122 * min}, ${t0 + 124 * min}]])`);

  assert.strictEqual(scale.gaps.length, 1, "one stretch had nothing running");
  // The two equal pieces of work must come out the same width.
  const first = scale.posOf(t0 + 2 * min) - scale.posOf(t0);
  const second = scale.posOf(t0 + 124 * min) - scale.posOf(t0 + 122 * min);
  assert.ok(Math.abs(first - second) < 0.01, `${first} vs ${second}`);
  assert.ok(
    first > 30,
    "each two-minute piece should now be a real share of the width",
  );
});

test("many breaks share one budget rather than crowding out the work", () => {
  // Forty gaps: at a fixed width each there would be no room left.
  const t0 = Date.parse("2026-01-01T00:00:00Z");
  const hour = 3600000;
  const busy = [];
  for (let i = 0; i < 40; i++)
    busy.push([t0 + i * hour, t0 + i * hour + 60000]);
  const scale = run(
    `return timeScale(${t0}, ${t0 + 40 * hour}, ${JSON.stringify(busy)})`,
  );
  assert.ok(scale.gaps.length >= 39);
  assert.ok(scale.room >= 88, `the work kept ${scale.room}% of the width`);
});

test("a run with no quiet stretch is laid out straight", () => {
  const t0 = Date.parse("2026-01-01T10:00:00Z");
  const scale = run(
    `return timeScale(${t0}, ${t0 + 600000}, [[${t0}, ${t0 + 600000}]])`,
  );
  assert.strictEqual(scale.gaps.length, 0);
  assert.strictEqual(scale.room, 100);
  assert.ok(
    Math.abs(scale.posOf(t0 + 300000) - 50) < 0.01,
    "half way is half way",
  );
});

test("what was skipped is stated, not swallowed", () => {
  const t = {
    steps: [
      {
        step_id: 1,
        source: "agent",
        timestamp: "2026-01-01T10:00:00Z",
        tool_calls: [{ tool_call_id: "c1", function_name: "Task" }],
        observation: {
          results: [
            {
              source_call_id: "c1",
              subagent_trajectory_ref: [{ trajectory_id: "a" }],
            },
          ],
        },
      },
      { step_id: 2, source: "agent", timestamp: "2026-01-02T10:00:00Z" },
    ],
    subagent_trajectories: [
      {
        trajectory_id: "a",
        agent: { name: "claude-code" },
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:00:10Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:02:00Z" },
        ],
      },
    ],
  };
  // The elision belongs to the timeline; the list has no axis to cut.
  const html = run(`LANEVIEW="time"; return drawLanes(${JSON.stringify(t)})`);
  assert.match(
    html,
    /of waiting hidden/,
    "a reader must be told time was left out",
  );
  assert.match(html, /shown of/, "and that the axis is shorter than the run");
  assert.match(html, /class="gapmark"/, "and see where");
  // How many gaps were collapsed is a fact about the drawing, not the run.
  assert.ok(
    !/\d+ quiet stretch/.test(html),
    "that is not the reader's business",
  );
});

test("every phrase in the timeline is computed, none of it written per run", () => {
  // The header and note are built from counts and durations alone: the same run
  // renders the same words, and a different one renders different ones for the
  // same reason. Nothing here is specific to any transcript.
  const twice = [1, 2].map(() =>
    run(`return drawLanes(${JSON.stringify(TWO_AT_ONCE)})`),
  );
  assert.strictEqual(twice[0], twice[1], "the same run must read the same way");

  const bigger = JSON.parse(JSON.stringify(TWO_AT_ONCE));
  bigger.subagent_trajectories[1].steps = [
    { step_id: 1, source: "agent", timestamp: "2026-01-01T10:30:00Z" },
    { step_id: 2, source: "agent", timestamp: "2026-01-01T10:40:00Z" },
  ];
  const changed = run(`return drawLanes(${JSON.stringify(bigger)})`);
  assert.notStrictEqual(
    changed,
    twice[0],
    "a different run must read differently",
  );
  assert.ok(!/overlapped/.test(changed), "and this one no longer overlaps");
});

test("a subagent no call spawned still gets a lane and a box", () => {
  // MALT records an advisor, an agent and its raters as trajectories in their
  // own right. Walking only the call references found none of them, so a record
  // with ten subagents drew no timeline and showed none of them anywhere.
  const t = {
    steps: [
      {
        step_id: 1,
        source: "user",
        message: "do the thing",
        timestamp: "2026-01-01T10:00:00Z",
      },
    ],
    subagent_trajectories: [
      {
        trajectory_id: "advisor",
        agent: { name: "metr-malt" },
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:00:05Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:02:00Z" },
        ],
      },
      {
        trajectory_id: "rater",
        agent: { name: "metr-malt" },
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:01:00Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:03:00Z" },
        ],
      },
    ],
  };

  const lanes = run(`return laneIndex(${JSON.stringify(t)})`);
  assert.strictEqual(lanes.length, 2, "both belong on the timeline");
  assert.deepStrictEqual(
    lanes.map((l) => l.fromStep),
    [null, null],
    "and neither claims a step spawned it",
  );

  const loose = run(
    `return looseSubagents(${JSON.stringify(t)}).map(s=>s.trajectory_id)`,
  );
  assert.deepStrictEqual(
    loose,
    ["advisor", "rater"],
    "the transcript must show them somewhere, or the lanes point at nothing",
  );
});

test("a subagent a call did spawn is not also shown loose", () => {
  const t = {
    steps: [
      {
        step_id: 1,
        source: "agent",
        tool_calls: [{ tool_call_id: "c1", function_name: "Task" }],
        observation: {
          results: [
            {
              source_call_id: "c1",
              subagent_trajectory_ref: [{ trajectory_id: "a" }],
            },
          ],
        },
      },
    ],
    subagent_trajectories: [
      {
        trajectory_id: "a",
        agent: { name: "claude-code" },
        extra: {},
        steps: [],
      },
    ],
  };
  const loose = run(`return looseSubagents(${JSON.stringify(t)})`);
  assert.deepStrictEqual(loose, [], "it already has a home in the run");
});

test("two durations read against each other share a unit", () => {
  // "75m shown of 1.6h" is two scales in one sentence.
  const pair = run(`return durationPair(75*60000, 96*60000)`);
  assert.deepStrictEqual(pair, ["1.3h", "1.6h"]);

  const wide = run(`return durationPair(31*3600000, 10*86400000)`);
  assert.deepStrictEqual(wide, ["1.3d", "10d"]);
});

test("a lane reaches its box even when the run has not been drawn that far", () => {
  // The bug: a long run paints 250 steps at a time, and in one real session all
  // thirty delegations happen after step 1,231 — so the box a lane pointed at
  // had never been painted and clicking did nothing whatever.
  const steps = [];
  for (let i = 0; i < 400; i++) {
    steps.push({ step_id: i + 1, source: "agent", message: "working" });
  }
  steps[300] = {
    step_id: 301,
    source: "agent",
    tool_calls: [{ tool_call_id: "c1", function_name: "Task" }],
    observation: {
      results: [
        {
          source_call_id: "c1",
          subagent_trajectory_ref: [{ trajectory_id: "far" }],
        },
      ],
    },
  };
  const t = {
    steps,
    subagent_trajectories: [
      {
        trajectory_id: "far",
        agent: { name: "claude-code" },
        extra: { agentType: "Explore" },
        steps: [],
      },
    ],
  };

  const out = run(
    setup({ ...ROW, key: "k" }) +
      `
    traj = ${JSON.stringify(t)};
    cur = "k"; lens = "all"; query = ""; limit = 250;
    render();
    const before = !!document.querySelector('details.branch[data-lane="far"]');
    LANES = laneIndex(traj);
    openLane("far");
    const after = !!document.querySelector('details.branch[data-lane="far"]');
    return { before, after, limit };
  `,
  );

  assert.strictEqual(
    out.before,
    false,
    "the box starts undrawn — this is the bug",
  );
  assert.strictEqual(out.after, true, "and the click has to reach it");
  assert.ok(
    out.limit > 300,
    `the run was drawn far enough (limit ${out.limit})`,
  );
});

test("a lane whose box is filtered away brings the view back rather than failing", () => {
  const t = {
    steps: [{ step_id: 1, source: "user", message: "go" }],
    subagent_trajectories: [
      {
        trajectory_id: "loose",
        agent: { name: "metr-malt" },
        extra: {},
        steps: [],
      },
    ],
  };
  const out = run(
    setup({ ...ROW, key: "k" }) +
      `
    traj = ${JSON.stringify(t)};
    cur = "k"; lens = "tools"; query = "nothing matches this"; limit = 250;
    render();
    LANES = laneIndex(traj);
    openLane("loose");
    return { lens, query, found: !!document.querySelector('details.branch[data-lane="loose"]') };
  `,
  );
  assert.strictEqual(out.lens, "all", "the lens is cleared to reach it");
  assert.strictEqual(out.query, "");
  assert.strictEqual(out.found, true);
});

test("a lane lands on the top of what it opened, not the middle of it", () => {
  // Centring put a long subagent halfway through itself. What was asked for is
  // the subagent, which starts at its first step.
  const moved = [];
  const box = {
    open: false,
    classList: { add() {}, remove() {} },
    getBoundingClientRect: () => ({ top: 900 }),
  };
  const main = {
    scrollTop: 200,
    getBoundingClientRect: () => ({ top: 60 }),
    scrollTo: (opts) => moved.push(opts),
  };
  global.__el = { main, box };

  run(`
    document.getElementById = (id) => (id === "main" ? globalThis.__el.main : null);
    laneScroll(globalThis.__el.box);
  `);

  assert.strictEqual(
    moved.length,
    1,
    "it should scroll the reading area itself",
  );
  // 200 + 900 - 60 - 16 of breathing room.
  assert.strictEqual(moved[0].top, 1024);
  assert.strictEqual(moved[0].behavior, "smooth");
});

test("landing does not scroll above the top of the run", () => {
  const moved = [];
  global.__el2 = {
    main: {
      scrollTop: 0,
      getBoundingClientRect: () => ({ top: 60 }),
      scrollTo: (o) => moved.push(o),
    },
    box: { getBoundingClientRect: () => ({ top: 10 }) },
  };
  run(`
    document.getElementById = (id) => (id === "main" ? globalThis.__el2.main : null);
    laneScroll(globalThis.__el2.box);
  `);
  assert.strictEqual(moved[0].top, 0, "never a negative offset");
});

test("the box a lane opened is marked, and the mark is a real rule", () => {
  const style = page.slice(page.indexOf("<style>"), page.indexOf("</style>"));
  // Whitespace-tolerant: the file is auto-formatted, and a test that pins
  // the spacing of a selector fails on a reformat that changed nothing.
  assert.match(style, /\.branch\.found\s*\{/, "the class must style something");
  const script = page.slice(
    page.indexOf("<script>"),
    page.lastIndexOf("</script>"),
  );
  assert.match(
    script,
    /classList\.add\("found"\)/,
    "and something must set it",
  );
});

test("a long run fills itself in rather than offering a button", () => {
  const steps = [];
  for (let i = 0; i < 900; i++) {
    steps.push({ step_id: i + 1, source: "agent", message: "working" });
  }
  const html = run(`
    traj = {steps: ${JSON.stringify(steps)}};
    cur = "k"; INDEX = [{key:"k"}]; lens = "all"; query = ""; limit = 250;
    return trajectoryTab(traj, {}, [], new Set(), {all: 900}, "");
  `);
  assert.ok(
    !/Show more/.test(html),
    "nobody wants a button; they want the transcript",
  );
  assert.match(html, /drawing the rest/, "but it should say more is coming");
  assert.match(html, /250 of 900 steps/);
});

test("once everything is drawn it says nothing at all", () => {
  const steps = [{ step_id: 1, source: "user", message: "hi" }];
  const html = run(`
    traj = {steps: ${JSON.stringify(steps)}};
    cur = "k"; INDEX = [{key:"k"}]; lens = "all"; query = ""; limit = 250;
    return trajectoryTab(traj, {}, [], new Set(), {all: 1}, "");
  `);
  assert.ok(!/drawing the rest/.test(html));
});

test("filling holds the reader's place, and stops when the run is done", () => {
  const out = run(`
    const steps = [];
    for (let i = 0; i < 1200; i++) steps.push({step_id:i+1, source:"agent", message:"x"});
    traj = {steps}; cur = "k"; INDEX = [{key:"k"}];
    lens = "all"; query = ""; tab = "trajectory"; limit = 250;
    main.scrollTop = 400;

    // Run the scheduler by hand: each tick draws one more chunk.
    let ticks = 0;
    globalThis.setTimeout = (fn) => { if (ticks++ < 50) fn(); return 0; };
    globalThis.clearTimeout = () => {};
    globalThis.requestIdleCallback = (fn) => fn();

    fillRest();
    return { limit, steps: traj.steps.length, at: main.scrollTop };
  `);
  assert.strictEqual(
    out.limit >= out.steps,
    true,
    `stopped at ${out.limit} of ${out.steps}`,
  );
  assert.strictEqual(out.at, 400, "the reader stays where they were");
});

// ---- a transcript has an address ---------------------------------------------

test("opening a transcript puts it in the address", () => {
  const out = run(`
    INDEX = [{key:"abc123", path:"/a.jsonl"}];
    globalThis.fetch = () => Promise.resolve({ok:true, json: () => Promise.resolve({steps:[]})});
    pick("abc123");
    return location.hash;
  `);
  assert.strictEqual(out, "#abc123", "so a refresh comes back here");
});

test("going back to the library clears it", () => {
  const out = run(`
    INDEX = [{key:"abc123", path:"/a.jsonl", group:"Local"}];
    location.hash = "#abc123";
    showLibrary();
    return location.hash;
  `);
  assert.strictEqual(out, "", "the library is not one transcript");
});

test("the whole key is used, never a prefix", () => {
  // Two transcripts can share a prefix; opening the wrong one would be silent.
  const out = run(`
    INDEX = [{key:"aaaaaaaaaaaaaaaa1111"},{key:"aaaaaaaaaaaaaaaa2222"}];
    globalThis.fetch = () => Promise.resolve({ok:true, json: () => Promise.resolve({steps:[]})});
    pick("aaaaaaaaaaaaaaaa2222");
    return { hash: location.hash, asked: addressed() };
  `);
  assert.strictEqual(out.hash, "#aaaaaaaaaaaaaaaa2222");
  assert.strictEqual(out.asked, "aaaaaaaaaaaaaaaa2222");
});

test("an address naming nothing in this library says so, and shows the library", () => {
  const out = run(`
    INDEX = [{key:"real"}];
    location.hash = "#gone";
    let said = "";
    note = (m) => { said = m; };
    followAddress();
    return { said, cur, hash: location.hash };
  `);
  assert.strictEqual(out.cur, "gone", "it tried");
  // followAddress hands off to pick, which fetches; what matters is it did not
  // silently sit on the library with a stale address.
  assert.strictEqual(out.hash, "#gone");
});

test("an empty address shows the library rather than a blank pane", () => {
  const out = run(`
    INDEX = [{key:"real", group:"Local"}];
    location.hash = "";
    cur = "real";
    followAddress();
    return cur;
  `);
  assert.strictEqual(out, null, "no address means no transcript");
});

// ---- packing, and the two views ----------------------------------------------

test("lanes that never overlap share one row", () => {
  const t = {
    steps: [{ step_id: 1, source: "agent", timestamp: "2026-01-01T10:00:00Z" }],
    subagent_trajectories: [
      {
        trajectory_id: "a",
        agent: {},
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:00:00Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:05:00Z" },
        ],
      },
      {
        trajectory_id: "b",
        agent: {},
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:06:00Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:10:00Z" },
        ],
      },
      {
        trajectory_id: "c",
        agent: {},
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:11:00Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:15:00Z" },
        ],
      },
    ],
  };
  const out = run(`
    const lanes = laneIndex(${JSON.stringify(t)});
    const rows = packLanes(lanes);
    return { rows, on: lanes.map(l => l.row) };
  `);
  assert.strictEqual(out.rows, 1, "three in a row is one row, not three");
  assert.deepStrictEqual(out.on, [0, 0, 0]);
});

test("a second row exists only because two things ran at once", () => {
  const t = {
    steps: [{ step_id: 1, source: "agent", timestamp: "2026-01-01T10:00:00Z" }],
    subagent_trajectories: [
      {
        trajectory_id: "a",
        agent: {},
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:00:00Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:10:00Z" },
        ],
      },
      {
        trajectory_id: "b",
        agent: {},
        extra: {},
        steps: [
          { step_id: 1, source: "agent", timestamp: "2026-01-01T10:05:00Z" },
          { step_id: 2, source: "agent", timestamp: "2026-01-01T10:12:00Z" },
        ],
      },
    ],
  };
  const out = run(`
    const lanes = laneIndex(${JSON.stringify(t)});
    return { rows: packLanes(lanes), on: lanes.map(l => l.row) };
  `);
  assert.strictEqual(out.rows, 2);
  assert.deepStrictEqual(out.on, [0, 1], "the overlap is what made the row");
});

test("the timeline is what the box shows first", () => {
  // Nothing stored: the timeline, whose packed rows carry the shape of the run.
  const html = run(`
    localStorage.getItem = () => null;
    LANEVIEW = localStorage.getItem("transcript-viewer.delegation") || "time";
    return drawLanes(${JSON.stringify(TWO_AT_ONCE)});
  `);
  assert.match(html, /class="track"/, "the chart is drawn");
  assert.ok(!/class="lanelist"/.test(html));
});

test("the list is one chip away, and shows what a chart cannot", () => {
  const html = run(
    `LANEVIEW="list"; return drawLanes(${JSON.stringify(TWO_AT_ONCE)})`,
  );
  assert.match(html, /class="lanelist"/);
  assert.match(html, /what it was asked to do/, "the task leads");
  assert.ok(!/class="track"/.test(html), "and no chart is drawn");
});

test("what was stored is what is shown", () => {
  const stored = run(`
    localStorage.getItem = (k) => (k === "transcript-viewer.delegation" ? "list" : null);
    return localStorage.getItem("transcript-viewer.delegation") || "time";
  `);
  assert.strictEqual(stored, "list", "a choice made once is kept");
});

test("the list names each one and says what it cost", () => {
  const html = run(
    `LANEVIEW="list"; return drawLanes(${JSON.stringify(TWO_AT_ONCE)})`,
  );
  assert.match(html, /find the theme/, "the task, where there was one");
  assert.match(html, /general-purpose/, "the kind, for the one with none");
  assert.match(html, /Grep/, "the tools it reached for");
});

test("a subagent with nothing written down is set apart, not left blank", () => {
  const html = run(
    `LANEVIEW="list"; return drawLanes(${JSON.stringify(TWO_AT_ONCE)})`,
  );
  assert.match(
    html,
    /class="lname untitled"/,
    "its kind stands in for a name, and reads as a handle rather than prose",
  );
});

test("the list sorts by what it is asked to sort by", () => {
  const order = (sort) =>
    run(`
    LANEVIEW="list"; LANESORT=${JSON.stringify(sort)};
    LANES = laneIndex(${JSON.stringify(TWO_AT_ONCE)});
    const html = laneList({});
    // The cell now opens with a disclosure, so read the whole cell and drop
    // the markup rather than assuming the name is the first text in it.
    return [...html.matchAll(/class="lname[^>]*>([\\s\\S]*?)<span class="lkind"/g)]
      .map(m => m[1].replace(/<button[\\s\\S]*?<\\/button>/g, "")
                    .replace(/<[^>]*>/g, "").trim());
  `);
  // By name, which has no ties to be arbitrary about.
  const up = order("name");
  const down = order("name-");
  assert.deepStrictEqual(up, ["find the theme", "general-purpose"]);
  assert.deepStrictEqual(
    down,
    up.slice().reverse(),
    "and reverses when asked twice",
  );
});

test("switching view is remembered", () => {
  const out = run(`
    const saved = [];
    localStorage.setItem = (k, v) => saved.push([k, v]);
    render = () => {};
    setLaneView("time");
    return { LANEVIEW, saved };
  `);
  assert.strictEqual(out.LANEVIEW, "time");
  assert.deepStrictEqual(out.saved, [["transcript-viewer.delegation", "time"]]);
});

/* A subagent that called subagents of its own. Depth is not a property of the
   format — the same harness produces both — so the view has to hold a tree
   without spending anything on runs that turn out to be flat. */
const NESTED = {
  // A real trajectory names itself, and the main thread being addressable
  // depends on it: without one, "is this the run?" is undefined === undefined.
  trajectory_id: "run",
  steps: [
    {
      step_id: 1,
      source: "agent",
      timestamp: "2026-01-01T10:00:00Z",
      tool_calls: [{ tool_call_id: "c1", function_name: "Task" }],
      observation: {
        results: [
          {
            source_call_id: "c1",
            subagent_trajectory_ref: [{ trajectory_id: "lead" }],
          },
        ],
      },
    },
    {
      step_id: 2,
      source: "agent",
      timestamp: "2026-01-01T10:00:30Z",
      tool_calls: [{ tool_call_id: "c2", function_name: "Task" }],
      observation: {
        results: [
          {
            source_call_id: "c2",
            subagent_trajectory_ref: [{ trajectory_id: "solo" }],
          },
        ],
      },
    },
    { step_id: 3, source: "agent", timestamp: "2026-01-01T10:30:00Z" },
  ],
  subagent_trajectories: [
    {
      trajectory_id: "lead",
      agent: { name: "claude-code" },
      extra: { agentType: "general-purpose", description: "run the audit" },
      steps: [
        {
          step_id: 1,
          source: "agent",
          timestamp: "2026-01-01T10:01:00Z",
          tool_calls: [{ tool_call_id: "d1", function_name: "Task" }],
          observation: {
            results: [
              {
                source_call_id: "d1",
                subagent_trajectory_ref: [{ trajectory_id: "lead.one" }],
              },
            ],
          },
        },
        {
          step_id: 2,
          source: "agent",
          timestamp: "2026-01-01T10:02:00Z",
          tool_calls: [{ tool_call_id: "d2", function_name: "Task" }],
          observation: {
            results: [
              {
                source_call_id: "d2",
                subagent_trajectory_ref: [{ trajectory_id: "lead.two" }],
              },
            ],
          },
        },
        { step_id: 3, source: "agent", timestamp: "2026-01-01T10:20:00Z" },
      ],
      subagent_trajectories: [
        // These two overlap, so within the group they cannot share a row.
        {
          trajectory_id: "lead.one",
          agent: { name: "claude-code" },
          extra: { agentType: "Explore", description: "read the config" },
          steps: [
            { step_id: 1, source: "agent", timestamp: "2026-01-01T10:03:00Z" },
            { step_id: 2, source: "agent", timestamp: "2026-01-01T10:12:00Z" },
          ],
        },
        {
          trajectory_id: "lead.two",
          agent: { name: "claude-code" },
          extra: { agentType: "Explore", description: "read the tests" },
          steps: [
            { step_id: 1, source: "agent", timestamp: "2026-01-01T10:08:00Z" },
            { step_id: 2, source: "agent", timestamp: "2026-01-01T10:18:00Z" },
          ],
        },
      ],
    },
    {
      trajectory_id: "solo",
      agent: { name: "claude-code" },
      extra: { agentType: "Explore", description: "check the docs" },
      steps: [
        { step_id: 1, source: "agent", timestamp: "2026-01-01T10:22:00Z" },
        { step_id: 2, source: "agent", timestamp: "2026-01-01T10:26:00Z" },
      ],
    },
  ],
};

const lanesOf = (t) => run(`return laneIndex(${JSON.stringify(t)})`);

test("a subagent's own subagents are found, and know who spawned them", () => {
  const lanes = lanesOf(NESTED);
  assert.deepStrictEqual(
    lanes.map((l) => [l.id, l.depth, l.parent]),
    [
      ["lead", 0, null],
      ["lead.one", 1, "lead"],
      ["lead.two", 1, "lead"],
      ["solo", 0, null],
    ],
  );
});

test("the count on a lane is what it spawned itself, not everything below it", () => {
  const lanes = lanesOf(NESTED);
  const kids = Object.fromEntries(lanes.map((l) => [l.id, l.kids]));
  // lead spawned two; neither of those spawned anything.
  assert.deepStrictEqual(kids, {
    lead: 2,
    "lead.one": 0,
    "lead.two": 0,
    solo: 0,
  });
});

test("a nested subagent is drawn only once its orchestrator is opened", () => {
  const rows = (open) =>
    run(`
    LANEOPEN = ${JSON.stringify(open)};
    LANES = laneIndex(${JSON.stringify(NESTED)});
    return laneRows(LANES, (l) => l.id + ";").map((r) => ({
      lane: r.lane ? r.lane.id : null, n: r.n || null, indent: r.indent, html: r.html,
    }));
  `);

  const shut = rows({});
  assert.deepStrictEqual(
    shut.map((r) => r.lane),
    ["lead", null],
  );
  assert.strictEqual(shut[1].html, "solo;", "what nested is not drawn yet");

  const open = rows({ lead: true });
  assert.deepStrictEqual(
    open.map((r) => [r.lane, r.n, r.indent]),
    [
      ["lead", null, 0],
      [null, 1, 1],
      [null, 2, 1],
      [null, 1, 0],
    ],
    "the children sit under the one that spawned them, indented",
  );
  assert.deepStrictEqual(
    open.map((r) => r.html),
    ["lead;", "lead.one;", "lead.two;", "solo;"],
  );
});

test("children pack among themselves, not against the rest of the run", () => {
  // lead.one runs 10:03-10:12 and lead.two 10:08-10:18, so they overlap and
  // need two rows. Those two rows belong to the group, not to the top level,
  // where solo still has a row of its own.
  const shape = run(`
    LANEOPEN = {lead: true};
    LANES = laneIndex(${JSON.stringify(NESTED)});
    return laneRows(LANES, () => "").map((r) => [r.lane ? r.lane.id : r.n, r.indent]);
  `);
  assert.deepStrictEqual(shape, [
    ["lead", 0],
    [1, 1],
    [2, 1],
    [1, 0],
  ]);
});

test("a flat run is drawn exactly as it was before nesting existed", () => {
  const shape = run(`
    LANEOPEN = {};
    LANES = laneIndex(${JSON.stringify(TWO_AT_ONCE)});
    return laneRows(LANES, () => "").map((r) => [r.lane ? r.lane.id : r.n, r.indent]);
  `);
  // Two subagents that overlap, no orchestrator among them: two numbered rows.
  assert.deepStrictEqual(shape, [
    [1, 0],
    [2, 0],
  ]);
});

test("opening a lane is remembered, and says which one", () => {
  const out = run(`
    const saved = [];
    localStorage.setItem = (k, v) => saved.push([k, v]);
    render = () => {};
    LANEOPEN = {};
    toggleLane("lead");
    return { LANEOPEN, saved };
  `);
  assert.deepStrictEqual(out.LANEOPEN, { lead: true });
  assert.deepStrictEqual(out.saved, [
    ["transcript-viewer.delegation-open", '{"lead":true}'],
  ]);
});

test("the header says how far the delegation went, and only when it went far", () => {
  const said = (t) =>
    run(`
    LANEVIEW = "list";
    const html = drawLanes(${JSON.stringify(t)});
    return html.slice(0, html.indexOf("</summary>"));
  `);
  assert.ok(/3 subagents/.test(said(NESTED)) === false, "four, not three");
  assert.ok(/4 subagents/.test(said(NESTED)));
  assert.ok(/2 levels deep/.test(said(NESTED)));
  assert.ok(
    /levels deep/.test(said(TWO_AT_ONCE)) === false,
    "a flat run says nothing",
  );
});

test("opening a nested lane unfolds the boxes it is buried inside", () => {
  const out = run(`
    const outer = { tagName: "DETAILS", open: false, parentElement: null };
    const box = {
      open: false, classList: { add() {}, remove() {} },
      parentElement: { tagName: "DIV", open: false, parentElement: outer },
    };
    document.querySelector = () => box;
    LANES = laneIndex(${JSON.stringify(NESTED)});
    openLane("lead.one");
    return { box: box.open, outer: outer.open };
  `);
  assert.strictEqual(out.box, true);
  assert.strictEqual(
    out.outer,
    true,
    "a box inside a folded box cannot be seen",
  );
});

test("a nested lane waits on its orchestrator's step being drawn, not its own", () => {
  const lanes = lanesOf(NESTED);
  const by = Object.fromEntries(lanes.map((l) => [l.id, l]));
  // lead was called at index 0 of the run; its children were called at index 0
  // and 1 of *lead*, which says nothing about how far the run has been painted.
  assert.strictEqual(by["lead"].rootIndex, 0);
  assert.strictEqual(by["lead.one"].rootIndex, 0);
  assert.strictEqual(by["lead.two"].rootIndex, 0);
  assert.strictEqual(by["solo"].rootIndex, 1);
});

test("the shape of a delegation is counted in links, not in subagents", () => {
  const shape = run(`
    const lanes = laneIndex(${JSON.stringify(NESTED)});
    return laneShape(${JSON.stringify(NESTED)}, lanes, linkIndex(${JSON.stringify(NESTED)}));
  `);
  // lead and solo came from the main thread; lead.one and lead.two came from
  // lead. Nothing addressed anything that was not its own child.
  assert.deepStrictEqual(shape.kinds, [
    ["orchestrator → subagent", 2],
    ["subagent ↔ subagent", 0],
    ["subagent → its own child", 2],
  ]);
  assert.strictEqual(shape.total, 4);
});

test("a subagent counted as a link is not counted again as a subagent", () => {
  // lead.one is on the end of one link and there are four subagents, so a
  // count of subagents and a count of links are different numbers.
  const shape = run(`
    const lanes = laneIndex(${JSON.stringify(NESTED)});
    return laneShape(${JSON.stringify(NESTED)}, lanes, linkIndex(${JSON.stringify(NESTED)}));
  `);
  assert.ok(shape.facts.includes("4 subagents"));
  assert.ok(shape.facts.includes("1 orchestrator"), shape.facts.join(" · "));
  assert.ok(shape.facts.includes("widest fan-out 2"));
});

test("a subagent addressing one that is not its own child is counted", () => {
  // No format read today records this, so the number is zero everywhere. It is
  // counted rather than assumed, which is what this asserts: given a ref that
  // leaves its own subtree, the count moves.
  const sideways = JSON.parse(JSON.stringify(NESTED));
  const lead = sideways.subagent_trajectories[0];
  lead.steps.push({
    step_id: 4,
    source: "agent",
    timestamp: "2026-01-01T10:21:00Z",
    tool_calls: [{ tool_call_id: "d3", function_name: "Task" }],
    observation: {
      results: [
        {
          source_call_id: "d3",
          // solo is the main thread's subagent, not lead's.
          subagent_trajectory_ref: [{ trajectory_id: "solo" }],
        },
      ],
    },
  });
  const shape = run(`
    const lanes = laneIndex(${JSON.stringify(sideways)});
    return laneShape(${JSON.stringify(sideways)}, lanes, linkIndex(${JSON.stringify(sideways)}));
  `);
  assert.deepStrictEqual(shape.kinds[1], ["subagent ↔ subagent", 1]);
});

test("a ref to a file that was split out is not counted as a sideways link", () => {
  const split = JSON.parse(JSON.stringify(NESTED));
  split.steps.push({
    step_id: 4,
    source: "agent",
    timestamp: "2026-01-01T10:29:00Z",
    tool_calls: [{ tool_call_id: "c9", function_name: "Task" }],
    observation: {
      results: [
        {
          source_call_id: "c9",
          subagent_trajectory_ref: [
            {
              trajectory_id: "elsewhere",
              trajectory_path: "run.trajectory.x.json",
            },
          ],
        },
      ],
    },
  });
  const shape = run(`
    const lanes = laneIndex(${JSON.stringify(split)});
    return laneShape(${JSON.stringify(split)}, lanes, linkIndex(${JSON.stringify(split)}));
  `);
  assert.deepStrictEqual(
    shape.kinds[1],
    ["subagent ↔ subagent", 0],
    "an external file is a missing trajectory, not a sideways message",
  );
});

test("a subagent nothing spawned is said to be, rather than counted as a link", () => {
  const loose = {
    steps: [{ step_id: 1, source: "user", message: "go" }],
    subagent_trajectories: [
      {
        trajectory_id: "rater",
        agent: { name: "metr-malt" },
        extra: {},
        steps: [],
      },
    ],
  };
  const shape = run(`
    const lanes = laneIndex(${JSON.stringify(loose)});
    return laneShape(${JSON.stringify(loose)}, lanes, linkIndex(${JSON.stringify(loose)}));
  `);
  assert.strictEqual(shape.total, 0, "nothing links it, so there is no link");
  assert.ok(
    shape.facts.includes("1 spawned by nothing"),
    shape.facts.join(" · "),
  );
});

test("a run with no links at all draws no ratio", () => {
  const html = run(
    `return drawShape({total: 0, kinds: [], facts: ["1 subagent"]})`,
  );
  assert.strictEqual(html, "", "a bar of nothing says nothing");
});

test("the ratio is shown, and a kind that did not happen keeps its place", () => {
  const html = run(`
    const lanes = laneIndex(${JSON.stringify(NESTED)});
    return drawShape(laneShape(${JSON.stringify(NESTED)}, lanes, linkIndex(${JSON.stringify(NESTED)})));
  `);
  assert.ok(/2 orchestrator → subagent <b>50%<\/b>/.test(html));
  assert.ok(/2 subagent → its own child <b>50%<\/b>/.test(html));
  assert.ok(/class="key none"/.test(html), "the zero is shown, not dropped");
  assert.ok(/0 subagent ↔ subagent <b>0%<\/b>/.test(html));
});

test("a line joins the agent that called to the agent that answered", () => {
  const wires = run(`
    LANEOPEN = {lead: true};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    const rows = laneRows(LANES, () => "");
    return laneWires(rows, (ms) => ms % 100, LINKS);
  `);
  // Four delegations — two the main thread made, two lead made — and each is
  // drawn twice, once for the call and once for the answer coming back.
  // Counted by the wires themselves: each one is now two paths, the line and
  // the wider invisible twin that takes the click.
  assert.strictEqual((wires.match(/class="wire /g) || []).length, 8);
  assert.strictEqual((wires.match(/ back"/g) || []).length, 4);
  // Row 0 is the main thread, so its calls leave from row 0.5 — and they are
  // the orchestrator's own kind, which the chart colours to match the ratio.
  // Read off the hit paths, which carry each geometry exactly once.
  assert.strictEqual(
    (wires.match(/class="wirehit" d="M [\d.]+ 0\.5 V/g) || []).length,
    2,
  );
  // lead and solo are the run's two delegations, so they are two branches;
  // lead's children are lead's branch, because they are part of it.
  assert.strictEqual(
    (wires.match(/class="wire b0"/g) || []).length,
    3,
    "lead, and the two it spawned",
  );
  assert.strictEqual((wires.match(/class="wire b1"/g) || []).length, 1, "solo");
  // The answer wears the same colour: it is the same relationship, returning.
  assert.strictEqual((wires.match(/class="wire b0 back"/g) || []).length, 3);
});

test("a line to a folded-away agent is not drawn", () => {
  const wires = run(`
    LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    return laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
  `);
  assert.strictEqual(
    (wires.match(/class="wire /g) || []).length,
    4,
    "lead's children have no row while lead is shut, so only lead and solo",
  );
});

test("a run that joins nothing draws no layer at all", () => {
  const wires = run(`
    LANEOPEN = {};
    const t = {steps: [{step_id: 1, source: "user"}], subagent_trajectories: [
      {trajectory_id: "loose", agent: {name: "metr-malt"}, extra: {}, steps: []}]};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    return laneWires(laneRows(LANES, () => ""), (ms) => 0, LINKS);
  `);
  assert.strictEqual(wires, "", "an empty overlay is markup for nothing");
});

test("the grid the lines are drawn on is rows, not pixels", () => {
  // The rows are stretched to whatever height they end up with, so a line has
  // to be placed in row units or it drifts the moment a row changes size.
  const wires = run(`
    LANEOPEN = {lead: true};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    const rows = laneRows(LANES, () => "");
    return {svg: laneWires(rows, (ms) => ms % 100, LINKS), rows: rows.length};
  `);
  assert.ok(
    wires.svg.includes(`viewBox="0 0 100 ${wires.rows + 1}"`),
    "one unit per row, plus the main thread's",
  );
  assert.ok(wires.svg.includes('preserveAspectRatio="none"'));
  assert.ok(wires.svg.includes('vector-effect="non-scaling-stroke"'));
});

test("one agent addressing another it did not spawn is drawn apart", () => {
  const sideways = JSON.parse(JSON.stringify(NESTED));
  sideways.subagent_trajectories[0].steps.push({
    step_id: 4,
    source: "agent",
    timestamp: "2026-01-01T10:21:00Z",
    tool_calls: [{ tool_call_id: "d3", function_name: "Task" }],
    observation: {
      results: [
        {
          source_call_id: "d3",
          subagent_trajectory_ref: [{ trajectory_id: "solo" }],
        },
      ],
    },
  });
  const wires = run(`
    LANEOPEN = {lead: true};
    const t = ${JSON.stringify(sideways)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    return laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
  `);
  assert.strictEqual((wires.match(/class="wire cross/g) || []).length, 1);
});

test("a message to an agent that had already finished is marked", () => {
  // Gosha's finding: teammates kept being written to after they died. It is
  // marked where it happened rather than reported somewhere else.
  const late = JSON.parse(JSON.stringify(NESTED));
  late.subagent_trajectories[0].steps.push({
    step_id: 4,
    source: "agent",
    // solo's last step is 10:26; this is after it.
    timestamp: "2026-01-01T10:29:00Z",
    tool_calls: [{ tool_call_id: "d4", function_name: "Task" }],
    observation: {
      results: [
        {
          source_call_id: "d4",
          subagent_trajectory_ref: [{ trajectory_id: "solo" }],
        },
      ],
    },
  });
  const wires = run(`
    LANEOPEN = {lead: true};
    const t = ${JSON.stringify(late)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    return laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
  `);
  assert.ok(
    /class="wire cross said late"/.test(wires),
    "sent after the recipient's last step",
  );
});

test("a wire does not run along the line the row already has", () => {
  // Every row draws a rule through its own centre. A wire that ran along it
  // would read as part of it, so the horizontal is held clear and only the
  // last short drop reaches the bar.
  const wires = run(`
    LANEOPEN = {lead: true};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    return laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
  `);
  for (const d of wires.match(/d="[^"]*"/g)) {
    const [, shelf, , land] = d.match(/V ([\d.-]+) H ([\d.-]+) V ([\d.-]+)/);
    assert.notStrictEqual(shelf, land, `the run sits on the rule: ${d}`);
    assert.ok(Math.abs(Number(land) - Number(shelf)) - 0.34 < 1e-9, d);
  }
});

test("everything under one delegation wears its colour", () => {
  const lanes = run(`return laneIndex(${JSON.stringify(NESTED)})`);
  const by = Object.fromEntries(lanes.map((l) => [l.id, l.branch]));
  assert.strictEqual(
    by["lead"],
    by["lead.one"],
    "a child is part of its branch",
  );
  assert.strictEqual(by["lead"], by["lead.two"]);
  assert.notStrictEqual(
    by["lead"],
    by["solo"],
    "a separate delegation is separate",
  );
});

test("a run with more branches than colours starts the palette again", () => {
  const wide = {
    steps: Array.from({ length: 8 }, (_, i) => ({
      step_id: i + 1,
      source: "agent",
      timestamp: `2026-01-01T10:0${i}:00Z`,
      tool_calls: [{ tool_call_id: "c" + i, function_name: "Task" }],
      observation: {
        results: [
          {
            source_call_id: "c" + i,
            subagent_trajectory_ref: [{ trajectory_id: "s" + i }],
          },
        ],
      },
    })),
    subagent_trajectories: Array.from({ length: 8 }, (_, i) => ({
      trajectory_id: "s" + i,
      agent: { name: "claude-code" },
      extra: {},
      steps: [
        { step_id: 1, source: "agent", timestamp: `2026-01-01T10:0${i}:30Z` },
      ],
    })),
  };
  const html = run(`
    LANEOPEN = {}; LANES = laneIndex(${JSON.stringify(wide)});
    return LANES.map((l) => "b" + (l.branch % 6)).join(" ");
  `);
  assert.strictEqual(
    html,
    "b0 b1 b2 b3 b4 b5 b0 b1",
    "eight delegations, six colours, and no lane left unpainted",
  );
});

test("the answer is drawn where the subagent stopped, not where it started", () => {
  const out = run(`
    LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    // Raw milliseconds as the scale, so a coordinate is a timestamp.
    const svg = laneWires(laneRows(LANES, () => ""), (ms) => ms, LINKS);
    const solo = LANES.find((l) => l.id === "solo");
    return {
      starts: [...svg.matchAll(/d="M ([\\d.]+) /g)].map((m) => Number(m[1])),
      called: solo.calledAt, ended: solo.span[1],
    };
  `);
  assert.ok(out.starts.includes(out.called), "one leaves at the call");
  assert.ok(
    out.starts.includes(out.ended),
    "the other leaves where it stopped",
  );
  assert.ok(out.ended > out.called, "and an answer comes after the question");
});

test("a bar shows when an agent worked, not only that it existed", () => {
  const out = run(`
    LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    const solo = LANES.find((l) => l.id === "solo");
    return {ticks: solo.ticks.length, steps: solo.steps};
  `);
  assert.strictEqual(out.ticks, out.steps, "one moment per step it took");
});

test("steps too close together to tell apart are drawn once", () => {
  // A tenth of a percent is under a pixel at any usable width, so two marks
  // there would be two elements painting one dot — and a subagent of a few
  // thousand steps would spend the whole render budget on it.
  const marks = run(`
    const close = Array.from({length: 400}, (_, i) => i);
    return tickMarks(close, 0, 100, (ms) => ms / 400);
  `);
  const at = [...marks.matchAll(/left:([\d.]+)%/g)].map((m) => m[1]);
  assert.strictEqual(at.length, new Set(at).size, "no position drawn twice");
  assert.ok(at.length < 400, `400 steps collapsed to ${at.length}`);
});

test("a mark outside the bar it belongs to is not drawn", () => {
  // The bar covers 0-10% of the track, so 0, 50 and 100 land on it — 100 on
  // its right edge, which is still on it — and 900 is nowhere near.
  const marks = run(
    `return tickMarks([0, 50, 100, 900], 0, 10, (ms) => ms / 10)`,
  );
  assert.strictEqual((marks.match(/<i /g) || []).length, 3);
});

test("a bar with no width draws no marks rather than dividing by it", () => {
  assert.strictEqual(run(`return tickMarks([1, 2], 0, 0, (ms) => ms)`), "");
});

test("the marks in one bar are bounded however many steps it took", () => {
  // 8,702 steps in one real session collapse to 1,255 marks across 18 bars.
  // The bound is what makes that safe: a tenth of a percent is the finest
  // position drawn, so a bar can hold a thousand marks and no more, whether
  // the agent took a thousand steps or a million.
  const marks = run(`
    const many = Array.from({length: 20000}, (_, i) => i);
    return tickMarks(many, 0, 100, (ms) => ms / 200);
  `);
  const n = (marks.match(/<i /g) || []).length;
  assert.ok(n <= 1001, `${n} marks for 20,000 steps`);
  assert.ok(n > 900, `and the bar is still full: ${n}`);
});

test("no two marks on an axis carry the same label", () => {
  // A 2.4-minute run put marks at 90s and 120s and labelled both "2m".
  const clash = run(`
    const bad = [];
    for (const total of [144e3, 90e3, 300e3, 3.6e6, 5400e3, 86400e3, 6e5]) {
      const seen = axisMarks(0, total).map((m) => m.label);
      if (seen.length !== new Set(seen).size) bad.push([total, seen]);
    }
    return bad;
  `);
  assert.deepStrictEqual(clash, [], "an axis that says the same thing twice");
});

test("a minute and a half is not two minutes", () => {
  const said = run(`return [duration(89e3), duration(90e3), duration(120e3)]`);
  assert.deepStrictEqual(said, ["89s", "1.5m", "2m"]);
});

test("an orchestrator is named beside its row, not across its bar", () => {
  // The name used to be written inside the bar, over the marks the bar exists
  // to show — and it moved with the bar, so no two names ever lined up.
  const html = run(`
    LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    // A scale that lands every step inside the bar, so the marks are drawn.
    return laneTracks(laneRows(LANES, (l) => laneBar(l, 10, 20, () => 15)),
      "", "", "", "");
  `);
  assert.ok(
    /class="lanelabel"[^>]*>run the audit</.test(html.replace(/\s+/g, " ")),
    "the name is in the label column",
  );
  assert.ok(!/barname/.test(html), "and no longer inside the bar");
  assert.ok(/class="tick"/.test(html), "which leaves the bar to its marks");
});

test("every mark the chart draws is named in the key", () => {
  const key = run(`return laneKey(true)`).replace(/\s+/g, " ");
  for (const said of [
    "the main thread, above its subagents",
    "one agent, marked at each step it took",
    "an agent calling a subagent",
    "a subagent's answer, back to the agent that called it",
    "a message to a subagent already running",
    "a message between two agents, neither of which spawned the other",
    "a stretch with nothing running, left out of the axis",
  ]) {
    assert.ok(key.includes(said), `the key never says: ${said}`);
  }
});

test("nothing in the key needs a pronoun the reader has to guess at", () => {
  // Every entry stands on its own line beside its own mark, so "its answer"
  // and "said to one" had nothing to refer back to.
  const key = run(`return laneKey(true)`)
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ");
  for (const entry of key.split("  ").filter(Boolean)) {
    assert.ok(
      !/^\s*(its|said to|a step it)\b/.test(entry),
      `an entry opens on a pronoun: ${entry}`,
    );
  }
});

test("the key draws the real lines, not a picture of them", () => {
  // A sample drifts from the thing it explains the first time a weight or a
  // colour changes, so the two must be the same markup — which means reading
  // the classes off the chart rather than listing what they ought to be. They
  // had already come apart once: the key drew `wire b0` while the chart drew
  // `b0`, and only one of them had a stroke colour, so half the key was blank.
  const out = run(`
    LANEOPEN = {lead: true};
    const t = ${JSON.stringify(NESTED)};
    LANES = laneIndex(t); LINKS = linkIndex(t);
    const chart = laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
    return {
      chart: [...chart.matchAll(/class="([^"]*)"/g)].map((m) => m[1]),
      key: [...laneKey(true).matchAll(/class="(wire[^"]*)"/g)].map((m) => m[1]),
    };
  `);

  const words = (list) =>
    new Set(list.flatMap((c) => c.split(/\s+/)).filter(Boolean));
  const onChart = words(out.chart);
  assert.ok(onChart.has("wire"), "every line on the chart is a wire");
  for (const cls of words(out.key)) {
    assert.ok(
      onChart.has(cls) || cls === "cross" || cls === "back" || cls === "said",
      `the key shows a "${cls}" the chart never draws`,
    );
  }
});

test("every wire class the key shows is one the stylesheet colours", () => {
  // The stylesheet is not reachable from the stub DOM, so read the file.
  const page = require("fs").readFileSync(
    require("path").join(__dirname, "../src/transcript_viewer/page.html"),
    "utf8",
  );
  for (const cls of ["b0", "b1", "b2", "b3", "b4", "b5"]) {
    assert.ok(
      new RegExp(`\\.wire\\.${cls}[^{]*\\{[^}]*stroke:`).test(page),
      `.wire.${cls} has no stroke, so it draws nothing`,
    );
  }
});

test("a subagent answering its orchestrator out of turn is not sideways traffic", () => {
  // It reads as "neither spawned the other" only if direction is ignored: the
  // ref runs from the child to the parent, so the parent is not among the
  // child's own children. That is the whole difference between a fleet talking
  // to itself and a subagent reporting back.
  const up = JSON.parse(JSON.stringify(NESTED));
  const lead = up.subagent_trajectories[0];
  lead.subagent_trajectories[0].steps.push({
    step_id: 3,
    source: "agent",
    timestamp: "2026-01-01T10:13:00Z",
    tool_calls: [{ tool_call_id: "u1", function_name: "SendMessage" }],
    observation: {
      results: [
        {
          source_call_id: "u1",
          subagent_trajectory_ref: [
            { trajectory_id: "lead", extra: { via: "SendMessage" } },
          ],
        },
      ],
    },
  });
  const shape = run(`
    const t = ${JSON.stringify(up)};
    const lanes = laneIndex(t);
    return laneShape(t, lanes, linkIndex(t));
  `);
  assert.deepStrictEqual(
    shape.kinds[1],
    ["subagent ↔ subagent", 0],
    "answering upward is not sideways",
  );
  assert.strictEqual(
    shape.kinds[2][1],
    3,
    "it belongs with the other traffic between that pair",
  );
});

test("a message to the main thread reaches its row rather than being dropped", () => {
  const up = JSON.parse(JSON.stringify(NESTED));
  up.subagent_trajectories[1].steps.push({
    step_id: 3,
    source: "agent",
    timestamp: "2026-01-01T10:27:00Z",
    tool_calls: [{ tool_call_id: "u2", function_name: "SendMessage" }],
    observation: {
      results: [
        {
          source_call_id: "u2",
          subagent_trajectory_ref: [
            { trajectory_id: up.trajectory_id, extra: { via: "SendMessage" } },
          ],
        },
      ],
    },
  });
  const out = run(`
    const t = ${JSON.stringify(up)};
    LANES = laneIndex(t); LINKS = linkIndex(t); LANEOPEN = {};
    const wires = laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
    return {up: (wires.match(/ said"/g) || []).length,
            shape: laneShape(t, LANES, LINKS).kinds};
  `);
  assert.strictEqual(out.up, 1, "the wire is drawn");
  assert.strictEqual(
    out.shape[0][1],
    3,
    "and counted with the main thread's own traffic",
  );
});

test("a chart that is not showing all of them says how many it is holding back", () => {
  // Anyone who counts the rows and gets a smaller number than the header is
  // right, and had no way of knowing why: orchestrators are shut by default,
  // so on a first look most of the count sits behind a caret.
  const said = (open) =>
    run(`
    LANEVIEW = "time"; LANEOPEN = ${JSON.stringify(open)};
    const html = drawLanes(${JSON.stringify(NESTED)});
    return html.slice(0, html.indexOf("</summary>"));
  `);
  assert.ok(/2 folded away/.test(said({})), said({}));
  assert.ok(
    !/folded away/.test(said({ lead: true })),
    "and says nothing once everything is out",
  );
});

test("the key can be put away, and stays put away", () => {
  const shut = run(`KEYOPEN = false; return laneKey(false)`);
  const open = run(`KEYOPEN = true; return laneKey(false)`);
  assert.ok(/<summary>Key<\/summary>/.test(shut));
  assert.ok(!/<details class="keybox" open/.test(shut));
  assert.ok(/<details class="keybox" open/.test(open));

  const saved = run(`
    const wrote = [];
    localStorage.setItem = (k, v) => wrote.push([k, v]);
    rememberKey(true);
    return {KEYOPEN, wrote};
  `);
  assert.strictEqual(saved.KEYOPEN, true);
  assert.deepStrictEqual(saved.wrote, [["transcript-viewer.key", "open"]]);
});

test("a place on the axis reads back as the time it stands for", () => {
  // A reader points at the chart, not at a timestamp, so a drag across it has
  // to come back as a range. Round-tripping is the property that makes that
  // safe: whatever posOf put at x, timeAt has to find again.
  const out = run(`
    const lo = 0, hi = 1000000;
    const busy = [[0, 10000], [900000, 1000000]];
    const s = timeScale(lo, hi, busy);
    const at = [0, 5000, 9000, 920000, 1000000];
    return at.map((t) => [t, Math.round(s.timeAt(s.posOf(t)))]);
  `);
  for (const [was, back] of out) {
    assert.ok(Math.abs(back - was) <= 1, `${was} came back as ${back}`);
  }
});

test("pointing inside a skipped stretch lands where the work resumes", () => {
  // The elided stretch takes up width while standing for no time, so there is
  // no moment inside it to return. The end of it is the only honest answer.
  const out = run(`
    const s = timeScale(0, 1000000, [[0, 10000], [900000, 1000000]]);
    // The break sits just after the first stretch of work.
    const gap = s.gaps[0];
    return {at: s.timeAt(gap.at + 0.2), resumes: 900000};
  `);
  assert.strictEqual(out.at, out.resumes);
});

test("reading off either end of the axis stays inside the run", () => {
  const out = run(`
    const s = timeScale(1000, 5000, [[1000, 5000]]);
    return [s.timeAt(-40), s.timeAt(400)];
  `);
  assert.deepStrictEqual(out, [1000, 5000]);
});

// ---- a command, shown on a terminal ------------------------------------------

/* The stylesheet is out of reach of the stub DOM, so the tests below read the
   file. Whitespace-tolerant throughout: the page is auto-formatted, and a test
   that pins the spacing of a selector fails on a reformat that changed nothing. */
const css = page.slice(page.indexOf("<style>"), page.indexOf("</style>"));

test("the terminal keeps its own colours whichever theme is chosen", () => {
  /* The whole point of the block is that it stays dark in paper, cool and dark
     alike, so its palette cannot be spelled with the tokens a theme redefines:
     one var(--ink) in here and two of the three get a screen made of paper. */
  const palette = /:root\s*\{[^}]*--term-bg[^}]*\}/.exec(css);
  assert.ok(palette, "the terminal has no palette of its own");
  assert.ok(
    !/var\(/.test(palette[0]),
    `a theme token leaked into the terminal: ${palette[0]}`,
  );
  for (const token of [
    "--term-bg",
    "--term-fg",
    "--term-edge",
    "--term-prompt",
    "--term-link",
    "--sh-cmd",
    "--sh-str",
    "--sh-flag",
    "--sh-op",
    "--sh-var",
    "--sh-com",
  ]) {
    assert.match(
      palette[0],
      new RegExp(`${token}:\\s*#`),
      `${token} is not a colour of the terminal's own`,
    );
  }
});

test("a command sits on a screen rather than on the page", () => {
  const rule = /\.sh\s*\{([^}]*)\}/.exec(css);
  assert.ok(rule, "there is no .sh rule at all");
  assert.match(rule[1], /background:\s*var\(--term-bg\)/, "the screen is dark");
  assert.match(
    rule[1],
    /color:\s*var\(--term-fg\)/,
    "and the ink is not the page's",
  );
  assert.match(
    rule[1],
    /font:[^;]*var\(--font-mono\)/,
    "a terminal is monospaced",
  );
  const mark = /\.sh::before\s*\{([^}]*)\}/.exec(css);
  assert.match(
    mark[1],
    /color:\s*var\(--term-prompt\)/,
    "the prompt is the screen's",
  );
});

test("the call box no longer flattens the screen back to transparent", () => {
  /* `.io pre` outranks `.sh` on specificity, so a command inside a tool box —
     which is every command there is — lost its background and all of its
     padding, and the prompt mark landed on top of the first two characters. */
  assert.match(
    css,
    /\.io pre:not\(\.sh\)\s*\{/,
    "the terminal must be left out of the rule that strips a pre bare",
  );
  assert.ok(
    !/\.io pre\s*\{/.test(css),
    "a rule that catches the terminal again has come back",
  );
});

test("a path in a command stays legible against the screen", () => {
  /* A link takes the theme's accent, and on near-black two of the three accents
     land under 4:1 — readable as a swatch, not as a path. */
  assert.match(css, /\.sh a\s*\{[^}]*color:\s*var\(--term-link\)/);
  const html = run(`return shBlock('ls /Users/someone/Documents')`);
  assert.match(html, /api\/reveal\?path=/, "and is still something to click");
});

test("only a command becomes a terminal; a file is still shown as a file", () => {
  const read = run(`return TOOLVIEW.Read.body({file_path:"/a/b.py"})`);
  const written = run(
    `return TOOLVIEW.Write.body({file_path:"/a/b.py",content:"x"})`,
  );
  const edited = run(
    `return TOOLVIEW.Edit.body({file_path:"/a/b.py",old_string:"a",new_string:"b"})`,
  );
  assert.ok(
    !/class="sh"/.test(read + written + edited),
    "a file view has borrowed the terminal",
  );
});

// ---- paths with spaces in them -----------------------------------------------

/* What each link in a piece of rendered prose actually points at. Reading the
   href back is the only way to tell a link that stops early from one that does
   not: both show the same words on screen. */
const revealed = (html) =>
  [...html.matchAll(/reveal\?path=([^"]+)/g)].map((m) =>
    decodeURIComponent(m[1]),
  );

test("a path with a space in it links past the space in prose too", () => {
  /* The reported one. It reached the page as prose rather than as a tool
     argument, and the prose scanner forbade spaces outright, so the link
     stopped at "Build" and revealed AIResearch — a directory that exists,
     which is why it looked like it had worked. */
  const html = run(
    `return md("wrote it to /Users/girish/Documents/AIResearch/Build An LLM");`,
  );
  assert.deepStrictEqual(revealed(html), [
    "/Users/girish/Documents/AIResearch/Build An LLM",
  ]);
});

test("a path with a space keeps the sentence that follows it out", () => {
  // The hard case: nothing marks where the name ends, so the shape of the next
  // word has to decide. "was" is not a name, so the path is three words.
  const html = run(`return md("/Users/x/Build An LLM was slow to open");`);
  assert.deepStrictEqual(revealed(html), ["/Users/x/Build An LLM"]);
  assert.match(html, /<\/a> was slow to open/, "the prose is still prose");
});

test("a spaced directory in the middle of a path does not end it", () => {
  // The space is plainly inside the path here: it is followed by more path.
  const html = run(
    `return md("see /Users/me/Library/Application Support/Code/user.json");`,
  );
  assert.deepStrictEqual(revealed(html), [
    "/Users/me/Library/Application Support/Code/user.json",
  ]);
});

test("a quoted path is taken whole, whichever quote holds it", () => {
  /* Transcripts quote paths constantly, and a quote says exactly where the
     name ends — so these need no guessing. Tool output arrives escaped, so
     &quot; has to count as a quote as much as " does. */
  const path = "/Users/girish/Documents/AIResearch/Build An LLM";
  for (const [open, close] of [
    ['"', '"'],
    ["'", "'"],
    ["&quot;", "&quot;"],
  ]) {
    const html = run(
      `return autolink(${JSON.stringify(`ran it on ${open}${path}${close} yesterday`)});`,
    );
    assert.deepStrictEqual(revealed(html), [path], `quoted with ${open}`);
  }
});

test("a lowercase name survives being quoted, where a bare one would not", () => {
  // Bare, "my notes" is indistinguishable from prose and the link stops short;
  // quoted, there is nothing to be unsure about.
  const quoted = run(`return autolink("in '/Users/me/my notes' today");`);
  assert.deepStrictEqual(revealed(quoted), ["/Users/me/my notes"]);
  const bare = run(`return md("in /Users/me/my notes today");`);
  assert.deepStrictEqual(revealed(bare), ["/Users/me/my"]);
});

test("one path guessing wrong does not cost the next one its link", () => {
  /* The scanner takes the whole run of words and gives back what it decides
     is not path, so it can hand back text with another path inside it. A list
     of spaced paths is the ordinary way that happens. */
  const html = run(
    `return md("checked /Users/a/My Notes, /Users/b/Other Thing and /tmp/c.log");`,
  );
  assert.deepStrictEqual(revealed(html), [
    "/Users/a/My Notes",
    "/Users/b/Other Thing",
    "/tmp/c.log",
  ]);
});

test("a full stop ends a path rather than joining it to the next sentence", () => {
  // "Then" is capitalised, which is the signal a name continuing would give —
  // the full stop before it is what says otherwise.
  const html = run(`return md("wrote /tmp/out.txt. Then I read /Users/x/y");`);
  assert.deepStrictEqual(revealed(html), ["/tmp/out.txt", "/Users/x/y"]);
});

test("a bare root is not stretched over the words after it", () => {
  // "~" and "/Users" are roots, not paths: there is no component there that
  // could be holding a space, so nothing follows them into the link.
  const html = run(`return md("cd ~ Then ls /Users Next");`);
  assert.deepStrictEqual(revealed(html), ["~", "/Users"]);
});

test("a path with spaces given as a tool argument is still linked whole", () => {
  // The delimited case, which has always worked, and must keep working: the
  // argument is the path in its entirety, so none of the guessing applies.
  const html = run(
    `return TOOLVIEW.Read.body({file_path:"/Users/girish/Documents/AIResearch/Build An LLM/notes.md"})`,
  );
  assert.deepStrictEqual(revealed(html), [
    "/Users/girish/Documents/AIResearch/Build An LLM/notes.md",
  ]);
});

test("a relative path with a space in it is still no more linkable", () => {
  // Allowing spaces must not have quietly widened what counts as a path.
  const html = run(`return md("open Build An LLM/notes.md now");`);
  assert.deepStrictEqual(revealed(html), []);
});

test("zooming draws the same run over a narrower stretch of it", () => {
  const out = run(`
    LANEVIEW = "time"; LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    const all = drawLanes(t);
    // 10:02 to 10:10, which is inside lead and covers none of solo.
    ZOOM = [Date.parse("2026-01-01T10:02:00Z"), Date.parse("2026-01-01T10:10:00Z")];
    const near = drawLanes(t);
    ZOOM = null;
    return {all, near};
  `);
  assert.ok(
    /2m to 10m/.test(out.near) || /to 10m/.test(out.near),
    "the header says where the window sits in the run",
  );
  assert.ok(/Whole run/.test(out.near), "and offers the whole run back");
  assert.ok(
    !/Whole run/.test(out.all),
    "which is not offered when nothing is hidden",
  );
});

test("a window is kept inside the run, and never narrower than a second", () => {
  const out = run(`
    const full = [1000, 100000];
    return {
      past: held([-9999, 999999], full),
      slivered: held([50000, 50000], full),
      inside: held([20000, 30000], full),
    };
  `);
  assert.deepStrictEqual(out.past, [1000, 100000], "clipped to the run");
  assert.deepStrictEqual(out.inside, [20000, 30000], "left alone when it fits");
  assert.strictEqual(
    out.slivered[1] - out.slivered[0],
    1000,
    "an empty window is opened to the point where the axis can still say something",
  );
});

test("inside a window the axis is literal, not elided", () => {
  // Skipping quiet stretches is what makes a ten-day run readable at a glance.
  // Inside a two-minute window it would mean the gap between two steps stopped
  // matching the space between them, which is what zooming in is for.
  const out = run(`
    LANEVIEW = "time"; LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    ZOOM = null;
    const wide = drawLanes(t);
    ZOOM = [Date.parse("2026-01-01T10:00:00Z"), Date.parse("2026-01-01T10:30:00Z")];
    const near = drawLanes(t);
    ZOOM = null;
    return {wide, near};
  `);
  assert.ok(
    !/of waiting hidden/.test(out.near),
    "nothing is hidden inside a window",
  );
});

test("a bar running past the edge of the window is cut off at it", () => {
  const out = run(`
    LANEVIEW = "time"; LANEOPEN = {};
    const t = ${JSON.stringify(NESTED)};
    // A window ending mid-way through lead, which runs 10:01 to 10:20.
    ZOOM = [Date.parse("2026-01-01T10:00:00Z"), Date.parse("2026-01-01T10:05:00Z")];
    const html = drawLanes(t);
    ZOOM = null;
    return [...html.matchAll(/left:([\d.]+)%;width:([\d.]+)%/g)]
      .map((m) => Number(m[1]) + Number(m[2]));
  `);
  for (const right of out) {
    assert.ok(right <= 100.01, `a bar reaches ${right}% of the chart`);
  }
});

test("a quote nobody closed is scanned in a moment, not in minutes", () => {
  /* Letting a quoted path hold spaces means the quoted form can fail to match,
     and the first version of it described the path as a run of components —
     ambiguous enough that failing took exponential time. A log with a stray
     quote in front of a deep path is not exotic; it hung the page. */
  // Deep enough that the old form takes tens of seconds, shallow enough that a
  // regression still finishes and reports rather than wedging the suite.
  const text = `"/Users${"/abc".repeat(30)} and then some words`;
  const started = Date.now();
  run(`return autolink(${JSON.stringify(text)});`);
  assert.ok(Date.now() - started < 1000, "the scan has gone exponential again");
});

// ---- following a line on the delegation timeline ------------------------------

/* NESTED with one message added: after solo is already running, the run says
   something to it. That link has no bar and no box — it exists nowhere in the
   transcript but on the step whose observation holds the ref. */
const MESSAGED = (() => {
  const t = JSON.parse(JSON.stringify(NESTED));
  t.steps.push({
    step_id: 4,
    source: "agent",
    timestamp: "2026-01-01T10:24:00Z",
    tool_calls: [{ tool_call_id: "m1", function_name: "SendMessage" }],
    observation: {
      results: [
        {
          source_call_id: "m1",
          subagent_trajectory_ref: [
            { trajectory_id: "solo", extra: { via: "SendMessage" } },
          ],
        },
      ],
    },
  });
  return t;
})();

const WIRES = (t) => `
  LANEOPEN = {lead: true};
  const t = ${JSON.stringify(t)};
  LANES = laneIndex(t); LINKS = linkIndex(t);
  return laneWires(laneRows(LANES, () => ""), (ms) => ms % 100, LINKS);
`;

test("only the lines take a click, never the layer they are drawn over", () => {
  // The layer covers every track. If it answered the mouse it would swallow the
  // clicks meant for the bars, which are what the chart is mostly for.
  assert.match(
    css,
    /\.wirelayer\s*\{[^}]*pointer-events:\s*none/,
    "the whole layer answers the mouse",
  );
  const hit = /\.wirehit\s*\{([^}]*)\}/.exec(css);
  assert.ok(hit, "no line on the chart offers itself to be clicked");
  assert.match(hit[1], /pointer-events:\s*stroke/);
  // A 1.2px hairline is a fine thing to look at and impossible to hit.
  const width = Number(/stroke-width:\s*([\d.]+)/.exec(hit[1])[1]);
  assert.ok(width >= 6, `a ${width}-wide target is not a target`);

  const svg = run(WIRES(NESTED));
  // Nine units on a grid 100 across and a few rows tall would be a target the
  // size of the chart, so the twin is measured in pixels like the line it hides
  // under.
  assert.match(
    svg,
    /class="wirehit"[^>]*vector-effect="non-scaling-stroke"/,
    "the hit area is in chart units, so it covers the whole chart",
  );
  assert.ok(
    !/aria-hidden/.test(svg),
    "a layer you can click is not decoration, and must not claim to be",
  );
});

test("every line offers a click, and says where it goes before it is clicked", () => {
  // A thing that goes somewhere and never says so is a thing nobody finds.
  const svg = run(WIRES(NESTED));
  const lines = (svg.match(/class="wire /g) || []).length;
  assert.ok(lines > 0);
  assert.strictEqual(
    (svg.match(/onclick="openWire\(\d+\)"/g) || []).length,
    lines,
  );
  // NESTED is four spawns and nothing else, so every line leads to a subagent.
  assert.strictEqual(
    (svg.match(/<title>open the subagent this call made<\/title>/g) || []).length,
    lines,
  );
});

test("a message line says it goes to a step, because that is all it is", () => {
  const svg = run(WIRES(MESSAGED));
  assert.match(svg, /<title>go to the step that sent this<\/title>/);
});

test("a spawn line opens the subagent it made, the call and the answer alike", () => {
  // Both halves of a delegation are one relationship drawn twice, and the
  // subagent's box is the only place either of them could sensibly land — the
  // same place the subagent's own bar goes.
  const out = run(`
    const t = ${JSON.stringify(NESTED)};
    traj = t; LANES = laneIndex(t); LINKS = linkIndex(t);
    const opened = []; let jumps = 0;
    openLane = (id) => opened.push(id);
    jump = () => { jumps++; return true; };
    LINKS.forEach((l, i) => openWire(i));
    return { opened, jumps, links: LINKS.length };
  `);
  assert.strictEqual(out.links, 4, "four delegations");
  assert.deepStrictEqual(out.opened, ["lead", "solo", "lead.one", "lead.two"]);
  assert.strictEqual(out.jumps, 0, "a spawn has no lone step to send anyone to");
});

test("a message line goes to the step that sent it, not to either bar it touches", () => {
  // A message brings no new agent into the run: there is no box to open and no
  // bar it started. The observation on the sending step is the only record of
  // it anywhere in the transcript, so that step is the honest destination.
  const out = run(`
    const t = ${JSON.stringify(MESSAGED)};
    traj = t; LANES = laneIndex(t); LINKS = linkIndex(t);
    const jumped = [];
    jump = (pos, id) => { jumped.push([pos, id]); return true; };
    openLane = () => jumped.push("opened a lane instead");
    openWire(LINKS.findIndex((l) => l.via === "SendMessage"));
    return { jumped,
             anchor: /id="(step-[^"]+)"/.exec(step(t.steps[3], t, 0, 3, ""))[1] };
  `);
  assert.deepStrictEqual(out.jumped, [[3, "4"]], "the run's fourth step");
  assert.strictEqual(
    "step-" + out.jumped[0][1],
    out.anchor,
    "and that is the anchor the step itself carries",
  );
});

test("a message sent by a subagent lands on that subagent's own step", () => {
  // Two things the run's own steps never need. Subagent steps number from 1 and
  // scope their anchors by trajectory, so the key is both halves; and they are
  // drawn inside the orchestrator's box, so it is the orchestrator's step of the
  // *run* that has to be painted before the target exists at all.
  const said = JSON.parse(JSON.stringify(NESTED));
  said.subagent_trajectories[0].subagent_trajectories[0].steps.push({
    step_id: 3,
    source: "agent",
    timestamp: "2026-01-01T10:13:00Z",
    tool_calls: [{ tool_call_id: "m2", function_name: "SendMessage" }],
    observation: {
      results: [
        {
          source_call_id: "m2",
          subagent_trajectory_ref: [
            { trajectory_id: "lead", extra: { via: "SendMessage" } },
          ],
        },
      ],
    },
  });
  const out = run(`
    const t = ${JSON.stringify(said)};
    traj = t; LANES = laneIndex(t); LINKS = linkIndex(t);
    const jumped = [];
    jump = (pos, id) => { jumped.push([pos, id]); return true; };
    openWire(LINKS.findIndex((l) => l.via === "SendMessage"));
    const sub = t.subagent_trajectories[0].subagent_trajectories[0];
    return { jumped,
             anchor: /id="(step-[^"]+)"/.exec(
               step(sub.steps[2], sub, 2, null, "lead.one-"))[1] };
  `);
  assert.deepStrictEqual(out.jumped, [[0, "lead.one-3"]]);
  assert.strictEqual(
    out.anchor,
    "step-lead.one-3",
    "which is the anchor that step carries",
  );
});

test("a line pointing at a step this view has filtered away says so", () => {
  // Failing quietly reads as a broken chart. The lens is the reader's own, so
  // it is not thrown away under them; they are told why nothing moved.
  const out = run(`
    const t = ${JSON.stringify(MESSAGED)};
    traj = t; LANES = laneIndex(t); LINKS = linkIndex(t);
    const said = []; note = (m) => said.push(m);
    jump = () => false;
    openWire(LINKS.findIndex((l) => l.via === "SendMessage"));
    return said;
  `);
  assert.strictEqual(out.length, 1);
  assert.match(out[0], /not in this view/);
});

test("a step buried in a subagent is unfolded before it is scrolled to", () => {
  // Opening the boxes above a target moves it. Scrolling first and unfolding
  // after leaves the reader above whatever they asked for — the mistake the
  // lane click already knew about, which the step jump now avoids too.
  const out = run(`
    traj = {steps: [{step_id: 1, source: "agent"}]};
    limit = 250;
    const order = [];
    const outer = { tagName: "DETAILS", parentElement: null,
                    set open(v) { order.push("unfold"); } };
    document.getElementById = () => ({
      parentElement: outer,
      querySelectorAll: () => [],
      scrollIntoView: () => order.push("scroll"),
      style: {},
    });
    return { order, found: jump(0, "lead.one-3") };
  `);
  assert.deepStrictEqual(out.order, ["unfold", "scroll"]);
  assert.strictEqual(out.found, true, "and it reports that it found the step");
});

// ---- the fill, and the reader it draws behind ---------------------------------

/* The fill runs on timers and reads the clock, so both are driven by hand here.
   Put back afterwards: a stub left behind fails somewhere else entirely, which
   is how a leaked querySelectorAll once broke a test three sections away. */
const onFakeClocks = (body) => {
  const real = {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
    requestIdleCallback: globalThis.requestIdleCallback,
    now: Date.now,
  };
  try {
    return run(body);
  } finally {
    globalThis.setTimeout = real.setTimeout;
    globalThis.clearTimeout = real.clearTimeout;
    globalThis.requestIdleCallback = real.requestIdleCallback;
    Date.now = real.now;
  }
};

test("the fill waits for a jump to arrive rather than cancelling it", () => {
  /* Clicking a bar on a long run stopped a third of the way to the subagent.
     The fill repaints every 500 steps and puts the reader back where it found
     them — and writing scrollTop mid-flight cancels a smooth scroll, so the
     journey ended wherever the next chunk happened to land. */
  const out = onFakeClocks(`
    const steps = [];
    for (let i = 0; i < 1200; i++) steps.push({step_id:i+1, source:"agent", message:"x"});
    traj = {steps}; cur = "k"; INDEX = [{key:"k"}];
    lens = "all"; query = ""; tab = "trajectory"; limit = 250;

    const waits = [];
    globalThis.setTimeout = (fn, ms) => { waits.push(ms); if (waits.length < 5) fn(); return 0; };
    globalThis.clearTimeout = () => {};
    globalThis.requestIdleCallback = (fn) => fn();

    travelling();   // a scroll has just set off
    fillRest();
    return { waits, limit };
  `);
  assert.strictEqual(out.limit, 250, "the fill repainted under the scroll");
  assert.ok(
    out.waits.length > 1 && out.waits.slice(1).every((ms) => ms > 0),
    `the fill came straight back: ${out.waits}`,
  );
});

test("and picks the fill back up once the reader has arrived", () => {
  // Waiting must not mean stopping: the rest of the run still has to be there
  // before anyone scrolls down to it.
  const out = onFakeClocks(`
    const steps = [];
    for (let i = 0; i < 1200; i++) steps.push({step_id:i+1, source:"agent", message:"x"});
    traj = {steps}; cur = "k"; INDEX = [{key:"k"}];
    lens = "all"; query = ""; tab = "trajectory"; limit = 250;
    main.scrollTop = 400;

    // A clock the test winds forward, so the wait can actually end.
    let clock = 1e12;
    Date.now = () => clock;
    let ticks = 0;
    globalThis.setTimeout = (fn, ms) => { clock += ms || 0; if (ticks++ < 50) fn(); return 0; };
    globalThis.clearTimeout = () => {};
    globalThis.requestIdleCallback = (fn) => fn();

    travelling();
    fillRest();
    return { limit, steps: traj.steps.length, at: main.scrollTop };
  `);
  assert.ok(
    out.limit >= out.steps,
    `the fill stalled at ${out.limit} of ${out.steps}`,
  );
  assert.strictEqual(out.at, 400, "and still held the reader's place");
});

test("the fill carries the unfolded subagent boxes across its own repaint", () => {
  /* Open is not in the markup, and a repaint writes fresh markup: without this
     the box someone just clicked into on the timeline folds shut behind them a
     moment after they got there. */
  const out = onFakeClocks(`
    const steps = [];
    for (let i = 0; i < 400; i++) steps.push({step_id:i+1, source:"agent", message:"x"});
    traj = {steps}; cur = "k"; INDEX = [{key:"k"}];
    lens = "all"; query = ""; tab = "trajectory"; limit = 250;
    main.scrollTop = 500;

    const reopened = [];
    main.querySelectorAll = () => [{ getAttribute: () => "far" }];
    document.querySelector = (sel) =>
      /data-lane="far"/.test(sel) ? { set open(v) { reopened.push(v); } } : null;
    let ticks = 0;
    globalThis.setTimeout = (fn) => { if (ticks++ < 10) fn(); return 0; };
    globalThis.clearTimeout = () => {};
    globalThis.requestIdleCallback = (fn) => fn();

    fillRest();
    return { reopened, at: main.scrollTop };
  `);
  assert.deepStrictEqual(out.reopened, [true], "the box came back open");
  assert.strictEqual(out.at, 500, "and the reader kept their place");
});

test("the lines are lifted over the rows, and the bars over the lines", () => {
  /* Left to paint in document order the tracks lie over every wire, and a track
     has no colour but does have a hit area — so nothing on the layer could be
     reached at all, however wide its target. The bars then go a step higher
     again: where a wire ends on a bar, the bar is what was being aimed at. */
  const block = (sel) => {
    const m = new RegExp(`\\n\\s*\\${sel}\\s*\\{([^}]*)\\}`).exec(css);
    assert.ok(m, `${sel} has no rule of its own`);
    return m[1];
  };
  const depth = (sel) => {
    const m = /z-index:\s*(\d+)/.exec(block(sel));
    assert.ok(m, `${sel} is left to paint in document order`);
    return Number(m[1]);
  };
  assert.ok(depth(".bar") > depth(".wirelayer"), "a wire covers the bars");
  // The stretch being chosen during a drag is drawn over both, and stays there.
  assert.ok(depth(".zoomband") > depth(".bar"));
});
