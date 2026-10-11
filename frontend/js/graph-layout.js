// Left-to-right layered layout for the service graph: callers sit left of what they call, so every edge reads in one direction.
// Used only for nodes the backend did not give coordinates for. Pure: no DOM.

export function layeredLayout(nodes, edges, { width = 760, left = 70, top = 44, rowGap = 54 } = {}) {
  const ids = nodes.map((node) => node.id), known = new Set(ids);
  const preds = new Map(ids.map((id) => [id, []]));
  edges.forEach(([from, to]) => { if (known.has(from) && known.has(to) && from !== to) preds.get(to).push(from); });

  const memo = new Map();
  const depth = (id, stack = new Set()) => {                       // longest call path from a root; a cycle is cut where it closes
    if (memo.has(id)) return memo.get(id);
    if (stack.has(id)) return 0;
    stack.add(id);
    let best = 0;
    for (const parent of preds.get(id)) best = Math.max(best, depth(parent, stack) + 1);
    stack.delete(id);
    memo.set(id, best);
    return best;
  };
  const columns = [];
  ids.forEach((id) => { const d = depth(id); (columns[d] = columns[d] || []).push(id); });
  const filled = columns.filter(Boolean);

  const row = new Map();                                            // one barycenter sweep keeps edge crossings down
  filled.forEach((column, index) => {
    const mean = (id) => { const rows = preds.get(id).map((parent) => row.get(parent)).filter((value) => value !== undefined); return rows.length ? rows.reduce((a, b) => a + b, 0) / rows.length : Infinity; };
    column.sort((a, b) => (index === 0 ? 0 : mean(a) - mean(b)) || a.localeCompare(b));
    column.forEach((id, position) => row.set(id, position));
  });

  const tallest = Math.max(1, ...filled.map((column) => column.length));
  const gap = filled.length > 1 ? (width - 2 * left) / (filled.length - 1) : 0;
  const positions = new Map();
  filled.forEach((column, index) => column.forEach((id, position) => {
    positions.set(id, { x: Math.round(left + index * gap), y: Math.round(top + ((tallest - column.length) / 2 + position) * rowGap) });
  }));
  return { positions, width, height: Math.round(top * 2 + (tallest - 1) * rowGap), columns: filled.length };
}
