import cytoscape from "cytoscape";
import { useEffect, useRef } from "react";
import type { Investigation } from "../api";

const EDGE: Record<string, string> = {
  claims_to_be: "claims to be", names: "names", uses_domain: "uses", uses_contact: "uses", claims: "claims",
  official_domain: "official site", supports: "supported by", contradicts: "contradicted by", warned_by: "warned by",
};
const BAD = new Set(["mismatch", "contradicted"]);
const GOOD = new Set(["verified", "supported"]);

// Cytoscape can't read CSS variables, so the ledger's inks are repeated here per theme.
function palette() {
  return matchMedia("(prefers-color-scheme: dark)").matches
    ? { ink: "#e8ebf0", muted: "#a6afbc", rule: "#43546c", paper: "#1b1e24", red: "#ff858a", blue: "#93b6ff", cover: "#b11e24" }
    : { ink: "#1b1e24", muted: "#545c69", rule: "#9db9d8", paper: "#fbfcfd", red: "#c42127", blue: "#1f4b99", cover: "#c42127" };
}

/** The evidence graph: message in the middle, what it says around it, the web's sources outside.
 *  Tapping a line or node calls `onFocus` with the evidence card or section to show. */
export function Graph({ d, onFocus }: { d: Investigation; onFocus: (selector: string) => void }) {
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!box.current) return;
    const p = palette();
    const { width, height } = box.current.getBoundingClientRect();
    const narrow = width < 520; // phone: shorter labels, so neighbouring columns don't collide
    const short = (t: string) => {
      const max = narrow ? 22 : 34;
      return t.length > max ? `${t.slice(0, max - 2)}…` : t;
    };
    const cy = cytoscape({
      container: box.current,
      elements: [
        ...d.graph.nodes.map((n) => ({
          data: { ...n, label: short(n.label) },
          classes: [n.type, n.flag && BAD.has(n.flag) ? "bad" : n.flag && GOOD.has(n.flag) ? "good" : n.flag === "suspicious" ? "bad" : "",
            n.type === "source" && (n.flag === "A" || n.flag === "B") ? "strong" : ""].join(" "),
        })),
        ...d.graph.edges.map((e) => ({
          data: { ...e, label: e.source === "message" ? "" : EDGE[e.kind] || e.kind },
          classes: ["warned_by", "contradicts"].includes(e.kind) ? "bad" : ["supports", "official_domain"].includes(e.kind) ? "good" : "",
        })),
      ],
      // Labels never shrink below ~10px; a graph bigger than the box pans instead.
      minZoom: 0.9,
      maxZoom: 2,
      // Left to right: the message, what it names and claims, then the web's sources. Breadthfirst stacks ranks
      // top to bottom, so it lays out in the box turned sideways and the transform turns it back.
      layout: {
        name: "breadthfirst", roots: "#message", padding: 24, animate: false, spacingFactor: 0.85, // default 1.75 overflows the box
        boundingBox: { x1: 0, y1: 0, w: height, h: width },
        transform: (_node: cytoscape.NodeSingular, pos: cytoscape.Position) => ({ x: pos.y, y: pos.x }),
      } as cytoscape.LayoutOptions,
      style: [
        { selector: "node", style: { label: "data(label)", "font-size": 12, "font-family": "Archivo Variable, system-ui, sans-serif", color: p.ink,
          "text-wrap": "wrap", "text-max-width": narrow ? "110px" : "150px", "text-valign": "bottom", "text-margin-y": 6, width: 20, height: 20,
          // a paper plate behind every label, so no line runs through the words
          "text-background-color": p.paper, "text-background-opacity": 0.92, "text-background-padding": "2px", "text-background-shape": "roundrectangle",
          "background-color": p.paper, "border-width": 2, "border-color": p.muted } },
        { selector: "node.message", style: { shape: "round-rectangle", width: 36, height: 26, "background-color": p.cover, "border-color": p.cover, "font-weight": "bold" } },
        { selector: "node.org, node.person", style: { width: 28, height: 28, "border-color": p.ink, "font-weight": "bold" } },
        { selector: "node.claim", style: { shape: "round-rectangle", width: 18, height: 12, "font-size": 11, color: p.muted } },
        { selector: "node.source", style: { shape: "rectangle", width: 18, height: 18, "border-color": p.muted } },
        { selector: "node.official", style: { shape: "round-tag" } },
        { selector: "node.strong", style: { "border-width": 3, "border-color": p.ink, "background-color": p.ink } },
        { selector: "node.bad", style: { "border-color": p.red, "border-width": 3 } },
        { selector: "node.good", style: { "border-color": p.blue, "border-width": 3 } },
        // Edge labels show on hover or tap only, so they never cross the node labels.
        { selector: "edge", style: { width: 1.5, "line-color": p.rule, "target-arrow-color": p.rule, "target-arrow-shape": "triangle",
          "arrow-scale": 0.8, "curve-style": "bezier", label: "data(label)", "font-size": 11, color: p.muted, "text-opacity": 0,
          "text-background-color": p.paper, "text-background-opacity": 0, "text-background-padding": "3px" } },
        { selector: "edge.hover, edge:selected", style: { "text-opacity": 1, "text-background-opacity": 1, "z-index": 10 } },
        { selector: "edge.bad", style: { width: 2.5, "line-color": p.red, "target-arrow-color": p.red, color: p.red } },
        { selector: "edge.good", style: { width: 2.5, "line-color": p.blue, "target-arrow-color": p.blue, color: p.blue } },
        { selector: ":selected", style: { "overlay-color": p.blue, "overlay-opacity": 0.15 } },
      ],
    });
    cy.on("mouseover", "edge", (e) => e.target.addClass("hover"));
    cy.on("mouseout", "edge", (e) => e.target.removeClass("hover"));
    cy.on("tap", "edge", (e) => {
      const id = e.target.data("evidence_id");
      onFocus(id ? `#ev-${CSS.escape(id)}` : `#n-${CSS.escape(e.target.data("target"))}`);
    });
    cy.on("tap", "node", (e) => {
      const n = e.target.data();
      if (n.type === "source") {
        const edge = d.graph.edges.find((x) => x.target === n.id && x.evidence_id);
        if (edge) onFocus(`#ev-${CSS.escape(edge.evidence_id!)}`);
      } else if (n.type === "official") {
        const edge = d.graph.edges.find((x) => x.target === n.id);
        if (edge) onFocus(`#n-${CSS.escape(edge.source)}`);
      } else {
        onFocus(`#n-${CSS.escape(n.id)}`);
      }
    });
    // Re-fit when the box changes size (window resize, phone rotation); otherwise the graph drifts out of view.
    const fit = new ResizeObserver(() => { cy.resize(); cy.fit(undefined, 24); });
    fit.observe(box.current);
    return () => { fit.disconnect(); cy.destroy(); };
  }, [d, onFocus]);

  return <div className="graph" ref={box} role="img" aria-label="Evidence graph. The same evidence is listed under Claims and evidence." />;
}
