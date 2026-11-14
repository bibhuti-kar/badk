// viewer.js - vendored mermaid@12.3.0 (static/mermaid.min.js, loaded only when a dag graph exists)
const graph = document.querySelector("pre#dag-graph");
if (graph) {
  (async () => {
    try {
      const ids = JSON.parse(graph.dataset.nodes); // synthetic node id -> node key
      const parents = JSON.parse(graph.dataset.parents || "{}"); // node id -> parent key
      mermaid.initialize({ startOnLoad: false, securityLevel: "strict" });
      await mermaid.run({ nodes: [graph] });
      for (const g of graph.querySelectorAll("g.node")) {
        const m = /-(n\d+)-\d+$/.exec(g.id);
        if (m && m[1] in ids) g.setAttribute("data-node", ids[m[1]]);
        if (m && m[1] in parents) g.setAttribute("data-parent", parents[m[1]]);
      }
    } catch (e) {
      graph.textContent = "dag graph unavailable"; // mermaid missing/failed; timeline still works
    }
  })();
}

const timeline = document.getElementById("timeline");
let docP = null; // one shared in-flight/settled fetch of the run document
const loadDoc = () =>
  (docP ??= fetch(`/api/v1/runs/${timeline.dataset.runId}`)
    .then(async (res) => {
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const d = await res.json();
      if (!Array.isArray(d.trace)) throw new Error("no trace in response");
      return d;
    })
    .catch((e) => {
      docP = null; // failures are not cached
      throw e;
    }));

timeline.addEventListener("click", async (ev) => {
  const row = ev.target.closest("tr[data-seq]");
  if (!row) return;
  const open = row.nextElementSibling;
  if (open && open.classList.contains("detail")) return open.remove();
  const tr = document.createElement("tr"); // placeholder first: a second click toggles it off
  tr.className = "detail";
  const td = tr.appendChild(document.createElement("td"));
  td.colSpan = row.cells.length;
  const pre = td.appendChild(document.createElement("pre"));
  pre.textContent = "loading...";
  row.after(tr);
  try {
    const rec = (await loadDoc()).trace.find((t) => String(t.seq) === row.dataset.seq);
    pre.textContent = JSON.stringify(rec?.detail ?? null, null, 2);
  } catch (e) {
    pre.textContent = `could not load detail: ${e.message}`;
  }
});
