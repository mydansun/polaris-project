/** Resizable left/right split-pane state + drag handler.
 *
 * Persists the pct to localStorage and clamps it to [MIN, MAX].  Returns
 * everything App.tsx needs to wire the divider:
 *   - `splitPct`: committed percentage (used for the left column width)
 *   - `dragPct`: live during-drag percentage (used for the ghost preview)
 *   - `dragging`: flag to show the overlay
 *   - `containerRef`: attach to the outer flex container so width can be
 *     measured
 *   - `startDrag`: onMouseDown handler for the divider
 */

import { useRef, useState } from "react";

const STORAGE_KEY = "polaris-split-pct";
const MIN_LEFT_PCT = 20;
const MAX_LEFT_PCT = 75;
const DEFAULT_LEFT_PCT = 42;

export function useSplitPane() {
  const [splitPct, setSplitPct] = useState<number>(() => {
    try {
      const saved = localStorage.getItem(STORAGE_KEY);
      if (saved) {
        const v = Number(saved);
        if (v >= MIN_LEFT_PCT && v <= MAX_LEFT_PCT) return v;
      }
    } catch {
      /* ignore */
    }
    return DEFAULT_LEFT_PCT;
  });
  const [dragging, setDragging] = useState(false);
  const [dragPct, setDragPct] = useState(splitPct);
  const containerRef = useRef<HTMLDivElement>(null);

  function startDrag(e: React.MouseEvent) {
    e.preventDefault();
    setDragging(true);
    setDragPct(splitPct);
    const onMove = (ev: MouseEvent) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const pct = ((ev.clientX - rect.left) / rect.width) * 100;
      setDragPct(Math.min(MAX_LEFT_PCT, Math.max(MIN_LEFT_PCT, pct)));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      setDragging(false);
      setDragPct((final) => {
        const clamped = Math.min(MAX_LEFT_PCT, Math.max(MIN_LEFT_PCT, final));
        setSplitPct(clamped);
        try {
          localStorage.setItem(STORAGE_KEY, String(Math.round(clamped)));
        } catch {
          /* ignore */
        }
        return clamped;
      });
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }

  return { splitPct, dragPct, dragging, containerRef, startDrag };
}
