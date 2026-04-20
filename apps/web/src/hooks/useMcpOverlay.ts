/** Debounced MCP tool-call overlay — true while any MCP call is running,
 *  with a 400ms lingering "off" edge so the overlay doesn't flicker across
 *  rapid consecutive Playwright calls (navigate → click → type → screenshot).
 */

import { useEffect, useRef, useState } from "react";

const OFF_DEBOUNCE_MS = 400;

export function useMcpOverlay(active: boolean): boolean {
  const [visible, setVisible] = useState(false);
  const offTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    if (active) {
      if (offTimer.current !== null) {
        clearTimeout(offTimer.current);
        offTimer.current = null;
      }
      setVisible(true);
    } else {
      offTimer.current = setTimeout(() => {
        setVisible(false);
        offTimer.current = null;
      }, OFF_DEBOUNCE_MS);
    }
    return () => {
      if (offTimer.current !== null) {
        clearTimeout(offTimer.current);
      }
    };
  }, [active]);

  return visible;
}
