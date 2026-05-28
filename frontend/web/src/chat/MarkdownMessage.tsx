import { Check, Copy } from "lucide-react";
import type { ReactNode } from "react";
import { useState } from "react";
import ReactMarkdown from "react-markdown";
import type { Components } from "react-markdown";
import remarkGfm from "remark-gfm";

function nodeText(node: ReactNode): string {
  if (typeof node === "string" || typeof node === "number") {
    return String(node);
  }
  if (Array.isArray(node)) {
    return node.map(nodeText).join("");
  }
  return "";
}

function CodeBlock({
  children,
  language
}: {
  children: ReactNode;
  language: string;
}) {
  const [copied, setCopied] = useState(false);
  const code = nodeText(children).replace(/\n$/, "");
  const label = language || "text";

  async function copyCode() {
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("Clipboard API unavailable");
      }
      await navigator.clipboard.writeText(code);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch (error) {
      console.warn("Failed to copy code block", error);
      setCopied(false);
    }
  }

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span>{label}</span>
        <button className="code-copy" type="button" onClick={copyCode}>
          {copied ? <Check size={14} /> : <Copy size={14} />}
          <span>{copied ? "Скопировано" : "Копировать"}</span>
        </button>
      </div>
      <pre>
        <code>{code}</code>
      </pre>
    </div>
  );
}

const markdownComponents: Components = {
  a({ children, href, node: _node, ...props }) {
    return (
      <a href={href} target="_blank" rel="noreferrer noopener" {...props}>
        {children}
      </a>
    );
  },
  code({ children, className, node: _node, ...props }) {
    const match = /language-([\w-]+)/.exec(className || "");
    if (match) {
      return <CodeBlock language={match[1]}>{children}</CodeBlock>;
    }
    return (
      <code className="markdown-inline-code" {...props}>
        {children}
      </code>
    );
  },
  pre({ children, node: _node }) {
    return <>{children}</>;
  },
  table({ children, node: _node, ...props }) {
    return (
      <div className="markdown-table-wrap">
        <table {...props}>{children}</table>
      </div>
    );
  }
};

export function MarkdownMessage({ text }: { text: string }) {
  return (
    <div className="markdown-message">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents} skipHtml>
        {text}
      </ReactMarkdown>
    </div>
  );
}
