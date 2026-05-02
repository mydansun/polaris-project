/** `useTypewriterPlaceholder` — animates one string at a time from a
 *  list, character-by-character.  After the full string holds for
 *  ``holdMs``, the placeholder is cleared in a single tick (no
 *  reverse character-by-character delete) and the next string starts
 *  typing.
 *
 *  Used on the welcome / new-project screen so the textarea
 *  placeholder cycles through the same prompts the example-project
 *  cards offer — gives the user concrete sample inputs to work from
 *  even when they're not clicking a card.
 *
 *  Tunables (defaults reverse-engineered to feel close to typical
 *  typewriter UX without dragging on):
 *    typeMs    — ms between each character while typing
 *    holdMs    — pause once the full string is shown, before clearing
 *    betweenMs — pause after clearing, before starting the next string
 *
 *  Returns the empty string before the first effect tick fires.
 *  Caller can fall back to a static placeholder during that frame.
 */
import { useEffect, useState } from "react";

type Phase = "typing" | "holding" | "between";

export function useTypewriterPlaceholder(
  strings: readonly string[],
  opts: {
    typeMs?: number;
    holdMs?: number;
    betweenMs?: number;
  } = {},
): string {
  const {
    typeMs = 45,
    holdMs = 1800,
    betweenMs = 350,
  } = opts;
  const [text, setText] = useState("");
  const [idx, setIdx] = useState(0);
  const [phase, setPhase] = useState<Phase>("typing");

  useEffect(() => {
    if (strings.length === 0) return;
    const target = strings[idx % strings.length];

    let timer: number | undefined;
    if (phase === "typing") {
      if (text.length < target.length) {
        timer = window.setTimeout(
          () => setText(target.slice(0, text.length + 1)),
          typeMs,
        );
      } else {
        // Already at full string — flip to holding immediately.
        setPhase("holding");
      }
    } else if (phase === "holding") {
      timer = window.setTimeout(() => {
        // Single-tick clear + advance to next string.  No
        // character-by-character reverse delete (felt fussy).
        setText("");
        setPhase("between");
      }, holdMs);
    } else {
      // between — short pause showing empty placeholder, then start typing next.
      timer = window.setTimeout(() => {
        setIdx((i) => (i + 1) % strings.length);
        setPhase("typing");
      }, betweenMs);
    }
    return () => {
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [text, idx, phase, strings, typeMs, holdMs, betweenMs]);

  // Reset the text when the source list changes (e.g. user toggled
  // language).  Without this, mid-typing we'd index into a stale
  // string and keep the obsolete prompt visible until the next cycle.
  useEffect(() => {
    setText("");
    setIdx(0);
    setPhase("typing");
  }, [strings]);

  return text;
}
