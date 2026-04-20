import { cn } from "@polaris/ui";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

/**
 * Renders an agent message as markdown.
 *
 * Why react-markdown:
 *   - Safe by default: no raw HTML pass-through, nothing to sanitize.
 *   - Component override API lets us route each element through our own
 *     token-aware styling instead of dropping a `prose` class bomb that
 *     would fight the rest of the design.
 *
 * Deliberately minimal:
 *   - No syntax highlighting (bundle cost outweighs current value).
 *   - No <img> rendering (blocks inline tracking pixels).
 *   - Headings capped at `text-base font-semibold` — message cards shouldn't
 *     have oversized H1 banners.
 */
const components: Components = {
  p: ({ children }) => (
    <p className="my-1.5 leading-6 first:mt-0 last:mb-0">{children}</p>
  ),
  ul: ({ children }) => (
    <ul className="my-1.5 ml-5 list-disc space-y-0.5 first:mt-0 last:mb-0">
      {children}
    </ul>
  ),
  ol: ({ children }) => (
    <ol className="my-1.5 ml-5 list-decimal space-y-0.5 first:mt-0 last:mb-0">
      {children}
    </ol>
  ),
  li: ({ children }) => <li className="leading-6">{children}</li>,
  h1: ({ children }) => (
    <h3 className="mb-1 mt-2 text-base font-semibold first:mt-0">{children}</h3>
  ),
  h2: ({ children }) => (
    <h3 className="mb-1 mt-2 text-base font-semibold first:mt-0">{children}</h3>
  ),
  h3: ({ children }) => (
    <h3 className="mb-1 mt-2 text-[15px] font-semibold first:mt-0">{children}</h3>
  ),
  h4: ({ children }) => (
    <h4 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h4>
  ),
  h5: ({ children }) => (
    <h5 className="mb-1 mt-2 text-sm font-medium first:mt-0">{children}</h5>
  ),
  h6: ({ children }) => (
    <h6 className="mb-1 mt-2 text-sm font-medium first:mt-0">{children}</h6>
  ),
  a: ({ href, children }) => (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="text-accent underline underline-offset-2 hover:text-accent/80"
    >
      {children}
    </a>
  ),
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  em: ({ children }) => <em className="italic">{children}</em>,
  del: ({ children }) => <del className="text-text-muted">{children}</del>,
  blockquote: ({ children }) => (
    <blockquote className="my-1.5 border-l-2 border-accent/40 bg-surface-alt/60 px-3 py-1 text-text-muted first:mt-0 last:mb-0">
      {children}
    </blockquote>
  ),
  hr: () => <hr className="my-2 border-border-light" />,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto first:mt-0 last:mb-0">
      <table className="min-w-full border-collapse text-[12px]">{children}</table>
    </div>
  ),
  thead: ({ children }) => (
    <thead className="border-b border-border-light text-left">{children}</thead>
  ),
  th: ({ children }) => <th className="px-2 py-1 font-semibold">{children}</th>,
  td: ({ children }) => <td className="border-t border-border-light px-2 py-1">{children}</td>,
  code: ({ className, children, ...rest }) => {
    // react-markdown passes className="language-ts" etc. only for block
    // code; inline code has no className.  We branch purely on that.
    const isBlock = typeof className === "string" && className.startsWith("language-");
    if (isBlock) {
      // Block code — let the <pre> handler take over the layout; here we
      // just emit raw text so `pre > code` can wrap it.
      return (
        <code className={cn("font-mono text-[12px]", className)} {...rest}>
          {children}
        </code>
      );
    }
    return (
      <code className="rounded bg-surface-alt px-1 py-0.5 font-mono text-[12px] text-text-primary">
        {children}
      </code>
    );
  },
  pre: ({ children }) => (
    // Scroll horizontally for long lines instead of forcing-wrap — most
    // code readers expect monospace + overflow.  Max height keeps
    // massive blocks from eating the card; vertical scroll kicks in.
    <pre className="my-2 max-h-72 overflow-auto rounded-md border border-border-light bg-surface-alt px-3 py-2 font-mono text-[12px] leading-[1.45] first:mt-0 last:mb-0">
      {children}
    </pre>
  ),
  // Defensive: strip any raw HTML or unknown elements silently.
  img: () => null,
};

export function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="text-[13px] leading-6 text-text-primary">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {text}
      </ReactMarkdown>
    </div>
  );
}
