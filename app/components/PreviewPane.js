"use client";
import { useState } from "react";

export default function PreviewPane({ pages, loading, fileName, onClose }) {
  const [zoom, setZoom] = useState(1);
  const clamp = (z) => Math.min(4, Math.max(0.4, z));

  return (
    <div className="pv">
      <div className="pv-bar">
        <span style={{ fontSize: 12, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {fileName || "Source preview"}
        </span>
        <div className="row" style={{ gap: 4 }}>
          <button className="btn btn-ghost btn-sm" onClick={() => setZoom((z) => clamp(z * 0.8))} title="Zoom out">−</button>
          <span className="muted" style={{ fontSize: 11, minWidth: 38, textAlign: "center" }}>
            {Math.round(zoom * 100)}%
          </span>
          <button className="btn btn-ghost btn-sm" onClick={() => setZoom((z) => clamp(z * 1.25))} title="Zoom in">+</button>
          <button className="btn btn-ghost btn-sm" onClick={() => setZoom(1)} title="Fit width">⤢</button>
          {onClose && <button className="btn btn-ghost btn-sm" onClick={onClose} title="Close">×</button>}
        </div>
      </div>
      <div className="pv-body" style={{ "--z": zoom }}>
        {loading && (
          <div style={{ color: "#e2e8f0", textAlign: "center", padding: 30, fontSize: 13 }}>
            <span className="spinner" style={{ marginRight: 8 }} /> Rendering preview…
          </div>
        )}
        {!loading && (!pages || pages.length === 0) && (
          <div style={{ color: "#cbd5e1", textAlign: "center", padding: 30, fontSize: 13 }}>
            No preview available.
          </div>
        )}
        {!loading &&
          pages?.map((src, i) => <img key={i} src={src} alt={`page ${i + 1}`} />)}
      </div>
    </div>
  );
}
