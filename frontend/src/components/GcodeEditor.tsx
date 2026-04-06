import { useRef, useCallback } from 'react';

interface GcodeEditorProps {
  value: string;
  onChange: (value: string) => void;
  minHeight?: string;
}

/**
 * Highlight G-code line into HTML spans.
 */
function highlightLine(line: string): string {
  const commentIdx = line.indexOf(';');
  let codePart = line;
  let commentHtml = '';

  if (commentIdx >= 0) {
    codePart = line.substring(0, commentIdx);
    const commentText = line.substring(commentIdx)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    commentHtml = `<span class="gc-comment">${commentText}</span>`;
  }

  const escaped = codePart
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  const highlighted = escaped
    // G commands
    .replace(/\b(G\d+(\.\d+)?)\b/gi, '<span class="gc-g">$1</span>')
    // M commands
    .replace(/\b(M\d+(\.\d+)?)\b/gi, '<span class="gc-m">$1</span>')
    // Parameters + value (X100, Y-50.5, F3000)
    .replace(/\b([XYZEFSPTRIJ])([-+]?\d+\.?\d*)\b/gi,
      '<span class="gc-param">$1</span><span class="gc-num">$2</span>')
    // Standalone numbers not already wrapped
    .replace(/(?<!["=\w>])\b(\d+\.?\d*)\b(?![<])/g, '<span class="gc-num">$1</span>');

  return highlighted + commentHtml;
}

function highlightGcode(code: string): string {
  return code.split('\n').map(highlightLine).join('\n');
}

/**
 * G-code editor with syntax highlighting.
 * Uses transparent textarea over highlighted pre for real editing + visual highlighting.
 */
export function GcodeEditor({ value, onChange, minHeight = '280px' }: GcodeEditorProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const preRef = useRef<HTMLPreElement>(null);

  const handleScroll = useCallback(() => {
    if (textareaRef.current && preRef.current) {
      preRef.current.scrollTop = textareaRef.current.scrollTop;
      preRef.current.scrollLeft = textareaRef.current.scrollLeft;
    }
  }, []);

  const sharedStyle = {
    fontFamily: 'ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace',
    fontSize: '13px',
    lineHeight: '1.6',
    padding: '12px',
    minHeight,
    tabSize: 2,
  };

  return (
    <>
      <style>{`
        .gc-g { color: #00e676; font-weight: 600; }
        .gc-m { color: #26c6da; font-weight: 600; }
        .gc-param { color: #ffab40; }
        .gc-num { color: #64b5f6; }
        .gc-comment { color: #616161; font-style: italic; }
      `}</style>
      <div className="flex-1 rounded-lg overflow-hidden border border-bambu-dark-tertiary relative" style={{ minHeight }}>
        {/* Highlighted layer (behind) */}
        <pre
          ref={preRef}
          className="absolute inset-0 overflow-auto whitespace-pre-wrap break-words m-0 pointer-events-none"
          style={{ ...sharedStyle, backgroundColor: '#0d0d1a', color: 'transparent' }}
          aria-hidden="true"
          dangerouslySetInnerHTML={{ __html: highlightGcode(value || '') + '\n' }}
        />
        {/* Textarea (front, transparent text with visible caret) */}
        <textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => onChange(e.target.value)}
          onScroll={handleScroll}
          spellCheck={false}
          className="relative w-full h-full resize-none bg-transparent outline-none whitespace-pre-wrap break-words m-0"
          style={{
            ...sharedStyle,
            color: 'transparent',
            caretColor: '#00c853',
            WebkitTextFillColor: 'transparent',
            minHeight,
          }}
          placeholder="; G-code macro&#10;G28 ; home all axes&#10;M400 ; wait for moves"
        />
      </div>
    </>
  );
}
